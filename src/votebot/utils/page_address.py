"""The address of a page, without what can identify a visitor (VOTEBOT-63).

A page address can carry a sign-in token, an email-link token or personal data in its query string or
fragment, or credentials before the host. The widget sends the full address, and an embedded copy (a cached
bundle, another site) will keep doing so, so what is written to the query log or posted to Slack goes through
`page_address` first. `core/prompts._page_url` applies the same rule to the address the model sees.
"""

from urllib.parse import urlparse


def page_address(value: object) -> str | None:
    """`scheme://host[:port]/path` of an http(s) address, with no credentials, query string or fragment.

    None for anything that is not an http(s) address with a host (so nothing is kept rather than a guess).
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value.strip())
    except ValueError:  # e.g. an unclosed "[" in the host: the value comes from a browser, so it can be anything
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.rpartition("@")[2]  # credentials come before the last "@"
    return f"{parsed.scheme}://{host}{parsed.path}" if host else None
