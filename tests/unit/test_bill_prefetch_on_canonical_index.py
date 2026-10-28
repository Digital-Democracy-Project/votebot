"""VOTEBOT-52: on the canonical-id index (VoteBot 3.0, ddp-next) the live bill lookup never consults the legacy CMS.

The legacy CMS path goes with VOTEBOT-17. A page context that still carries a `slug` used to make
`_prefetch_bill_info` read the bill from the CMS on every message. On the new index the page already names the
bill, so nothing is asked of it, configured or not; the legacy index is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from votebot.api.schemas.chat import PageContext
from votebot.config import LEGACY_PINECONE_INDEX_NAME, Settings
from votebot.core.agent import VoteBotAgent
from votebot.services.webflow_lookup import BillDetailsResult

NEW_INDEX = "ddp-knowledge-base"


def _agent(index: str, details: BillDetailsResult | None = None):
    agent = VoteBotAgent.__new__(VoteBotAgent)
    agent.settings = Settings(pinecone_index_name=index, _env_file=None)
    agent.webflow_lookup = SimpleNamespace(get_bill_details=AsyncMock(return_value=details or BillDetailsResult(
        name="A Bill", identifier="HB 7", status="", description="", jurisdiction="fl", slug="a-bill", found=True,
        session="2026", openstates_url="")))
    found = SimpleNamespace(found=True)
    agent.bill_votes = SimpleNamespace(
        get_bill_info=AsyncMock(return_value=found),
        get_bill_info_by_url=AsyncMock(return_value=None),
        format_bill_info_document=lambda result, page_url=None: "BILL-INFO",
    )
    agent.retrieval = SimpleNamespace(
        vector_store=SimpleNamespace(query=AsyncMock(return_value=[])), _legislative_scope=lambda page: {}
    )
    agent._own_page_url = lambda page, result: None
    return agent


PAGE = PageContext(type="bill", id="HB 1", slug="a-bill", jurisdiction="FL", session="2026")


class TestTheCanonicalIndex:
    async def test_the_cms_is_never_consulted_for_a_page_with_a_slug(self):
        agent = _agent(NEW_INDEX)
        await agent._prefetch_bill_info("What is the status of this bill?", PAGE, None)
        agent.webflow_lookup.get_bill_details.assert_not_called()

    async def test_the_bill_is_the_one_the_page_names(self):
        agent = _agent(NEW_INDEX)
        out = await agent._prefetch_bill_info("What is the status of this bill?", PAGE, None)
        assert out  # the live lookup still ran, for the page's own bill (HB 1), not a CMS-resolved one
        asked = agent.bill_votes.get_bill_info.await_args.kwargs
        assert asked["bill_identifier"] == "HB 1"
        assert (asked["jurisdiction"].lower(), asked["session"]) == ("fl", "2026")  # the page's, with no CMS to supply them

    async def test_a_page_without_a_slug_is_the_same(self):
        agent = _agent(NEW_INDEX)
        page = PageContext(type="bill", id="HB 1", jurisdiction="FL", session="2026")
        await agent._prefetch_bill_info("What is the status of this bill?", page, None)
        agent.webflow_lookup.get_bill_details.assert_not_called()
        agent.bill_votes.get_bill_info.assert_awaited_once()


class TestTheLegacyIndexIsUnchanged:
    async def test_it_still_resolves_the_bill_from_the_cms_by_slug(self):
        agent = _agent(LEGACY_PINECONE_INDEX_NAME)
        await agent._prefetch_bill_info("What is the status of this bill?", PAGE, None)
        agent.webflow_lookup.get_bill_details.assert_awaited_once_with(slug="a-bill")
