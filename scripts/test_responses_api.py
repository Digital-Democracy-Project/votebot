#!/usr/bin/env python3
"""Test script for OpenAI Responses API with web search fallback."""

import asyncio
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from votebot.config import get_settings
from votebot.services.llm import LLMService


async def test_basic_completion():
    """Test basic completion without web search."""
    print("\n" + "=" * 60)
    print("TEST 1: Basic Completion (No Web Search)")
    print("=" * 60)

    settings = get_settings()
    llm = LLMService(settings)

    messages = [{"role": "user", "content": "What is the Digital Democracy Project?"}]
    system_prompt = "You are a helpful assistant for the Digital Democracy Project."

    try:
        response = await llm.complete(
            messages=messages,
            system_prompt=system_prompt,
            enable_web_search=False,
        )

        print(f"\nModel: {response.model}")
        print(f"Tokens used: {response.tokens_used}")
        print(f"Web search used: {response.web_search_used}")
        print(f"Response ID: {response.response_id}")
        print(f"\nResponse:\n{response.content[:500]}...")
        return True
    except Exception as e:
        print(f"\nERROR: {e}")
        return False


async def test_web_search_completion():
    """Test completion with web search enabled."""
    print("\n" + "=" * 60)
    print("TEST 2: Completion WITH Web Search")
    print("=" * 60)

    settings = get_settings()
    llm = LLMService(settings)

    # Use a query that would benefit from current information
    messages = [{"role": "user", "content": "What bills related to housing affordability are being considered in Congress right now in 2025?"}]
    system_prompt = "You are a helpful assistant for the Digital Democracy Project. When asked about current legislation, use web search to find the most up-to-date information."

    try:
        response = await llm.complete(
            messages=messages,
            system_prompt=system_prompt,
            enable_web_search=True,
        )

        print(f"\nModel: {response.model}")
        print(f"Tokens used: {response.tokens_used}")
        print(f"Web search used: {response.web_search_used}")
        print(f"Web citations count: {len(response.web_citations)}")
        print(f"Response ID: {response.response_id}")

        if response.web_citations:
            print("\nWeb Citations:")
            for i, citation in enumerate(response.web_citations[:5], 1):
                print(f"  {i}. {citation.title}")
                print(f"     URL: {citation.url}")

        print(f"\nResponse:\n{response.content[:800]}...")
        return True
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_fallback_behavior():
    """Test the complete_with_fallback method."""
    print("\n" + "=" * 60)
    print("TEST 3: Fallback Behavior (Low RAG Confidence)")
    print("=" * 60)

    settings = get_settings()
    llm = LLMService(settings)

    messages = [{"role": "user", "content": "What is the current status of climate legislation in California?"}]
    system_prompt = "You are a helpful assistant. Answer based on the most current information available."

    try:
        # Test with low RAG confidence (should trigger web search)
        print("\nWith RAG confidence 0.2 (below threshold 0.5):")
        response = await llm.complete_with_fallback(
            messages=messages,
            system_prompt=system_prompt,
            rag_confidence=0.2,  # Low confidence should trigger web search
        )

        print(f"  Web search used: {response.web_search_used}")
        print(f"  Citations count: {len(response.web_citations)}")

        # Test with high RAG confidence (should NOT trigger web search)
        print("\nWith RAG confidence 0.8 (above threshold 0.5):")
        response2 = await llm.complete_with_fallback(
            messages=messages,
            system_prompt=system_prompt,
            rag_confidence=0.8,  # High confidence should NOT trigger web search
        )

        print(f"  Web search used: {response2.web_search_used}")
        print(f"  Citations count: {len(response2.web_citations)}")

        return True
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_health_check():
    """Test the health check method."""
    print("\n" + "=" * 60)
    print("TEST 4: Health Check")
    print("=" * 60)

    settings = get_settings()
    llm = LLMService(settings)

    try:
        result = await llm.health_check()
        print(f"\nHealth check passed: {result}")
        return True
    except Exception as e:
        print(f"\nHealth check failed: {e}")
        return False


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("OPENAI RESPONSES API TEST SUITE")
    print("=" * 60)

    settings = get_settings()
    print(f"\nConfiguration:")
    print(f"  Model: {settings.openai_model}")
    print(f"  Web search enabled: {settings.web_search_enabled}")
    print(f"  Web search context size: {settings.web_search_context_size}")
    print(f"  Web search on low confidence: {settings.web_search_on_low_confidence}")
    print(f"  Confidence threshold: {settings.web_search_confidence_threshold}")

    results = {}

    # Run tests
    results["health_check"] = await test_health_check()
    results["basic_completion"] = await test_basic_completion()
    results["web_search"] = await test_web_search_completion()
    results["fallback"] = await test_fallback_behavior()

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, passed_flag in results.items():
        status = "PASSED" if passed_flag else "FAILED"
        print(f"  {test_name}: {status}")

    print(f"\nTotal: {passed}/{total} tests passed")

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
