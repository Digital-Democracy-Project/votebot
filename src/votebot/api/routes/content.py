"""Content resolution endpoint for chat widget context."""

import re
import uuid
from urllib.parse import unquote, urlparse

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query

from votebot.config import get_settings

logger = structlog.get_logger()
router = APIRouter(prefix="/content", tags=["content"])

# ddp-next bill URLs (VOTEBOT-8). A Webflow bill slug is never all digits, so a numeric single
# segment is a ddp-broker-py bill id; the three-segment form is the bill's natural key.
DDP_NEXT_BILL_BY_ID = re.compile(r"^/bills/(\d+)/?$")
DDP_NEXT_BILL_BY_KEY = re.compile(r"^/bills/([A-Za-z]{2})/([^/]+)/([^/]+)/?$")
# The new site's bill page: /explore/{JURISDICTION}/{SESSION}/{IDENTIFIER}, identifier URL-encoded
# ("HB%20219"). Same natural key as above, resolved the same way.
DDP_NEXT_EXPLORE_BILL = re.compile(r"^/explore/([A-Za-z]{2})/([^/]+)/([^/]+)/?$")

# URL patterns for DDP content (Webflow-hosted pages; kept until Webflow is retired)
DDP_PATTERNS = {
    "bill": re.compile(r"^/bills/([^/]+)/?$"),
    "legislator": re.compile(r"^/legislators/([^/]+)/?$"),
    "organization": re.compile(r"^/member-organizations/([^/]+)/?$"),
}


@router.get("/resolve")
async def resolve_content(
    url: str = Query(..., description="DDP URL to resolve"),
):
    """
    Resolve a DDP URL to content metadata for the chat widget.

    ddp-next bill URLs (`/explore/{jurisdiction}/{session}/{gov_id}`, `/bills/{broker_id}` or
    `/bills/{jurisdiction}/{session}/{gov_id}`) are resolved through ddp-broker-py to the bill's
    OpenStates id, which is what retrieval filters on once VoteBot reads the canonical-id index.
    Every other URL is parsed for content type and slug and looked up in the Webflow CMS, exactly
    as before. ddp-next legislator (`/legislators/{numeric id}`) and organization URLs are not
    resolved yet: see `_match_ddp_next_bill`.

    Args:
        url: Full DDP URL (e.g., https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025)

    Returns:
        Content metadata including type, id, title, jurisdiction, etc.
    """
    settings = get_settings()

    # Parse URL
    parsed = urlparse(url)
    path = parsed.path

    next_bill = _match_ddp_next_bill(path)
    if next_bill:
        return await resolve_ddp_next_bill(url, next_bill)

    # Determine content type and extract slug
    content_type = None
    slug = None

    for ctype, pattern in DDP_PATTERNS.items():
        match = pattern.match(path)
        if match:
            content_type = ctype
            slug = match.group(1)
            break

    if not content_type or not slug:
        raise HTTPException(
            status_code=400,
            detail=f"Unable to parse DDP URL: {url}. Expected format: /bills/{{slug}}, /legislators/{{slug}}, or /member-organizations/{{slug}}",
        )

    logger.info(
        "Resolving DDP content",
        content_type=content_type,
        slug=slug,
    )

    # Get the appropriate collection ID
    collection_id = None
    if content_type == "bill":
        collection_id = settings.webflow_bills_collection_id
    elif content_type == "legislator":
        collection_id = settings.webflow_legislators_collection_id
    elif content_type == "organization":
        collection_id = settings.webflow_organizations_collection_id

    if not collection_id:
        raise HTTPException(
            status_code=500,
            detail=f"Webflow collection not configured for {content_type}",
        )

    # Fetch from Webflow
    try:
        item = await fetch_webflow_item_by_slug(
            collection_id=collection_id,
            slug=slug,
            api_key=settings.webflow_votebot_api_key.get_secret_value(),
        )
    except Exception as e:
        logger.error(
            "Failed to fetch from Webflow",
            content_type=content_type,
            slug=slug,
            error=str(e),
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch content from CMS: {str(e)}",
        )

    if not item:
        raise HTTPException(
            status_code=404,
            detail=f"Content not found: {slug}",
        )

    # Extract metadata based on content type
    fields = item.get("fieldData", {})

    if content_type == "bill":
        jurisdiction = extract_jurisdiction(fields)
        session = extract_session(fields, slug, jurisdiction)
        return {
            "type": "bill",
            "id": f"{fields.get('bill-prefix', '')} {fields.get('bill-number', '')}".strip() or slug,
            "title": fields.get("name", ""),
            "jurisdiction": jurisdiction,
            "session": session,
            "description": truncate_text(strip_html(fields.get("description", "")), 200),
            "status": fields.get("status", ""),
            "url": url,
            "slug": slug,
            "webflow_id": item.get("id"),  # Used for Pinecone filtering
        }
    elif content_type == "legislator":
        return {
            "type": "legislator",
            "id": fields.get("openstatesid", slug),
            "title": fields.get("name", ""),
            "jurisdiction": extract_jurisdiction(fields),
            "party": fields.get("party-2", fields.get("party", "")),
            "chamber": fields.get("chamber", ""),
            "url": url,
            "slug": slug,
            "webflow_id": item.get("id"),  # Used for Pinecone filtering
        }
    elif content_type == "organization":
        return {
            "type": "organization",
            "id": slug,
            "title": fields.get("name", ""),
            "organization_type": fields.get("type-2", ""),
            "url": url,
            "slug": slug,
            "webflow_id": item.get("id"),  # Used for Pinecone filtering
        }

    return {"type": "general", "url": url}


