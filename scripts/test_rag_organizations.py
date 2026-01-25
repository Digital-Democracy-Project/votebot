#!/usr/bin/env python3
"""
Test script for organization RAG queries in VoteBot.

This script tests the RAG pipeline for organization-related queries:
1. Fetches sample organizations from Webflow
2. Generates test queries for each organization
3. Runs queries through the RAG pipeline via the API
4. Reports success rates, confidence, and citation metrics

Usage:
    python scripts/test_rag_organizations.py [options]

Options:
    --api-url URL      API base URL (default: http://localhost:8000)
    --limit N          Number of organizations to test (default: 10)
    --log-level LEVEL  Logging level (default: WARNING)
"""

import argparse
import asyncio
import json
import random
import sys
import time
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


# Test questions for organization queries
ORGANIZATION_QUESTIONS = [
    "What is {name}?",
    "What are {name}'s policy positions?",
    "What bills does {name} support?",
    "What bills does {name} oppose?",
    "What type of organization is {name}?",
    "Who funds {name}?",
    "What organizations are affiliated with {name}?",
    "Tell me about {name}'s stance on legislation.",
]


async def fetch_sample_organizations(limit: int = 10) -> list[dict]:
    """
    Fetch sample organizations from Webflow for testing.

    Args:
        limit: Number of organizations to fetch

    Returns:
        List of organization dicts with name and metadata
    """
    settings = get_settings()
    metadata_extractor = MetadataExtractor()
    webflow = WebflowSource(settings, metadata_extractor)

    organizations = []
    async for doc in webflow.fetch_organizations(limit=0):  # Fetch all, then sample
        organizations.append({
            "name": doc.metadata.title,
            "webflow_id": doc.metadata.extra.get("webflow_id", ""),
            "organization_type": doc.metadata.extra.get("organization_type", ""),
            "bills_support_count": doc.metadata.extra.get("bills_support_count", 0),
            "bills_oppose_count": doc.metadata.extra.get("bills_oppose_count", 0),
        })

    # Prioritize organizations with bill positions for more interesting tests
    orgs_with_positions = [
        o for o in organizations
        if o["bills_support_count"] > 0 or o["bills_oppose_count"] > 0
    ]
    orgs_without_positions = [
        o for o in organizations
        if o["bills_support_count"] == 0 and o["bills_oppose_count"] == 0
    ]

    # Mix: 70% with positions, 30% without
    selected = []
    with_positions_count = min(int(limit * 0.7), len(orgs_with_positions))
    without_positions_count = min(limit - with_positions_count, len(orgs_without_positions))

    if orgs_with_positions:
        selected.extend(random.sample(orgs_with_positions, with_positions_count))
    if orgs_without_positions:
        selected.extend(random.sample(orgs_without_positions, without_positions_count))

    # If we still need more, fill from whatever is available
    remaining = limit - len(selected)
    if remaining > 0:
        all_remaining = [o for o in organizations if o not in selected]
        if all_remaining:
            selected.extend(random.sample(all_remaining, min(remaining, len(all_remaining))))

    return selected[:limit]


