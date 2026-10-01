"""VOTEBOT-10: chunks are grouped by bill version, the current one is marked, and the prompt requires
every claim to name its version."""

from votebot.core.prompts import (
    NO_CURRENT_VERSION_NOTE,
    VERSION_CONTEXT_PROMPT,
    build_system_prompt,
    format_retrieved_chunks,
)


def _text(doc_id, note, date, content, n=0, gov_id="HB 1"):
    return {
        "id": f"bill-text:b:{doc_id}-chunk-{n}",
        "content": content,
        "metadata": {
            "document_type": "bill-text", "document_id": doc_id, "gov_id": gov_id, "source": "OpenStates archive",
            "version_note": note, "version_date": date, "version_stage": "x", "url": "https://gov.example/hb1",
        },
    }


def _diff(doc_id, note, date, from_note, content="+ added line"):
    chunk = _text(doc_id, note, date, content)
    chunk["id"] = f"bill-version-diff:b:{doc_id}-chunk-0"
    chunk["metadata"].update(document_type="bill-version-diff", from_version_note=from_note)
    return chunk


LEGISLATOR = {"id": "legislator-1", "content": "Jane Doe, FL House", "metadata": {"document_type": "legislator", "source": "OpenStates"}}


class TestGrouping:
    def test_chunks_of_one_version_sit_under_one_header_marked_current(self):
        out = format_retrieved_chunks(
            [_text("103", "Enrolled", "2026-04-01", "alpha", 0), _text("103", "Enrolled", "2026-04-01", "beta", 1)],
            current_document_id="103",
        )
        assert out.count("## HB 1 · Enrolled · 2026-04-01 · current") == 1
        assert out.index("alpha") < out.index("beta") and out.count("### Source") == 2

    def test_only_the_current_version_is_marked(self):
        out = format_retrieved_chunks(
            [_text("102", "Engrossed", "2026-03-04", "old"), _text("103", "Enrolled", "2026-04-01", "new")],
            current_document_id="103",
        )
        assert "## HB 1 · Engrossed · 2026-03-04\n" in out  # no "current" on the older version
        assert "## HB 1 · Enrolled · 2026-04-01 · current" in out

    def test_nothing_is_marked_when_the_current_version_is_unknown(self):
        out = format_retrieved_chunks([_text("103", "Enrolled", "2026-04-01", "x")])
        assert "· current" not in out

    def test_a_later_chunk_of_a_version_joins_its_group_wherever_it_was_ranked(self):
        chunks = [
            _text("103", "Enrolled", "2026-04-01", "first-103", 0),
            LEGISLATOR,
            _text("103", "Enrolled", "2026-04-01", "second-103", 1),
        ]
        out = format_retrieved_chunks(chunks, current_document_id="103")
        assert out.index("first-103") < out.index("second-103") < out.index("Jane Doe")

    def test_sources_are_numbered_in_the_order_they_are_shown(self):
        out = format_retrieved_chunks(
            [_text("103", "Enrolled", "d", "a", 0), LEGISLATOR, _text("103", "Enrolled", "d", "b", 1)], current_document_id="103"
        )
        assert [line.split(":")[0] for line in out.splitlines() if line.startswith("### Source")] == [
            "### Source 1", "### Source 2", "### Source 3",
        ]

    def test_other_chunks_keep_their_own_block_and_get_no_version_header(self):
        out = format_retrieved_chunks([LEGISLATOR])
        assert "### Source 1: OpenStates" in out and "## " not in out.replace("### ", "")


class TestNoCurrentVersionNote:
    def test_versions_with_no_current_marker_come_with_an_explicit_warning(self):
        out = format_retrieved_chunks([_text("101", "Introduced", "d", "a"), _text("102", "Engrossed", "d", "b")])
        assert out.startswith(NO_CURRENT_VERSION_NOTE)
        assert "could not be determined" in out and "name the version for every claim" in out

    def test_no_warning_when_a_current_version_is_known(self):
        out = format_retrieved_chunks([_text("103", "Enrolled", "d", "a")], current_document_id="103")
        assert "could not be determined" not in out

    def test_no_warning_when_there_are_no_version_groups(self):
        assert "could not be determined" not in format_retrieved_chunks([LEGISLATOR])
        assert "could not be determined" not in format_retrieved_chunks([])


class TestDiffHeader:
    def test_a_diff_names_both_versions(self):
        out = format_retrieved_chunks([_diff("102", "Engrossed", "2026-03-04", "Introduced")], current_document_id="103")
        assert "## HB 1 · Changes in Engrossed · 2026-03-04 · from Introduced" in out
        assert "current" not in out  # 102 is not the current version

    def test_the_current_versions_diff_is_marked_current(self):
        out = format_retrieved_chunks([_diff("103", "Enrolled", "2026-04-01", "Engrossed")], current_document_id="103")
        assert "## HB 1 · Changes in Enrolled · 2026-04-01 · from Engrossed · current" in out


class TestLegacyFormattingIsUnchanged:
    def test_unlabelled_bill_text_is_not_grouped(self):
        legacy = {"id": "bill-pdf-wf1-chunk-0", "content": "old text", "metadata": {"document_type": "bill-text", "source": "Webflow"}}
        out = format_retrieved_chunks([legacy], current_document_id="103")
        assert out.startswith("### Source 1: Webflow") and "##" not in out.replace("###", "")

    def test_the_legacy_changelog_version_change_line_survives(self):
        chunk = {
            "id": "bill-changelog-x", "content": "summary",
            "metadata": {"document_type": "bill-changelog", "source": "S", "version_from_note": "A", "version_to_note": "B"},
        }
        assert "**Version Change:** A → B" in format_retrieved_chunks([chunk])


class TestPrompt:
    def test_a_bill_page_prompt_requires_the_version_to_be_named(self):
        prompt = build_system_prompt("bill", {"title": "HB 1"}, version_aware=True)
        assert VERSION_CONTEXT_PROMPT in prompt
        assert "Name the version for every claim" in prompt and "**From:** [version] → **To:** [version]" in prompt

    def test_the_legacy_index_prompt_is_unchanged(self):
        # No version headers exist there, so the version instructions would only confuse the model.
        legacy = build_system_prompt("bill", {"title": "HB 1"})
        assert VERSION_CONTEXT_PROMPT not in legacy and "## Bill Versions" not in legacy

    def test_the_prompt_says_what_to_do_when_no_version_is_current(self):
        assert "could not be determined" in VERSION_CONTEXT_PROMPT
        assert "do not present any one version as current" in VERSION_CONTEXT_PROMPT

    def test_other_pages_do_not_get_it(self):
        for page_type in ("legislator", "organization", "general"):
            assert VERSION_CONTEXT_PROMPT not in build_system_prompt(page_type, {})