def _match_ddp_next_bill(path: str) -> dict | None:
    """Recognise a ddp-next bill URL path; None for anything else (including Webflow slugs).

    Deliberately not recognised yet, so they fall through to the Webflow path: ddp-next
    `/legislators/{numeric id}` (the broker has no public endpoint that returns a legislator's
    OpenStates person id; its list and scorecard endpoints do not carry it) and organization
    URLs (ddp-next has no organization page). Add them here once the broker exposes the id.
    """
    match = DDP_NEXT_BILL_BY_ID.match(path)
    if match:
        return {"broker_id": int(match.group(1))}
    match = DDP_NEXT_BILL_BY_KEY.match(path) or DDP_NEXT_EXPLORE_BILL.match(path)
    if match:
        return {
            "jurisdiction": match.group(1),
            "session": unquote(match.group(2)),
            "gov_id": unquote(match.group(3)),
        }
    return None


async def _broker_get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
    """GET a ddp-broker-py JSON endpoint; raises httpx.HTTPStatusError on 4xx/5xx.

    A 200 whose body is not a JSON object breaks the broker's contract: a bad gateway (502).
    """
    response = await client.get(url, params=params)
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        logger.error("Broker returned a non-JSON-object body", url=url)
        raise HTTPException(status_code=502, detail="ddp-broker-py returned an invalid response")
    return body


