"""API key authentication middleware."""

import secrets
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from votebot.config import Settings, get_settings

logger = structlog.get_logger()

# API key header scheme
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def api_key_auth(
    api_key: Annotated[str | None, Security(api_key_header)],
    settings: Settings = Depends(get_settings),
) -> str:
    """
    Validate API key from request header.

    Args:
        api_key: API key from X-API-Key header
        settings: Application settings

    Returns:
        The validated API key

    Raises:
        HTTPException: If API key is missing or invalid
    """
    if api_key is None:
        logger.warning("Missing API key in request")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Include X-API-Key header.",
        )

    # Use constant-time comparison to prevent timing attacks
    expected_key = settings.api_key.get_secret_value()
    if not secrets.compare_digest(api_key, expected_key):
        logger.warning("Invalid API key attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return api_key


class APIKeyValidator:
    """
    Callable class for API key validation with support for multiple keys.

    Useful for scenarios where different clients have different API keys.
    """

    def __init__(self, valid_keys: list[str] | None = None):
        """
        Initialize the validator.

        Args:
            valid_keys: List of valid API keys. If None, uses settings.
        """
        self.valid_keys = valid_keys

    async def __call__(
        self,
        api_key: Annotated[str | None, Security(api_key_header)],
        settings: Settings = Depends(get_settings),
    ) -> str:
        """Validate the API key."""
        if api_key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing API key",
            )

        # Use configured key if no specific keys provided
        keys_to_check = self.valid_keys or [settings.api_key.get_secret_value()]

        for valid_key in keys_to_check:
            if secrets.compare_digest(api_key, valid_key):
                return api_key

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


def get_optional_api_key(
    api_key: Annotated[str | None, Security(api_key_header)],
) -> str | None:
    """
    Get API key without requiring authentication.

    Useful for endpoints that have optional authentication.
    """
    return api_key
