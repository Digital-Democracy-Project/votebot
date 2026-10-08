"""Tests for openstates_client.py's DDP-replica-vs-public routing (VOTEBOT-2).

The one incident this module exists to prevent a repeat of: ddp-broker-py's own
DDPOpenStates client sent the wrong auth shape (x-api-key/?apikey instead of
Authorization: Bearer) to ddp-api's proxy, which silently 401s rather than erroring
loudly -- that broke a production nightly job on 2026-07-13
(notes/incident-2026-07-13-nightly-401-ddp-replica-auth.md, ddp-broker-py). These
tests exist so the same mistake can't ship here unnoticed.
"""

import pytest
from pydantic import SecretStr

from votebot.config import Settings
from votebot.services.openstates_client import (
    PUBLIC_OPENSTATES_API_BASE,
    openstates_base_url,
    openstates_headers,
)


def _settings(**overrides) -> Settings:
    defaults = dict(
        environment="development",
        debug=True,
        api_key="test-api-key",
        openai_api_key="test-openai-key",
        pinecone_api_key="test-pinecone-key",
        pinecone_index_name="votebot-test",
        openstates_api_key=SecretStr("public-key"),
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # a developer's local .env must not change what these tests see


class TestOpenstatesBaseUrl:
    def test_defaults_to_public_api(self):
        """use_ddp_openstates_replica defaults to False -- the flag must be an
        explicit opt-in, not something a missing env var accidentally enables."""
        assert openstates_base_url(_settings()) == PUBLIC_OPENSTATES_API_BASE

    def test_replica_enabled_uses_configured_root(self):
        settings = _settings(
            use_ddp_openstates_replica=True,
            ddp_openstates_api_root="https://api.digitaldemocracyproject.org/openstates",
        )
        assert (
            openstates_base_url(settings)
            == "https://api.digitaldemocracyproject.org/openstates"
        )

    def test_replica_root_trailing_slash_is_stripped(self):
        """A trailing slash in DDP_OPENSTATES_API_ROOT must not produce a
        double-slash when a call site appends its own leading-slash path."""
        settings = _settings(
            use_ddp_openstates_replica=True,
            ddp_openstates_api_root="https://api.digitaldemocracyproject.org/openstates/",
        )
        assert (
            openstates_base_url(settings)
            == "https://api.digitaldemocracyproject.org/openstates"
        )

    def test_replica_enabled_without_root_raises_loudly(self):
        """No hardcoded fallback exists on purpose (see the module's own
        docstring) -- enabling the flag without configuring the root must fail
        loudly at request time, not silently fall back to some default host or
        proceed with an empty base URL."""
        settings = _settings(use_ddp_openstates_replica=True, ddp_openstates_api_root="")
        with pytest.raises(RuntimeError, match="DDP_OPENSTATES_API_ROOT"):
            openstates_base_url(settings)


class TestOpenstatesHeaders:
    def test_public_mode_sends_x_api_key_only(self):
        headers = openstates_headers(_settings())
        assert headers == {"x-api-key": "public-key"}

    def test_replica_mode_sends_bearer_only(self):
        """The exact case the 2026-07-13 incident got wrong: ddp-api's proxy
        wants Authorization: Bearer, not x-api-key -- and the two auth shapes
        must never both be present on the same request."""
        settings = _settings(
            use_ddp_openstates_replica=True,
            ddp_openstates_api_root="https://api.digitaldemocracyproject.org/openstates",
            ddp_openstates_bearer_token=SecretStr("replica-token"),
        )
        headers = openstates_headers(settings)
        assert headers == {"Authorization": "Bearer replica-token"}
        assert "x-api-key" not in headers

    def test_replica_mode_can_send_x_api_key_for_api_v3_itself(self):
        """api-v3 accepts only X-API-Key (a Bearer token gets 403): replica mode pointed straight at it.
        Still exactly one auth shape on the request, never both."""
        settings = _settings(
            use_ddp_openstates_replica=True,
            ddp_openstates_api_root="http://10.0.0.11:8002",
            ddp_openstates_bearer_token=SecretStr("api-v3-key"),
            ddp_openstates_auth_header="x-api-key",
        )
        headers = openstates_headers(settings)
        assert headers == {"x-api-key": "api-v3-key"}
        assert "Authorization" not in headers

    def test_the_default_replica_shape_is_still_bearer(self):
        settings = _settings(
            use_ddp_openstates_replica=True,
            ddp_openstates_api_root="https://example.test/openstates",
            ddp_openstates_bearer_token=SecretStr("t"),
        )
        assert settings.ddp_openstates_auth_header == "bearer"
        assert openstates_headers(settings) == {"Authorization": "Bearer t"}

    def test_the_header_choice_does_nothing_unless_the_replica_flag_is_on(self):
        settings = _settings(
            use_ddp_openstates_replica=False,
            ddp_openstates_bearer_token=SecretStr("t"),
            ddp_openstates_auth_header="x-api-key",
        )
        assert openstates_headers(settings) == {"x-api-key": "public-key"}  # the public key, never the replica token

    def test_an_unknown_header_choice_is_rejected_at_startup(self):
        with pytest.raises(ValueError):
            _settings(ddp_openstates_auth_header="basic")

    def test_public_mode_never_leaks_a_bearer_header(self):
        """Mirror of the test above: a bearer token configured but the flag off
        (e.g. mid-rollout, before enabling) must not leak into the public-API
        request either."""
        settings = _settings(
            use_ddp_openstates_replica=False,
            ddp_openstates_bearer_token=SecretStr("replica-token"),
        )
        headers = openstates_headers(settings)
        assert "Authorization" not in headers
        assert headers == {"x-api-key": "public-key"}
