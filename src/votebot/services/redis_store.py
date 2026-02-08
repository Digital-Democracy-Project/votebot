"""Redis client wrapper for cross-worker shared state and pub/sub.

Provides:
- Thread-to-session mapping (Redis hash) for Slack handoff routing
- Pub/sub for agent event delivery across uvicorn workers
- Graceful fallback: all methods no-op when Redis is unavailable
"""

import asyncio
import json
from typing import Callable, Optional

import structlog

from votebot.config import get_settings

logger = structlog.get_logger()

# Redis key constants
THREAD_HASH_KEY = "votebot:threads"
AGENT_EVENTS_CHANNEL = "votebot:agent_events"


class RedisStore:
    """Thin wrapper around redis.asyncio for shared state + pub/sub."""

    def __init__(self):
        self._client = None
        self._pubsub = None
        self._subscriber_task: Optional[asyncio.Task] = None

    @property
    def is_available(self) -> bool:
        return self._client is not None

    async def connect(self):
        """Connect to Redis. Called from main.py lifespan startup."""
        try:
            import redis.asyncio as aioredis

            settings = get_settings()
            self._client = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
            )
            # Verify connectivity
            await self._client.ping()
            logger.info("Redis connected for cross-worker state", url=settings.redis_url)
        except Exception as e:
            logger.warning(
                "Redis unavailable — falling back to in-memory state (single-worker only)",
                error=str(e),
            )
            self._client = None

    async def disconnect(self):
        """Disconnect from Redis. Called from main.py lifespan shutdown."""
        if self._subscriber_task and not self._subscriber_task.done():
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except asyncio.CancelledError:
                pass
            self._subscriber_task = None

        if self._pubsub:
            try:
                await self._pubsub.unsubscribe(AGENT_EVENTS_CHANNEL)
                await self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None

        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            logger.info("Redis disconnected")

    # -- Thread-to-session mapping (Redis hash) --

    async def set_thread_mapping(self, thread_ts: str, session_id: str):
        """Store thread_ts → session_id mapping in Redis."""
        if not self._client:
            return
        try:
            await self._client.hset(THREAD_HASH_KEY, thread_ts, session_id)
        except Exception as e:
            logger.error("Redis: failed to set thread mapping", error=str(e))

    async def get_session_for_thread(self, thread_ts: str) -> Optional[str]:
        """Look up session_id for a Slack thread_ts from Redis."""
        if not self._client:
            return None
        try:
            return await self._client.hget(THREAD_HASH_KEY, thread_ts)
        except Exception as e:
            logger.error("Redis: failed to get thread mapping", error=str(e))
            return None

    async def remove_thread_mapping(self, thread_ts: str):
        """Remove a thread_ts mapping from Redis."""
        if not self._client:
            return
        try:
            await self._client.hdel(THREAD_HASH_KEY, thread_ts)
        except Exception as e:
            logger.error("Redis: failed to remove thread mapping", error=str(e))

    # -- Sync task storage --

    SYNC_TASK_PREFIX = "votebot:sync:task:"
    SYNC_TASK_TTL = 86400  # 24 hours

    async def set_sync_task(self, task_id: str, task_data: dict):
        """Store sync task state in Redis with TTL."""
        if not self._client:
            return
        try:
            await self._client.set(
                f"{self.SYNC_TASK_PREFIX}{task_id}",
                json.dumps(task_data),
                ex=self.SYNC_TASK_TTL,
            )
        except Exception as e:
            logger.error("Redis: failed to set sync task", task_id=task_id, error=str(e))

    async def get_sync_task(self, task_id: str) -> dict | None:
        """Retrieve sync task state from Redis."""
        if not self._client:
            return None
        try:
            data = await self._client.get(f"{self.SYNC_TASK_PREFIX}{task_id}")
            if data:
                return json.loads(data)
        except Exception as e:
            logger.error("Redis: failed to get sync task", task_id=task_id, error=str(e))
        return None

    # -- Pub/sub for agent events --

    async def publish_agent_event(self, event_type: str, session_id: str, payload: dict):
        """Publish an agent event to all workers."""
        if not self._client:
            return
        try:
            event = json.dumps({
                "event_type": event_type,
                "session_id": session_id,
                "payload": payload,
            })
            await self._client.publish(AGENT_EVENTS_CHANNEL, event)
        except Exception as e:
            logger.error("Redis: failed to publish agent event", error=str(e))

    async def subscribe_agent_events(self, handler: Callable):
        """Subscribe to agent events and dispatch to handler.

        Starts a background task that listens for events and calls
        handler(event_data: dict) for each one.
        """
        if not self._client:
            return

        # Don't start duplicate subscribers
        if self._subscriber_task and not self._subscriber_task.done():
            return

        self._pubsub = self._client.pubsub()
        await self._pubsub.subscribe(AGENT_EVENTS_CHANNEL)

        async def _listen():
            try:
                async for message in self._pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        event_data = json.loads(message["data"])
                        await handler(event_data)
                    except json.JSONDecodeError:
                        logger.warning("Redis: invalid JSON in agent event")
                    except Exception as e:
                        logger.error("Redis: error handling agent event", error=str(e))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Redis: pub/sub listener crashed", error=str(e))

        self._subscriber_task = asyncio.create_task(_listen())
        logger.info("Redis pub/sub subscriber started", channel=AGENT_EVENTS_CHANNEL)


# Singleton
_redis_store: Optional[RedisStore] = None


def get_redis_store() -> RedisStore:
    """Get the singleton RedisStore instance."""
    global _redis_store
    if _redis_store is None:
        _redis_store = RedisStore()
    return _redis_store
