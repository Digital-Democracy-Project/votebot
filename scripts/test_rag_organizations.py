#!/usr/bin/env python3
"""
Test script for organization RAG queries in VoteBot.

This script tests the RAG pipeline for organization-related queries:
1. Fetches sample organizations from Webflow
2. Generates test queries for each organization
3. Runs queries through the RAG pipeline via the API
4. Reports success rates, confidence, and citation metrics

Supports single-turn (with optional ground truth validation) and multi-turn modes.

Usage:
    # Standalone single-turn tests
    python scripts/test_rag_organizations.py --limit 5

    # Multi-turn mode
    python scripts/test_rag_organizations.py --limit 5 --mode multi

    # Both modes
    python scripts/test_rag_organizations.py --limit 5 --mode both
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


# Single-turn question templates (standalone)
ORGANIZATION_QUESTIONS = [
    "What is {name}?",
    "What are {name}'s policy positions?",
    "What bills does {name} support?",
    "What bills does {name} oppose?",
    "What type of organization is {name}?",
    "Who funds {name}?",
    "What organizations are affiliated with {name}?",
    "Tell me about {name}'s stance on legislation.",
    # Dispute/verification follow-ups (trigger Webflow CMS verification)
    "Are you sure about {name}'s bill positions? Can you verify that?",
    "I don't think that's right about {name}. Double check for me.",
]


async def fetch_sample_organizations(limit: int = 10) -> list[dict]:
    """Fetch sample organizations from Webflow for testing."""
    settings = get_settings()
    metadata_extractor = MetadataExtractor()
    webflow = WebflowSource(settings, metadata_extractor)

    organizations = []
    async for doc in webflow.fetch_organizations(limit=0):
        organizations.append({
            "name": doc.metadata.title,
            "webflow_id": doc.metadata.extra.get("webflow_id", ""),
            "organization_type": doc.metadata.extra.get("organization_type", ""),
            "bills_support_count": doc.metadata.extra.get("bills_support_count", 0),
            "bills_oppose_count": doc.metadata.extra.get("bills_oppose_count", 0),
        })

    # Prioritize organizations with bill positions
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

    remaining = limit - len(selected)
    if remaining > 0:
        all_remaining = [o for o in organizations if o not in selected]
        if all_remaining:
            selected.extend(random.sample(all_remaining, min(remaining, len(all_remaining))))

    return selected[:limit]


async def _run_single_mode_standalone(
    client: VoteBotTestClient,
    organizations: list[dict],
    verbose: bool = False,
) -> list[TestResult]:
    """Run single-turn tests without ground truth (standalone mode)."""
    results = []
    total = len(organizations) * len(ORGANIZATION_QUESTIONS)
    completed = 0

    for org in organizations:
        for question_template in ORGANIZATION_QUESTIONS:
            question = question_template.format(name=org["name"])
            resp = await client.send_message(question)
            completed += 1

            result = TestResult(
                test_id=f"org-{org.get('webflow_id', '')[:8]}-{completed}",
                category="organizations",
                entity_type="organization",
                entity_name=org["name"],
                entity_slug="",
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
            print(f"  [{completed}/{total}] Done: {org['name'][:40]}")

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

    _, _, organizations_gt = ground_truth

    if limit > 0:
        organizations_gt = organizations_gt[:limit]

    if not organizations_gt:
        print("  No organizations found for ground truth testing")
        return []

    generator = DynamicTestGenerator()
    test_cases = generator.generate_organization_tests(organizations_gt)
    print(f"  Generated {len(test_cases)} organization test cases from ground truth")

    results = []
    for i, tc in enumerate(test_cases):
        page_context = {"type": "general"}
        if tc.get("webflow_id"):
            page_context = {
                "type": "organization",
                "webflow_id": tc["webflow_id"],
                "slug": tc.get("entity_slug", ""),
            }
        resp = await client.send_message(tc["prompt"], page_context=page_context)

        result = TestResult(
            test_id=tc["id"],
            category="organizations",
            entity_type="organization",
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
            print(f"  Progress: {i + 1}/{len(test_cases)} organization tests completed")

        await asyncio.sleep(0.3)

    return results


async def _run_multi_mode(
    client: VoteBotTestClient,
    organizations: list[dict],
    verbose: bool = False,
) -> list[TestResult]:
    """Run multi-turn conversation tests for organizations."""
    results = []

    for org in organizations:
        session_id = f"test-org-multi-{uuid.uuid4().hex[:8]}"

        # Select 3-4 random questions for multi-turn conversation
        n_questions = random.randint(3, min(4, len(ORGANIZATION_QUESTIONS)))
        selected_templates = random.sample(ORGANIZATION_QUESTIONS, n_questions)

        prompts = [t.format(name=org["name"]) for t in selected_templates]

        if verbose:
            print(f"\n  Multi-turn: {org['name'][:40]}")

        # Build page_context with webflow_id and slug for CMS verification
        page_context = {
            "type": "organization",
            "webflow_id": org.get("webflow_id", ""),
            "slug": org.get("slug", ""),
        }

        for turn_idx, prompt in enumerate(prompts):
            resp = await client.send_message(prompt, session_id=session_id, page_context=page_context)

            result = TestResult(
                test_id=f"{session_id}-turn{turn_idx}",
                category="organizations",
                entity_type="organization",
                entity_name=org["name"],
                prompt=prompt,
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
                success=resp["success"],
                error=resp["error"],
                mode="multi",
            )
            results.append(result)

            if verbose:
                status = "OK" if resp["success"] else "ERR"
                print(f"    [{status}] Turn {turn_idx}: {prompt[:50]}... conf={resp['confidence']:.2f}")

            if not resp["success"]:
                break

        if verbose:
            print(f"    Completed {len(prompts)} turns for: {org['name'][:40]}")

    return results


async def run_tests(
    client: VoteBotTestClient,
    ground_truth: tuple[list, list, list] | None = None,
    limit: int = 0,
    jurisdiction: str | None = None,
    mode: str = "single",
    verbose: bool = False,
) -> list[TestResult]:
    """Run organization tests with unified interface.

    Args:
        client: VoteBotTestClient instance.
        ground_truth: Optional (bills_gt, legislators_gt, organizations_gt) tuple.
        limit: Max organizations to test.
        jurisdiction: Not used for organizations (they're not jurisdiction-scoped).
        mode: "single", "multi", or "both".
        verbose: Print per-test output.

    Returns:
        List of TestResult objects.
    """
    results = []

    if mode in ("single", "both"):
        if ground_truth and ground_truth[2]:
            print("\n--- Organization Tests: Single-turn (ground truth) ---")
            results.extend(await _run_single_mode_ground_truth(
                client, ground_truth, limit=limit, jurisdiction=jurisdiction, verbose=verbose,
            ))
        else:
            print("\n--- Organization Tests: Single-turn (standalone) ---")
            effective_limit = limit if limit > 0 else 10
            organizations = await fetch_sample_organizations(limit=effective_limit)
            print(f"  Fetched {len(organizations)} organizations for testing")
            results.extend(await _run_single_mode_standalone(client, organizations, verbose=verbose))

    if mode in ("multi", "both"):
        print("\n--- Organization Tests: Multi-turn ---")
        effective_limit = limit if limit > 0 else 10
        organizations = await fetch_sample_organizations(limit=effective_limit)
        print(f"  Fetched {len(organizations)} organizations for multi-turn testing")
        results.extend(await _run_multi_mode(client, organizations, verbose=verbose))

    return results


async def main():
    """Main entry point for standalone execution."""
    parser = argparse.ArgumentParser(
        description="Test organization RAG queries in VoteBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--api-url", default="http://localhost:8000",
                        help="API base URL (default: http://localhost:8000)")
    parser.add_argument("--limit", type=int, default=10,
                        help="Number of organizations to test (default: 10)")
    parser.add_argument("--mode", choices=["single", "multi", "both"], default="single",
                        help="Test mode (default: single)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    parser.add_argument("--output", help="Write results to JSON file")

    args = parser.parse_args()

    print("=" * 70)
    print("VOTEBOT ORGANIZATION RAG TEST")
    print("=" * 70)

    settings = get_settings()
    client = VoteBotTestClient(args.api_url, settings.api_key.get_secret_value())

    results = await run_tests(
        client=client,
        ground_truth=None,
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
