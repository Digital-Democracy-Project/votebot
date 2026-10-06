"""VOTEBOT-15 (part 2): positions from the broker and links to our own pages, canonical-id index."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.unit.test_ocd_bill_id_retrieval import BILL, NEW_INDEX, _hit, _service
from votebot.api.schemas.chat import PageContext
from votebot.config import LEGACY_PINECONE_INDEX_NAME, Settings
from votebot.core.agent import VoteBotAgent
from votebot.services import broker_lookup
from votebot.services.broker_lookup import (
    BillOrgPositions,
    BillPositionOfOrg,
    BrokerLookupService,
    OrgBillPositions,
    OrgDetails,
    OrgPositionOnBill,
    format_bill_org_positions,
    format_org_bill_positions,
    format_org_details,
)
from votebot.utils.ddp_urls import ddp_bill_url, ddp_bill_url_from_metadata

SITE = "https://site.test"
BROKER = "https://broker.test"


class TestDdpUrls:
    def test_the_explore_page_with_an_encoded_identifier(self):
        assert ddp_bill_url(SITE, "fl", "2026", "HB 219") == "https://site.test/explore/FL/2026/HB%20219"

    def test_a_trailing_slash_and_a_biennium_session(self):
        assert ddp_bill_url(SITE + "/", "WA", "2025-2026", "HB 1") == "https://site.test/explore/WA/2025-2026/HB%201"

    @pytest.mark.parametrize("missing", [("", "FL", "2026", "HB 1"), (SITE, "", "2026", "HB 1"),
                                         (SITE, "FL", None, "HB 1"), (SITE, "FL", "2026", "")])
    def test_nothing_is_built_without_every_part(self, missing):
        assert ddp_bill_url(*missing) is None

    def test_only_bill_documents_of_the_canonical_index_get_a_page(self):
        meta = {"document_type": "bill-text", "jurisdiction": "FL", "session_code": "2026", "gov_id": "HB 219"}
        assert ddp_bill_url_from_metadata(SITE, meta) == "https://site.test/explore/FL/2026/HB%20219"
        assert ddp_bill_url_from_metadata(SITE, {**meta, "document_type": "organization"}) is None
        assert ddp_bill_url_from_metadata(SITE, {"document_type": "bill-text"}) is None


def _bill_chunk(**extra):
    chunk = _hit("bill-text", ocd_bill_id=BILL, jurisdiction="FL", session_code="2026", gov_id="HB 219",
                 url="https://flsenate.gov/hb219.pdf", source="OpenStates archive", **extra)
    chunk.score = 0.9
    return chunk


class TestChunksLinkToOurPages:
    def _svc(self, base, index=NEW_INDEX):
        svc = _service(index)
        svc.settings = svc.settings.model_copy(update={"ddp_site_base_url": base})
        return svc

    def test_a_bill_chunk_points_at_our_page_and_keeps_the_legislature_url(self):
        (chunk,) = self._svc(SITE)._link_to_ddp_pages([_bill_chunk()])
        assert chunk.metadata["url"] == "https://site.test/explore/FL/2026/HB%20219"
        assert chunk.metadata["source_url"] == "https://flsenate.gov/hb219.pdf"

    def test_the_original_is_not_mutated(self):
        original = _bill_chunk()
        self._svc(SITE)._link_to_ddp_pages([original])
        assert original.metadata["url"] == "https://flsenate.gov/hb219.pdf"

    def test_an_existing_source_url_is_not_overwritten(self):
        (chunk,) = self._svc(SITE)._link_to_ddp_pages([_bill_chunk(source_url="https://archive.test/x")])
        assert chunk.metadata["source_url"] == "https://archive.test/x"

    def test_unset_base_url_changes_nothing(self):
        chunk = _bill_chunk()
        assert self._svc("")._link_to_ddp_pages([chunk])[0].metadata["url"] == "https://flsenate.gov/hb219.pdf"

    def test_organization_chunks_and_the_legacy_index_are_untouched(self):
        org = _hit("organization", broker_org_id="7", url="https://org.test")
        assert self._svc(SITE)._link_to_ddp_pages([org])[0].metadata["url"] == "https://org.test"
        legacy = self._svc(SITE, LEGACY_PINECONE_INDEX_NAME)
        assert legacy._link_to_ddp_pages([_bill_chunk()])[0].metadata["url"] == "https://flsenate.gov/hb219.pdf"

    async def test_retrieve_returns_the_linked_chunks(self):
        svc = self._svc(SITE)

        def respond(f):
            return [_bill_chunk()] if f and f.get("ocd_bill_id") and f.get("document_type") == "bill-text" else []

        svc.vector_store.query = self._store(respond)
        result = await svc.retrieve("what does it do", PageContext(type="bill", ocd_bill_id=BILL))
        assert result.chunks and result.chunks[0].metadata["url"].startswith(SITE + "/explore/FL/2026/")

    @staticmethod
    def _store(respond):
        async def query(query, top_k=10, filter=None, include_metadata=True):
            return respond(filter)

        return query

    def test_a_citation_of_our_page_matches_the_chunk_and_links_to_it(self):
        (chunk,) = self._svc(SITE)._link_to_ddp_pages([_bill_chunk()])
        agent = VoteBotAgent.__new__(VoteBotAgent)
        answer = "It limits fees. [Source: OpenStates archive](https://site.test/explore/FL/2026/HB%20219)"
        (citation,) = agent._extract_citations(answer, [chunk])
        assert citation.document_id == chunk.id  # matched to the chunk, so the bill id is still in it
        assert citation.url == "https://site.test/explore/FL/2026/HB%20219"


REAL_CLIENT = httpx.AsyncClient  # captured before any patching, so a test may patch more than once


def _mock_broker(monkeypatch, handler):
    monkeypatch.setattr(
        broker_lookup.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)
    )


def _service_with_root(root=BROKER) -> BrokerLookupService:
    return BrokerLookupService(Settings(ddp_broker_api_root=root, _env_file=None))


def _json(body, status=200):
    return httpx.Response(status, json=body)


class TestBrokerBillPositions:
    async def test_parses_positions_and_sends_the_natural_key(self, monkeypatch):
        seen = []

        def handler(request):
            seen.append(request)
            return _json({"found": True, "bill_version_id": 3, "positions": [
                {"org_name": "ACLU", "position": "oppose", "citation_url": "https://aclu.test/x", "verification_explanation": "..."},
                {"org_name": "AARP", "position": "support", "citation_url": ""},
                {"org_name": "Odd", "position": "neutral"},      # not a position we format
                {"position": "support"},                         # no name
                "junk",
            ]})

        _mock_broker(monkeypatch, handler)
        result = await _service_with_root().get_bill_org_positions("FL", "2026", "HB 219")

        assert [(p.org_name, p.position) for p in result.positions] == [("ACLU", "oppose"), ("AARP", "support")]
        assert seen[0].url.path == "/api/bill-organization-positions/current/"
        assert dict(seen[0].url.params) == {"jurisdiction": "FL", "session": "2026", "gov_id": "HB 219"}

    @pytest.mark.parametrize("answer", [_json({"found": False}), _json({"found": True, "positions": "x"}),
                                        _json([1]), _json({}, 500), httpx.Response(200, content=b"<html>")])
    async def test_anything_unusable_is_none(self, monkeypatch, answer):
        _mock_broker(monkeypatch, lambda request: answer)
        assert await _service_with_root().get_bill_org_positions("FL", "2026", "HB 219") is None

    async def test_unset_root_or_missing_parts_make_no_request(self, monkeypatch):
        _mock_broker(monkeypatch, lambda request: pytest.fail("no request expected"))
        assert await _service_with_root("").get_bill_org_positions("FL", "2026", "HB 219") is None
        assert await _service_with_root().get_bill_org_positions("FL", None, "HB 219") is None

    async def test_an_unreachable_broker_is_none(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("down")

        _mock_broker(monkeypatch, handler)
        assert await _service_with_root().get_bill_org_positions("FL", "2026", "HB 219") is None


class TestBrokerOrganization:
    def _row(self, gov_id, position="support"):
        return {"gov_id": gov_id, "bill_title": f"Title {gov_id}", "jurisdiction_iso2": "FL",
                "session_code": "2026", "position": position, "citation_url": "https://s.test"}

    async def test_follows_pages_until_there_is_no_next(self, monkeypatch):
        pages = {"1": {"results": [self._row("HB 1")], "next": "x"}, "2": {"results": [self._row("HB 2", "oppose")], "next": None}}
        _mock_broker(monkeypatch, lambda request: _json(pages[request.url.params["page"]]))
        result = await _service_with_root().get_org_bill_positions("42")
        assert [(r.gov_id, r.position) for r in result.positions] == [("HB 1", "support"), ("HB 2", "oppose")]
        assert result.complete is True

    async def test_a_failure_on_the_first_page_is_none_and_on_a_later_page_keeps_what_we_have(self, monkeypatch):
        _mock_broker(monkeypatch, lambda request: _json({}, 500))
        assert await _service_with_root().get_org_bill_positions("42") is None

        def handler(request):
            return _json({"results": [self._row("HB 1")], "next": "x"}) if request.url.params["page"] == "1" else _json({}, 500)

        _mock_broker(monkeypatch, handler)
        partial = await _service_with_root().get_org_bill_positions("42")
        assert [r.gov_id for r in partial.positions] == ["HB 1"] and partial.complete is False

    async def test_pages_are_bounded(self, monkeypatch):
        calls = []

        def handler(request):
            calls.append(1)
            return _json({"results": [self._row("HB 1")], "next": "more"})

        _mock_broker(monkeypatch, handler)
        result = await _service_with_root().get_org_bill_positions("42")
        assert len(calls) == broker_lookup.MAX_POSITION_PAGES and result.complete is False

    async def test_details(self, monkeypatch):
        _mock_broker(monkeypatch, lambda request: _json({"id": 42, "name": "AARP", "org_type": "Nonprofit", "website": "https://aarp.org", "description": "d"}))
        details = await _service_with_root().get_org_details("42")
        assert (details.name, details.org_type, details.website) == ("AARP", "Nonprofit", "https://aarp.org")
        _mock_broker(monkeypatch, lambda request: _json({"id": 42}))
        assert await _service_with_root().get_org_details("42") is None


class TestFormatting:
    def test_bill_positions_grouped_with_sources(self):
        text = format_bill_org_positions(BillOrgPositions([
            OrgPositionOnBill("AARP", "support", "https://aarp.test/p"), OrgPositionOnBill("ACLU", "oppose")]))
        assert "### Organizations Supporting This Bill\n- AARP ([source](https://aarp.test/p))" in text
        assert "### Organizations Opposing This Bill\n- ACLU" in text and "Authoritative Source" in text

    def test_no_positions_says_so_and_unknown_is_empty(self):
        assert "No organizations have a verified position" in format_bill_org_positions(BillOrgPositions([]))
        assert format_bill_org_positions(None) == ""

    def test_org_positions_link_to_our_pages_only_with_a_base_url(self):
        rows = [BillPositionOfOrg("HB 219", "Fees", "FL", "2026", "support"), BillPositionOfOrg("SB 2", "Tax", "FL", "2026", "oppose")]
        linked = format_org_bill_positions(OrgDetails("AARP"), OrgBillPositions(rows), SITE)
        assert "- [HB 219 Fees](https://site.test/explore/FL/2026/HB%20219)" in linked and "### Bills Opposed" in linked
        plain = format_org_bill_positions(OrgDetails("AARP"), OrgBillPositions(rows), "")
        assert "- HB 219 Fees" in plain and "](" not in plain
        assert "may be incomplete" not in plain
        assert "may be incomplete" in format_org_bill_positions(OrgDetails("AARP"), OrgBillPositions(rows, complete=False), "")

    def test_org_positions_edge_cases(self):
        assert format_org_bill_positions(None, None) == ""
        assert "No bill positions have been verified" in format_org_bill_positions(OrgDetails("AARP"), OrgBillPositions([]))

    def test_details_text(self):
        text = format_org_details(OrgDetails("AARP", "Nonprofit", "https://aarp.org", "x" * 500))
        assert "AARP" in text and "Nonprofit" in text and "x" * 300 in text and "x" * 301 not in text
        assert format_org_details(None) == ""


def _agent(index: str = NEW_INDEX, base: str = SITE) -> VoteBotAgent:
    a = VoteBotAgent.__new__(VoteBotAgent)
    a.settings = SimpleNamespace(
        bill_filter_key="ocd_bill_id" if index == NEW_INDEX else "webflow_id", ddp_site_base_url=base
    )
    a.broker_lookup = SimpleNamespace(
        get_bill_org_positions=AsyncMock(return_value=BillOrgPositions([OrgPositionOnBill("AARP", "support")])),
        get_org_bill_positions=AsyncMock(
            return_value=OrgBillPositions([BillPositionOfOrg("HB 1", "T", "FL", "2026", "support")])
        ),
        get_org_details=AsyncMock(return_value=OrgDetails("AARP", "Nonprofit")),
    )
    a.webflow_lookup = SimpleNamespace(
        get_bill_org_positions=AsyncMock(return_value=SimpleNamespace(found=False)),
        get_org_bill_positions=AsyncMock(return_value=SimpleNamespace(found=False)),
    )
    return a


class TestAgentEnrichments:
    async def test_bill_page_positions_come_from_the_broker_on_the_canonical_index(self):
        agent = _agent()
        ctx = PageContext(type="bill", id="HB 219", jurisdiction="FL", session="2026", ocd_bill_id=BILL)
        text = await agent._prefetch_bill_org_positions(ctx)
        assert "AARP" in text
        agent.broker_lookup.get_bill_org_positions.assert_awaited_once_with("FL", "2026", "HB 219")
        agent.webflow_lookup.get_bill_org_positions.assert_not_called()

    async def test_org_page_bills_come_from_the_broker_and_link_to_our_pages(self):
        agent = _agent()
        text = await agent._prefetch_org_bill_positions(PageContext(type="organization", id="42"))
        assert "[HB 1 T](https://site.test/explore/FL/2026/HB%201)" in text and "AARP" in text
        agent.broker_lookup.get_org_bill_positions.assert_awaited_once_with("42")
        agent.webflow_lookup.get_org_bill_positions.assert_not_called()

    async def test_an_org_page_without_a_broker_id_looks_nothing_up(self):
        agent = _agent()
        assert await agent._prefetch_org_bill_positions(PageContext(type="organization", id="some-slug", slug="some-slug")) == ""
        agent.broker_lookup.get_org_bill_positions.assert_not_called()

    async def test_dispute_verification_checks_organizations_only(self):
        agent = _agent()
        assert "AARP" in await agent._verify_from_webflow(PageContext(type="organization", id="42"))
        assert await agent._verify_from_webflow(PageContext(type="bill", id="HB 1")) == ""
        assert await agent._verify_from_webflow(PageContext(type="legislator", id="x")) == ""
        agent.broker_lookup.get_org_details.assert_awaited_once_with("42")

    async def test_the_legacy_index_still_uses_webflow(self):
        agent = _agent(LEGACY_PINECONE_INDEX_NAME)
        await agent._prefetch_bill_org_positions(PageContext(type="bill", slug="a-bill", webflow_id="wf1"))
        await agent._prefetch_org_bill_positions(PageContext(type="organization", slug="an-org", webflow_id="wf2"))
        agent.webflow_lookup.get_bill_org_positions.assert_awaited_once()
        agent.webflow_lookup.get_org_bill_positions.assert_awaited_once()
        agent.broker_lookup.get_bill_org_positions.assert_not_called()


class TestTimeBudget:
    async def _slow(self, *a, **k):
        await asyncio.sleep(5)

    async def test_a_slow_broker_costs_the_enrichment_not_the_answer(self, monkeypatch):
        monkeypatch.setattr("votebot.core.agent.BUDGET_SECONDS", 0.05)
        agent = _agent()
        agent.broker_lookup = SimpleNamespace(
            get_bill_org_positions=self._slow, get_org_bill_positions=self._slow, get_org_details=self._slow
        )
        assert await agent._prefetch_bill_org_positions(PageContext(type="bill", id="HB 1", jurisdiction="FL", session="2026")) == ""
        assert await agent._prefetch_org_bill_positions(PageContext(type="organization", id="42")) == ""
        assert await agent._verify_from_webflow(PageContext(type="organization", id="42")) == ""
