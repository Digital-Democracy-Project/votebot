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


@dataclass(frozen=True)
class BillVersion:
    """One version of a bill as api-v3 reports it."""

    document_id: str | None  # archived_document_id as a string, the vectors' `document_id`; None if not archived
    note: str
    date: str
    stage: str
    ordinal: int | None


def current_version(versions: list[BillVersion] | None) -> BillVersion | None:
    """The newest version that retrieval can actually answer from.

    Stage-unknown versions are never current (api-v3 keeps them out of the diff lineage), and a
    version with no archived document has no text in the index yet, so the newest one that has both
    wins. `versions` is api-v3's order, latest last; it is not re-sorted here.
    """
    for version in reversed(versions or []):
        if version.stage != STAGE_UNKNOWN and version.document_id:
            return version
    return None


class BillVersionService:
    """Asks api-v3 for a bill's ordered versions, with a short in-process cache."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._cache: dict[str, tuple[float, list[BillVersion]]] = {}

    async def get_versions(self, ocd_bill_id: str) -> list[BillVersion] | None:
        """Versions in api-v3 order (latest last), or None when they cannot be determined.

        None is never cached, and callers treat it as "no version filter": retrieval then returns
        the bill's chunks from every version, each labelled with its own version.
        """
        if not self.settings.use_ddp_openstates_replica:
            return None  # the public OpenStates API carries none of the DDP version fields

        cached = self._cache.get(ocd_bill_id)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

        data = await self._fetch(ocd_bill_id)
        if data is None:
            return None
        versions = [
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
        self._cache[ocd_bill_id] = (time.monotonic(), versions)
        return versions

    async def _fetch(self, ocd_bill_id: str) -> dict | None:
        url = f"{openstates_base_url(self.settings)}/bills/ocd-bill/{ocd_bill_id}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
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