async def resolve_ddp_next_bill(url: str, key: dict) -> dict:
    """Resolve a ddp-next bill URL to the page context the widget sends back with each message.

    ddp-broker-py has no bill-detail endpoint, so this uses the two public ones it does have:
    `/api/bills/{id}/scorecard/` (broker id -> jurisdiction, session, gov_id) and
    `/api/bills/resolve/` (jurisdiction, session, gov_id -> OpenStates bill id, bare UUID).
    """
    root = get_settings().ddp_broker_api_root.rstrip("/")
    if not root:
        raise HTTPException(
            status_code=503,
            detail="ddp-broker-py is not configured (DDP_BROKER_API_ROOT); cannot resolve ddp-next bill URLs",
        )

    title = None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if "broker_id" in key:
                scorecard = await _broker_get(client, f"{root}/api/bills/{key['broker_id']}/scorecard/")
                meta = scorecard.get("bill")
                meta = meta if isinstance(meta, dict) else {}
                key = {
                    "jurisdiction": meta.get("jurisdictionIso2"),
                    "session": (meta.get("session") if isinstance(meta.get("session"), dict) else {}).get("code"),
                    "gov_id": meta.get("govId"),
                }
                title = meta.get("title")
                if not all(key.values()):
                    raise HTTPException(status_code=502, detail="Broker scorecard is missing bill identity fields")
            resolved = await _broker_get(client, f"{root}/api/bills/resolve/", params=key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Bill not found: {url}")
        logger.error("Broker rejected bill lookup", url=url, status=e.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to resolve bill via ddp-broker-py")
    except httpx.RequestError as e:
        logger.error("Broker unreachable", url=url, error=str(e))
        raise HTTPException(status_code=502, detail="Failed to reach ddp-broker-py")

    # A 200 that does not carry a valid bare UUID breaks the broker's contract: a bad gateway, not
    # "not found" (a real miss is the 404 above). An id that does not match the vectors' metadata
    # would otherwise just retrieve nothing, silently.
    try:
        ocd_bill_id = str(uuid.UUID(str(resolved.get("bill_openstates_id"))))
    except ValueError:
        logger.error("Broker returned no valid bill_openstates_id", url=url)
        raise HTTPException(status_code=502, detail="ddp-broker-py returned an invalid bill id")

    payload = {
        "type": "bill",
        "id": key["gov_id"],
        "ocd_bill_id": ocd_bill_id,
        "gov_id": key["gov_id"],
        "jurisdiction": key["jurisdiction"].upper(),
        "session": key["session"],
        "url": url,
        "ddp_url": url,
    }
    if title:
        payload["title"] = title
    return payload


async def fetch_webflow_item_by_slug(
    collection_id: str,
    slug: str,
    api_key: str,
) -> dict | None:
    """
    Fetch a single item from Webflow by slug.

    Args:
        collection_id: Webflow collection ID
        slug: Item slug
        api_key: Webflow API key

    Returns:
        Item data or None if not found
    """
    base_url = "https://api.webflow.com/v2"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Webflow API doesn't support direct slug lookup, so we need to
        # paginate through items. For efficiency, we could cache this,
        # but for now we'll search with a reasonable limit.
        offset = 0
        page_size = 100
        max_pages = 10  # Safety limit

        for _ in range(max_pages):
            response = await client.get(
                f"{base_url}/collections/{collection_id}/items",
                headers=headers,
                params={"limit": page_size, "offset": offset},
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("items", [])

            for item in items:
                if item.get("fieldData", {}).get("slug") == slug:
                    return item

            # Check if there are more pages
            pagination = data.get("pagination", {})
            total = pagination.get("total", 0)
            if offset + len(items) >= total or len(items) < page_size:
                break

            offset += page_size

    return None


def extract_session(fields: dict, slug: str, jurisdiction: str) -> str:
    """Extract legislative session from CMS fields or slug."""
    # Check if session is explicitly set in CMS
    # Webflow uses 'session-code' for the OpenStates-friendly session identifier
    session = fields.get("session-code") or fields.get("session") or fields.get("legislative-session")
    if session:
        return str(session)

    # Try to extract year from slug (e.g., "one-big-beautiful-bill-act-hr1-2025")
    year_match = re.search(r"-(\d{4})$", slug)
    if year_match:
        year = int(year_match.group(1))
        # For federal bills, convert year to Congress number
        if jurisdiction == "US":
            congress = ((year - 1789) // 2) + 1
            return str(congress)
        # For state bills, use the year as session
        return str(year)

    # Default to current year/Congress
    from datetime import datetime
    current_year = datetime.now().year
    if jurisdiction == "US":
        congress = ((current_year - 1789) // 2) + 1
        return str(congress)
    return str(current_year)


def extract_jurisdiction(fields: dict) -> str:
    """Extract jurisdiction from CMS fields."""
    jurisdiction = fields.get("jurisdiction")
    if isinstance(jurisdiction, str):
        # 2-letter state code
        if len(jurisdiction) == 2:
            return jurisdiction.upper()
        # Webflow reference ID (24-char hex) - return US as default
        if len(jurisdiction) == 24 and jurisdiction.isalnum():
            return "US"
        return jurisdiction
    if isinstance(jurisdiction, list) and jurisdiction:
        # Reference field array - would need to resolve, default to US
        return "US"
    if isinstance(jurisdiction, dict):
        return jurisdiction.get("name", "US")
    return "US"


def strip_html(text: str) -> str:
    """Remove HTML tags from text."""
    if not text:
        return ""
    # Simple HTML stripping
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def truncate_text(text: str, max_length: int) -> str:
    """Truncate text to max length with ellipsis."""
    if not text or len(text) <= max_length:
        return text
    return text[: max_length - 3].rsplit(" ", 1)[0] + "..."
