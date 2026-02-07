#!/usr/bin/env python3
"""
Test script for bill RAG queries in VoteBot.

This script tests the RAG pipeline for bill-related queries:
1. Fetches sample bills from Webflow
2. Generates test queries for each bill
3. Runs queries through the RAG pipeline via the API
4. Reports success rates, confidence, and citation metrics

Supports both single-turn (with optional ground truth validation) and
multi-turn conversation modes.

Usage:
    # Standalone single-turn tests
    python scripts/test_rag_bills.py --limit 5

    # Multi-turn mode
    python scripts/test_rag_bills.py --limit 5 --mode multi

    # Both modes
    python scripts/test_rag_bills.py --limit 5 --mode both
"""

import argparse
import asyncio
import random
import sys
import uuid
from pathlib import Path

# Add src and scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import httpx
from votebot.config import get_settings

# Clear settings cache to ensure fresh env vars are loaded
get_settings.cache_clear()

from votebot.ingestion.metadata import MetadataExtractor
from votebot.ingestion.sources.webflow import WebflowSource

from rag_test_common import (
    TestResult,
    VoteBotTestClient,
    validate_response,
    generate_report,
    print_report,
    save_report,
)


# Single-turn question templates (standalone, no ground truth)
BILL_QUESTIONS = [
    "What is {name}?",
    "What organizations support {name}?",
    "What organizations oppose {name}?",
    "What are the arguments for {name}?",
    "What are the arguments against {name}?",
    "What is the status of {name}?",
    "Tell me about the debate around {name}.",
]

# Multi-turn question templates (absorbed from test_rag_comprehensive.py)
BILL_QUESTION_TEMPLATES = [
    {
        "initial": "What is {bill_title} about?",
        "follow_ups": [
            "What are the main arguments in support of this bill?",
            "What are the arguments against it?",
            "What is the current status of this bill?",
        ]
    },
    {
        "initial": "Can you summarize {bill_title}?",
        "follow_ups": [
            "Who sponsored this legislation?",
            "What committee is it assigned to?",
        ]
    },
    {
        "initial": "What would change if {bill_title} passes?",
        "follow_ups": [
            "Who would be most affected by this bill?",
            "Are there similar bills in other states?",
        ]
    },
    {
        "initial": "Tell me about the {jurisdiction} bill on {topic}",
        "follow_ups": [
            "What's the bill number?",
            "Has there been any public debate on this?",
        ]
    },
]

# Vote-specific multi-turn templates (absorbed from test_rag_comprehensive.py)
VOTE_QUESTION_TEMPLATES = [
    {
        "initial": "How did legislators vote on {bill_title}?",
        "follow_ups": [
            "Who voted yes on this bill?",
            "Who voted no?",
            "Was this a close vote?",
        ]
    },
    {
        "initial": "What was the vote count on {bill_title}?",
        "follow_ups": [
            "Did this bill pass committee?",
            "Were there any abstentions?",
            "Which party mostly supported it?",
        ]
    },
    {
        "initial": "Did {bill_title} pass?",
        "follow_ups": [
            "What was the final vote tally?",
            "Who were the key supporters?",
            "Were there any surprising votes?",
        ]
    },
    {
        "initial": "Show me the voting record for {bill_title}",
        "follow_ups": [
            "How did the committee vote compare to the floor vote?",
            "Were there any bipartisan votes?",
        ]
    },
    {
        "initial": "Who opposed {bill_title}?",
        "follow_ups": [
            "What were their reasons for voting no?",
            "How many Democrats voted against it?",
            "How many Republicans voted against it?",
        ]
    },
]


async def fetch_sample_bills(limit: int = 10) -> list[dict]:
    """Fetch sample bills from Webflow for testing."""
    settings = get_settings()
    metadata_extractor = MetadataExtractor()
    webflow = WebflowSource(settings, metadata_extractor)

    bills = []
    async for doc in webflow.fetch(
        collection_id=webflow.bills_collection_id,
        limit=0,
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
            "jurisdiction": doc.metadata.extra.get("jurisdiction", ""),
        })

    # Prioritize bills with organization positions
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

    remaining = limit - len(selected)
    if remaining > 0:
        all_remaining = [b for b in bills if b not in selected]
        if all_remaining:
            selected.extend(random.sample(all_remaining, min(remaining, len(all_remaining))))

    return selected[:limit]