async def test_organization_query(
    client: httpx.AsyncClient,
    api_url: str,
    org: dict,
    question_template: str,
) -> dict:
    """
    Test a single organization query via the API.

    Args:
        client: httpx AsyncClient
        api_url: Base URL for API
        org: Organization dict
        question_template: Question template with {name} placeholder

    Returns:
        Test result dict
    """
    question = question_template.format(name=org["name"])
    settings = get_settings()

    start_time = datetime.now()
    try:
        response = await client.post(
            f"{api_url}/votebot/v1/chat",
            json={
                "message": question,
                "session_id": f"test-org-{org['webflow_id'][:8]}",
                "human_active": False,
                "page_context": None,  # General query, not page-specific
            },
            headers={
                "X-API-Key": settings.api_key.get_secret_value(),
            },
            timeout=60.0,
        )

        latency = (datetime.now() - start_time).total_seconds()

        if response.status_code != 200:
            return {
                "org_name": org["name"],
                "organization_type": org["organization_type"],
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
            "org_name": org["name"],
            "organization_type": org["organization_type"],
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
            "org_name": org["name"],
            "organization_type": org["organization_type"],
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
    organizations: list[dict],
    questions: list[str],
) -> list[dict]:
    """
    Run all tests for the given organizations and questions.

    Args:
        api_url: API base URL
        organizations: List of organization dicts
        questions: List of question templates

    Returns:
        List of test result dicts
    """
    results = []
    total_tests = len(organizations) * len(questions)
    completed = 0

    async with httpx.AsyncClient() as client:
        for org in organizations:
            org_results = []
            for question in questions:
                result = await test_organization_query(client, api_url, org, question)
                org_results.append(result)
                results.append(result)
                completed += 1

            # Print progress
            success_count = sum(1 for r in org_results if r["success"])
            avg_confidence = sum(r["confidence"] for r in org_results) / len(org_results) if org_results else 0
            citation_rate = sum(1 for r in org_results if r["has_citations"]) / len(org_results) * 100 if org_results else 0

            print(
                f"[{completed}/{total_tests}] Testing: {org['name'][:40]}\n"
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
        for question in ORGANIZATION_QUESTIONS:
            q_results = [r for r in success_results if question.split("{name}")[0] in r["question"]]
            if q_results:
                q_conf = sum(r["confidence"] for r in q_results) / len(q_results)
                q_citations = sum(1 for r in q_results if r["has_citations"]) / len(q_results) * 100
                print(f"  \"{question[:45]}...\"")
                print(f"    {len(q_results)} tests, conf: {q_conf:.2f}, citations: {q_citations:.0f}%")

        # Results by organization type
        org_types = set(r["organization_type"] for r in success_results if r["organization_type"])
        if org_types:
            print(f"\nResults by Organization Type:")
            for org_type in sorted(org_types)[:5]:  # Limit to top 5 types
                type_results = [r for r in success_results if r["organization_type"] == org_type]
                type_conf = sum(r["confidence"] for r in type_results) / len(type_results)
                print(f"  {org_type[:40]}: {len(type_results)} queries, avg conf: {type_conf:.2f}")


def print_sample_responses(results: list[dict], count: int = 5):
    """Print sample responses for inspection."""
    print("\n" + "=" * 70)
    print("SAMPLE RESPONSES")
    print("=" * 70)

    # Select diverse samples
    samples = random.sample(results, min(count, len(results)))

    for result in samples:
        print(f"\n--- {result['org_name'][:40]} ({result['organization_type'][:30]}) ---")
        print(f"Q: {result['question']}")
        if result["success"]:
            print(f"A: {result['answer'][:300]}...")
            print(f"[Confidence: {result['confidence']:.2f}, Citations: {result['has_citations']}]")
        else:
            print(f"ERROR: {result['error']}")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test organization RAG queries in VoteBot",
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
        help="Number of organizations to test (default: 10)",
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
    print("VOTEBOT ORGANIZATION RAG TEST")
    print("=" * 70)

    print(f"\nFetching {args.limit} sample organizations from Webflow...")
    organizations = await fetch_sample_organizations(limit=args.limit)
    print(f"Found {len(organizations)} organizations")

    if not organizations:
        print("No organizations found. Have you run sync_organizations.py?")
        sys.exit(1)

    # Show organization summary
    with_positions = sum(
        1 for o in organizations
        if o["bills_support_count"] > 0 or o["bills_oppose_count"] > 0
    )
    print(f"Organizations with bill positions: {with_positions}/{len(organizations)}")

    print("\n" + "=" * 70)
    print("RUNNING TESTS")
    print("=" * 70 + "\n")

    results = await run_tests(args.api_url, organizations, ORGANIZATION_QUESTIONS)

    # Print summary
    print_summary(results)
    print_sample_responses(results)

    # Save detailed results
    output_file = Path(__file__).parent / "organization_test_results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nDetailed results saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
