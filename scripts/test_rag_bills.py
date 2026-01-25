#!/usr/bin/env python3
"""
Test script for bill RAG queries in VoteBot.

This script tests the RAG pipeline for bill-related queries:
1. Fetches sample bills from Webflow
2. Generates test queries for each bill
3. Runs queries through the RAG pipeline via the API
4. Reports success rates, confidence, and citation metrics

Usage:
    python scripts/test_rag_bills.py [options]

Options:
    --api-url URL      API base URL (default: http://localhost:8000)
    --limit N          Number of bills to test (default: 10)
    --log-level LEVEL  Logging level (default: WARNING)
"""

import argparse
import asyncio
import json
import random
import sys
from datetime import datetime
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx
from votebot.config import get_settings

# Clear settings cache to ensure fresh env vars are loaded
get_settings.cache_clear()

from votebot.ingestion.metadata import MetadataExtractor
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.utils.logging import setup_logging

import structlog

logger = structlog.get_logger()


# Test questions for bill queries
BILL_QUESTIONS = [
    "What is {name}?",
    "What organizations support {name}?",
    "What organizations oppose {name}?",
    "What are the arguments for {name}?",
    "What are the arguments against {name}?",
    "What is the status of {name}?",
    "Tell me about the debate around {name}.",
]


async def fetch_sample_bills(limit: int = 10) -> list[dict]:
    """
    Fetch sample bills from Webflow for testing.

    Args:
        limit: Number of bills to fetch

    Returns:
        List of bill dicts with name and metadata
    """
    settings = get_settings()
    metadata_extractor = MetadataExtractor()
    webflow = WebflowSource(settings, metadata_extractor)

    bills = []
    async for doc in webflow.fetch(
        collection_id=webflow.bills_collection_id,
        limit=0,  # Fetch all, then sample
        include_pdfs=False,
    ):
        bills.append({
            "name": doc.metadata.title,
            "webflow_id": doc.metadata.extra.get("webflow_id", ""),
            "bill_prefix": doc.metadata.extra.get("bill_prefix", ""),
            "bill_number": doc.metadata.extra.get("bill_number", ""),
            "slug": doc.metadata.extra.get("slug", ""),
            "status": doc.metadata.extra.get("status", ""),
            "supporting_orgs_count": doc.metadata.extra.get("supporting_orgs_count", 0),
            "opposing_orgs_count": doc.metadata.extra.get("opposing_orgs_count", 0),
        })

    # Prioritize bills with organization positions for more interesting tests
    bills_with_positions = [
        b for b in bills
        if b["supporting_orgs_count"] > 0 or b["opposing_orgs_count"] > 0
    ]
    bills_without_positions = [
        b for b in bills
        if b["supporting_orgs_count"] == 0 and b["opposing_orgs_count"] == 0
    ]

    # Mix: 70% with positions, 30% without
    selected = []
    with_positions_count = min(int(limit * 0.7), len(bills_with_positions))
    without_positions_count = min(limit - with_positions_count, len(bills_without_positions))

    if bills_with_positions:
        selected.extend(random.sample(bills_with_positions, with_positions_count))
    if bills_without_positions:
        selected.extend(random.sample(bills_without_positions, without_positions_count))

    # If we still need more, fill from whatever is available
    remaining = limit - len(selected)
    if remaining > 0:
        all_remaining = [b for b in bills if b not in selected]
        if all_remaining:
            selected.extend(random.sample(all_remaining, min(remaining, len(all_remaining))))

    return selected[:limit]


async def test_bill_query(
    client: httpx.AsyncClient,
    api_url: str,
    bill: dict,
    question_template: str,
) -> dict:
    """
    Test a single bill query via the API.

    Args:
        client: httpx AsyncClient
        api_url: Base URL for API
        bill: Bill dict
        question_template: Question template with {name} placeholder

    Returns:
        Test result dict
    """
    # Create short name for question (bill prefix + number if available)
    short_name = f"{bill['bill_prefix']} {bill['bill_number']}".strip()
    if not short_name:
        short_name = bill["name"][:50]

    question = question_template.format(name=short_name)
    settings = get_settings()

    start_time = datetime.now()
    try:
        response = await client.post(
            f"{api_url}/votebot/v1/chat",
            json={
                "message": question,
                "session_id": f"test-bill-{bill['webflow_id'][:8]}",
                "human_active": False,
                "page_context": {"type": "general"},
            },
            headers={
                "Authorization": f"Bearer {settings.api_key.get_secret_value()}",
            },
            timeout=60.0,
        )

        latency = (datetime.now() - start_time).total_seconds()

        if response.status_code != 200:
            return {
                "bill_name": bill["name"],
                "bill_id": short_name,
                "question": question,
                "answer": None,
                "confidence": 0,
                "has_citations": False,
                "citation_count": 0,
                "latency": latency,
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}",
            }

        data = response.json()

        return {
            "bill_name": bill["name"],
            "bill_id": short_name,
            "question": question,
            "answer": data.get("message", "")[:500],
            "confidence": data.get("confidence", 0),
            "has_citations": bool(data.get("citations")),
            "citation_count": len(data.get("citations", [])),
            "latency": latency,
            "success": True,
            "error": None,
        }

    except Exception as e:
        latency = (datetime.now() - start_time).total_seconds()
        return {
            "bill_name": bill["name"],
            "bill_id": short_name,
            "question": question,
            "answer": None,
            "confidence": 0,
            "has_citations": False,
            "citation_count": 0,
            "latency": latency,
            "success": False,
            "error": str(e),
        }


