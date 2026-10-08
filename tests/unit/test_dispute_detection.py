"""VOTEBOT-29: an ordinary question is not a dispute.

`_is_dispute_or_correction` used to match phrases as substrings and had bare words ("is the", "became",
"was elected") in its list, so "What is the latest status..." counted as the user correcting the bot. A false
dispute runs the vote verification (a warning in the log), forces a web search and adds more lookups.
"""

import pytest

from votebot.core.agent import VoteBotAgent

is_dispute = VoteBotAgent._is_dispute_or_correction


@pytest.mark.parametrize("message", [
    "What is the latest status and vote history for HB 7089?",
    "What is the latest status and vote history for Transparency in Health and Human Services?",
    "What is the status of HB 7089?",
    "This is the bill about education funding",
    "Who is the sponsor?",
    "Is the bill still in committee?",
    "What became of this bill?",
    "Who was elected in 2024?",
    "Which organizations have confirmed positions on this bill?",
    "Read me the bulletin",
    "Tell us about the theory behind this",
    "Summarize this bill",
    "How did the vote on this bill go?",
])
def test_an_ordinary_question_is_not_a_dispute(message):
    assert is_dispute(None, message) is False


@pytest.mark.parametrize("message", [
    "that's wrong",
    "That’s wrong, check again",           # a typed curly apostrophe
    "Are you sure?",
    "Please double check the vote",
    "Can you verify that?",
    "Can you confirm that?",
    "No, that cannot be right",
    "She is a senator now",
    "He's the governor",
    "actually, he is the governor",
    "They became senators last year",
    "she was elected in 2024",
    "I think she is currently a representative",
])
def test_a_real_dispute_or_correction_is_still_one(message):
    assert is_dispute(None, message) is True
