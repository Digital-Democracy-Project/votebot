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
])
def test_a_federal_bill_is_still_recognised(message):
    assert guess(None, message) == "US"


def test_a_named_state_still_wins():
    assert guess(None, "What is the status of Texas HB 5?") == "TX"
    assert guess(None, "status of VA HB 2724") == "VA"
