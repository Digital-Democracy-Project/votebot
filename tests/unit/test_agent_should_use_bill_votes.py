"""Tests for _should_use_bill_votes_tool() conversation-history extension (Fix F3).

Covers the 8-case test matrix from PLAN-quick-action-buttons Fix F3.
The function is exercised directly without constructing a full VoteBotAgent —
it only depends on self.settings.bill_votes_tool_enabled and self._extract_bill_from_text.
"""

from types import SimpleNamespace

import pytest

from votebot.core.agent import VoteBotAgent


@pytest.fixture
def agent():
    """Build a minimal VoteBotAgent that can call _should_use_bill_votes_tool.

    Bypasses __init__ to avoid constructing LLM/Pinecone/Webflow services.
    Only attributes the method actually touches are set.
    """
    a = VoteBotAgent.__new__(VoteBotAgent)
    a.settings = SimpleNamespace(
        bill_votes_tool_enabled=True,
        bill_votes_rag_confidence_threshold=0.4,
    )
    return a


@pytest.fixture
def bill_page_context():
    return SimpleNamespace(type="bill", id="HR 7147")


@pytest.fixture
def general_page_context():
    return SimpleNamespace(type="general", id=None)


# Case 1: Happy path — current message references a bill in user history.
def test_happy_path_user_mentioned_bill_then_followup(agent, general_page_context):
    history = [
        {"role": "user", "content": "Tell me about HR 7147"},
        {"role": "assistant", "content": "HR 7147 is a DHS appropriations bill..."},
    ]
    assert agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="is it still active?",
        page_context=general_page_context,
        conversation_history=history,
    ) is True


# Case 2: Multi-bill history — most recent USER mention wins.
def test_multi_bill_history_most_recent_user_mention(agent, general_page_context):
    history = [
        {"role": "user", "content": "Tell me about HR 7147"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "What about HB 1234?"},
        {"role": "assistant", "content": "..."},
    ]
    assert agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="what's the status?",
        page_context=general_page_context,
        conversation_history=history,
    ) is True


# Case 3: No bill in conversation, no bill in current message — does NOT fire.
def test_no_bill_anywhere_does_not_fire(agent, general_page_context):
    history = [
        {"role": "user", "content": "Hi there"},
        {"role": "assistant", "content": "Hello! How can I help?"},
    ]
    # Status keywords alone on a general page should not fire — no bill resolvable.
    assert agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="what's the status?",
        page_context=general_page_context,
        conversation_history=history,
    ) is False


# Case 4: Bill mentioned ONLY in bot response, never in user message — does NOT fire.
# This is the false-positive case the user-mention dominance guard prevents.
def test_bill_only_in_bot_message_does_not_fire(agent, general_page_context):
    history = [
        {"role": "user", "content": "tell me about immigration bills"},
        {"role": "assistant", "content": "Some recent bills include HR 7147 and HB 1234..."},
    ]
    assert agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="is it active?",
        page_context=general_page_context,
        conversation_history=history,
    ) is False


# Case 4b: Bill mentioned in user message AND bot response — fires.
def test_bill_in_user_and_bot_message_fires(agent, general_page_context):
    history = [
        {"role": "user", "content": "tell me about HR 7147"},
        {"role": "assistant", "content": "HR 7147 is similar to other bills like HB 1234..."},
    ]
    assert agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="is it active?",
        page_context=general_page_context,
        conversation_history=history,
    ) is True


# Case 5: Bill mentioned beyond 6-turn window — does NOT fire.
def test_bill_beyond_6_turn_window_does_not_fire(agent, general_page_context):
    history = [
        {"role": "user", "content": "Tell me about HR 7147"},  # turn 1 — out of window
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "what else"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "go on"},   # bills from this point onward
        {"role": "assistant", "content": "..."},  # are within the 6-turn window
    ]
    # Last 6 turns: turns 3-8, none mention a bill
    assert agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="what's the status?",
        page_context=general_page_context,
        conversation_history=history,
    ) is False


# Case 6: Ambiguous reference (no bill prefix) — does NOT fire.
def test_ambiguous_reference_does_not_fire(agent, general_page_context):
    history = [
        {"role": "user", "content": "I'm interested in resolution 7147"},
        {"role": "assistant", "content": "..."},
    ]
    assert agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="is it active?",
        page_context=general_page_context,
        conversation_history=history,
    ) is False


# Case 7: Backward-compat — call without conversation_history kwarg works.
def test_backward_compat_no_conversation_history_arg(agent, general_page_context):
    # Call without conversation_history — should use default None and behave as before.
    result = agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="hello",
        page_context=general_page_context,
    )
    # Hello message should not trigger, no bill identifier, no history scan.
    assert result is False


# Case 7b: Backward-compat — vote keyword still triggers without conversation_history.
def test_backward_compat_vote_keyword_still_fires(agent, general_page_context):
    result = agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="how did the senate vote?",
        page_context=general_page_context,
    )
    assert result is True


# Sanity check: tool_enabled=False short-circuits everything.
def test_tool_enabled_false_short_circuits(agent, general_page_context):
    agent.settings.bill_votes_tool_enabled = False
    history = [{"role": "user", "content": "tell me about HR 7147"}]
    assert agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="is it active?",
        page_context=general_page_context,
        conversation_history=history,
    ) is False


# Sanity check: bill identifier in current message fires regardless of history.
def test_bill_id_in_current_message_fires(agent, general_page_context):
    assert agent._should_use_bill_votes_tool(
        rag_confidence=0.7,
        message="tell me about HR 1234",
        page_context=general_page_context,
        conversation_history=None,
    ) is True
