"""Tests for openstates_client.py: OpenStates comes only from DDP's replica (VOTEBOT-2, VOTEBOT-59).

The one incident this module exists to prevent a repeat of: ddp-broker-py's own
DDPOpenStates client sent the wrong auth shape (x-api-key/?apikey instead of
Authorization: Bearer) to ddp-api's proxy, which silently 401s rather than erroring
loudly -- that broke a production nightly job on 2026-07-13
(notes/incident-2026-07-13-nightly-401-ddp-replica-auth.md, ddp-broker-py). These
tests exist so the same mistake can't ship here unnoticed.
"""

from pathlib import Path

import pytest
from pydantic import SecretStr

from votebot.config import Settings
from votebot.services.openstates_client import (
    check_openstates_config,
    openstates_base_url,
    openstates_headers,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "votebot"


def _settings(**overrides) -> Settings:
    defaults = dict(
        environment="development",
        debug=True,
        api_key="test-api-key",
        openai_api_key="test-openai-key",
        pinecone_api_key="test-pinecone-key",
        pinecone_index_name="votebot-test",
        ddp_openstates_api_root="https://api.digitaldemocracyproject.org/openstates",
        ddp_openstates_bearer_token=SecretStr("replica-token"),
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # a developer's local .env must not change what these tests see


class TestOpenstatesBaseUrl:
    def test_uses_the_configured_root(self):
        assert openstates_base_url(_settings()) == "https://api.digitaldemocracyproject.org/openstates"

    def test_root_trailing_slash_is_stripped(self):
        """A trailing slash in DDP_OPENSTATES_API_ROOT must not produce a
        double-slash when a call site appends its own leading-slash path."""
        settings = _settings(ddp_openstates_api_root="https://api.digitaldemocracyproject.org/openstates/")
        assert openstates_base_url(settings) == "https://api.digitaldemocracyproject.org/openstates"

    def test_without_a_root_it_raises_loudly(self):
        """No hardcoded fallback exists on purpose, and no public-API one either: a missing root must fail,
        not proceed with an empty base URL or some default host."""
        with pytest.raises(RuntimeError, match="DDP_OPENSTATES_API_ROOT"):
            openstates_base_url(_settings(ddp_openstates_api_root=""))


class TestNoPublicOpenstatesApi:
    def test_there_is_no_setting_that_selects_the_public_api(self):
        fields = Settings.model_fields
        assert "use_ddp_openstates_replica" not in fields
        assert "openstates_api_key" not in fields

    def test_an_old_environment_cannot_switch_it_back_on(self, monkeypatch):
        monkeypatch.setenv("USE_DDP_OPENSTATES_REPLICA", "false")
        monkeypatch.setenv("OPENSTATES_API_KEY", "public-key")
        settings = _settings()
        assert openstates_base_url(settings) == "https://api.digitaldemocracyproject.org/openstates"
        assert openstates_headers(settings) == {"Authorization": "Bearer replica-token"}

    def test_the_public_host_appears_nowhere_in_the_source(self):
        hits = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "v3.openstates.org" in p.read_text()]
        assert hits == []

    @pytest.mark.parametrize("missing", ["ddp_openstates_api_root", "ddp_openstates_bearer_token"])
    def test_startup_refuses_a_missing_root_or_credential(self, missing):
        blank = "" if missing == "ddp_openstates_api_root" else SecretStr("")
        with pytest.raises(RuntimeError, match=missing.upper()):
            check_openstates_config(_settings(**{missing: blank}))

    def test_startup_accepts_a_complete_configuration(self):
        check_openstates_config(_settings())


class TestOpenstatesHeaders:
    def test_bearer_only_for_ddp_apis_proxy(self):
        """The exact case the 2026-07-13 incident got wrong: ddp-api's proxy
        wants Authorization: Bearer, not x-api-key -- and the two auth shapes
        must never both be present on the same request."""
        headers = openstates_headers(_settings())
        assert headers == {"Authorization": "Bearer replica-token"}
        assert "x-api-key" not in headers

    def test_x_api_key_for_api_v3_itself(self):
        """api-v3 accepts only X-API-Key (a Bearer token gets 403). Still exactly one auth shape, never both."""
        settings = _settings(ddp_openstates_api_root="http://10.0.0.11:8002", ddp_openstates_auth_header="x-api-key")
        headers = openstates_headers(settings)
        assert headers == {"x-api-key": "replica-token"}
        assert "Authorization" not in headers

    def test_the_default_shape_is_bearer(self):
        assert _settings().ddp_openstates_auth_header == "bearer"

    def test_an_unknown_header_choice_is_rejected_at_startup(self):
        with pytest.raises(ValueError):
            _settings(ddp_openstates_auth_header="basic")
