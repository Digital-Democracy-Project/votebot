"""Links to our own pages on the new website (ddp-next).

The bill page is `/explore/{JURISDICTION}/{SESSION}/{IDENTIFIER}` (the identifier URL-encoded, e.g.
`HB%20219`), the same shape `/content/resolve` accepts. Nothing is built unless `DDP_SITE_BASE_URL`
is set: there is no default host, so an unset value leaves every link as it was.
"""

from urllib.parse import quote

BILL_DOCUMENT_TYPES = frozenset({"bill-text", "bill-version-diff", "bill-votes"})


def ddp_bill_url(base_url: str, jurisdiction: str | None, session: str | None, gov_id: str | None) -> str | None:
    """The bill's page on the new site, or None when the base URL or any part is missing."""
    if not (base_url and jurisdiction and session and gov_id):
        return None
    return (
        f"{base_url.rstrip('/')}/explore/{quote(jurisdiction.upper(), safe='')}"
        f"/{quote(session, safe='')}/{quote(gov_id, safe='')}"
    )


def ddp_bill_url_from_metadata(base_url: str, metadata: dict) -> str | None:
    """The page URL for a bill document of the canonical-id index (it carries `jurisdiction`,
    `session_code` and `gov_id`), or None for any other document."""
    if metadata.get("document_type") not in BILL_DOCUMENT_TYPES:
        return None
    return ddp_bill_url(
        base_url, metadata.get("jurisdiction"), metadata.get("session_code"), metadata.get("gov_id")
    )
