"""VOTEBOT-8 / VOTEBOT-13: /content/resolve turns a ddp-next bill URL into a page context with ocd_bill_id.

ddp-broker-py has no bill-detail endpoint, so resolution uses its public `scorecard` and
`resolve` endpoints. The Webflow path must keep working untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from votebot.api.routes import content

BILL = "a3f7c0d1-1111-4222-8333-444455556666"
BROKER = "https://broker.test"


def _settings(broker: str = BROKER):
    return SimpleNamespace(
        ddp_broker_api_root=broker,
        webflow_bills_collection_id="bills-collection",
        webflow_votebot_api_key=SecretStr("k"),
    )


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(content, "get_settings", lambda: _settings())


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", BROKER)
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))


class TestMatchDdpNextBill:
    def test_numeric_single_segment_is_a_broker_id(self):
        assert content._match_ddp_next_bill("/bills/123") == {"broker_id": 123}
        assert content._match_ddp_next_bill("/bills/123/") == {"broker_id": 123}

    def test_three_segments_are_the_natural_key_and_are_decoded(self):
        assert content._match_ddp_next_bill("/bills/fl/2026/HB%20123") == {
            "jurisdiction": "fl",
            "session": "2026",
            "gov_id": "HB 123",
        }

    def test_a_webflow_slug_is_not_a_ddp_next_url(self):
        assert content._match_ddp_next_bill("/bills/one-big-beautiful-bill-act-hr1-2025") is None
        assert content._match_ddp_next_bill("/legislators/jane-doe") is None


class TestResolveByNaturalKey:
    async def test_returns_the_page_context_with_a_bare_ocd_bill_id(self, monkeypatch):
        broker = AsyncMock(return_value={"bill_openstates_id": BILL})
        monkeypatch.setattr(content, "_broker_get", broker)

        result = await content.resolve_content(url="https://digitaldemocracyproject.org/bills/fl/2026/HB%20123")

        assert result == {
            "type": "bill",
            "id": "HB 123",
            "ocd_bill_id": BILL,
            "gov_id": "HB 123",
            "jurisdiction": "FL",
            "session": "2026",
            "url": "https://digitaldemocracyproject.org/bills/fl/2026/HB%20123",
            "ddp_url": "https://digitaldemocracyproject.org/bills/fl/2026/HB%20123",
        }
        assert broker.call_args.args[1] == f"{BROKER}/api/bills/resolve/"
        assert broker.call_args.kwargs["params"] == {
            "jurisdiction": "fl",
            "session": "2026",
            "gov_id": "HB 123",
        }


class TestResolveByBrokerId:
    async def test_looks_up_the_scorecard_then_resolves_its_natural_key(self, monkeypatch):
        scorecard = {
            "bill": {
                "id": "123",
                "title": "Online Protections for Minors",
                "jurisdictionIso2": "FL",
                "govId": "HB 1",
                "session": {"code": "2026"},
            }
        }
        broker = AsyncMock(side_effect=[scorecard, {"bill_openstates_id": BILL}])
        monkeypatch.setattr(content, "_broker_get", broker)

        result = await content.resolve_content(url="https://digitaldemocracyproject.org/bills/123")

        assert broker.call_args_list[0].args[1] == f"{BROKER}/api/bills/123/scorecard/"
        assert broker.call_args_list[1].args[1] == f"{BROKER}/api/bills/resolve/"
        assert result["ocd_bill_id"] == BILL
        assert (result["gov_id"], result["jurisdiction"], result["session"]) == ("HB 1", "FL", "2026")
        assert result["title"] == "Online Protections for Minors"

    async def test_a_scorecard_missing_identity_fields_is_a_bad_gateway(self, monkeypatch):
        monkeypatch.setattr(content, "_broker_get", AsyncMock(return_value={"bill": {"govId": "HB 1"}}))
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url="https://digitaldemocracyproject.org/bills/123")
        assert err.value.status_code == 502


class TestBrokerFailures:
    URL = "https://digitaldemocracyproject.org/bills/fl/2026/HB%20123"

    async def test_unknown_bill_is_404(self, monkeypatch):
        monkeypatch.setattr(content, "_broker_get", AsyncMock(side_effect=_status_error(404)))
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url=self.URL)
        assert err.value.status_code == 404

    async def test_broker_error_is_502(self, monkeypatch):
        monkeypatch.setattr(content, "_broker_get", AsyncMock(side_effect=_status_error(500)))
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url=self.URL)
        assert err.value.status_code == 502

    async def test_unreachable_broker_is_502(self, monkeypatch):
        monkeypatch.setattr(content, "_broker_get", AsyncMock(side_effect=httpx.ConnectError("down")))
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url=self.URL)
        assert err.value.status_code == 502

    @pytest.mark.parametrize(
        "answer",
        [{}, {"bill_openstates_id": None}, {"bill_openstates_id": ""}, {"bill_openstates_id": "  "},
         {"bill_openstates_id": "ocd-bill/not-a-bare-uuid"}],
    )
    async def test_a_200_without_a_valid_bare_uuid_is_a_bad_gateway_not_a_miss(self, monkeypatch, answer):
        # A real miss is the broker's own 404; a 200 that cannot be used breaks its contract.
        monkeypatch.setattr(content, "_broker_get", AsyncMock(return_value=answer))
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url=self.URL)
        assert err.value.status_code == 502

    async def test_the_id_is_normalised_to_the_lower_case_form_the_vectors_use(self, monkeypatch):
        monkeypatch.setattr(content, "_broker_get", AsyncMock(return_value={"bill_openstates_id": BILL.upper()}))
        result = await content.resolve_content(url=self.URL)
        assert result["ocd_bill_id"] == BILL

    async def test_unconfigured_broker_is_503_not_a_silent_fallback(self, monkeypatch):
        monkeypatch.setattr(content, "get_settings", lambda: _settings(broker=""))
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url=self.URL)
        assert err.value.status_code == 503


class TestWebflowPathIsUnchanged:
    async def test_a_webflow_slug_still_resolves_through_the_cms(self, monkeypatch):
        broker = AsyncMock()
        monkeypatch.setattr(content, "_broker_get", broker)
        monkeypatch.setattr(
            content,
            "fetch_webflow_item_by_slug",
            AsyncMock(
                return_value={
                    "id": "wf123",
                    "fieldData": {
                        "name": "One Big Beautiful Bill Act",
                        "bill-prefix": "HR",
                        "bill-number": "1",
                        "jurisdiction": "US",
                        "session-code": "119",
                    },
                }
            ),
        )

        result = await content.resolve_content(
            url="https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025"
        )

        assert result["webflow_id"] == "wf123" and result["id"] == "HR 1" and result["session"] == "119"
        assert "ocd_bill_id" not in result
        broker.assert_not_called()

    async def test_the_webflow_path_does_not_need_the_broker_configured(self, monkeypatch):
        monkeypatch.setattr(content, "get_settings", lambda: _settings(broker=""))
        monkeypatch.setattr(
            content,
            "fetch_webflow_item_by_slug",
            AsyncMock(return_value={"id": "wf1", "fieldData": {"name": "A bill"}}),
        )
        result = await content.resolve_content(url="https://digitaldemocracyproject.org/bills/some-bill-2026")
        assert result["webflow_id"] == "wf1"


class TestExploreUrls:
    """VOTEBOT-13: the new site's bill page, /explore/{JURISDICTION}/{SESSION}/{IDENTIFIER}."""

    def test_the_explore_path_is_the_natural_key_with_a_decoded_space(self):
        assert content._match_ddp_next_bill("/explore/FL/2026/HB%20219") == {
            "jurisdiction": "FL",
            "session": "2026",
            "gov_id": "HB 219",
        }
        assert content._match_ddp_next_bill("/explore/FL/2026/HB%20219/") is not None

    @pytest.mark.parametrize(
        "path",
        ["/explore", "/explore/FL", "/explore/FL/2026", "/explore/Florida/2026/HB%201", "/explore/FL/2026/HB%201/votes"],
    )
    def test_other_explore_paths_are_not_bills(self, path):
        assert content._match_ddp_next_bill(path) is None

    async def test_resolves_through_the_broker_to_the_same_payload_as_the_bills_form(self, monkeypatch):
        broker = AsyncMock(return_value={"bill_openstates_id": BILL})
        monkeypatch.setattr(content, "_broker_get", broker)
        url = "https://digitaldemocracyproject.org/explore/FL/2026/HB%20219"

        result = await content.resolve_content(url=url)

        assert result == {
            "type": "bill",
            "id": "HB 219",
            "ocd_bill_id": BILL,
            "gov_id": "HB 219",
            "jurisdiction": "FL",
            "session": "2026",
            "url": url,
            "ddp_url": url,
        }
        assert broker.call_args.args[1] == f"{BROKER}/api/bills/resolve/"
        assert broker.call_args.kwargs["params"] == {"jurisdiction": "FL", "session": "2026", "gov_id": "HB 219"}

    async def test_a_biennium_session_code_is_passed_through_unparsed(self, monkeypatch):
        broker = AsyncMock(return_value={"bill_openstates_id": BILL})
        monkeypatch.setattr(content, "_broker_get", broker)
        result = await content.resolve_content(
            url="https://digitaldemocracyproject.org/explore/WA/2023-2024/HB%201234"
        )
        assert broker.call_args.kwargs["params"]["session"] == "2023-2024"
        assert result["session"] == "2023-2024"

    async def test_an_unknown_session_is_404(self, monkeypatch):
        monkeypatch.setattr(content, "_broker_get", AsyncMock(side_effect=_status_error(404)))
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url="https://digitaldemocracyproject.org/explore/FL/1999/HB%20219")
        assert err.value.status_code == 404

    async def test_unconfigured_broker_is_503(self, monkeypatch):
        monkeypatch.setattr(content, "get_settings", lambda: _settings(broker=""))
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url="https://digitaldemocracyproject.org/explore/FL/2026/HB%20219")
        assert err.value.status_code == 503


