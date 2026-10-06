"""Query-time lookups against ddp-broker-py, for the canonical-id index (VOTEBOT-15).

The organization/bill positions and organization profiles that used to come from the Webflow CMS
live in the broker now. These are its public, unauthenticated reads:

* `GET /api/bill-organization-positions/current/?jurisdiction=&session=&gov_id=`: the verified
  positions organizations hold on a bill (`{"found", "positions": [{org_name, position,
  citation_url, verification_explanation}]}`)
* `GET /api/organizations/{id}/positions/`: the verified positions one organization holds
  (paginated)
* `GET /api/organizations/{id}/`: the organization's public profile

Every method returns None when the broker is unconfigured, unreachable or answers something
unusable, so a broker problem costs the enrichment, never the answer.
"""

import time
from dataclasses import dataclass, field

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.utils.ddp_urls import ddp_bill_url

logger = structlog.get_logger()

TIMEOUT_SECONDS = 5.0  # one request; these run before the answer starts streaming
BUDGET_SECONDS = 8.0  # ALL the enrichment lookups for one message together (broker, legislators), paging included
POSITIONS_PAGE_SIZE = 200  # the broker's maximum
MAX_POSITION_PAGES = 3


@dataclass
class OrgPositionOnBill:
    org_name: str
    position: str  # "support" or "oppose"
    citation_url: str = ""


@dataclass
class BillOrgPositions:
    positions: list[OrgPositionOnBill] = field(default_factory=list)


@dataclass
class BillPositionOfOrg:
    bill_id: int | None  # the broker's bill id, to keep one row per bill
    gov_id: str
    title: str
    jurisdiction: str
    session: str
    position: str  # "support" or "oppose"
    citation_url: str = ""


@dataclass
class OrgBillPositions:
    """An organization's positions. `complete` is False when paging stopped early (a later page
    failed, or the page cap was reached), so the answer does not present it as the whole history."""

    positions: list[BillPositionOfOrg] = field(default_factory=list)
    complete: bool = True


@dataclass
class OrgDetails:
    name: str
    org_type: str = ""
    website: str = ""
    description: str = ""


class BrokerLookupService:
    """Positions and organization profiles from ddp-broker-py."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def _get(self, path: str, params: dict | None = None, deadline: float | None = None) -> dict | None:
        """GET a broker endpoint. `deadline` is a `time.monotonic()` value shared by everything one
        message looks up, so a request never waits past it."""
        root = self.settings.ddp_broker_api_root.rstrip("/")
        if not root:
            logger.debug("DDP_BROKER_API_ROOT is not set; skipping broker lookup", path=path)
            return None
        timeout = TIMEOUT_SECONDS if deadline is None else min(TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic()))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{root}{path}", params=params)
                response.raise_for_status()
                body = response.json()
        except Exception as e:  # noqa: BLE001 -- see the module docstring
            logger.warning("Broker lookup failed", path=path, error=str(e))
            return None
        if not isinstance(body, dict):
            logger.warning("Broker returned a non-object body", path=path)
            return None
        return body

    async def get_bill_org_positions(
        self, jurisdiction: str | None, session: str | None, gov_id: str | None, deadline: float | None = None
    ) -> BillOrgPositions | None:
        """Organizations' verified positions on a bill; None when unknown (see `found`)."""
        if not (jurisdiction and session and gov_id):
            return None
        body = await self._get(
            "/api/bill-organization-positions/current/",
            {"jurisdiction": jurisdiction, "session": session, "gov_id": gov_id},
            deadline,
        )
        if not body or not body.get("found"):
            return None
        rows = body.get("positions")
        if not isinstance(rows, list):
            return None
        return BillOrgPositions(
            [
                OrgPositionOnBill(r["org_name"], r["position"], r.get("citation_url") or "")
                for r in rows
                if isinstance(r, dict) and r.get("org_name") and r.get("position") in ("support", "oppose")
            ]
        )

    async def get_org_bill_positions(self, org_id: str, deadline: float | None = None) -> OrgBillPositions | None:
        """The verified positions one organization holds, one row per bill.

        The broker returns one row per bill VERSION, ordered by version id, and exposes no
        timestamp, so a bill with positions on several versions shows up several times. The last row
        per bill (the highest version id, normally the newest version) is kept, so a bill is never
        listed twice, nor under both Support and Oppose. Paging stops at `deadline` and returns
        what it has, marked incomplete, instead of discarding it.
        """
        by_bill: dict[object, BillPositionOfOrg] = {}
        complete = True
        for page in range(1, MAX_POSITION_PAGES + 1):
            if page > 1 and deadline is not None and time.monotonic() >= deadline:
                complete = False
                break
            body = await self._get(
                f"/api/organizations/{org_id}/positions/", {"page": page, "page_size": POSITIONS_PAGE_SIZE}, deadline
            )
            rows = body.get("results") if body else None
            if not isinstance(rows, list):
                if page == 1:
                    return None
                complete = False
                break
            for r in rows:
                if not (isinstance(r, dict) and r.get("position") in ("support", "oppose")):
                    continue
                row = BillPositionOfOrg(
                    r.get("bill_id") if isinstance(r.get("bill_id"), int) else None,
                    r.get("gov_id") or "",
                    r.get("bill_title") or "",
                    r.get("jurisdiction_iso2") or "",
                    r.get("session_code") or "",
                    r["position"],
                    r.get("citation_url") or "",
                )
                key = row.bill_id if row.bill_id is not None else (row.jurisdiction, row.session, row.gov_id)
                by_bill.pop(key, None)  # the later row replaces the earlier and moves to the end
                by_bill[key] = row
            if not body.get("next"):
                return OrgBillPositions(list(by_bill.values()))
        else:
            complete = False  # the page cap was reached
        return OrgBillPositions(list(by_bill.values()), complete=complete)

    async def get_org_details(self, org_id: str, deadline: float | None = None) -> OrgDetails | None:
        body = await self._get(f"/api/organizations/{org_id}/", None, deadline)
        if not body or not body.get("name"):
            return None
        return OrgDetails(
            body["name"], body.get("org_type") or "", body.get("website") or "", body.get("description") or ""
        )


