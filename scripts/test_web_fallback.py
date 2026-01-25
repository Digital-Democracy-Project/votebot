#!/usr/bin/env python3
"""Test web search fallback when RAG returns no usable results."""

import asyncio
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from votebot.config import get_settings
from votebot.api.schemas.chat import PageContext
from votebot.core.agent import VoteBotAgent


async def test_web_fallback():
    """Test that web search kicks in when RAG returns no usable results."""
    print("\n" + "=" * 60)
    print("WEB SEARCH FALLBACK TEST")
    print("=" * 60)

    settings = get_settings()
    print(f"\nConfiguration:")
    print(f"  Model: {settings.openai_model}")
    print(f"  Web search enabled: {settings.web_search_enabled}")
    print(f"  Web search on low confidence: {settings.web_search_on_low_confidence}")
    print(f"  Confidence threshold: {settings.web_search_confidence_threshold}")

    agent = VoteBotAgent(settings)

    # Test queries that are unlikely to have good RAG matches but would benefit from web search
    test_cases = [
        {
            "query": "What are the latest news about California climate legislation in 2025?",
            "page_type": "general",
            "description": "Current events query - should trigger web search",
        },
        {
            "query": "What is the current status of federal housing bills in Congress?",
            "page_type": "general",
            "description": "Current legislative status - should trigger web search",
        },
        {
            "query": "What is Senate Bill 1234 about?",
            "page_type": "bill",
            "description": "Generic bill query - may or may not have RAG match",
        },
    ]

    results = []

    for i, test in enumerate(test_cases, 1):
        print(f"\n{'='*60}")
        print(f"TEST {i}: {test['description']}")
        print(f"{'='*60}")
        print(f"Query: {test['query']}")

        try:
            page_context = PageContext(
                type=test["page_type"],
                jurisdiction=None,
            )

            result = await agent.process_message(
                message=test["query"],
                session_id=f"test-fallback-{i}",
                page_context=page_context,
            )

            print(f"\nResults:")
            print(f"  RAG retrieval count: {result.retrieval_count}")
            print(f"  Web search used: {result.web_search_used}")
            print(f"  Confidence: {result.confidence:.2f}")
            print(f"  Citations: {len(result.citations)}")

            if result.web_citations:
                print(f"  Web citations: {len(result.web_citations)}")
                for citation in result.web_citations[:3]:
                    print(f"    - {citation.title[:60]}...")
                    print(f"      URL: {citation.url}")

            print(f"\nResponse preview:")
            print(f"  {result.response[:300]}...")

            results.append({
                "test": test["description"],
                "web_search_used": result.web_search_used,
                "confidence": result.confidence,
                "success": True,
            })

        except Exception as e:
            print(f"\nERROR: {e}")
            import traceback
            traceback.print_exc()
            results.append({
                "test": test["description"],
                "web_search_used": False,
                "confidence": 0,
                "success": False,
            })

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    for r in results:
        status = "PASSED" if r["success"] else "FAILED"
        web = "YES" if r["web_search_used"] else "NO"
        print(f"  {r['test']}")
        print(f"    Status: {status}, Web Search: {web}, Confidence: {r['confidence']:.2f}")

    return all(r["success"] for r in results)


if __name__ == "__main__":
    success = asyncio.run(test_web_fallback())
    sys.exit(0 if success else 1)
