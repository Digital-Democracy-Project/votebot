"""VOTEBOT-40: a title can arrive HTML-escaped ("America&#39;s"); the server hands on text.

The chat widget decodes it before showing it (VOTEBOT-1); the server decodes the titles it returns from /content/resolve and
the one it receives in every page context, so an older cached widget, the mobile app and REST clients get the same.
"""

from types import SimpleNamespace

import pytest

from votebot.api.routes import content, websocket as ws
from votebot.api.schemas.chat import PageContext
from votebot.utils.text import decode_entities

FOOD = "Making America&#39;s Food Safer Act"
FOOD_TEXT = "Making America's Food Safer Act"


class TestDecodeEntities:
    def test_numeric_and_the_five_named_entities_become_text(self):
        assert decode_entities(FOOD) == FOOD_TEXT
        assert decode_entities("It&#x27;s &amp; &quot;that&quot; &apos;s &#65; &#X41;") == "It's & \"that\" 's A A"
        assert decode_entities("&lt;b&gt;") == "<b>"

    def test_one_pass_so_a_doubly_escaped_title_loses_one_level(self):
        assert decode_entities("&amp;#39;") == "&#39;"

    @pytest.mark.parametrize("same", ["AT&T and Q&A", "Tax < 5 > 3", "&#0;", "&#1114112;", "&#99999999999999999999;", "&nbsp;", "&copy", "&#;", ""])
    def test_everything_else_is_left_alone(self, same):
        assert decode_entities(same) == same

    def test_none_stays_none(self):
        assert decode_entities(None) is None


class TestPageContext:
    def test_the_title_is_decoded_wherever_a_page_context_is_built(self):
        assert PageContext(type="bill", title=FOOD).title == FOOD_TEXT
        assert PageContext.model_validate({"type": "bill", "title": FOOD}).title == FOOD_TEXT

    def test_no_title_and_a_plain_title_are_unchanged(self):
        assert PageContext(type="general").title is None
        assert PageContext(type="bill", title="HB 1").title == "HB 1"

    def test_the_websocket_payload_builder_decodes_it_too(self):
        assert ws._page_context_from_payload({"type": "bill", "id": "S 4974", "title": FOOD}).title == FOOD_TEXT


class FakeSecret:
    def get_secret_value(self):
        return "key"


def _settings(**extra):
    return SimpleNamespace(
        webflow_bills_collection_id="bills",
        webflow_legislators_collection_id="legislators",
        webflow_organizations_collection_id="orgs",
        webflow_votebot_api_key=FakeSecret(),
        ddp_broker_api_root="http://broker.test",
        **extra,
    )


class TestContentResolve:
    @pytest.mark.parametrize(
        "path, expected_type",
        [("/bills/a-bill", "bill"), ("/legislators/a-person", "legislator"), ("/member-organizations/an-org", "organization")],
    )
    async def test_a_webflow_name_is_returned_decoded(self, monkeypatch, path, expected_type):
        async def item(**kwargs):
            return {"id": "w1", "fieldData": {"name": FOOD, "slug": "x"}}

        monkeypatch.setattr(content, "get_settings", lambda: _settings())
        monkeypatch.setattr(content, "fetch_webflow_item_by_slug", item)
        result = await content.resolve_content(url=f"https://digitaldemocracyproject.org{path}")
        assert result["type"] == expected_type and result["title"] == FOOD_TEXT

    async def test_the_broker_scorecard_title_is_returned_decoded(self, monkeypatch):
        monkeypatch.setattr(content, "get_settings", lambda: _settings())

        async def broker_get(client, url, params=None):
            if "scorecard" in url:
                return {"bill": {"jurisdictionIso2": "US", "session": {"code": "119"}, "govId": "S 4974", "title": FOOD}}
            return {"bill_openstates_id": "d57834a5-4f83-4541-be6f-fe073e767245"}

        monkeypatch.setattr(content, "_broker_get", broker_get)
        result = await content.resolve_content(url="https://digitaldemocracyproject.org/bills/580")
        assert result["title"] == FOOD_TEXT and result["id"] == "S 4974"

    async def test_a_title_without_entities_is_returned_as_it_was(self, monkeypatch):
        async def item(**kwargs):
            return {"id": "w1", "fieldData": {"name": "AT&T Act", "slug": "x"}}

        monkeypatch.setattr(content, "get_settings", lambda: _settings())
        monkeypatch.setattr(content, "fetch_webflow_item_by_slug", item)
        assert (await content.resolve_content(url="https://digitaldemocracyproject.org/bills/a"))["title"] == "AT&T Act"
