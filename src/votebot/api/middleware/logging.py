"""Request logging middleware."""

import time
import uuid
from typing import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = structlog.get_logger()


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware for logging HTTP requests and responses.

    Logs request/response details with timing information and
    adds a request ID for tracing.
    """

    def __init__(self, app: ASGIApp):
        """Initialize the middleware."""
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process the request and log relevant information."""
        # Generate request ID
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        # Start timing
        start_time = time.perf_counter()

        # Extract request details
        method = request.method
        path = request.url.path
        client_ip = request.client.host if request.client else "unknown"

        # Log incoming request
        logger.info(
            "Request started",
            request_id=request_id,
            method=method,
            path=path,
            client_ip=client_ip,
        )

        # Process request
        try:
            response = await call_next(request)
        except Exception as exc:
            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000

            # Log error
            logger.error(
                "Request failed",
                request_id=request_id,
                method=method,
                path=path,
                duration_ms=round(duration_ms, 2),
                error=str(exc),
            )
            raise

        # Calculate duration
        duration_ms = (time.perf_counter() - start_time) * 1000

        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id

        # Log response
        log_level = "info" if response.status_code < 400 else "warning"
        getattr(logger, log_level)(
            "Request completed",
            request_id=request_id,
            method=method,
            path=path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )

        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Middleware for setting up request context for structured logging.

    Binds request-specific context variables to the logger.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Add request context to logger and process request."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4())[:8])

        # Bind context variables to logger
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )

        try:
            response = await call_next(request)
            return response
        finally:
            # Clear context after request
            structlog.contextvars.clear_contextvars()
