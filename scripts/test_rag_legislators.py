#!/usr/bin/env python3
"""
Test RAG retrieval for legislator pages.

This script tests that VoteBot can correctly answer questions about legislators
when provided with legislator page context, including:
- Voting record and accountability
- DDP Accountability Score
- Sponsored bills
- Contact information
- Party and chamber information

Usage:
    python scripts/test_rag_legislators.py [options]

Options:
    --api-url URL      API base URL (default: http://localhost:8000)
    --sample-size N    Number of legislators to test (default: 10)
    --verbose          Show full responses
"""

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
from votebot.config import get_settings

# Clear settings cache
get_settings.cache_clear()
settings = get_settings()


@dataclass
class TestResult:
    """Result of a single legislator test."""
    legislator_name: str
    legislator_id: str
    jurisdiction: str
    question: str
    response: str
    confidence: float
    has_citations: bool
    latency: float
    success: bool
    error: Optional[str] = None


# Test questions for legislator pages
LEGISLATOR_QUESTIONS = [
    {
        "question": "What is this legislator's voting record?",
        "keywords": ["vote", "record", "bill", "support", "oppose"],
        "description": "Voting record query",
    },
    {
        "question": "What is their DDP accountability score?",
        "keywords": ["score", "accountability", "ddp", "rating", "%"],
        "description": "DDP score query",
    },
    {
        "question": "How did they vote on education bills?",
        "keywords": ["education", "school", "vote", "bill"],
        "description": "Topic-specific voting query",
    },
    {
        "question": "What bills have they sponsored?",
        "keywords": ["sponsor", "bill", "legislation", "introduced"],
        "description": "Sponsorship query",
    },
    {
        "question": "How do I contact this legislator?",
        "keywords": ["contact", "email", "phone", "office", "address"],
        "description": "Contact info query",
    },
    {
        "question": "What party does this legislator belong to?",
        "keywords": ["party", "republican", "democrat", "democratic"],
        "description": "Party affiliation query",
    },
    {
        "question": "What district does this legislator represent?",
        "keywords": ["district", "represent", "area", "constituent"],
        "description": "District query",
    },
    {
        "question": "Tell me about this legislator's positions on key issues.",
        "keywords": ["position", "issue", "stance", "policy", "vote"],
        "description": "Issue positions query",
    },
]


async def fetch_sample_legislators(sample_size: int = 10) -> list[dict]:
    """
    Fetch sample legislators from Webflow CMS.

    Args:
        sample_size: Number of legislators to sample

    Returns:
        List of legislator items from Webflow
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {
            "Authorization": f"Bearer {settings.webflow_api_key.get_secret_value()}",
            "accept": "application/json",
        }

        collection_id = settings.webflow_legislators_collection_id
        if not collection_id:
            print("ERROR: No legislators collection ID configured")
            return []

        all_legislators = []
        offset = 0

        # Fetch all legislators
        while True:
            response = await client.get(
                f"https://api.webflow.com/v2/collections/{collection_id}/items",
                headers=headers,
                params={"limit": 100, "offset": offset},
            )

            if response.status_code != 200:
                print(f"Error fetching legislators: {response.status_code}")
                break

            data = response.json()
            items = data.get("items", [])

            if not items:
                break

            all_legislators.extend(items)
            offset += 100

            if len(items) < 100:
                break

        # Filter to legislators with OpenStates ID
        legislators_with_id = [
            leg for leg in all_legislators
            if leg.get("fieldData", {}).get("openstatesid")
        ]

        print(f"Found {len(legislators_with_id)} legislators with OpenStates IDs")

        # Sample from different jurisdictions if possible
        if len(legislators_with_id) <= sample_size:
            return legislators_with_id

        # Group by jurisdiction and sample
        by_jurisdiction = {}
        for leg in legislators_with_id:
            j = leg.get("fieldData", {}).get("jurisdiction", "unknown")
            if j not in by_jurisdiction:
                by_jurisdiction[j] = []
            by_jurisdiction[j].append(leg)

        # Sample proportionally
        sampled = []
        remaining = sample_size
        jurisdictions = list(by_jurisdiction.keys())

        for j in jurisdictions:
            n = max(1, remaining // len(jurisdictions))
            n = min(n, len(by_jurisdiction[j]))
            sampled.extend(random.sample(by_jurisdiction[j], n))
            remaining -= n
            jurisdictions = [jur for jur in jurisdictions if jur != j]

        return sampled[:sample_size]


async def query_legislator_context(
    legislator_id: str,
    jurisdiction: str,
    question: str,
    api_url: str,
    api_key: str,
) -> dict:
    """
    Send a query with legislator page context.

    Args:
        legislator_id: OpenStates person ID
        jurisdiction: State code
        question: Question to ask
        api_url: API base URL
        api_key: API key

    Returns:
        API response dict
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{api_url}/votebot/v1/chat",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "message": question,
                "session_id": f"test-leg-{random.randint(10000, 99999)}",
                "human_active": False,
                "page_context": {
                    "type": "legislator",
                    "id": legislator_id,
                    "jurisdiction": jurisdiction,
                },
            },
        )

        if response.status_code != 200:
            return {
                "error": f"HTTP {response.status_code}: {response.text[:200]}",
                "success": False,
            }

        data = response.json()
        data["success"] = True
        return data


