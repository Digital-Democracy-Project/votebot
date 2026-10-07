"""VOTEBOT-15: "what changed" is read live from api-v3's stored diff, because diffs are not embedded.

api-v3 carries `diff_from_previous_version` on every classifiable version (OPEN-118), on the
single-bill detail call (`include=versions`). These tests run the real `BillVersionService`
selection and the real retrieval phase; only the HTTP fetch is replaced.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.unit.test_ocd_bill_id_retrieval import BILL, NEW_INDEX, _service
from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.retrieval import (
    DIFF_CHUNK_CHARS,
    DIFF_MAX_CHARS,
    DIFF_NONE_NOTE,
    DIFF_UNAVAILABLE_NOTE,
)
from votebot.services.bill_versions import BillVersionService


def _version(note, date, stage, ordinal, archive_id, diff):
    return {
        "note": note, "date": date, "version_stage": stage, "version_ordinal": ordinal,
        "archived_document_id": archive_id, "diff_from_previous_version": diff,
    }


API = {
    "versions": [
        _version("Introduced", "2026-01-10", "introduced", 0, 101, None),
        _version("Engrossed", "2026-03-04", "chamber_passage", 1, 102, "+ added in engrossed"),
        _version("Enrolled", "2026-04-01", "final_passage", 2, 103, "+ added in enrolled"),
        _version("Odd note", "2026-05-01", "unknown", None, 999, "ignored: stage unknown"),
    ]
}


def _versions_service(api=API, replica=True) -> BillVersionService:
    settings = Settings(
        use_ddp_openstates_replica=replica, ddp_openstates_api_root="https://api.test", _env_file=None
    )
    service = BillVersionService(settings)
    service._fetch = AsyncMock(return_value=api)
    return service


class TestGetDiffs:
    async def test_the_current_version_by_default_labelled_with_its_predecessor(self):
        diffs = await _versions_service().get_diffs(BILL)
        assert [(d.document_id, d.note, d.from_note, d.from_document_id, d.text) for d in diffs] == [
            ("103", "Enrolled", "Engrossed", "102", "+ added in enrolled")
        ]

    async def test_a_named_stage(self):
        diffs = await _versions_service().get_diffs(BILL, stages=("chamber_passage",))
        assert [(d.document_id, d.from_note) for d in diffs] == [("102", "Introduced")]

    async def test_a_named_date(self):
        diffs = await _versions_service().get_diffs(BILL, dates=("2026-04-01",))
        assert [d.document_id for d in diffs] == ["103"]

    async def test_a_version_without_a_stored_diff_is_absent_not_invented(self):
        # The first version has no predecessor, and api-v3 keeps diffs for the last two only.
        assert await _versions_service().get_diffs(BILL, stages=("introduced",)) == []

    async def test_stage_unknown_versions_are_never_chosen(self):
        assert await _versions_service().get_diffs(BILL, stages=("unknown",)) == []

    async def test_a_diff_whose_predecessor_is_not_archived_is_dropped_not_mislabelled(self):
        # Introduced exists in the lineage but has no archived text: what Engrossed was compared with is unknown.
        unarchived = _version("Introduced", "2026-01-10", "introduced", 0, None, None)
        api = {"versions": [unarchived, API["versions"][1], API["versions"][2]]}
        assert await _versions_service(api).get_diffs(BILL, stages=("chamber_passage",)) == []
        (enrolled,) = await _versions_service(api).get_diffs(BILL)  # its own predecessor is archived: fine
        assert enrolled.from_note == "Engrossed"

    async def test_the_predecessor_skips_stage_unknown_versions(self):
        api = {"versions": [API["versions"][0], API["versions"][3], API["versions"][1]]}
        (diff,) = await _versions_service(api).get_diffs(BILL, stages=("chamber_passage",))
        assert diff.from_note == "Introduced"

    async def test_every_matching_version_is_returned_and_the_size_budget_decides_what_fits(self):
        many = {"versions": [_version(f"Amendment {i}", f"2026-02-0{i + 1}", "amendment", i, 200 + i, f"+ {i}" if i else None) for i in range(6)]}
        diffs = await _versions_service(many).get_diffs(BILL, stages=("amendment",))
        assert [d.note for d in diffs] == [f"Amendment {i}" for i in range(1, 6)]  # the first has no predecessor, so no diff

    @pytest.mark.parametrize("api", [None, {}, {"versions": "x"}, {"versions": [None, "x"]}])
    async def test_nothing_usable_is_none_or_empty(self, api):
        result = await _versions_service(api).get_diffs(BILL)
        assert not result

    async def test_without_the_replica_flag_api_v3s_version_fields_do_not_exist(self):
        service = _versions_service(replica=False)
        assert await service.get_diffs(BILL) is None
        service._fetch.assert_not_called()

    async def test_diffs_are_not_cached(self):
        service = _versions_service()
        await service.get_diffs(BILL)
        await service.get_diffs(BILL)
        assert service._fetch.await_count == 2  # large, and a rare question


def _retrieval(api=API):
    svc = _service(NEW_INDEX, queries=[], respond=lambda f: [])
    svc.bill_versions = _versions_service(api)
    return svc


def _bill():
    return PageContext(type="bill", ocd_bill_id=BILL)


class TestRetrievalPhaseFive:
    async def test_a_changelog_question_leads_with_the_current_versions_diff(self):
        result = await _retrieval().retrieve("what changed in this bill?", _bill())
        first = result.chunks[0]
        assert first.metadata["document_type"] == "bill-version-diff"
        assert (first.metadata["from_version_note"], first.metadata["version_note"]) == ("Engrossed", "Enrolled")
        assert first.content == "+ added in enrolled" and first.metadata["source"] == "OpenStates (live)"

    async def test_a_named_version_gets_its_own_diff(self):
        result = await _retrieval().retrieve("what changed in the engrossed version?", _bill())
        first = result.chunks[0]
        assert first.metadata["document_id"] == "102" and first.metadata["from_version_note"] == "Introduced"

    async def test_an_ordinary_question_reads_no_diff(self):
        svc = _retrieval()
        result = await svc.retrieve("what does this bill do?", _bill())
        assert not any(c.metadata.get("document_type") == "bill-version-diff" for c in result.chunks)

    async def test_a_long_diff_is_cut_and_says_so(self):
        long_diff = "+" + "x" * 30000
        api = {"versions": [API["versions"][0], _version("Enrolled", "2026-04-01", "final_passage", 1, 103, long_diff)]}
        result = await _retrieval(api).retrieve("what changed in this bill?", _bill())
        diffs = [c for c in result.chunks if c.metadata["document_type"] == "bill-version-diff"]
        text = "".join(c.content for c in diffs)
        assert len(diffs) > 1 and all(len(c.content) <= DIFF_CHUNK_CHARS for c in diffs)
        assert f"first {DIFF_MAX_CHARS} of {len(long_diff)} characters" in text
        assert len(text) < DIFF_MAX_CHARS + 200

    async def test_the_budget_is_for_all_the_versions_together_and_the_rest_are_said_to_be_omitted(self):
        big = "+" + "y" * 9000
        api = {"versions": [
            _version("Amendment A", "2026-02-01", "amendment", 0, 201, None),
            _version("Amendment B", "2026-02-02", "amendment", 1, 202, big),
            _version("Amendment C", "2026-02-03", "amendment", 2, 203, big),
            _version("Amendment D", "2026-02-04", "amendment", 3, 204, big),
        ]}
        result = await _retrieval(api).retrieve("what changed in the amended version?", _bill())
        diffs = [c for c in result.chunks if c.metadata["document_type"] == "bill-version-diff"]
        assert sum(len(c.content) for c in diffs) < DIFF_MAX_CHARS + 200  # not 3 x 9000
        assert {c.metadata["version_note"] for c in diffs} == {"Amendment B", "Amendment C"}  # D had no budget left
        assert any("omitted for length" in n for n in result.notes)

    async def test_api_v3_down_tells_the_model_it_could_not_read_the_change(self):
        result = await _retrieval(None).retrieve("what changed in this bill?", _bill())  # must not raise
        assert not any(c.metadata.get("document_type") == "bill-version-diff" for c in result.chunks)
        assert result.notes == [DIFF_UNAVAILABLE_NOTE]

    async def test_a_lookup_that_raises_is_the_same_as_api_v3_being_down(self):
        svc = _retrieval()
        svc.bill_versions.get_diffs = AsyncMock(side_effect=RuntimeError("boom"))
        result = await svc.retrieve("what changed in this bill?", _bill())
        assert result.notes == [DIFF_UNAVAILABLE_NOTE]

    async def test_no_stored_diff_is_said_differently_from_an_outage(self):
        result = await _retrieval().retrieve("what changed in the introduced version?", _bill())
        assert result.notes == [DIFF_NONE_NOTE] and "first version" in DIFF_NONE_NOTE

    async def test_the_answer_is_told_exactly_which_versions_were_compared(self):
        # "first and latest" was answered as if the whole history had been compared; only the last pair was read
        result = await _retrieval().retrieve("What changed between the first and the latest version?", _bill())
        (note,) = [n for n in result.notes if "give only these comparison" in n]
        assert "Engrossed -> Enrolled (final_passage, 2026-04-01)" in note
        assert "only these comparison(s) were read" in note and "earlier steps were not compared" in note
        assert "Introduced" not in note  # not a pair that was compared

    async def test_each_comparison_read_is_named_and_an_omitted_one_is_not(self):
        big = "+" + "y" * 9000
        api = {"versions": [
            _version("Amendment A", "2026-02-01", "amendment", 0, 201, None),
            _version("Amendment B", "2026-02-02", "amendment", 1, 202, big),
            _version("Amendment C", "2026-02-03", "amendment", 2, 203, big),
            _version("Amendment D", "2026-02-04", "amendment", 3, 204, big),
        ]}
        result = await _retrieval(api).retrieve("what changed in the amended version?", _bill())
        (note,) = [n for n in result.notes if "give only these comparison" in n]
        assert "Amendment A -> Amendment B" in note and "Amendment B -> Amendment C" in note
        assert "Amendment D" not in note  # it had no budget left, and has its own "omitted" note

    async def test_a_version_with_no_recorded_predecessor_is_still_named_without_inventing_one(self):
        from votebot.core.retrieval import diff_scope_note
        from votebot.services.bill_versions import VersionDiff

        shown = [(VersionDiff("9", "Enrolled", "2026-04-01", "final_passage", None, None, "+ x"), None)]
        note = diff_scope_note(shown)
        assert "the version before it (its name is not recorded) -> Enrolled (final_passage, 2026-04-01)" in note

    async def test_missing_stage_or_date_leaves_no_empty_brackets(self):
        from votebot.core.retrieval import diff_scope_note
        from votebot.services.bill_versions import VersionDiff

        assert "A -> B)" not in diff_scope_note([(VersionDiff("9", "B", "", "", "A", "8", "+ x"), None)])
        assert "A -> B." in diff_scope_note([(VersionDiff("9", "B", "", "", "A", "8", "+ x"), None)])

    async def test_one_comparison_gets_the_exact_heading_so_the_model_cannot_write_a_wider_one(self):
        # VOTEBOT-22: live runs still headed an accurate answer "Earliest to Latest Version" (2 of 5)
        from votebot.core.retrieval import diff_scope_note
        from votebot.services.bill_versions import VersionDiff

        note = diff_scope_note([(VersionDiff("9", "H 7089 er", "d", "final_passage", "H 7089 e2", "8", "+ x"), None)])
        assert 'Start your answer with exactly this heading: "## What changed: H 7089 e2 -> H 7089 er"' in note

    async def test_several_comparisons_get_no_single_heading(self):
        from votebot.core.retrieval import diff_scope_note
        from votebot.services.bill_versions import VersionDiff

        shown = [(VersionDiff("9", "B", "", "", "A", "8", "+ x"), None), (VersionDiff("10", "C", "", "", "B", "9", "+ y"), None)]
        assert "exactly this heading" not in diff_scope_note(shown)

    async def test_a_diff_that_was_cut_for_length_is_said_to_be_partly_read(self):
        long_diff = "+" + "x" * 30000
        api = {"versions": [API["versions"][0], _version("Enrolled", "2026-04-01", "final_passage", 1, 103, long_diff)]}
        result = await _retrieval(api).retrieve("what changed in this bill?", _bill())
        (note,) = [n for n in result.notes if "give only these comparison" in n]
        assert f"only the first {DIFF_MAX_CHARS} of {len(long_diff)} characters of this diff were read" in note

    async def test_the_note_is_about_the_live_records_not_every_source(self):
        # a source that explicitly compares another pair is not forbidden: only presenting it as read here is
        from votebot.core.retrieval import diff_scope_note
        from votebot.services.bill_versions import VersionDiff

        note = diff_scope_note([(VersionDiff("9", "B", "d", "s", "A", "8", "+ x"), None)])
        assert "live version records" in note and "as if you had read it" in note
        assert "Do not describe changes between any other pair" not in note

    async def test_an_ordinary_question_adds_no_notes(self):
        assert (await _retrieval().retrieve("what does this bill do?", _bill())).notes == []

    async def test_the_notes_reach_the_prompt_ahead_of_the_sources(self):
        from tests.unit.test_agent_stream_votes_flag import _agent

        agent = _agent("", should_use_tool=False)
        prompts = []

        async def stream(**kwargs):
            prompts.append(kwargs["system_prompt"])
            from votebot.services.llm import StreamChunk

            yield StreamChunk(text="ok", done=True)

        agent.llm = type("L", (), {"stream": staticmethod(stream)})()
        agent.retrieval = _retrieval(None)
        async for _ in agent.process_message_stream(message="what changed in this bill?", session_id="s", page_context=_bill()):
            pass
        assert DIFF_UNAVAILABLE_NOTE in prompts[0]

    async def test_the_diff_chunks_carry_the_labels_the_prompt_groups_by(self):
        from votebot.core.prompts import format_retrieved_chunks

        result = await _retrieval().retrieve("what changed in this bill?", _bill())
        text = format_retrieved_chunks(
            [{"id": c.id, "content": c.content, "metadata": c.metadata} for c in result.chunks],
            current_document_id=result.current_document_id,
        )
        assert "Changes in Enrolled" in text and "from Engrossed" in text
