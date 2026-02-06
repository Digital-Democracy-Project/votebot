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

Supports single-turn (with optional ground truth validation) and multi-turn modes.

Usage:
    # Standalone single-turn tests
    python scripts/test_rag_legislators.py --sample-size 5

    # Multi-turn mode
    python scripts/test_rag_legislators.py --sample-size 5 --mode multi

    # Both modes
    python scripts/test_rag_legislators.py --sample-size 5 --mode both
"""

import argparse
import asyncio
import random
import sys
import time
import uuid
from pathlib import Path

# Add src and scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import httpx
from votebot.config import get_settings

# Clear settings cache
get_settings.cache_clear()
settings = get_settings()

from rag_test_common import (
    TestResult,
    VoteBotTestClient,
    validate_response,
    generate_report,
    print_report,
    save_report,
)


# Global jurisdiction mapping (populated by fetch_jurisdiction_mapping)
JURISDICTION_MAP: dict[str, str] = {}


async def fetch_jurisdiction_mapping() -> dict[str, str]:
    """Fetch jurisdiction reference ID to state code mapping from Webflow."""
    global JURISDICTION_MAP

    if JURISDICTION_MAP:
        return JURISDICTION_MAP

    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {
            "Authorization": f"Bearer {settings.webflow_api_key.get_secret_value()}",
            "accept": "application/json",
        }

        collection_id = settings.webflow_jurisdiction_collection_id
        if not collection_id:
            return {}

        response = await client.get(
            f"https://api.webflow.com/v2/collections/{collection_id}/items",
            headers=headers,
            params={"limit": 100},
        )

        if response.status_code != 200:
            return {}

        data = response.json()
        for item in data.get("items", []):
            item_id = item.get("id", "")
            fields = item.get("fieldData", {})
            state_code = fields.get("slug", "").upper()[:2]
            if len(state_code) == 2:
                JURISDICTION_MAP[item_id] = state_code

    return JURISDICTION_MAP


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
    """Fetch sample legislators from Webflow CMS."""
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

        if len(legislators_with_id) <= sample_size:
            return legislators_with_id

        # Group by jurisdiction and sample proportionally
        by_jurisdiction = {}
        for leg in legislators_with_id:
            j = leg.get("fieldData", {}).get("jurisdiction", "unknown")
            if j not in by_jurisdiction:
                by_jurisdiction[j] = []
            by_jurisdiction[j].append(leg)

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


def _resolve_jurisdiction(jurisdiction_ref) -> str:
    """Resolve a Webflow jurisdiction reference to a state code."""
    if isinstance(jurisdiction_ref, str) and len(jurisdiction_ref) > 2:
        return JURISDICTION_MAP.get(jurisdiction_ref, "US")
    elif isinstance(jurisdiction_ref, str) and len(jurisdiction_ref) == 2:
        return jurisdiction_ref.upper()
    return "US"


def _build_page_context(legislator_id: str, jurisdiction: str) -> dict:
    """Build the critical legislator page context for API calls.

    This page context is unique to legislator tests and enables the API
    to scope retrieval to the specific legislator.
    """
    return {
        "type": "legislator",
        "id": legislator_id,
        "jurisdiction": jurisdiction,
    }


async def _run_single_mode_standalone(
    client: VoteBotTestClient,
    legislators: list[dict],
    verbose: bool = False,
) -> list[TestResult]:
    """Run single-turn tests using LEGISLATOR_QUESTIONS with page_context."""
    results = []

    for i, legislator in enumerate(legislators):
        fields = legislator.get("fieldData", {})
        name = fields.get("name", "Unknown")
        legislator_id = fields.get("openstatesid", "")
        jurisdiction_ref = fields.get("jurisdiction", "")
        jurisdiction = _resolve_jurisdiction(jurisdiction_ref)
        page_context = _build_page_context(legislator_id, jurisdiction)

        if verbose:
            print(f"\n  [{i + 1}/{len(legislators)}] Testing: {name} ({jurisdiction})")

        for q_info in LEGISLATOR_QUESTIONS:
            question = q_info["question"]
            start_time = time.time()

            resp = await client.send_message(question, page_context=page_context)
            latency = time.time() - start_time

            result = TestResult(
                test_id=f"leg-{legislator_id[:8]}-{q_info['description'][:10]}",
                category="legislators",
                entity_type="legislator",
                entity_name=name,
                entity_slug=fields.get("slug", ""),
                prompt=question,
                response_text=resp["response"],
                response_preview=resp["response"][:500],
                confidence=resp["confidence"],
                has_citations=resp["citation_count"] > 0,
                citation_count=resp["citation_count"],
                latency=resp["latency"],
                passed=None,  # No ground truth in standalone
                data_source="none",
                jurisdiction=jurisdiction,
                success=resp["success"],
                error=resp["error"],
                mode="single",
            )
            results.append(result)

            if verbose:
                status = "OK" if resp["success"] else "ERR"
                print(f"    [{status}] {question[:50]}... conf={resp['confidence']:.2f}")

    return results


async def _run_single_mode_ground_truth(
    client: VoteBotTestClient,
    ground_truth: tuple[list, list, list],
    limit: int = 0,
    jurisdiction: str | None = None,
    verbose: bool = False,
) -> list[TestResult]:
    """Run single-turn tests with ground truth validation and page_context."""
    from test_rag_quality import DynamicTestGenerator

    _, legislators_gt, _ = ground_truth

    if jurisdiction:
        legislators_gt = [l for l in legislators_gt if l.jurisdiction == jurisdiction.upper()]

    if limit > 0:
        legislators_gt = legislators_gt[:limit]

    if not legislators_gt:
        print("  No legislators found for ground truth testing")
        return []

    generator = DynamicTestGenerator()
    test_cases = generator.generate_legislator_tests(legislators_gt)
    print(f"  Generated {len(test_cases)} legislator test cases from ground truth")

    results = []
    for i, tc in enumerate(test_cases):
        # Build page_context from ground truth data
        page_context = _build_page_context(
            tc.get("openstates_id", ""),
            tc.get("jurisdiction", "US"),
        )

        resp = await client.send_message(tc["prompt"], page_context=page_context)

        result = TestResult(
            test_id=tc["id"],
            category="legislators",
            entity_type="legislator",
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
            print(f"  Progress: {i + 1}/{len(test_cases)} legislator tests completed")

        await asyncio.sleep(0.3)

    return results


async def _run_multi_mode(
    client: VoteBotTestClient,
    legislators: list[dict],
    verbose: bool = False,
) -> list[TestResult]:
    """Run multi-turn conversation tests for legislators with page_context."""
    results = []

    for i, legislator in enumerate(legislators):
        fields = legislator.get("fieldData", {})
        name = fields.get("name", "Unknown")
        legislator_id = fields.get("openstatesid", "")
        jurisdiction_ref = fields.get("jurisdiction", "")
        jurisdiction = _resolve_jurisdiction(jurisdiction_ref)
        page_context = _build_page_context(legislator_id, jurisdiction)
        session_id = f"test-leg-multi-{uuid.uuid4().hex[:8]}"

        # Select 3-4 random questions for multi-turn conversation
        n_questions = random.randint(3, min(4, len(LEGISLATOR_QUESTIONS)))
        selected = random.sample(LEGISLATOR_QUESTIONS, n_questions)

        if verbose:
            print(f"\n  [{i + 1}/{len(legislators)}] Multi-turn: {name} ({jurisdiction})")

        for turn_idx, q_info in enumerate(selected):
            question = q_info["question"]
            resp = await client.send_message(question, session_id=session_id, page_context=page_context)

            result = TestResult(
                test_id=f"{session_id}-turn{turn_idx}",
                category="legislators",
                entity_type="legislator",
                entity_name=name,
                entity_slug=fields.get("slug", ""),
                prompt=question,
                response_text=resp["response"],
                response_preview=resp["response"][:500],
                confidence=resp["confidence"],
                has_citations=resp["citation_count"] > 0,
                citation_count=resp["citation_count"],
                latency=resp["latency"],
                passed=None,  # Multi-turn: no ground truth
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
                print(f"    [{status}] Turn {turn_idx}: {question[:50]}... conf={resp['confidence']:.2f}")

            if not resp["success"]:
                break

        if verbose:
            print(f"    Completed {len(selected)} turns for: {name}")

    return results


async def run_tests(
    client: VoteBotTestClient,
    ground_truth: tuple[list, list, list] | None = None,
    limit: int = 0,
    jurisdiction: str | None = None,
    mode: str = "single",
    verbose: bool = False,
) -> list[TestResult]:
    """Run legislator tests with unified interface.

    Args:
        client: VoteBotTestClient instance.
        ground_truth: Optional (bills_gt, legislators_gt, organizations_gt) tuple.
        limit: Max legislators to test.
        jurisdiction: Filter by state code.
        mode: "single", "multi", or "both".
        verbose: Print per-test output.

    Returns:
        List of TestResult objects.
    """
    # Ensure jurisdiction mapping is loaded for page_context resolution
    await fetch_jurisdiction_mapping()

    results = []

    if mode in ("single", "both"):
        if ground_truth and ground_truth[1]:
            print("\n--- Legislator Tests: Single-turn (ground truth) ---")
            results.extend(await _run_single_mode_ground_truth(
                client, ground_truth, limit=limit, jurisdiction=jurisdiction, verbose=verbose,
            ))
        else:
            print("\n--- Legislator Tests: Single-turn (standalone) ---")
            effective_limit = limit if limit > 0 else 10
            legislators = await fetch_sample_legislators(effective_limit)
            print(f"  Fetched {len(legislators)} legislators for testing")
            results.extend(await _run_single_mode_standalone(client, legislators, verbose=verbose))

    if mode in ("multi", "both"):
        print("\n--- Legislator Tests: Multi-turn ---")
        effective_limit = limit if limit > 0 else 10
        legislators = await fetch_sample_legislators(effective_limit)
        print(f"  Fetched {len(legislators)} legislators for multi-turn testing")
        results.extend(await _run_multi_mode(client, legislators, verbose=verbose))

    return results


async def main():
    """Run legislator RAG tests (standalone)."""
    parser = argparse.ArgumentParser(
        description="Test RAG retrieval for legislator pages",
    )
    parser.add_argument("--api-url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--sample-size", type=int, default=10,
                        help="Number of legislators to test")
    parser.add_argument("--mode", choices=["single", "multi", "both"], default="single",
                        help="Test mode (default: single)")
    parser.add_argument("--verbose", action="store_true", help="Show full responses")
    parser.add_argument("--output", help="Write results to JSON file")

    args = parser.parse_args()
    api_key = settings.api_key.get_secret_value()

    print("=" * 70)
    print("VOTEBOT LEGISLATOR RAG TEST")
    print("=" * 70)

    client = VoteBotTestClient(args.api_url, api_key)

    results = await run_tests(
        client=client,
        ground_truth=None,
        limit=args.sample_size,
        mode=args.mode,
        verbose=args.verbose,
    )

    report = generate_report(results)
    print_report(report, verbose=args.verbose)

    if args.output:
        save_report(report, args.output)

    # Exit with error code if too many failures
    failure_count = sum(1 for r in results if not r.success)
    if results and failure_count / len(results) > 0.5:
        print(f"\nWARNING: High failure rate ({failure_count / len(results) * 100:.0f}%)")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
