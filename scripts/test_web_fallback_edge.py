#!/usr/bin/env python3
"""Test web search fallback for queries that RAG definitely can't answer."""

import asyncio
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from votebot.config import get_settings
from votebot.api.schemas.chat import PageContext
from votebot.core.agent import VoteBotAgent


async def test_edge_case_queries():
    """Test queries that are impossible for RAG to answer."""
    print("\n" + "=" * 60)
    print("EDGE CASE WEB SEARCH FALLBACK TEST")
    print("=" * 60)

    settings = get_settings()
    print(f"\nConfiguration:")
    print(f"  Model: {settings.openai_model}")
    print(f"  Web search enabled: {settings.web_search_enabled}")
    print(f"  Web search on low confidence: {settings.web_search_on_low_confidence}")
    print(f"  Confidence threshold: {settings.web_search_confidence_threshold}")

    agent = VoteBotAgent(settings)

    # Test queries that RAG definitely can't answer
    test_cases = [
        {
            "query": "What is the weather forecast for tomorrow in Washington DC?",
            "page_type": "general",
            "description": "Weather query - RAG has no weather data",
        },
        {
            "query": "Who won the Super Bowl in 2025?",
            "page_type": "general",
            "description": "Sports query - RAG has no sports data",
        },
        {
            "query": "What are the stock prices for Apple today?",
            "page_type": "general",
            "description": "Stock prices - RAG has no financial data",
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
                session_id=f"test-edge-{i}",
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
                "retrieval_count": result.retrieval_count,
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
                "retrieval_count": 0,
                "confidence": 0,
                "success": False,
            })

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    web_search_triggered = sum(1 for r in results if r["web_search_used"])

    for r in results:
        status = "PASSED" if r["success"] else "FAILED"
        web = "YES" if r["web_search_used"] else "NO"
        print(f"  {r['test']}")
        print(f"    Status: {status}, RAG: {r['retrieval_count']}, Web Search: {web}, Confidence: {r['confidence']:.2f}")

    print(f"\n  Web search triggered: {web_search_triggered}/{len(results)} tests")

    return all(r["success"] for r in results)


if __name__ == "__main__":
    success = asyncio.run(test_edge_case_queries())
    sys.exit(0 if success else 1)
