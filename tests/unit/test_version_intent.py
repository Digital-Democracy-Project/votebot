"""VOTEBOT-10: recognising when a question names a bill version (stage or date)."""

import pytest

from votebot.utils.intent import VALID_RETRIEVAL_SOURCES, VERSION_STAGE_KEYWORDS, detect_version_request

# The only stage labels api-v3 emits (api/bills.py STAGE_LABELS), minus "unknown".
API_V3_STAGES = {"introduced", "amendment", "chamber_passage", "final_passage", "enacted"}


def test_the_vocabulary_only_uses_stages_api_v3_actually_emits():
    assert set(VERSION_STAGE_KEYWORDS) == API_V3_STAGES


@pytest.mark.parametrize(
    "query, stage",
    [
        ("show me the bill as introduced", "introduced"),
        ("what is in the original version?", "introduced"),
        ("what does the engrossed version say?", "chamber_passage"),
        ("the text as passed the house", "chamber_passage"),
        ("the enrolled bill", "final_passage"),
        ("the bill as enacted", "enacted"),
        ("what did the committee substitute change?", "amendment"),
    ],
)
def test_names_a_stage(query, stage):
    assert detect_version_request(query).stages == (stage,)


@pytest.mark.parametrize(
    "query",
    [
        "when was it introduced?",  # a status question, not a request for that version
        "was this bill amended by the committee?",
        "who sponsored the bill?",
        "what does this bill do?",
    ],
)
def test_ordinary_questions_name_no_version(query):
    assert not detect_version_request(query)


def test_iso_and_written_dates_when_the_query_is_about_a_version():
    assert detect_version_request("the version from 2026-03-04").dates == ("2026-03-04",)
    assert detect_version_request("the version from March 4, 2026").dates == ("2026-03-04",)
    assert detect_version_request("the draft as of Mar 4th, 2026").dates == ("2026-03-04",)


@pytest.mark.parametrize(
    "query",
    [
        "does this take effect March 4, 2026?",  # a date inside a question about the bill's content
        "what is the deadline of 2026-07-01 for?",
        "the text on 2026-03-04",
    ],
)
def test_a_date_alone_is_not_a_version_request(query):
    assert not detect_version_request(query)


def test_an_impossible_date_is_not_a_request():
    assert not detect_version_request("the version from Feb 31, 2026")


def test_stage_and_date_together_and_repeats_collapse():
    request = detect_version_request("the engrossed version of 2026-03-04, again 2026-03-04")
    assert (request.stages, request.dates) == (("chamber_passage",), ("2026-03-04",))


def test_bill_version_diff_is_a_known_retrieval_source():
    # Otherwise normalize_retrieval_sources reports it as "unknown" in analytics.
    assert "bill-version-diff" in VALID_RETRIEVAL_SOURCES
