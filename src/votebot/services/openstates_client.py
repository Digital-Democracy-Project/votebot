"""Shared OpenStates request routing (VOTEBOT-2).

VoteBot's OpenStates call sites (bill_votes.py, agent.py, federal_legislator_cache.py)
historically hit the public v3.openstates.org API directly. That API is rate-limited
(2 req/s) and never reflects DDP's own scraper corrections. ddp-api already exposes a
proxy (`/openstates/*`) to DDP's self-hosted replica (production api-v3 now runs on
its own EC2/RDS instance, not the old Mac Studio setup -- ddp-api's own
OPENSTATES_SERVICE_URL needs to point at that instance for this to actually reach
current data; that's a ddp-api-side config fix, not a votebot one).

VoteBot goes through ddp-api's proxy (Bearer token) rather than calling api-v3
directly, the same shape ddp-broker-py's `DDPOpenStates` client uses in its own
"proxy" mode. `use_ddp_openstates_replica` gates a staged, per-environment rollout;
`ddp_openstates_api_root` has no hardcoded default (see config.py) so a stale target
can't hide behind a code fallback -- each environment must set
DDP_OPENSTATES_API_ROOT explicitly before enabling the flag.
"""

from votebot.config import Settings

PUBLIC_OPENSTATES_API_BASE = "https://v3.openstates.org"


def openstates_base_url(settings: Settings) -> str:
    """Base URL for OpenStates requests, routed per `use_ddp_openstates_replica`."""
    if settings.use_ddp_openstates_replica:
        if not settings.ddp_openstates_api_root:
            raise RuntimeError(
                "use_ddp_openstates_replica is enabled but DDP_OPENSTATES_API_ROOT "
                "is not set -- set it in this environment's .env before enabling the flag."
            )
        return settings.ddp_openstates_api_root.rstrip("/")
    return PUBLIC_OPENSTATES_API_BASE


def openstates_headers(settings: Settings) -> dict[str, str]:
    """Auth header for OpenStates requests, matching `openstates_base_url`'s routing.

    Exactly one shape is ever sent: the public API's and api-v3's `x-api-key`, or ddp-api's proxy's
    `Authorization: Bearer` (the replica flag with `DDP_OPENSTATES_AUTH_HEADER=bearer`, the default).
    """
    if settings.use_ddp_openstates_replica:
        token = settings.ddp_openstates_bearer_token.get_secret_value()
        if settings.ddp_openstates_auth_header == "x-api-key":
            return {"x-api-key": token}
        return {"Authorization": f"Bearer {token}"}
    return {"x-api-key": settings.openstates_api_key.get_secret_value()}
