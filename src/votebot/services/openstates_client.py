"""Shared OpenStates request routing (VOTEBOT-2, VOTEBOT-59).

VoteBot reads OpenStates data only from DDP's own replica (api-v3), never from the public
OpenStates API: that API is rate-limited (2 req/s) and never reflects DDP's own scraper
corrections. `DDP_OPENSTATES_API_ROOT` has no hardcoded default, so a stale target cannot hide behind a
code fallback; each environment sets it, and `check_openstates_config` stops the app at startup when it
(or the credential) is missing.

Two shapes of the same replica exist, chosen by `DDP_OPENSTATES_AUTH_HEADER`: ddp-api's proxy
(`/openstates/*`, `Authorization: Bearer`, the default) and api-v3 itself (`X-API-Key`, which refuses a
Bearer token with 403). Exactly one shape is ever sent.
"""

from votebot.config import Settings


def check_openstates_config(settings: Settings) -> None:
    """Raise at startup when the replica is not fully configured (there is no public-API fallback)."""
    missing = []
    if not settings.ddp_openstates_api_root:
        missing.append("DDP_OPENSTATES_API_ROOT")
    if not settings.ddp_openstates_bearer_token.get_secret_value():
        missing.append("DDP_OPENSTATES_BEARER_TOKEN")
    if missing:
        raise RuntimeError(
            f"{' and '.join(missing)} must be set: VoteBot reads OpenStates only from the DDP replica (api-v3), "
            "never from the public API."
        )


def openstates_base_url(settings: Settings) -> str:
    """Base URL for OpenStates requests: the DDP replica."""
    if not settings.ddp_openstates_api_root:
        raise RuntimeError("DDP_OPENSTATES_API_ROOT is not set -- set it in this environment's .env.")
    return settings.ddp_openstates_api_root.rstrip("/")


def openstates_headers(settings: Settings) -> dict[str, str]:
    """Auth header for OpenStates requests: `X-API-Key` for api-v3, `Authorization: Bearer` for ddp-api's proxy."""
    token = settings.ddp_openstates_bearer_token.get_secret_value()
    if settings.ddp_openstates_auth_header == "x-api-key":
        return {"x-api-key": token}
    return {"Authorization": f"Bearer {token}"}
