"""Bearer token authentication middleware."""

import secrets
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from votebot.config import Settings, get_settings

logger = structlog.get_logger()

# Bearer token scheme
bearer_scheme = HTTPBearer(auto_error=False)


async def bearer_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
    settings: Settings = Depends(get_settings),
) -> str:
    """
    Validate Bearer token from Authorization header.

    Args:
        credentials: Bearer token from Authorization header
        settings: Application settings

    Returns:
        The validated token

    Raises:
        HTTPException: If token is missing or invalid
    """
    if credentials is None:
        logger.warning("Missing Bearer token in request")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token. Include Authorization: Bearer <token> header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Use constant-time comparison to prevent timing attacks
    expected_token = settings.api_key.get_secret_value()
    if not secrets.compare_digest(token, expected_token):
        logger.warning("Invalid Bearer token attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


# Alias for backward compatibility
api_key_auth = bearer_auth


class BearerTokenValidator:
    """
    Callable class for Bearer token validation with support for multiple tokens.

    Useful for scenarios where different clients have different tokens.
    """

    def __init__(self, valid_tokens: list[str] | None = None):
        """
        Initialize the validator.

        Args:
            valid_tokens: List of valid tokens. If None, uses settings.
        """
        self.valid_tokens = valid_tokens

    async def __call__(
        self,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
        settings: Settings = Depends(get_settings),
    ) -> str:
        """Validate the Bearer token."""
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = credentials.credentials

        # Use configured token if no specific tokens provided
        tokens_to_check = self.valid_tokens or [settings.api_key.get_secret_value()]

        for valid_token in tokens_to_check:
            if secrets.compare_digest(token, valid_token):
                return token

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_optional_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
) -> str | None:
    """
    Get Bearer token without requiring authentication.

    Useful for endpoints that have optional authentication.
    """
    if credentials is None:
        return None
    return credentials.credentials