async def _run_single_mode_standalone(
    client: VoteBotTestClient,
    bills: list[dict],
    verbose: bool = False,
) -> list[TestResult]:
    """Run single-turn tests without ground truth (standalone mode)."""
    results = []
    total = len(bills) * len(BILL_QUESTIONS)
    completed = 0

    for bill in bills:
        short_name = f"{bill['bill_prefix']} {bill['bill_number']}".strip()
        if not short_name:
            short_name = bill["name"][:50]

        for question_template in BILL_QUESTIONS:
            question = question_template.format(name=short_name)
            resp = await client.send_message(question)
            completed += 1

            result = TestResult(
                test_id=f"bill-{bill.get('webflow_id', '')[:8]}-{completed}",
                category="bills",
                entity_type="bill",
                entity_name=bill["name"],
                entity_slug=bill.get("slug", ""),
                prompt=question,
                response_text=resp["response"],
                response_preview=resp["response"][:500],
                confidence=resp["confidence"],
                has_citations=resp["citation_count"] > 0,
                citation_count=resp["citation_count"],
                latency=resp["latency"],
                passed=None,  # No ground truth
                data_source="none",
                success=resp["success"],
                error=resp["error"],
                mode="single",
            )
            results.append(result)

            if verbose:
                status = "OK" if resp["success"] else "ERR"
                print(f"  [{status}] {question[:60]}... conf={resp['confidence']:.2f}")

        if verbose:
            print(f"  [{completed}/{total}] Done: {short_name}")

    return results


async def _run_single_mode_ground_truth(
    client: VoteBotTestClient,
    ground_truth: tuple[list, list, list],
    limit: int = 0,
    jurisdiction: str | None = None,
    verbose: bool = False,
) -> list[TestResult]:
    """Run single-turn tests with ground truth validation."""
    from test_rag_quality import DynamicTestGenerator

    bills_gt, _, _ = ground_truth

    # Filter by jurisdiction if specified
    if jurisdiction:
        bills_gt = [b for b in bills_gt if b.jurisdiction == jurisdiction.upper()]

    if limit > 0:
        bills_gt = bills_gt[:limit]

    if not bills_gt:
        print("  No bills found for ground truth testing")
        return []

    generator = DynamicTestGenerator()
    test_cases = generator.generate_bill_tests(bills_gt)
    print(f"  Generated {len(test_cases)} bill test cases from ground truth")

    results = []
    for i, tc in enumerate(test_cases):
        # Build page_context with webflow_id and slug for Webflow CMS lookup
        page_context = {
            "type": "bill",
            "slug": tc.get("entity_slug", ""),
            "webflow_id": tc.get("webflow_id", ""),
            "jurisdiction": tc.get("jurisdiction", ""),
        }
        resp = await client.send_message(tc["prompt"], page_context=page_context)

        result = TestResult(
            test_id=tc["id"],
            category="bills",
            entity_type="bill",
            entity_name=tc.get("prompt", ""),
            entity_slug=tc.get("entity_slug", ""),
            prompt=tc["prompt"],
            response_text=resp["response"],
            response_preview=resp["response"][:500],
            confidence=resp["confidence"],
            has_citations=resp["citation_count"] > 0,
            citation_count=resp["citation_count"],
            latency=resp["latency"],
            expected_data=tc.get("expected_data", []),
            validation_mode=tc.get("validation", "contains"),
            data_source=tc.get("data_source", "webflow_cms"),
            success=resp["success"],
            error=resp["error"],
            jurisdiction=tc.get("jurisdiction", ""),
            mode="single",
        )

        if not resp["success"]:
            result.passed = False
        elif tc.get("expected_data"):
            passed, found, missing = validate_response(
                resp["response"],
                tc["expected_data"],
                tc.get("validation", "contains"),
                tc.get("min_matches", 1),
            )
            result.passed = passed
            result.found_data = found
            result.missing_data = missing
        else:
            result.passed = None

        results.append(result)

        if verbose:
            status = "PASS" if result.passed else ("FAIL" if result.passed is False else "N/A")
            print(f"  [{status}] {tc['id']}: {tc['prompt'][:50]}...")

        if (i + 1) % 10 == 0:
            print(f"  Progress: {i + 1}/{len(test_cases)} bill tests completed")

        await asyncio.sleep(0.3)

    return results


