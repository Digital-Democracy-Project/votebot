"""Bill version lookup for version-aware retrieval (VOTEBOT-10).

PLAN-enterprise-search.md 5.6: every version of a bill is embedded in the canonical-id index, and
"current" is *looked up, not stored*: a version stops being current when a newer one arrives, and
rewriting old vectors' metadata for that would be costly and racy. So retrieval asks api-v3 for the
bill's ordered versions and filters on the current one's `document_id`.

Nothing here orders or classifies versions. api-v3 returns `versions` in chronological order, latest
last, with `version_stage` (``version_ordering.note_stage``), `version_ordinal` and
`archived_document_id` on each (single-bill detail with ``include=versions``). Those fields exist
only on DDP's api-v3, so this is skipped unless OpenStates calls are routed to the DDP replica.
"""

import time
from dataclasses import dataclass

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.services.openstates_client import openstates_base_url, openstates_headers

logger = structlog.get_logger()

STAGE_UNKNOWN = "unknown"  # api-v3's label for a version the stage classifier could not place
CACHE_TTL_SECONDS = 120  # a new version appears at most this late; saves a call per chat message
FAILURE_TTL_SECONDS = 45  # while api-v3 is down, retry this often rather than on every chat message
FETCH_TIMEOUT_SECONDS = 3.0  # the lookup runs before retrieval, so it must not hold up the reply


@dataclass(frozen=True)
class BillVersion:
    """One version of a bill as api-v3 reports it."""

    document_id: str | None  # archived_document_id as a string, the vectors' `document_id`; None if not archived
    note: str
    date: str
    stage: str
    ordinal: int | None


@dataclass(frozen=True)
class VersionDiff:
    """The stored diff of one version against the one before it, as api-v3 reports it."""

    document_id: str
    note: str
    date: str
    stage: str
    from_note: str | None  # the previous classifiable version
    from_document_id: str | None
    text: str



def current_version(versions: list[BillVersion] | None) -> BillVersion | None:
    """The bill's current version: the latest classifiable one, and only if it can be filtered on.

    Stage-unknown versions are never current: the classifier cannot place them in time, which is
    also why api-v3 keeps them out of the diff lineage. They are skipped, and the latest
    *classifiable* version is current. If that version has no archived document yet, there is no
    current version: promoting an older one would label stale text "current" while a newer
    version exists. `versions` is api-v3's order, latest last; it is not re-sorted here.
    """
    for version in reversed(versions or []):
        if version.stage != STAGE_UNKNOWN:
            return version if version.document_id else None
    return None


class BillVersionService:
    """Asks api-v3 for a bill's ordered versions, with a short in-process cache."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._cache: dict[str, tuple[float, list[BillVersion] | None]] = {}  # None = recent failure

    async def get_versions(self, ocd_bill_id: str) -> list[BillVersion] | None:
        """Versions in api-v3 order (latest last), or None when they cannot be determined.

        Callers treat None as "no version filter": retrieval then returns the bill's chunks from
        every version, each labelled with its own version. A failure is remembered briefly so a
        down api-v3 does not add a timeout to every message.
        """
        if not self.settings.use_ddp_openstates_replica:
            return None  # the public OpenStates API carries none of the DDP version fields

        cached = self._cache.get(ocd_bill_id)
        if cached:
            ttl = CACHE_TTL_SECONDS if cached[1] is not None else FAILURE_TTL_SECONDS
            if time.monotonic() - cached[0] < ttl:
                return cached[1]

        data = await self._fetch(ocd_bill_id)
        if data is None:
            self._cache[ocd_bill_id] = (time.monotonic(), None)
            return None
        versions = self._parse_versions(data)
        self._cache[ocd_bill_id] = (time.monotonic(), versions)
        return versions

    @staticmethod
    def _parse_versions(data: dict) -> list[BillVersion]:
        return [
            BillVersion(
                document_id=(
                    str(v["archived_document_id"]) if v.get("archived_document_id") is not None else None
                ),
                note=v.get("note") or "",
                date=v.get("date") or "",
                stage=v.get("version_stage") or STAGE_UNKNOWN,
                ordinal=v.get("version_ordinal"),
            )
            for v in data.get("versions") or []
        ]

    async def get_diffs(
        self, ocd_bill_id: str, stages: tuple[str, ...] = (), dates: tuple[str, ...] = ()
    ) -> list[VersionDiff] | None:
        """What changed in a version, read live from api-v3's stored `diff_from_previous_version`.

        Diffs are not embedded (the index holds version text only), so a "what changed" question
        reads them here. The current version's diff by default, or the versions a question names by
        stage and/or date. api-v3 stores a diff only for a bill's latest version and the one before
        it, so an older named version may have none (it is then simply absent). Not cached: diffs
        can be large and the question is rare. None when api-v3 cannot be asked or has no answer.
        """
        if not self.settings.use_ddp_openstates_replica:
            return None
        data = await self._fetch(ocd_bill_id)
        if data is None:
            return None
        raw = [r for r in data.get("versions") or [] if isinstance(r, dict)]
        versions = self._parse_versions({"versions": raw})
        # api-v3's own lineage: every classifiable version in chronological order (stage-unknown ones
        # are outside it), archived or not. A diff compares a version with the one before it in
        # THIS list, so that is the label it gets.
        lineage = [(v, r) for v, r in zip(versions, raw, strict=True) if v.stage != STAGE_UNKNOWN]
        if stages or dates:
            chosen = [
                i for i, (v, _) in enumerate(lineage)
                if v.document_id and (not stages or v.stage in stages) and (not dates or v.date in dates)
            ]
        else:
            current = current_version(versions)
            chosen = [i for i, (v, _) in enumerate(lineage) if current and v.document_id == current.document_id]
        diffs = []
        for i in chosen:
            version, record = lineage[i]
            text = record.get("diff_from_previous_version")
            if not isinstance(text, str) or not text.strip():
                continue
            previous = lineage[i - 1][0] if i > 0 else None
            if previous is not None and not previous.document_id:
                # The version before it has no archived text, so what this diff was computed against
                # is not established: better no comparison than one labelled with the wrong version.
                logger.warning("Skipping a diff whose predecessor is not archived", ocd_bill_id=ocd_bill_id, version=version.note)
                continue
            diffs.append(
                VersionDiff(
                    document_id=version.document_id,
                    note=version.note,
                    date=version.date,
                    stage=version.stage,
                    from_note=previous.note if previous else None,
                    from_document_id=previous.document_id if previous else None,
                    text=text,
                )
            )
        return diffs

    async def _fetch(self, ocd_bill_id: str) -> dict | None:
        try:
            url = f"{openstates_base_url(self.settings)}/bills/ocd-bill/{ocd_bill_id}"
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    url,
                    headers={"accept": "application/json", **openstates_headers(self.settings)},
                    params=[("include", "versions")],
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:  # noqa: BLE001 -- a failed lookup must degrade retrieval, never break it
            logger.warning("Could not read bill versions from api-v3", ocd_bill_id=ocd_bill_id, error=str(e))
            return None
