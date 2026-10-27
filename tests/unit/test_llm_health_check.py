"""VOTEBOT-51: `/health/ready` makes at most one real model call a minute, and still fails when chat could not be served.

Readiness means OpenAI can serve a completion, so the check keeps its minimal completion; what is bounded is how often
it runs (a result is kept, checks that arrive together share one call).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from votebot.api.routes import health
from votebot.config import Settings
from votebot.services import llm as llm_module
from votebot.services.llm import HEALTH_FAIL_TTL, HEALTH_OK_TTL, LLMService


def _service(create: AsyncMock) -> LLMService:
    service = LLMService(Settings(openai_api_key="k", openai_model="gpt-test", _env_file=None))
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return service


def _age(service: LLMService, seconds: float) -> None:
    when, outcome = service._health
    service._health = (when - seconds, outcome)


class TestOneCallAWindow:
    async def test_the_check_is_still_a_minimal_completion(self):
        create = AsyncMock()
        assert await _service(create).health_check() is True
        create.assert_awaited_once_with(model="gpt-test", input="ping", max_output_tokens=16)

    async def test_repeated_checks_inside_the_window_make_one_call(self):
        create = AsyncMock()
        service = _service(create)
        for _ in range(20):
            assert await service.health_check() is True
        assert create.await_count == 1

    async def test_after_the_window_the_next_check_calls_again(self):
        create = AsyncMock()
        service = _service(create)
        await service.health_check()
        _age(service, HEALTH_OK_TTL + 1)
        await service.health_check()
        assert create.await_count == 2

    async def test_checks_that_arrive_together_share_one_call(self):
        async def slow(**kwargs):
            await asyncio.sleep(0.05)

        create = AsyncMock(side_effect=slow)
        service = _service(create)
        assert await asyncio.gather(*[service.health_check() for _ in range(10)]) == [True] * 10
        assert create.await_count == 1


class TestItStillFailsWhenChatCouldNotBeServed:
    @pytest.mark.parametrize(
        "error",
        [RuntimeError("401 bad key"), ConnectionError("unreachable"), TimeoutError("slow"), PermissionError("no generation rights"),
         RuntimeError("429 insufficient_quota")],
    )
    async def test_any_failure_raises(self, error):
        with pytest.raises(type(error)):
            await _service(AsyncMock(side_effect=error)).health_check()

    async def test_a_failure_is_remembered_briefly_not_for_a_minute(self):
        create = AsyncMock(side_effect=RuntimeError("429 insufficient_quota"))
        service = _service(create)
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await service.health_check()
        assert create.await_count == 1  # a poll storm during an outage does not call OpenAI each time
        _age(service, HEALTH_FAIL_TTL + 1)
        with pytest.raises(RuntimeError):
            await service.health_check()
        assert create.await_count == 2  # but it looks again soon

    async def test_a_recovery_is_seen_after_the_short_window(self):
        create = AsyncMock(side_effect=[RuntimeError("down"), None])
        service = _service(create)
        with pytest.raises(RuntimeError):
            await service.health_check()
        _age(service, HEALTH_FAIL_TTL + 1)
        assert await service.health_check() is True

    async def test_a_healthy_result_does_not_hide_a_later_failure_forever(self):
        create = AsyncMock(side_effect=[None, RuntimeError("quota gone")])
        service = _service(create)
        assert await service.health_check() is True
        _age(service, HEALTH_OK_TTL + 1)
        with pytest.raises(RuntimeError):
            await service.health_check()


class TestReadinessUsesTheSharedService:
    async def _readiness(self, llm: LLMService):
        ok = AsyncMock(return_value=True)
        shared = SimpleNamespace(llm=llm)
        with patch("votebot.core.agent._services_for", return_value=shared), patch(
            "votebot.services.vector_store.VectorStoreService.__init__", lambda self, *a, **k: None
        ), patch("votebot.services.vector_store.VectorStoreService.health_check", ok), patch.object(
            health, "openstates_base_url", return_value="http://api-v3.test"
        ), patch.object(health.httpx, "AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None))
            return await health.readiness_check(Settings(openai_api_key="k", _env_file=None))

    async def test_polling_readiness_makes_one_completion_call(self):
        create = AsyncMock()
        llm = _service(create)
        for _ in range(10):
            result = await self._readiness(llm)
        assert result.dependencies["openai"] == "healthy" and create.await_count == 1

    async def test_a_failing_check_marks_openai_unhealthy_and_the_service_degraded(self):
        result = await self._readiness(_service(AsyncMock(side_effect=RuntimeError("429 insufficient_quota"))))
        assert result.dependencies["openai"] == "unhealthy"
        assert result.status == "degraded"

    def test_the_module_exposes_the_windows(self):
        assert llm_module.HEALTH_OK_TTL == 60.0 and llm_module.HEALTH_FAIL_TTL == 10.0