async def _run_multi_mode(
    client: VoteBotTestClient,
    bills: list[dict],
    verbose: bool = False,
) -> list[TestResult]:
    """Run multi-turn conversation tests for bills."""
    results = []

    # Generate multi-turn test cases from bill + vote templates
    all_templates = BILL_QUESTION_TEMPLATES + VOTE_QUESTION_TEMPLATES

    for bill in bills:
        title = bill["name"]
        jurisdiction = bill.get("jurisdiction", "Unknown")
        topic_words = title.split()[:3]
        topic = " ".join(topic_words).lower()

        template = random.choice(all_templates)
        session_id = f"test-bill-multi-{uuid.uuid4().hex[:8]}"

        initial = template["initial"].format(
            bill_title=title, jurisdiction=jurisdiction, topic=topic,
        )
        n_follow_ups = random.randint(1, min(3, len(template["follow_ups"])))
        follow_ups = random.sample(template["follow_ups"], n_follow_ups)

        prompts = [initial] + [
            q.format(bill_title=title, jurisdiction=jurisdiction, topic=topic)
            for q in follow_ups
        ]

        for turn_idx, prompt in enumerate(prompts):
            resp = await client.send_message(prompt, session_id=session_id, page_context={"type": "bill"})

            result = TestResult(
                test_id=f"{session_id}-turn{turn_idx}",
                category="bills",
                entity_type="bill",
                entity_name=title,
                entity_slug=bill.get("slug", ""),
                prompt=prompt,
                response_text=resp["response"],
                response_preview=resp["response"][:500],
                confidence=resp["confidence"],
                has_citations=resp["citation_count"] > 0,
                citation_count=resp["citation_count"],
                latency=resp["latency"],
                passed=None,  # Multi-turn: no ground truth validation
                data_source="none",
                turn_index=turn_idx,
                session_id=session_id,
                jurisdiction=jurisdiction,
                success=resp["success"],
                error=resp["error"],
                mode="multi",
            )
            results.append(result)

            if verbose:
                status = "OK" if resp["success"] else "ERR"
                print(f"  [{status}] Turn {turn_idx}: {prompt[:50]}... conf={resp['confidence']:.2f}")

            if not resp["success"]:
                break  # Stop conversation on error

        if verbose:
            print(f"  Completed {len(prompts)} turns for: {title[:40]}")

    return results


async def run_tests(
    client: VoteBotTestClient,
    ground_truth: tuple[list, list, list] | None = None,
    limit: int = 0,
    jurisdiction: str | None = None,
    mode: str = "single",
    verbose: bool = False,
) -> list[TestResult]:
    """Run bill tests with unified interface.

    Args:
        client: VoteBotTestClient instance.
        ground_truth: Optional (bills_gt, legislators_gt, organizations_gt) tuple.
        limit: Max bills to test (0 = unlimited for ground truth, 10 default for standalone).
        jurisdiction: Filter by state code (e.g., "FL").
        mode: "single", "multi", or "both".
        verbose: Print per-test output.

    Returns:
        List of TestResult objects.
    """
    results = []

    if mode in ("single", "both"):
        if ground_truth and ground_truth[0]:
            print("\n--- Bill Tests: Single-turn (ground truth) ---")
            results.extend(await _run_single_mode_ground_truth(
                client, ground_truth, limit=limit, jurisdiction=jurisdiction, verbose=verbose,
            ))
        else:
            print("\n--- Bill Tests: Single-turn (standalone) ---")
            effective_limit = limit if limit > 0 else 10
            bills = await fetch_sample_bills(limit=effective_limit)
            print(f"  Fetched {len(bills)} bills for testing")
            results.extend(await _run_single_mode_standalone(client, bills, verbose=verbose))

    if mode in ("multi", "both"):
        print("\n--- Bill Tests: Multi-turn ---")
        effective_limit = limit if limit > 0 else 10
        bills = await fetch_sample_bills(limit=effective_limit)
        print(f"  Fetched {len(bills)} bills for multi-turn testing")
        results.extend(await _run_multi_mode(client, bills, verbose=verbose))

    return results


async def main():
    """Main entry point for standalone execution."""
    parser = argparse.ArgumentParser(
        description="Test bill RAG queries in VoteBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--api-url", default="http://localhost:8000",
                        help="API base URL (default: http://localhost:8000)")
    parser.add_argument("--limit", type=int, default=10,
                        help="Number of bills to test (default: 10)")
    parser.add_argument("--mode", choices=["single", "multi", "both"], default="single",
                        help="Test mode (default: single)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    parser.add_argument("--output", help="Write results to JSON file")

    args = parser.parse_args()

    print("=" * 70)
    print("VOTEBOT BILL RAG TEST")
    print("=" * 70)

    settings = get_settings()
    client = VoteBotTestClient(args.api_url, settings.api_key.get_secret_value())

    results = await run_tests(
        client=client,
        ground_truth=None,  # Standalone mode - no ground truth
        limit=args.limit,
        mode=args.mode,
        verbose=args.verbose,
    )

    report = generate_report(results)
    print_report(report, verbose=args.verbose)

    if args.output:
        save_report(report, args.output)


if __name__ == "__main__":
    asyncio.run(main())
