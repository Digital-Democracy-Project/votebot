#!/usr/bin/env python3
"""Test script for bill votes tool integration."""

import asyncio
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from votebot.config import get_settings
from votebot.services.bill_votes import BillVotesService
from votebot.services.llm import LLMService

# Clear settings cache
get_settings.cache_clear()
settings = get_settings()


async def test_bill_votes_service():
    """Test BillVotesService directly."""
    print("=" * 60)
    print("Testing BillVotesService directly")
    print("=" * 60)

    service = BillVotesService(settings)

    # Test with a known bill
    print("\nFetching votes for FL HB 1 (2025 session)...")
    result = await service.get_bill_votes(
        jurisdiction="fl",
        session="2025",
        bill_identifier="HB1",
    )

    if result:
        print(f"  Bill: {result.bill_identifier}")
        print(f"  Title: {result.title}")
        print(f"  Jurisdiction: {result.jurisdiction}")
        print(f"  Cached: {result.cached}")
        print(f"  Number of votes: {len(result.votes)}")
        if result.votes:
            vote = result.votes[0]
            print(f"  First vote: {vote.motion_text}")
            print(f"    Result: {vote.result} (Yes: {vote.yes_count}, No: {vote.no_count})")
    else:
        print("  No result returned")


async def test_llm_tool_building():
    """Test that LLM service builds tools correctly."""
    print("\n" + "=" * 60)
    print("Testing LLM tool building")
    print("=" * 60)

    llm = LLMService(settings)

    # Test tool building with bill votes enabled
    tools = llm._build_tools(enable_web_search=False, enable_bill_votes=True)

    if tools:
        print(f"\nTools built: {len(tools)}")
        for tool in tools:
            print(f"  - Type: {tool.get('type')}")
            if tool.get('type') == 'function':
                print(f"    Name: {tool.get('name')}")
                print(f"    Description: {tool.get('description', '')[:100]}...")
    else:
        print("\nNo tools built")

    # Test with both tools enabled
    tools = llm._build_tools(enable_web_search=True, enable_bill_votes=True)
    print(f"\nWith both tools enabled: {len(tools) if tools else 0} tools")


async def test_full_integration():
    """Test full integration with agent."""
    print("\n" + "=" * 60)
    print("Testing full agent integration")
    print("=" * 60)

    from votebot.core.agent import VoteBotAgent
    from votebot.api.schemas.chat import PageContext

    agent = VoteBotAgent(settings)

    # Test a vote-related query
    print("\nProcessing vote query...")
    result = await agent.process_message(
        message="How did legislators vote on Florida HB 1 in 2025?",
        session_id="test-session-123",
        page_context=PageContext(type="general"),
    )

    print(f"  Response length: {len(result.response)} chars")
    print(f"  Confidence: {result.confidence:.2f}")
    print(f"  Bill votes tool used: {result.bill_votes_tool_used}")
    if result.bill_votes_result:
        print(f"  Bill votes result found: {result.bill_votes_result.found}")
        print(f"  Jurisdiction: {result.bill_votes_result.jurisdiction}")
        print(f"  Bill: {result.bill_votes_result.bill_identifier}")

    print(f"\nResponse preview:")
    print(f"  {result.response[:500]}..." if len(result.response) > 500 else f"  {result.response}")


async def main():
    """Run all tests."""
    try:
        await test_bill_votes_service()
        await test_llm_tool_building()
        await test_full_integration()
        print("\n" + "=" * 60)
        print("All tests completed!")
        print("=" * 60)
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