async def run_legislator_test(
    legislator: dict,
    api_url: str,
    api_key: str,
    verbose: bool = False,
) -> list[TestResult]:
    """
    Run all test questions for a single legislator.

    Args:
        legislator: Webflow legislator item
        api_url: API base URL
        api_key: API key
        verbose: Whether to print full responses

    Returns:
        List of TestResult objects
    """
    fields = legislator.get("fieldData", {})
    name = fields.get("name", "Unknown")
    legislator_id = fields.get("openstatesid", "")
    jurisdiction = fields.get("jurisdiction", "US")

    # If jurisdiction is a reference ID, try to extract state code
    if len(jurisdiction) > 2:
        jurisdiction = "US"  # Default if we can't resolve

    results = []

    for q_info in LEGISLATOR_QUESTIONS:
        question = q_info["question"]
        start_time = time.time()

        try:
            response = await query_legislator_context(
                legislator_id=legislator_id,
                jurisdiction=jurisdiction,
                question=question,
                api_url=api_url,
                api_key=api_key,
            )

            latency = time.time() - start_time

            if not response.get("success"):
                results.append(TestResult(
                    legislator_name=name,
                    legislator_id=legislator_id,
                    jurisdiction=jurisdiction,
                    question=question,
                    response="",
                    confidence=0.0,
                    has_citations=False,
                    latency=latency,
                    success=False,
                    error=response.get("error", "Unknown error"),
                ))
                continue

            response_text = response.get("response", "")
            confidence = response.get("confidence", 0.0)
            citations = response.get("citations", [])

            results.append(TestResult(
                legislator_name=name,
                legislator_id=legislator_id,
                jurisdiction=jurisdiction,
                question=question,
                response=response_text,
                confidence=confidence,
                has_citations=len(citations) > 0,
                latency=latency,
                success=True,
            ))

            if verbose:
                print(f"\n  Q: {question}")
                print(f"  A: {response_text[:200]}...")
                print(f"  Confidence: {confidence:.2f}, Citations: {len(citations)}")

        except Exception as e:
            results.append(TestResult(
                legislator_name=name,
                legislator_id=legislator_id,
                jurisdiction=jurisdiction,
                question=question,
                response="",
                confidence=0.0,
                has_citations=False,
                latency=time.time() - start_time,
                success=False,
                error=str(e),
            ))

    return results