async def run_tests(
    api_url: str,
    bills: list[dict],
    questions: list[str],
) -> list[dict]:
    """
    Run all tests for the given bills and questions.

    Args:
        api_url: API base URL
        bills: List of bill dicts
        questions: List of question templates

    Returns:
        List of test result dicts
    """
    results = []
    total_tests = len(bills) * len(questions)
    completed = 0

    async with httpx.AsyncClient() as client:
        for bill in bills:
            bill_results = []
            for question in questions:
                result = await test_bill_query(client, api_url, bill, question)
                bill_results.append(result)
                results.append(result)
                completed += 1

            # Print progress
            success_count = sum(1 for r in bill_results if r["success"])
            avg_confidence = sum(r["confidence"] for r in bill_results) / len(bill_results) if bill_results else 0
            citation_rate = sum(1 for r in bill_results if r["has_citations"]) / len(bill_results) * 100 if bill_results else 0

            bill_id = f"{bill['bill_prefix']} {bill['bill_number']}".strip() or bill["name"][:30]
            print(
                f"[{completed}/{total_tests}] Testing: {bill_id}\n"
                f"  {success_count}/{len(questions)} succeeded, "
                f"avg confidence: {avg_confidence:.2f}, "
                f"citations: {citation_rate:.0f}%"
            )

    return results


def print_summary(results: list[dict]):
    """Print test summary statistics."""
    total = len(results)
    successful = sum(1 for r in results if r["success"])
    failed = total - successful

    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)

    print(f"\nTotal queries: {total}")
    print(f"Successful: {successful} ({successful/total*100:.1f}%)")
    print(f"Failed: {failed} ({failed/total*100:.1f}%)")

    if successful > 0:
        success_results = [r for r in results if r["success"]]

        avg_confidence = sum(r["confidence"] for r in success_results) / len(success_results)
        high_confidence = sum(1 for r in success_results if r["confidence"] > 0.7)
        with_citations = sum(1 for r in success_results if r["has_citations"])
        avg_latency = sum(r["latency"] for r in success_results) / len(success_results)

        print(f"\nPerformance Metrics:")
        print(f"  Avg confidence: {avg_confidence:.2f}")
        print(f"  High confidence (>0.7): {high_confidence/len(success_results)*100:.1f}%")
        print(f"  With citations: {with_citations/len(success_results)*100:.1f}%")
        print(f"  Avg latency: {avg_latency:.2f}s")

        # Results by question type
        print(f"\nResults by Question Type:")
        for question in BILL_QUESTIONS:
            q_results = [r for r in success_results if question.split("{name}")[0] in r["question"]]
            if q_results:
                q_conf = sum(r["confidence"] for r in q_results) / len(q_results)
                q_citations = sum(1 for r in q_results if r["has_citations"]) / len(q_results) * 100
                print(f"  \"{question[:45]}...\"")
                print(f"    {len(q_results)} tests, conf: {q_conf:.2f}, citations: {q_citations:.0f}%")


def print_sample_responses(results: list[dict], count: int = 5):
    """Print sample responses for inspection."""
    print("\n" + "=" * 70)
    print("SAMPLE RESPONSES")
    print("=" * 70)

    # Select diverse samples - try to get org-related questions
    org_questions = [r for r in results if "organizations" in r["question"].lower() and r["success"]]
    other_questions = [r for r in results if "organizations" not in r["question"].lower() and r["success"]]

    samples = []
    if org_questions:
        samples.extend(random.sample(org_questions, min(3, len(org_questions))))
    if other_questions:
        samples.extend(random.sample(other_questions, min(count - len(samples), len(other_questions))))

    for result in samples:
        print(f"\n--- {result['bill_id']} ---")
        print(f"Q: {result['question']}")
        if result["success"]:
            print(f"A: {result['answer'][:400]}...")
            print(f"[Confidence: {result['confidence']:.2f}, Citations: {result['citation_count']}]")
        else:
            print(f"ERROR: {result['error']}")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test bill RAG queries in VoteBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of bills to test (default: 10)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: WARNING)",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)

    print("=" * 70)
    print("VOTEBOT BILL RAG TEST")
    print("=" * 70)

    print(f"\nFetching {args.limit} sample bills from Webflow...")
    bills = await fetch_sample_bills(limit=args.limit)
    print(f"Found {len(bills)} bills")

    if not bills:
        print("No bills found. Have you run sync_bills.py?")
        sys.exit(1)

    # Show bill summary
    with_positions = sum(
        1 for b in bills
        if b["supporting_orgs_count"] > 0 or b["opposing_orgs_count"] > 0
    )
    print(f"Bills with organization positions: {with_positions}/{len(bills)}")

    print("\n" + "=" * 70)
    print("RUNNING TESTS")
    print("=" * 70 + "\n")

    results = await run_tests(args.api_url, bills, BILL_QUESTIONS)

    # Print summary
    print_summary(results)
    print_sample_responses(results)

    # Save detailed results
    output_file = Path(__file__).parent / "bill_test_results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nDetailed results saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
