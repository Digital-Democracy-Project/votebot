"""The agent and retrieval read a state from text with one table, not two (VOTEBOT-3)."""

import pytest

from tests.unit.test_ocd_bill_id_retrieval import _service
from votebot.core.agent import VoteBotAgent
from votebot.core.retrieval import STATE_MAPPINGS
from votebot.utils.jurisdictions import STATE_NAME_TO_CODE

guess = VoteBotAgent._extract_jurisdiction_from_message


def test_the_table_has_the_fifty_states_with_distinct_codes():
    assert len(STATE_NAME_TO_CODE) == 50
    assert len(set(STATE_NAME_TO_CODE.values())) == 50


@pytest.mark.parametrize("name,code", sorted(STATE_NAME_TO_CODE.items()))
def test_both_readers_agree_on_every_state(name, code):
    query = f"What does {name.title()} HB 363 do?"
    assert guess(None, query) == code
    assert _service()._extract_bill_from_query(query).jurisdiction == code.lower()


@pytest.mark.parametrize("name,code", sorted(STATE_NAME_TO_CODE.items()))
def test_retrieval_reads_a_capitalised_code_for_every_state(name, code):
    assert _service()._extract_bill_from_query(f"What does {code} HB 363 do?").jurisdiction == code.lower()


def test_west_virginia_is_not_virginia_in_retrieval():
    assert _service()._extract_bill_from_query("West Virginia HB 363").jurisdiction == "wv"
    assert _service()._extract_bill_from_query("Virginia HB 363").jurisdiction == "va"


@pytest.mark.parametrize("query", [
    "Who voted in HB 363?",       # IN
    "Is HB 363 or HB 364 better?",  # OR
    "Tell me about HB 363",        # ME-free sentence with "me"
    "Show me HB 363",              # ME
    "Hi, what is HB 363?",         # HI
    "Is HB 363 ok?",               # OK
    "Oh, what is HB 363?",         # OH
])
def test_codes_that_are_ordinary_words_need_capitals_in_retrieval(query):
    assert _service()._extract_bill_from_query(query).jurisdiction is None


def test_the_federal_words_and_the_old_lower_case_codes_still_work():
    svc = _service()
    assert svc._extract_bill_from_query("federal HB 363").jurisdiction == "us"
    assert svc._extract_bill_from_query("fl hb 363").jurisdiction == "fl"
    assert svc._extract_bill_from_query("tx hb 363").jurisdiction == "tx"


def test_every_mapping_value_is_a_lower_case_code_or_us():
    assert set(STATE_MAPPINGS.values()) == {c.lower() for c in STATE_NAME_TO_CODE.values()} | {"us"}
