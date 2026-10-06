"""Legislator facts, read live from api-v3's `/people` (VOTEBOT-15).

Legislators are not embedded in the canonical-id index (SYNC-94), so questions about a legislator
(party, chamber, district, contact, links) are answered from api-v3's people records, the way votes
come from the live bill lookup. api-v3 is reached the same way as every other OpenStates call
(`openstates_base_url` / `openstates_headers`), so it is the DDP copy when the replica flag is on.

Every method returns None when api-v3 cannot answer, so a problem costs the enrichment, never the answer.
"""

from dataclasses import dataclass, field

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.services.openstates_client import openstates_base_url, openstates_headers

logger = structlog.get_logger()

TIMEOUT_SECONDS = 5.0
MAX_CANDIDATES = 4  # shown when a name matches several people
CHAMBERS = {"upper": "Senate", "lower": "House"}


@dataclass
class Legislator:
    person_id: str
    name: str
    party: str = ""
    title: str = ""
    chamber: str = ""
    district: str = ""
    jurisdiction: str = ""
    current: bool = False  # has a current role in the records
    email: str = ""
    offices: list[str] = field(default_factory=list)  # "Capitol Office: address; phone"
    links: list[tuple[str, str]] = field(default_factory=list)  # (note, url)
    profile_url: str = ""


def _person(raw: dict) -> Legislator | None:
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
        return None
    role = raw.get("current_role") if isinstance(raw.get("current_role"), dict) else {}
    jurisdiction = raw.get("jurisdiction") if isinstance(raw.get("jurisdiction"), dict) else {}
    offices = []
    for o in raw.get("offices") or []:
        if isinstance(o, dict) and o.get("name"):
            detail = "; ".join(x for x in (o.get("address"), o.get("voice")) if x)
            offices.append(f"{o['name']}: {detail}" if detail else o["name"])
    return Legislator(
        person_id=raw["id"],
        name=raw["name"],
        party=raw.get("party") or "",
        title=role.get("title") or "",
        chamber=CHAMBERS.get(role.get("org_classification"), role.get("org_classification") or ""),
        district=str(role.get("district") or ""),
        jurisdiction=jurisdiction.get("name") or "",
        current=bool(role),
        email=raw.get("email") or "",
        offices=offices,
        links=[(link.get("note") or "", link["url"]) for link in raw.get("links") or [] if isinstance(link, dict) and link.get("url")],
        profile_url=raw.get("openstates_url") or "",
    )


@dataclass
class PeopleMatch:
    """What api-v3 answered: the people returned and how many matched in all (`total` can exceed
    `len(people)` when a name matches many)."""

    people: list[Legislator] = field(default_factory=list)
    total: int = 0


class LegislatorLookupService:
    """People records from api-v3. None means api-v3 could not answer (a failure); a PeopleMatch with
    no people means it answered and nobody matched. The two are told apart in what the model sees."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def _people(self, params: dict) -> PeopleMatch | None:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{openstates_base_url(self.settings)}/people",
                    headers={"accept": "application/json", **openstates_headers(self.settings)},
                    params=[*params.items(), ("include", "offices"), ("include", "links")],
                )
                response.raise_for_status()
                body = response.json()
        except Exception as e:  # noqa: BLE001 -- see the module docstring
            logger.warning("People lookup failed", params=params, error=str(e))
            return None
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            return None
        people = [p for p in map(_person, results) if p]
        meta = body.get("pagination") if isinstance(body.get("pagination"), dict) else {}
        total = meta.get("total_items") if isinstance(meta.get("total_items"), int) else len(people)
        logger.info("People lookup", params=params, returned=len(people), total=total)
        return PeopleMatch(people, max(total, len(people)))

    async def find_by_id(self, person_id: str) -> PeopleMatch | None:
        return await self._people({"id": person_id})

    async def find_by_name(self, name: str, jurisdiction: str | None = None) -> PeopleMatch | None:
        """People matching a name (api-v3 matches the name as a case-insensitive substring, and
        other names too). With a jurisdiction api-v3 returns only its current members."""
        params = {"name": name, "per_page": 10}
        if jurisdiction:
            params["jurisdiction"] = jurisdiction
        return await self._people(params)


SOURCE = "Authoritative Source: OpenStates people records, current"

UNAVAILABLE = (
    "## Legislator records unavailable\n"
    "Current legislator records could not be retrieved right now. Do not state a legislator's current role, "
    "party, district or contact details from memory; tell the user you could not retrieve them just now."
)


def format_legislators(match: PeopleMatch | None, asked: str = "") -> str:
    """Markdown for the LLM context: one profile, or the candidates when several people match
    (so the model asks which one instead of guessing). Current members are preferred over former.
    A match that api-v3 reports as larger than what it returned says so."""
    if not match or not match.people:
        return ""
    people = match.people
    current = [p for p in people if p.current]
    pool = current or people
    if len(pool) > 1 or (len(pool) == 1 and match.total > len(people)):
        lines = [f"## Several legislators match \"{asked}\" ({SOURCE})",
                 "Do not guess which one the user means; ask them."]
        if match.total > min(len(pool), MAX_CANDIDATES):
            lines.append(f"{match.total} people match in all; only some are listed, so ask for a fuller name.")
        for p in pool[:MAX_CANDIDATES]:
            role = ", ".join(x for x in (p.title, f"District {p.district}" if p.district else "", p.chamber, p.jurisdiction) if x)
            lines.append(f"- {p.name}" + (f" ({p.party})" if p.party else "") + (f": {role}" if role else ""))
        return "\n".join(lines)
    p = pool[0]
    lines = [f"## Legislator Profile ({SOURCE})", f"**{p.name}**" + (f" ({p.party})" if p.party else "")]
    if p.current:
        role = ", ".join(x for x in (p.title, f"District {p.district}" if p.district else "", p.chamber, p.jurisdiction) if x)
        lines.append(f"- **Current role:** {role}")
    else:
        lines.append("- **Current role:** none in the records (a former legislator)")
    if p.email:
        lines.append(f"- **Email:** {p.email}")
    lines += [f"- **Office:** {o}" for o in p.offices]
    lines += [f"- **Link:** [{note or url}]({url})" for note, url in p.links[:5]]
    if p.profile_url:
        lines.append(f"- **OpenStates profile:** {p.profile_url}")
    return "\n".join(lines)
