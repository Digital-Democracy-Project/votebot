"""Content resolution endpoint for chat widget context."""

import re
from urllib.parse import urlparse

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query

from votebot.config import get_settings

logger = structlog.get_logger()
router = APIRouter(prefix="/content", tags=["content"])

# URL patterns for DDP content
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

    Parses the URL to determine content type and slug, then fetches
    metadata from Webflow CMS.

    Args:
        url: Full DDP URL (e.g., https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025)

    Returns:
        Content metadata including type, id, title, jurisdiction, etc.
    """
    settings = get_settings()

    # Parse URL
    parsed = urlparse(url)
    path = parsed.path

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
            api_key=settings.webflow_api_key.get_secret_value(),
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