class TestDeferredDdpNextUrls:
    """Legislator and organization URLs of ddp-next are deliberately not recognised yet (VOTEBOT-13)."""

    @pytest.mark.parametrize("path", ["/legislators/123", "/legislators/123/", "/organizations/7", "/explore/FL"])
    def test_not_matched_as_ddp_next_bills(self, path):
        assert content._match_ddp_next_bill(path) is None

    async def test_a_numeric_legislator_url_still_takes_the_webflow_path(self, monkeypatch):
        broker = AsyncMock()
        monkeypatch.setattr(content, "_broker_get", broker)
        monkeypatch.setattr(content, "fetch_webflow_item_by_slug", AsyncMock(return_value=None))
        monkeypatch.setattr(
            content,
            "get_settings",
            lambda: SimpleNamespace(
                ddp_broker_api_root=BROKER,
                webflow_legislators_collection_id="legislators-collection",
                webflow_votebot_api_key=SecretStr("k"),
            ),
        )
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url="https://digitaldemocracyproject.org/legislators/123")
        assert err.value.status_code == 404  # not found in the CMS, and the broker was never asked
        broker.assert_not_called()


class TestBrokerBodyShape:
    """A 200 whose body is not a JSON object is the broker breaking its contract: 502, not 500."""

    @staticmethod
    def _client(body: bytes, content_type: str = "application/json") -> httpx.AsyncClient:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={"content-type": content_type}))
        return httpx.AsyncClient(transport=transport)

    @pytest.mark.parametrize("body", [b"<html>gateway</html>", b"", b"[1, 2]", b'"text"', b"null"])
    async def test_non_json_or_non_dict_body_is_a_bad_gateway(self, body):
        async with self._client(body) as client:
            with pytest.raises(HTTPException) as err:
                await content._broker_get(client, f"{BROKER}/api/bills/resolve/")
        assert err.value.status_code == 502

    async def test_a_json_object_is_returned(self):
        async with self._client(b'{"bill_openstates_id": "x"}') as client:
            assert await content._broker_get(client, f"{BROKER}/api/bills/resolve/") == {"bill_openstates_id": "x"}

    @pytest.mark.parametrize("scorecard", [{"bill": "oops"}, {"bill": ["x"]}, {"bill": {"session": "2026", "govId": "HB 1", "jurisdictionIso2": "FL"}}])
    async def test_a_malformed_scorecard_is_a_bad_gateway(self, monkeypatch, scorecard):
        monkeypatch.setattr(content, "_broker_get", AsyncMock(return_value=scorecard))
        with pytest.raises(HTTPException) as err:
            await content.resolve_content(url="https://digitaldemocracyproject.org/bills/123")
        assert err.value.status_code == 502
