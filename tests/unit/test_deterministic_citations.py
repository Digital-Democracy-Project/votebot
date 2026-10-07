"""VOTEBOT-21: an answer built from retrieved text gets citations even when the model writes no [Source: ...].

The sample answer is shortened from a real one (FL HB 7089, 2026-10-07) that came back with the model's
source links in a form `_extract_citations` does not read; the bill text is hand-written in the bill's own
words (not pulled from the index), so the word-overlap thresholds are checked here against a plausible
pair and need the live check in the PR description on real vectors.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.agent import VoteBotAgent
from votebot.core.citations import MAX_CITATIONS, chunks_used_by
from votebot.core.retrieval import RetrievalResult
from votebot.services.llm import LLMResponse, StreamChunk
from votebot.services.vector_store import SearchResult

BILL = "f3dbc843-937d-4231-bc2d-e7ab4ff9a92b"
PAGE = PageContext(
    type="bill", id="HB 7089", title="Transparency in Health and Human Services", jurisdiction="FL",
    session="2024", ocd_bill_id=BILL,
)
URL = "https://site.test/explore/FL/2024/HB%207089"

BILL_TEXT = (
    "A health care provider or facility may not commence an action to collect medical debt after "
    "3 years; the statute of limitations is established for medical debt collection. Up to $10,000 "
    "in value of a single motor vehicle is exempt from attachment, garnishment, or other legal process "
    "in an action to collect medical debt. A licensed facility shall post a consumer-friendly list of "
    "standard charges for at least 300 shoppable health care services, and shall provide a good faith "
    "estimate of anticipated charges for nonemergency services."
)

UNCITED_ANSWER = (
    "## Overview\n\nThe bill increases transparency in health care pricing and strengthens protections "
    "for patients regarding medical debt.\n\n- **Medical debt collection**: establishes a 3-year statute "
    "of limitations for collecting medical debt.\n- **Exemptions**: exempts up to $10,000 for a single "
    "motor vehicle from garnishment in actions to collect medical debt.\n- **Price transparency**: "
    "licensed facilities must post a consumer-friendly list of standard charges for shoppable services "
    "and give a good faith estimate for nonemergency services."
)


def _chunk(text=BILL_TEXT, id=f"bill-text:{BILL}:13717-chunk-4", score=0.5, **metadata):
    meta = {"source": "OpenStates archive", "url": URL, "document_id": 13717, "ocd_bill_id": BILL, **metadata}
    return SearchResult(id=id, content=text, score=score, metadata=meta)


class TestWhichChunksWereUsed:
    def test_an_answer_built_from_the_text_cites_the_chunk(self):
        (used,) = chunks_used_by(UNCITED_ANSWER, [_chunk()], "HB 7089 Transparency in Health and Human Services FL")
        assert used.id.endswith("13717-chunk-4")

    def test_a_greeting_or_refusal_cites_nothing(self):
        chunks = [_chunk()]
        assert chunks_used_by("Thanks, happy to help! Ask me anything else about this bill.", chunks) == []
        assert chunks_used_by("I can only answer questions about legislation, so I cannot help with that.", chunks) == []

    def test_an_answer_that_only_repeats_the_page_title_cites_nothing(self):
        # the page's own title and number are in the prompt, so repeating them says nothing about the chunks
        title_chunk = _chunk("Transparency in Health and Human Services, an act relating to transparency")
        echoed = "This is Transparency in Health and Human Services, which concerns transparency in health care."
        assert chunks_used_by(echoed, [title_chunk], "Transparency in Health and Human Services") == []

    def test_a_live_data_answer_that_shares_nothing_with_the_chunks_cites_nothing(self):
        vote = "The House passed it 80 to 30 on March 4, with 12 Republicans and 18 Democrats absent."
        assert chunks_used_by(vote, [_chunk()]) == []

    def test_an_unrelated_chunk_is_not_cited(self):
        other = _chunk("Appropriations for highway resurfacing and bridge maintenance across the counties.", id="x")
        assert chunks_used_by(UNCITED_ANSWER, [other]) == []

    def test_ten_chunks_of_one_version_are_one_source(self):
        chunks = [_chunk(id=f"bill-text:{BILL}:13717-chunk-{n}") for n in range(10)]
        assert len(chunks_used_by(UNCITED_ANSWER, chunks)) == 1

    def test_two_versions_are_two_sources_strongest_first(self):
        weak = _chunk("The statute of limitations for medical debt collection is established at 3 years.",
                      id="v1", document_id=1, url=URL + "?v=1")
        strong = _chunk(id="v2", document_id=2, url=URL + "?v=2")
        assert [c.id for c in chunks_used_by(UNCITED_ANSWER, [weak, strong])] == ["v2", "v1"]

    def test_at_most_three_sources(self):
        chunks = [_chunk(id=f"v{n}", document_id=n, url=f"{URL}?v={n}") for n in range(6)]
        assert len(chunks_used_by(UNCITED_ANSWER, chunks)) == MAX_CITATIONS

    def test_no_chunks_no_citations(self):
        assert chunks_used_by(UNCITED_ANSWER, []) == []


def _agent(answer: str, chunks, **settings) -> VoteBotAgent:
    agent = VoteBotAgent.__new__(VoteBotAgent)
    agent.settings = Settings(pinecone_index_name="ddp-knowledge-base", _env_file=None, **settings)
    agent.retrieval = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=RetrievalResult(chunks=chunks, query_used="q", filters_applied={}, total_retrieved=len(chunks))
        )
    )

    async def stream(**kwargs):
        yield StreamChunk(text=answer[:40])
        yield StreamChunk(text=answer[40:], done=True)

    async def complete(**kwargs):
        return LLMResponse(content=answer, tokens_used=1, model="fake", web_citations=[])

    agent.llm = SimpleNamespace(stream=stream, complete=complete)
    agent._should_use_bill_votes_tool = lambda *a, **k: False
    agent._perform_web_search = AsyncMock(return_value=[])
    agent._log_query = lambda **kwargs: None
    return agent


async def _streamed(agent):
    chunks = [c async for c in agent.process_message_stream(message="What does it do?", session_id="s", page_context=PAGE)]
    (final,) = [c for c in chunks if c.done]
    return final.citations


async def _non_streamed(agent):
    return (await agent.process_message(message="What does it do?", session_id="s", page_context=PAGE)).citations


class TestBothAgentPaths:
    async def test_streaming_cites_the_chunk_the_uncited_answer_was_built_from(self):
        (citation,) = await _streamed(_agent(UNCITED_ANSWER, [_chunk()]))
        assert BILL in citation.document_id  # the smoke test's isolation check reads this
        assert citation.url == URL and citation.source == "OpenStates archive" and citation.relevance_score == 0.5

    async def test_non_streaming_cites_the_chunk_too(self):
        (citation,) = await _non_streamed(_agent(UNCITED_ANSWER, [_chunk()]))
        assert BILL in citation.document_id and citation.url == URL

    async def test_a_conversational_answer_keeps_no_citations_on_both_paths(self):
        agent = _agent("Thanks, glad to help! Anything else about this bill?", [_chunk()])
        assert await _streamed(agent) == [] and await _non_streamed(agent) == []

    async def test_citations_the_model_wrote_are_left_alone(self):
        written = UNCITED_ANSWER + f"\n\n[Source: OpenStates archive]({URL})"
        (citation,) = await _streamed(_agent(written, [_chunk(), _chunk(id="other", document_id=2, url=URL + "?v=2")]))
        assert citation.document_id.endswith("13717-chunk-4")  # matched as before, not replaced or added to

    async def test_the_setting_turns_it_off(self):
        agent = _agent(UNCITED_ANSWER, [_chunk()], deterministic_citations=False)
        assert await _streamed(agent) == [] and await _non_streamed(agent) == []

    async def test_confidence_counts_the_citation(self):
        agent = _agent(UNCITED_ANSWER, [_chunk()])
        with_citation = agent._calculate_confidence(
            UNCITED_ANSWER, 1, await _streamed(agent), retrieval_result=None
        )
        without = agent._calculate_confidence(UNCITED_ANSWER, 1, [], retrieval_result=None)
        assert with_citation > without
