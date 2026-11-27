"""Tell "a service VoteBot depends on is refusing or down" from "VoteBot failed" (VOTEBOT-79).

On 2026-11-26 Pinecone refused every read (its monthly read limit, VOTEBOT-77) and every chat message got the same
"Sorry, I encountered an error processing your message. Please try again." A visitor cannot fix that and retrying does
not help, so the websocket sends a different code for it (`service_unavailable`) and the widget words it plainly.

A failure counts only when the evidence is in the error itself: an HTTP status that means a limit, a quota or a service
that is down (402, 429, 5xx) **on an error that comes from a dependency's own library** (a status on an error of our own,
or of FastAPI, proves nothing about a dependency), or a connection or timeout error. Everything else stays a
`processing_error`. The builtin `ConnectionError` and `TimeoutError` count because nothing in VoteBot raises them
itself (its own `asyncio.wait_for` timeouts are caught where they are made); what raises them is a socket.
"""

from __future__ import annotations

import httpx
import openai
import redis.exceptions
from tenacity import RetryError

_MAX_DEPTH = 8  # how far down a chain of "raised from" errors to look

# The libraries whose errors say something about a dependency (the clients of Pinecone, OpenAI, Redis and the HTTP calls).
_DEPENDENCY_LIBRARIES = frozenset({"openai", "pinecone", "httpx", "httpcore", "urllib3", "redis"})

# Errors that mean the other end could not be reached or did not answer in time.
_UNREACHABLE = (
    openai.APIConnectionError,  # includes APITimeoutError
    httpx.TransportError,  # connect, read and write failures and timeouts, including the OpenStates replica
    redis.exceptions.ConnectionError,
    redis.exceptions.TimeoutError,
    ConnectionError,
    TimeoutError,
)


def _from_a_dependency_library(error: BaseException) -> bool:
    return any(base.__module__.partition(".")[0] in _DEPENDENCY_LIBRARIES for base in type(error).__mro__)


def _status(error: BaseException) -> int | None:
    """The HTTP status an error from a dependency's library carries, whichever way the library names it
    (`status_code` for OpenAI and Pinecone 10, `status` for older Pinecone, `response.status_code` for httpx)."""
    if not _from_a_dependency_library(error):
        return None
    for holder, name in ((error, "status_code"), (error, "status"), (getattr(error, "response", None), "status_code")):
        value = getattr(holder, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _chain(error: BaseException):
    """The error and what it was raised from, looking inside tenacity's `RetryError` (which hides the last attempt)."""
    seen: set[int] = set()
    for _ in range(_MAX_DEPTH):
        if error is None or id(error) in seen:
            return
        seen.add(id(error))
        yield error
        if isinstance(error, RetryError):
            inner = error.last_attempt.exception() if error.last_attempt is not None else None
            error = inner or error.__cause__ or error.__context__
        else:
            error = error.__cause__ or error.__context__


def is_dependency_unavailable(error: BaseException) -> bool:
    """True when `error`, or the error it wraps, says a service VoteBot depends on is refusing (limit, quota) or down."""
    for found in _chain(error):
        status = _status(found)
        if status is not None and (status in (402, 429) or status >= 500):
            return True
        if isinstance(found, _UNREACHABLE):
            return True
    return False
