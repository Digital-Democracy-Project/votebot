"""Structured logging configuration using structlog."""

import logging
import sys
from typing import Any

import structlog


def setup_logging(log_level: str = "INFO") -> None:
    """
    Configure structured logging for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    # Convert string to logging level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )

    # Shared processors for all loggers
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    # Configure structlog
    if sys.stderr.isatty():
        # Development: colored console output
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]
    else:
        # Production: JSON output
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Quiet noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """
    Get a logger instance.

    Args:
        name: Logger name (defaults to calling module)

    Returns:
        Bound logger instance
    """
    return structlog.get_logger(name)


class RequestLogger:
    """Context manager for logging request lifecycle."""

    def __init__(self, request_id: str, **context: Any):
        """
        Initialize the request logger.

        Args:
            request_id: Unique request identifier
            **context: Additional context to bind
        """
        self.request_id = request_id
        self.context = context
        self.logger = structlog.get_logger()

    def __enter__(self) -> structlog.stdlib.BoundLogger:
        """Enter the context and bind request context."""
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=self.request_id,
            **self.context,
        )
        return self.logger

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the context and clear context vars."""
        if exc_val:
            self.logger.exception(
                "Request failed",
                error=str(exc_val),
                error_type=exc_type.__name__ if exc_type else None,
            )
        structlog.contextvars.clear_contextvars()


def log_performance(logger: Any, operation: str, duration_ms: float, **extra: Any) -> None:
    """
    Log a performance metric.

    Args:
        logger: Logger instance
        operation: Name of the operation
        duration_ms: Duration in milliseconds
        **extra: Additional context
    """
    logger.info(
        "Performance metric",
        operation=operation,
        duration_ms=round(duration_ms, 2),
        **extra,
    )