SOURCE = "Authoritative Source: Digital Democracy Project database"


def format_bill_org_positions(result: BillOrgPositions | None) -> str:
    """Markdown for the LLM context; empty when the broker had nothing to say."""
    if result is None:
        return ""
    if not result.positions:
        return (
            f"## Organization Positions ({SOURCE})\n\n"
            "No organizations have a verified position on this bill in the Digital Democracy Project database."
        )
    parts = [f"## Organization Positions ({SOURCE})"]
    for heading, wanted in (("Organizations Supporting This Bill", "support"), ("Organizations Opposing This Bill", "oppose")):
        rows = [p for p in result.positions if p.position == wanted]
        if rows:
            lines = [f"### {heading}"]
            lines += [
                f"- {p.org_name}" + (f" ([source]({p.citation_url}))" if p.citation_url else "") for p in rows
            ]
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


def format_org_bill_positions(
    org: OrgDetails | None, result: OrgBillPositions | None, site_base_url: str = ""
) -> str:
    """Markdown for the LLM context: the bills an organization supports or opposes, each linked to
    its page on the new site when `site_base_url` is set."""
    if result is None:
        return ""
    positions = result.positions
    name = org.name if org else "this organization"
    if not positions:
        return (
            f"## Bill Positions for {name} ({SOURCE})\n\n"
            "No bill positions have been verified for this organization in the Digital Democracy Project database."
        )
    parts = [f"## Bill Positions for {name} ({SOURCE})"]
    for heading, wanted in (("Bills Supported", "support"), ("Bills Opposed", "oppose")):
        rows = [p for p in positions if p.position == wanted]
        if rows:
            lines = [f"### {heading}"]
            for p in rows:
                label = f"{p.gov_id} {p.title}".strip() or "Bill"
                url = ddp_bill_url(site_base_url, p.jurisdiction, p.session, p.gov_id)
                lines.append(f"- [{label}]({url})" if url else f"- {label}")
            parts.append("\n".join(lines))
    if not result.complete:
        parts.append("Note: this list may be incomplete; say so if asked for every bill this organization has a position on.")
    return "\n\n".join(parts)


def format_org_details(org: OrgDetails | None) -> str:
    """Markdown for dispute verification: what the database says about the organization."""
    if org is None:
        return ""
    lines = [f"## Organization Details ({SOURCE})", f"- **Name:** {org.name}"]
    if org.org_type:
        lines.append(f"- **Type:** {org.org_type}")
    if org.website:
        lines.append(f"- **Website:** {org.website}")
    if org.description:
        lines.append(f"- **Description:** {org.description[:300]}")
    return "\n".join(lines)
