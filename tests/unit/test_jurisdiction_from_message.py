"""Guessing a jurisdiction from a message must not read "us" inside other words (VOTEBOT-25).

The Status & votes button asks "What is the latest status and vote history for HB 7089?"; "us " inside
"status" made every bill, in every state, a federal one, so the live lookup found nothing.
"""

import pytest

from votebot.core.agent import VoteBotAgent

guess = VoteBotAgent._extract_jurisdiction_from_message


@pytest.mark.parametrize("message", [
    "What is the latest status and vote history for HB 7089?",
    "What is the latest status and vote history for this bill?",
    "What is the status of HB 7089?",
    "Does the bonus program apply?",
    "Which campus is mentioned?",
    "Tell us about this bill",
    "How did the vote on this bill go?",
    "Is this a USA bill?",  # an acronym that merely contains US
    "Does the USDA fund this?",
])
def test_no_jurisdiction_is_guessed_from_letters_inside_words_or_the_pronoun(message):
    assert guess(None, message) is None


@pytest.mark.parametrize("message", [
    "Is there a federal version of this?",
    "Was this discussed in Congress?",
    "Is this a U.S. bill?",
    "Tell me about the United States bill",
    "US HR 1 vote",
    "What does the US bill do?",
    "Is this a U.S., not state, bill?",   # punctuation after the abbreviation
    "Is it a U.S.? I mean federal",
    "Was a congressional bill filed?",
    "Is this federally funded legislation?",
    "what does the us bill do?",          # lower-case country abbreviation before 'bill'
    "us hr 1 vote",
])
def test_a_federal_bill_is_still_recognised(message):
    assert guess(None, message) == "US"


def test_a_named_state_still_wins():
    assert guess(None, "What is the status of Texas HB 5?") == "TX"
    assert guess(None, "status of VA HB 2724") == "VA"
    assert guess(None, "Was Texas HB 5 discussed in Congress?") == "TX"  # a state beats a federal word


# VOTEBOT-28: a two-letter English word before a bill number is not a state, and "West Virginia" is not Virginia
@pytest.mark.parametrize("message", [
    "Who voted in HB 7089?",
    "What changed in HB 7089 between versions?",
    "Is it or HB 5 better?",
    "Tell me about me HB 3",
    "What is hi HB 4?",
    "ok HB 9 passed?",
])
def test_a_lower_case_word_before_a_bill_number_is_not_a_state(message):
    assert guess(None, message) is None


@pytest.mark.parametrize("message, expected", [
    ("VA HB 2724", "VA"),
    ("What is the status of FL SB 4?", "FL"),
    ("va hb 2724", None),            # a code in lower case is not read as a state (the page's own state is used)
    ("IN HB 7089 status", "IN"),     # written in capitals it is a code
    ("What about West Virginia HB 12?", "WV"),
    ("Virginia HB 3", "VA"),
    ("virginia hb 3", "VA"),
    ("Ohio HB 5", "OH"),
    ("the remaining indiana bills", "IN"),
    ("the remaining bills", None),   # no state inside "remaining"
    ("VA hb 3", "VA"),               # a capital code with a lower-case bill prefix
    ("West-Virginia HB 12", "WV"),   # a hyphen, repeated spaces or a line break inside a two-word name
    ("West  Virginia HB 12", "WV"),
    ("West\nVirginia HB 12", "WV"),
    ("New Mexico HB 2", "NM"),
    ("north carolina sb 5", "NC"),
    ("South Dakota HB 1", "SD"),
    ("Virginia's HB 3", "VA"),       # a possessive and a comma are not part of the name
    ("Virginia, HB 3", "VA"),
    ("(Virginia) HB 3", "VA"),
    ("VirginiaBeach HB 3", None),    # a state name inside a longer word is not a state
])
def test_state_codes_in_capitals_and_state_names_as_whole_words(message, expected):
    assert guess(None, message) == expected
