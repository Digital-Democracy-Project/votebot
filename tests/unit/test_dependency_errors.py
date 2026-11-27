"""VOTEBOT-79: tell "a service VoteBot depends on is refusing or down" from "VoteBot failed".

The real exception classes of the libraries are used, so a library that renames or reshapes an error shows up here.
"""

from __future__ import annotations

import concurrent.futures

import httpx
import openai
import pytest
import redis.exceptions
from fastapi import HTTPException
from pinecone.errors.exceptions import ApiError, RateLimitError, ServiceError
from tenacity import RetryError

from votebot.utils.dependency_errors import is_dependency_unavailable

_REQUEST = httpx.Request("POST", "https://dependency.test")


def _retry_error(inner: BaseException) -> RetryError:
    attempt: concurrent.futures.Future = concurrent.futures.Future()
    attempt.set_exception(inner)
    return RetryError(attempt)


def _raised_from(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


UNAVAILABLE = {
    "pinecone monthly limit (429)": RateLimitError("egress limit", status_code=429),
    "pinecone service error (500)": ServiceError("down", status_code=503),
    "pinecone payment required (402)": ApiError("plan", status_code=402),
    "openai quota (429)": openai.RateLimitError("quota", response=httpx.Response(429, request=_REQUEST), body=None),
    "openai server error (500)": openai.InternalServerError("oops", response=httpx.Response(500, request=_REQUEST), body=None),
    "openai connection": openai.APIConnectionError(request=_REQUEST),
    "openai timeout": openai.APITimeoutError(request=_REQUEST),
    "replica connect error": httpx.ConnectError("refused"),
    "replica read timeout": httpx.ReadTimeout("slow"),
    "replica server error": httpx.HTTPStatusError("503", request=_REQUEST, response=httpx.Response(503, request=_REQUEST)),
    "redis connection": redis.exceptions.ConnectionError("refused"),
    "redis timeout": redis.exceptions.TimeoutError("slow"),
    "builtin connection": ConnectionResetError("reset"),
    "builtin timeout": TimeoutError("slow"),
    "an older pinecone client's `status`": type(
        "PineconeApiException", (Exception,), {"status": 429, "__module__": "pinecone.core.client.exceptions"}
    )(),
}

NOT_UNAVAILABLE = {
    "a bug": RuntimeError("boom"),
    "a bad value": ValueError("no such page type"),
    "a missing key": KeyError("ocd_bill_id"),
    "a bad request (400)": ApiError("malformed", status_code=400),
    "not found (404)": ApiError("no such index", status_code=404),
    "openai bad request (400)": openai.BadRequestError("bad", response=httpx.Response(400, request=_REQUEST), body=None),
    "replica not found (404)": httpx.HTTPStatusError("404", request=_REQUEST, response=httpx.Response(404, request=_REQUEST)),
    "a `status` that is not a number": type("Odd", (Exception,), {"status": "429"})(),
    # a status proves something about a dependency only on an error that comes from a dependency's library
    "fastapi's own HTTPException (500)": HTTPException(status_code=500),
    "an error of our own that carries a 503": type("OurError", (Exception,), {"status_code": 503})(),
    "an error of our own with a `status` of 429": type("OurError", (Exception,), {"status": 429})(),
    "an error of our own with a response of 502": type("OurError", (Exception,), {"response": httpx.Response(502, request=_REQUEST)})(),
}


@pytest.mark.parametrize("error", UNAVAILABLE.values(), ids=UNAVAILABLE.keys())
def test_a_dependency_limit_or_outage_is_recognised(error):
    assert is_dependency_unavailable(error) is True


@pytest.mark.parametrize("error", NOT_UNAVAILABLE.values(), ids=NOT_UNAVAILABLE.keys())
def test_an_ordinary_failure_is_not(error):
    assert is_dependency_unavailable(error) is False


class TestWrappedErrors:
    def test_tenacity_hides_the_last_attempt_and_it_is_looked_into(self):
        # `VectorStoreService.query` retries 3 times; the websocket sees only the RetryError (the 11-26 traceback)
        assert is_dependency_unavailable(_retry_error(RateLimitError("egress limit", status_code=429))) is True

    def test_a_retried_bug_is_still_a_bug(self):
        assert is_dependency_unavailable(_retry_error(ValueError("bad"))) is False

    def test_an_error_raised_from_a_dependency_error_counts(self):
        assert is_dependency_unavailable(_raised_from(RuntimeError("retrieval failed"), httpx.ConnectError("refused"))) is True

    def test_an_error_raised_while_handling_one_counts(self):
        try:
            try:
                raise openai.RateLimitError("quota", response=httpx.Response(429, request=_REQUEST), body=None)
            except openai.RateLimitError:
                raise RuntimeError("could not answer")
        except RuntimeError as outer:
            assert is_dependency_unavailable(outer) is True

    def test_a_chain_that_loops_does_not_hang(self):
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__cause__, b.__cause__ = b, a
        assert is_dependency_unavailable(a) is False

    def test_a_very_long_chain_stops_looking(self):
        error: BaseException = RateLimitError("deep", status_code=429)
        for i in range(20):
            error = _raised_from(RuntimeError(f"layer {i}"), error)
        assert is_dependency_unavailable(error) is False  # the limit is deeper than the search goes, by design