async def main():
    """Run legislator RAG tests."""
    parser = argparse.ArgumentParser(
        description="Test RAG retrieval for legislator pages",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="API base URL",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=10,
        help="Number of legislators to test",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show full responses",
    )

    args = parser.parse_args()
    api_key = settings.api_key.get_secret_value()

    print("=" * 70)
    print("VOTEBOT LEGISLATOR RAG TEST")
    print("=" * 70)

    # Fetch sample legislators
    print(f"\nFetching {args.sample_size} sample legislators from Webflow...")
    legislators = await fetch_sample_legislators(args.sample_size)

    if not legislators:
        print("No legislators found. Ensure legislators are synced to Webflow.")
        sys.exit(1)

    print(f"Selected {len(legislators)} legislators for testing")

    # Run tests
    print("\n" + "=" * 70)
    print("RUNNING TESTS")
    print("=" * 70)

    all_results = []

    for i, legislator in enumerate(legislators):
        name = legislator.get("fieldData", {}).get("name", "Unknown")
        print(f"\n[{i + 1}/{len(legislators)}] Testing: {name}")

        results = await run_legislator_test(
            legislator,
            args.api_url,
            api_key,
            verbose=args.verbose,
        )
        all_results.extend(results)

        # Summary for this legislator
        successful = [r for r in results if r.success]
        if successful:
            avg_conf = sum(r.confidence for r in successful) / len(successful)
            citations_pct = sum(1 for r in successful if r.has_citations) / len(successful) * 100
            print(f"  {len(successful)}/{len(results)} succeeded, "
                  f"avg confidence: {avg_conf:.2f}, citations: {citations_pct:.0f}%")
        else:
            print(f"  All {len(results)} tests failed")

    # Overall summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)

    successful = [r for r in all_results if r.success]
    failed = [r for r in all_results if not r.success]

    print(f"\nTotal queries: {len(all_results)}")
    print(f"Successful: {len(successful)} ({len(successful)/len(all_results)*100:.1f}%)")
    print(f"Failed: {len(failed)} ({len(failed)/len(all_results)*100:.1f}%)")

    if successful:
        all_confidences = [r.confidence for r in successful]
        all_latencies = [r.latency for r in successful]
        citations_count = sum(1 for r in successful if r.has_citations)

        print(f"\nPerformance Metrics:")
        print(f"  Avg confidence: {sum(all_confidences)/len(all_confidences):.2f}")
        print(f"  High confidence (>0.7): {sum(1 for c in all_confidences if c > 0.7)/len(all_confidences)*100:.1f}%")
        print(f"  With citations: {citations_count/len(successful)*100:.1f}%")
        print(f"  Avg latency: {sum(all_latencies)/len(all_latencies):.2f}s")

    # Results by question type
    print(f"\nResults by Question Type:")
    by_question = {}
    for r in successful:
        q = r.question
        if q not in by_question:
            by_question[q] = []
        by_question[q].append(r)

    for q, q_results in by_question.items():
        avg_conf = sum(r.confidence for r in q_results) / len(q_results)
        citations_pct = sum(1 for r in q_results if r.has_citations) / len(q_results) * 100
        print(f"  \"{q[:40]}...\"")
        print(f"    {len(q_results)} tests, conf: {avg_conf:.2f}, citations: {citations_pct:.0f}%")

    # Results by jurisdiction
    print(f"\nResults by Jurisdiction:")
    by_jurisdiction = {}
    for r in successful:
        j = r.jurisdiction
        if j not in by_jurisdiction:
            by_jurisdiction[j] = []
        by_jurisdiction[j].append(r)

    for j, j_results in sorted(by_jurisdiction.items()):
        avg_conf = sum(r.confidence for r in j_results) / len(j_results)
        print(f"  {j}: {len(j_results)} queries, avg confidence: {avg_conf:.2f}")

    # Save detailed results
    output_file = Path(__file__).parent / "legislator_test_results.json"
    with open(output_file, "w") as f:
        json.dump([{
            "legislator_name": r.legislator_name,
            "legislator_id": r.legislator_id,
            "jurisdiction": r.jurisdiction,
            "question": r.question,
            "response": r.response,
            "confidence": r.confidence,
            "has_citations": r.has_citations,
            "latency": r.latency,
            "success": r.success,
            "error": r.error,
        } for r in all_results], f, indent=2)

    print(f"\nDetailed results saved to: {output_file}")

    # Show sample responses
    if successful:
        print("\n" + "=" * 70)
        print("SAMPLE RESPONSES")
        print("=" * 70)

        samples = random.sample(successful, min(5, len(successful)))
        for r in samples:
            print(f"\n--- {r.legislator_name} ({r.jurisdiction}) ---")
            print(f"Q: {r.question}")
            response_preview = r.response[:300] + "..." if len(r.response) > 300 else r.response
            print(f"A: {response_preview}")
            print(f"[Confidence: {r.confidence:.2f}, Citations: {r.has_citations}]")

    # Exit with error code if too many failures
    failure_rate = len(failed) / len(all_results)
    if failure_rate > 0.5:
        print(f"\nWARNING: High failure rate ({failure_rate*100:.0f}%)")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
