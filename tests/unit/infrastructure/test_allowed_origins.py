"""VOTEBOT-83: the production allowed-origin list names the dev and the stage sites, and the real CORS middleware obeys it.

On 2026-11-27 the stage site had no chat, and once it has one, its browser calls to VoteBot (`/votebot/v1/features`,
`/votebot/v1/content/resolve`) are blocked unless VoteBot answers them with `access-control-allow-origin`. The list is one
line in `infrastructure/docker/prod.env.defaults`, changed by a pull request. These tests read that shipped line and run
it through the app's own middleware, so the value and its effect cannot drift apart. Nothing touches the network.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from votebot import main
from votebot.config import Settings

REPO = Path(__file__).resolve().parents[3]
DEV = "https://dev.digitaldemocracyproject.org"
STAGE = "https://stage.digitaldemocracyproject.org"


def _shipped_line() -> str:
    lines = [
        line for line in (REPO / "infrastructure/docker/prod.env.defaults").read_text().splitlines()
        if line.startswith("ALLOWED_ORIGINS=")
    ]
    assert len(lines) == 1, "the defaults file must carry exactly one ALLOWED_ORIGINS line"
    return lines[0].split("=", 1)[1]


def _shipped() -> list[str]:
    return json.loads(_shipped_line())


@pytest.fixture
def client():
    # the app as it would be built with the shipped list (no lifespan: nothing starts, nothing is called)
    with patch.object(main, "settings", Settings(_env_file=None, allowed_origins=_shipped())):
        yield TestClient(main.create_app())


def _acao(client, origin):
    return client.get("/votebot/v1/health/live", headers={"Origin": origin}).headers.get("access-control-allow-origin")


class TestTheShippedList:
    def test_it_is_a_json_list_of_bare_https_origins(self):
        origins = _shipped()
        assert isinstance(origins, list) and origins
        for origin in origins:
            assert origin.startswith("https://") and origin == origin.rstrip("/"), origin
            assert "/" not in origin[len("https://"):] and "*" not in origin, origin  # an origin, not a path or a wildcard

    def test_it_names_the_dev_site_and_the_stage_site(self):
        assert DEV in _shipped() and STAGE in _shipped()

    def test_settings_accepts_the_line_as_the_container_will_read_it(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_ORIGINS", _shipped_line())
        assert Settings(_env_file=None).allowed_origins == _shipped()


class TestTheMiddlewareObeysIt:
    @pytest.mark.parametrize("origin", [DEV, STAGE])
    def test_a_listed_site_gets_the_header(self, client, origin):
        assert _acao(client, origin) == origin

    @pytest.mark.parametrize(
        "origin",
        [
            "https://example.invalid",
            STAGE + ".evil.example",  # starts like stage, is not
            "http://stage.digitaldemocracyproject.org",  # the same host over http
            "https://stage.digitaldemocracyproject.org:8443",  # the same host on another port
            "https://digitaldemocracyproject.org",  # the production site is a separate cutover item (NEXT-36)
            "null",
        ],
    )
    def test_an_unlisted_origin_gets_no_header(self, client, origin):
        assert _acao(client, origin) is None

    def test_a_preflight_from_stage_is_allowed(self, client):
        response = client.options(
            "/votebot/v1/features",
            headers={"Origin": STAGE, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "content-type"},
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == STAGE
