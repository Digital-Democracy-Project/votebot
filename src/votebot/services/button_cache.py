"""Redis-backed cache for quick-action button responses.

See plans/PLAN-quick-action-buttons.md for the full architecture.

Cacheable types: 'summary' and 'pros_cons' — these depend only on bill text,
which only changes when ddp-sync detects a new bill version (publishes an
invalidation event). 'status_votes' is NEVER cached because it must always
hit live OpenStates for fresh data.

Keys: votebot:button:{slug}:{button_type}
TTL: 7-day safety net only — primary invalidation is amendment-triggered.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

import structlog

from votebot.services.redis_store import RedisStore

logger = structlog.get_logger()


# Pub/sub channel used by ddp-sync to signal that a bill's cached responses
# should be invalidated (because Pinecone was just re-ingested with newer text).
INVALIDATE_CHANNEL = "votebot:cache:invalidate"


# Type names accepted from the widget. status_votes is intentionally excluded
# from CACHEABLE_TYPES — it must always run the full pipeline with live
# OpenStates lookup.
CACHEABLE_TYPES: tuple[str, ...] = ("summary", "pros_cons")
ALL_BUTTON_TYPES: tuple[str, ...] = ("summary", "pros_cons", "status_votes")

# Safety net TTL in seconds (7 days). Primary invalidation is the
# votebot:cache:invalidate pub/sub event from ddp-sync; this catches edge
# cases like bills being removed from Webflow entirely.
SAFETY_TTL = 7 * 24 * 60 * 60

# Key prefix used by all button cache entries. Module-level constant so the
# startup reconciliation scan can find them without instantiating the cache.
KEY_PREFIX = "votebot:button:"


def make_key(slug: str, button_type: str) -> str:
    return f"{KEY_PREFIX}{slug}:{button_type}"


class ButtonCache:
    """Redis-backed cache for quick-action button responses.

    All methods gracefully no-op when Redis is unavailable (dev/test) or
    when the requested button_type isn't cacheable.
    """

    def __init__(self, redis_store: RedisStore):
        self._store = redis_store

    @property
    def _client(self):
        # RedisStore exposes _client as a private attribute. Reading it
        # directly is the established pattern in this repo (see how
        # build_legislator_votes.py uses vector_store.index, etc.).
        return self._store._client

    async def get(self, slug: str, button_type: str) -> Optional[dict]:
        """Return cached response dict, or None on miss / non-cacheable type / Redis down."""
        if button_type not in CACHEABLE_TYPES:
            return None
        if self._client is None:
            return None
        try:
            raw = await self._client.get(make_key(slug, button_type))
            if not raw:
                return None
            return json.loads(raw)
        except Exception as e:
            logger.warning(
                "ButtonCache: get failed",
                slug=slug,
                button_type=button_type,
                error=str(e),
            )
            return None

    async def set(self, slug: str, button_type: str, response: dict) -> None:
        """Cache a response. No-op for non-cacheable types or when Redis is down.

        The stored payload is augmented with cached_at + button_type so the
        startup reconciliation scan can compare timestamps without re-deriving
        them from the key.
        """
        if button_type not in CACHEABLE_TYPES:
            return
        if self._client is None:
            return
        payload = {
            **response,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "button_type": button_type,
        }
        try:
            await self._client.set(
                make_key(slug, button_type),
                json.dumps(payload),
                ex=SAFETY_TTL,
            )
            logger.info(
                "ButtonCache: set",
                slug=slug,
                button_type=button_type,
                size_bytes=len(json.dumps(payload)),
            )
        except Exception as e:
            logger.warning(
                "ButtonCache: set failed",
                slug=slug,
                button_type=button_type,
                error=str(e),
            )

    async def invalidate_bill(self, slug: str) -> int:
        """Delete all cached entries for a bill slug. Returns count deleted.

        Idempotent — Redis DEL on a missing key is a no-op.
        """
        if self._client is None:
            return 0
        deleted = 0
        for bt in CACHEABLE_TYPES:
            try:
                result = await self._client.delete(make_key(slug, bt))
                deleted += int(result or 0)
            except Exception as e:
                logger.warning(
                    "ButtonCache: invalidate failed",
                    slug=slug,
                    button_type=bt,
                    error=str(e),
                )
        if deleted:
            logger.info("ButtonCache: invalidated bill", slug=slug, deleted=deleted)
        return deleted

    async def list_cached_keys(self) -> list[str]:
        """List all currently-cached button keys (for startup reconciliation).

        Uses SCAN, not KEYS, to avoid blocking Redis on large keyspaces.
        """
        if self._client is None:
            return []
        keys: list[str] = []
        try:
            async for key in self._client.scan_iter(match=f"{KEY_PREFIX}*", count=500):
                keys.append(key)
        except Exception as e:
            logger.warning("ButtonCache: scan failed", error=str(e))
        return keys


# Singleton accessor — instantiated lazily so tests can construct an
# isolated ButtonCache without poisoning the module state.
_button_cache: Optional[ButtonCache] = None


def get_button_cache() -> ButtonCache:
    global _button_cache
    if _button_cache is None:
        from votebot.services.redis_store import get_redis_store
        _button_cache = ButtonCache(get_redis_store())
    return _button_cache


# -------- Pub/sub subscriber lifecycle --------
#
# We run the subscriber on every uvicorn worker. Cache invalidation is
# idempotent (delete-on-empty is a no-op), so duplicate processing across
# workers is harmless — and avoids the complexity of a Redis-based leader
# election. The plan originally suggested "leader worker only" but the
# simpler all-workers approach is functionally equivalent at this scale.

_invalidate_pubsub = None
_invalidate_task: Optional[asyncio.Task] = None


async def start_invalidate_subscriber(redis_store: RedisStore) -> None:
    """Start listening for invalidation events on votebot:cache:invalidate.

    Runs a supervisor loop that auto-reconnects on Redis disconnects with
    exponential backoff (capped at 60s). Without auto-reconnect, a transient
    Redis blip would silently stop invalidation processing until the next
    full service restart — stale cached responses would then accumulate
    until each entry hit the 7-day safety TTL.

    No-op when Redis is unavailable at startup. Safe to call multiple times
    — duplicate starts are detected and ignored.
    """
    global _invalidate_pubsub, _invalidate_task

    if redis_store._client is None:
        logger.info("ButtonCache subscriber skipped — Redis unavailable")
        return
    if _invalidate_task is not None and not _invalidate_task.done():
        return  # already running

    cache = ButtonCache(redis_store)

    async def _process_messages(pubsub):
        """Process messages from a single pubsub connection.

        Returns when the connection drops (caller reconnects). Raises
        CancelledError on shutdown (caller propagates).
        """
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                payload = json.loads(message["data"])
                slug = payload.get("slug")
                if not slug:
                    continue
                deleted = await cache.invalidate_bill(slug)
                logger.info(
                    "ButtonCache: invalidation event handled",
                    slug=slug,
                    reason=payload.get("reason"),
                    version_note=payload.get("version_note"),
                    deleted=deleted,
                )
            except json.JSONDecodeError:
                logger.warning(
                    "ButtonCache: invalid JSON on invalidate channel",
                    raw=message.get("data"),
                )
            except Exception as e:
                logger.error(
                    "ButtonCache: error handling invalidation event",
                    error=str(e),
                )

    async def _supervisor():
        """Outer loop: reconnect on disconnect with exponential backoff."""
        global _invalidate_pubsub
        backoff = 1
        max_backoff = 60
        while True:
            try:
                if redis_store._client is None:
                    # Redis went away entirely — wait + retry
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                    continue

                _invalidate_pubsub = redis_store._client.pubsub()
                await _invalidate_pubsub.subscribe(INVALIDATE_CHANNEL)
                logger.info(
                    "ButtonCache subscriber connected",
                    channel=INVALIDATE_CHANNEL,
                )
                backoff = 1  # reset backoff after a successful connect

                await _process_messages(_invalidate_pubsub)

                # listen() returned cleanly (no error) — connection probably
                # closed; loop and reconnect
                logger.warning(
                    "ButtonCache subscriber stream ended; reconnecting",
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "ButtonCache subscriber connection error; will reconnect",
                    error=str(e),
                    backoff_seconds=backoff,
                )
                # Best-effort cleanup of the dead pubsub
                try:
                    if _invalidate_pubsub:
                        await _invalidate_pubsub.close()
                except Exception:
                    pass
                _invalidate_pubsub = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    _invalidate_task = asyncio.create_task(_supervisor())
    logger.info("ButtonCache subscriber started", channel=INVALIDATE_CHANNEL)


async def reconcile_on_startup(redis_store: RedisStore) -> None:
    """Drop cache entries generated before the bill was last re-ingested.

    Pub/sub is fire-and-forget — events emitted while VoteBot was down or
    restarting are lost. This catches that case at startup.

    Algorithm:
      1. Scan ddp:bill_version:* records (small set — one per actively-synced bill).
      2. For each record, read bill_slug + last_checked.
      3. For each cacheable button_type, check if the cached entry's cached_at
         predates last_checked. If so, delete it.

    The slug↔webflow_id mapping is stored in the version record itself
    (bill_slug field added by ddp-sync alongside last_checked), so we don't
    need a separate index or a Webflow lookup.

    Falls back gracefully if any step fails — the 7-day safety TTL eventually
    cleans up anything missed.
    """
    if redis_store._client is None:
        return

    BILL_VERSION_PREFIX = "ddp:bill_version:"
    cache = ButtonCache(redis_store)

    invalidated = 0
    scanned = 0
    try:
        async for ver_key in redis_store._client.scan_iter(
            match=f"{BILL_VERSION_PREFIX}*", count=500
        ):
            scanned += 1
            try:
                raw = await redis_store._client.get(ver_key)
                if not raw:
                    continue
                version_data = json.loads(raw)
                slug = version_data.get("bill_slug")
                last_checked_str = version_data.get("last_checked")
                if not slug or not last_checked_str:
                    continue

                # Parse last_checked (ISO without timezone — ddp-sync uses utcnow)
                last_checked = datetime.fromisoformat(
                    last_checked_str.replace("Z", "")
                )

                for bt in CACHEABLE_TYPES:
                    cache_key = make_key(slug, bt)
                    cached_raw = await redis_store._client.get(cache_key)
                    if not cached_raw:
                        continue
                    cached_at_str = json.loads(cached_raw).get("cached_at")
                    if not cached_at_str:
                        continue
                    cached_at = datetime.fromisoformat(
                        cached_at_str.replace("Z", "").replace("+00:00", "")
                    )
                    if cached_at < last_checked:
                        await redis_store._client.delete(cache_key)
                        invalidated += 1
                        logger.info(
                            "ButtonCache reconciliation: invalidated stale entry",
                            slug=slug,
                            button_type=bt,
                            cached_at=cached_at.isoformat(),
                            last_checked=last_checked.isoformat(),
                        )
            except Exception as e:
                logger.warning(
                    "ButtonCache reconciliation: error processing version key",
                    key=ver_key,
                    error=str(e),
                )
    except Exception as e:
        logger.warning("ButtonCache reconciliation: scan failed", error=str(e))

    logger.info(
        "ButtonCache reconciliation complete",
        bill_versions_scanned=scanned,
        invalidated=invalidated,
    )


async def stop_invalidate_subscriber() -> None:
    """Cancel the subscriber task and unsubscribe from the channel.

    Called from the FastAPI lifespan shutdown path.
    """
    global _invalidate_pubsub, _invalidate_task

    if _invalidate_task is not None and not _invalidate_task.done():
        _invalidate_task.cancel()
        try:
            await _invalidate_task
        except asyncio.CancelledError:
            pass
        _invalidate_task = None

    if _invalidate_pubsub is not None:
        try:
            await _invalidate_pubsub.unsubscribe(INVALIDATE_CHANNEL)
            await _invalidate_pubsub.close()
        except Exception:
            pass
        _invalidate_pubsub = None
    logger.info("ButtonCache subscriber stopped")
