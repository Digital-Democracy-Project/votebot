"""VOTEBOT-51: `/health/ready` does not ask the model for a completion; a bad key or an unreachable OpenAI still fails it."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from votebot.api.routes import health
from votebot.config import Settings
from votebot.services.llm import LLMService


def _service(retrieve: AsyncMock) -> tuple[LLMService, SimpleNamespace]:
    service = LLMService(Settings(openai_api_key="k", openai_model="gpt-test", _env_file=None))

    def forbidden(name):
        async def call(*a, **k):
            raise AssertionError(f"health check generated something through {name}")

        return call

    client = SimpleNamespace(
        models=SimpleNamespace(retrieve=retrieve),
        responses=SimpleNamespace(create=forbidden("responses.create")),
        chat=SimpleNamespace(completions=SimpleNamespace(create=forbidden("chat.completions.create"))),
    )
    service.client = client
    return service, client


class TestTheCheckGeneratesNothing:
    async def test_it_retrieves_the_model_and_makes_no_completion_call(self):
        retrieve = AsyncMock(return_value=SimpleNamespace(id="gpt-test"))
        service, _client = _service(retrieve)
        assert await service.health_check() is True
        retrieve.assert_awaited_once_with("gpt-test")  # the configured model, nothing else was called

    async def test_repeated_checks_cost_nothing_but_the_lookup(self):
        retrieve = AsyncMock(return_value=SimpleNamespace(id="gpt-test"))
        service, _client = _service(retrieve)
        for _ in range(5):
            await service.health_check()
        assert retrieve.await_count == 5  # a cheap request each, no tokens (a completion call would have raised)


class TestItStillFails:
    @pytest.mark.parametrize("error", [RuntimeError("401 bad key"), ConnectionError("unreachable"), TimeoutError("slow")])
    async def test_a_bad_key_or_an_unreachable_openai_raises(self, error):
        service, _client = _service(AsyncMock(side_effect=error))
        with pytest.raises(type(error)):
            await service.health_check()

    async def test_a_model_the_project_cannot_see_fails_it_too(self):
        service, _client = _service(AsyncMock(side_effect=LookupError("404 model not found")))
        with pytest.raises(LookupError):
            await service.health_check()


class TestReadinessReportsIt:
    async def _readiness(self, llm_health: AsyncMock):
        ok = AsyncMock(return_value=True)
        with patch("votebot.services.llm.LLMService.health_check", llm_health), patch(
            "votebot.services.vector_store.VectorStoreService.__init__", lambda self, *a, **k: None
        ), patch("votebot.services.vector_store.VectorStoreService.health_check", ok), patch.object(
            health, "openstates_base_url", return_value="http://api-v3.test"
        ), patch.object(health.httpx, "AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None))
            return await health.readiness_check(Settings(openai_api_key="k", _env_file=None))

    async def test_a_healthy_openai_is_healthy(self):
        result = await self._readiness(AsyncMock(return_value=True))
        assert result.dependencies["openai"] == "healthy"

    async def test_a_failing_check_marks_openai_unhealthy_and_the_service_degraded(self):
        result = await self._readiness(AsyncMock(side_effect=RuntimeError("401 bad key")))
        assert result.dependencies["openai"] == "unhealthy"
        assert result.status == "degraded"
