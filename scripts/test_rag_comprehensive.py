#!/usr/bin/env python3
"""
Orchestrated RAG Test Suite for VoteBot.

Establishes ground truth from Webflow CMS (+ optional OpenStates), delegates
to focused test modules (bills, legislators, organizations, DDP, out-of-system
votes), and produces a unified report with metrics broken down by category.

Usage:
    # Run all categories with defaults
    python scripts/test_rag_comprehensive.py

    # Run specific categories
    python scripts/test_rag_comprehensive.py --category bills --category legislators

    # Run with multi-turn conversations
    python scripts/test_rag_comprehensive.py --mode both --limit 5

    # Run with ground truth and OpenStates enrichment
    python scripts/test_rag_comprehensive.py --with-openstates --limit 10

    # Dry run to see test plan
    python scripts/test_rag_comprehensive.py --dry-run

    # Save JSON report
    python scripts/test_rag_comprehensive.py --output test_report.json
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

from rag_test_common import (
    TestResult,
    VoteBotTestClient,
    fetch_ground_truth,
    generate_report,
    print_report,
    save_report,
)


# ── DDP General Knowledge Questions ──────────────────────────────────────────

DDP_QUESTIONS = [
    {
        "initial": "What is the Digital Democracy Project?",
        "follow_ups": [
            "How do voters participate in the platform?",
            "What makes DDP different from regular elections?",
        ]
    },
    {
        "initial": "How do I sign up for Digital Democracy Project?",
        "follow_ups": [
            "What ID do I need to verify my identity?",
            "Is my vote anonymous?",
        ]
    },
    {
        "initial": "What states does Digital Democracy Project operate in?",
        "follow_ups": [
            "Can I vote on federal legislation?",
            "When will DDP expand to more states?",
        ]
    },
    {
        "initial": "How does DDP score legislators?",
        "follow_ups": [
            "Where can I see my representative's scorecard?",
            "What happens when a legislator votes against their constituents?",
        ]
    },
    {
        "initial": "Is Digital Democracy Project partisan?",
        "follow_ups": [
            "Does DDP endorse candidates?",
            "How does DDP choose which bills to feature?",
        ]
    },
    {
        "initial": "What is the Voatz app?",
        "follow_ups": [
            "Is the Voatz app secure?",
            "Can I vote without downloading an app?",
        ]
    },
    {
        "initial": "How are votes tallied on DDP?",
        "follow_ups": [
            "Can I see how other people in my district voted?",
            "Are the results shared with legislators?",
        ]
    },
    {
        "initial": "What's the DDP tagline?",
        "follow_ups": [
            "What does 'You vote. We track it. So you know the score.' mean?",
        ]
    },
]


# ── Out-of-System Vote Tests ─────────────────────────────────────────────────

OUT_OF_SYSTEM_VOTE_TESTS = [
    {
        "bill_id": "FL HB 1",
        "jurisdiction": "Florida",
        "session": "2024",
        "description": "Florida 2024 session bill (not in current CMS)",
        "questions": [
            "How did legislators vote on Florida HB 1 from the 2024 session?",
            "Who voted yes on FL HB 1 in 2024?",
            "Did Florida HB 1 pass in 2024?",
        ]
    },
    {
        "bill_id": "CA SB 1047",
        "jurisdiction": "California",
        "session": "2024",
        "description": "California AI safety bill (not tracked in DDP)",
        "questions": [
            "How did California legislators vote on SB 1047, the AI safety bill?",
            "What was the vote count on California SB 1047?",
            "Who opposed SB 1047 in California?",
        ]
    },
    {
        "bill_id": "TX HB 1",
        "jurisdiction": "Texas",
        "session": "2023",
        "description": "Texas bill from previous session",
        "questions": [
            "How did Texas legislators vote on HB 1 in 2023?",
            "What was the final vote on Texas HB 1?",
        ]
    },
    {
        "bill_id": "NY S2421",
        "jurisdiction": "New York",
        "session": "2024",
        "description": "New York bill (not in DDP tracking)",
        "questions": [
            "Can you tell me how New York senators voted on S2421?",
            "Did NY S2421 pass? What was the vote breakdown?",
        ]
    },
    {
        "bill_id": "US HR 2",
        "jurisdiction": "US Federal",
        "session": "118",
        "description": "Federal bill from 118th Congress",
        "questions": [
            "How did Congress vote on HR 2 in the 118th Congress?",
            "Who voted against HR 2 in the House?",
            "What was the vote count on the Secure the Border Act?",
        ]
    },
]


# ── DDP and Out-of-System Test Runners ───────────────────────────────────────

async def run_ddp_tests(
    client: VoteBotTestClient,
    mode: str = "single",
    verbose: bool = False,
) -> list[TestResult]:
    """Run DDP general knowledge tests."""
    print("\n--- DDP Tests ---")
    results = []

    for q_set in DDP_QUESTIONS:
        session_id = f"test-ddp-{uuid.uuid4().hex[:8]}"

        if mode == "single":
            # Single-turn: just the initial question
            prompts = [q_set["initial"]]
        else:
            # Multi-turn: initial + follow-ups
            n_follow_ups = random.randint(1, min(3, len(q_set["follow_ups"])))
            follow_ups = random.sample(q_set["follow_ups"], n_follow_ups)
            prompts = [q_set["initial"]] + follow_ups

        for turn_idx, prompt in enumerate(prompts):
            resp = await client.send_message(
                prompt, session_id=session_id, page_context={"type": "general"},
            )

            result = TestResult(
                test_id=f"{session_id}-turn{turn_idx}",
                category="ddp",
                entity_type="ddp",
                entity_name="DDP Knowledge",
                prompt=prompt,
                response_text=resp["response"],
                response_preview=resp["response"][:500],
                confidence=resp["confidence"],
                has_citations=resp["citation_count"] > 0,
                citation_count=resp["citation_count"],
                latency=resp["latency"],
                passed=None,  # No CMS ground truth for DDP
                data_source="none",
                turn_index=turn_idx,
                session_id=session_id,
                jurisdiction="General",
                success=resp["success"],
                error=resp["error"],
                mode="multi" if len(prompts) > 1 else "single",
            )
            results.append(result)

            if verbose:
                status = "OK" if resp["success"] else "ERR"
                print(f"  [{status}] {prompt[:60]}... conf={resp['confidence']:.2f}")

            if not resp["success"]:
                break

    print(f"  Completed {len(results)} DDP test queries")
    return results


async def run_out_of_system_vote_tests(
    client: VoteBotTestClient,
    mode: str = "single",
    verbose: bool = False,
) -> list[TestResult]:
    """Run tests for bills NOT in Webflow CMS (dynamic OpenStates lookup)."""
    print("\n--- Out-of-System Vote Tests ---")
    results = []

    for bill_test in OUT_OF_SYSTEM_VOTE_TESTS:
        session_id = f"test-oos-{uuid.uuid4().hex[:8]}"
        questions = bill_test["questions"]

        if mode == "single":
            prompts = [questions[0]]
        else:
            prompts = questions

        bill_label = f"{bill_test['bill_id']} ({bill_test['session']})"

        for turn_idx, prompt in enumerate(prompts):
            resp = await client.send_message(
                prompt, session_id=session_id, page_context={"type": "general"},
            )

            result = TestResult(
                test_id=f"{session_id}-turn{turn_idx}",
                category="out_of_system_votes",
                entity_type="out_of_system_vote",
                entity_name=bill_label,
                prompt=prompt,
                response_text=resp["response"],
                response_preview=resp["response"][:500],
                confidence=resp["confidence"],
                has_citations=resp["citation_count"] > 0,
                citation_count=resp["citation_count"],
                latency=resp["latency"],
                passed=None,  # No CMS ground truth
                data_source="none",
                turn_index=turn_idx,
                session_id=session_id,
                jurisdiction=bill_test["jurisdiction"],
                success=resp["success"],
                error=resp["error"],
                mode="multi" if len(prompts) > 1 else "single",
            )
            results.append(result)

            if verbose:
                status = "OK" if resp["success"] else "ERR"
                print(f"  [{status}] {prompt[:60]}... conf={resp['confidence']:.2f}")

            if not resp["success"]:
                break

    print(f"  Completed {len(results)} out-of-system vote test queries")
    return results


# ── CLI and Orchestration ────────────────────────────────────────────────────

ALL_CATEGORIES = ["bills", "legislators", "organizations", "ddp", "out_of_system_votes"]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Orchestrated RAG Test Suite for VoteBot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--category", action="append", dest="categories",
        choices=ALL_CATEGORIES,
        help="Category to test (repeatable; default: all)",
    )
    parser.add_argument(
        "--mode", choices=["single", "multi", "both"], default="single",
        help="Test mode: single, multi, or both (default: single)",
    )
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Max entities per category (default: 10)",
    )
    parser.add_argument(
        "--jurisdiction",
        help="Filter by state code (e.g., FL, VA)",
    )
    parser.add_argument(
        "--with-openstates", action="store_true",
        help="Enrich ground truth with OpenStates data",
    )
    parser.add_argument(
        "--api-url", default="http://localhost:8000",
        help="VoteBot API URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--output",
        help="JSON report output path",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Per-test output",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show test plan without executing",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    categories = args.categories or ALL_CATEGORIES

    print("=" * 70)
    print("VOTEBOT ORCHESTRATED RAG TEST SUITE")
    print("=" * 70)
    print(f"  Categories: {', '.join(categories)}")
    print(f"  Mode: {args.mode}")
    print(f"  Limit: {args.limit} per category")
    if args.jurisdiction:
        print(f"  Jurisdiction: {args.jurisdiction}")
    if args.with_openstates:
        print(f"  OpenStates enrichment: enabled")

    if args.dry_run:
        print("\n[DRY RUN] Would run tests for categories:", ", ".join(categories))
        needs_gt = set(categories) & {"bills", "legislators", "organizations"}
        if needs_gt:
            print(f"  Ground truth fetch: {', '.join(needs_gt)}")
        if "ddp" in categories:
            print(f"  DDP tests: {len(DDP_QUESTIONS)} question sets")
        if "out_of_system_votes" in categories:
            print(f"  Out-of-system vote tests: {len(OUT_OF_SYSTEM_VOTE_TESTS)} bill tests")
        return 0

    # Load API key from settings
    from votebot.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    client = VoteBotTestClient(args.api_url, settings.api_key.get_secret_value())

    # ── Phase 1: Establish ground truth ──────────────────────────────────
    ground_truth = None
    needs_ground_truth = set(categories) & {"bills", "legislators", "organizations"}

    if needs_ground_truth:
        print("\n" + "=" * 70)
        print("PHASE 1: FETCHING GROUND TRUTH")
        print("=" * 70)

        entity_types = list(needs_ground_truth)
        ground_truth = await fetch_ground_truth(
            limit=args.limit,
            jurisdiction=args.jurisdiction,
            entity_types=entity_types,
            with_openstates=args.with_openstates,
        )

    # ── Phase 2: Run tests by category ───────────────────────────────────
    print("\n" + "=" * 70)
    print("PHASE 2: RUNNING TESTS")
    print("=" * 70)

    all_results: list[TestResult] = []

    if "bills" in categories:
        from test_rag_bills import run_tests as run_bill_tests
        all_results.extend(await run_bill_tests(
            client, ground_truth=ground_truth, limit=args.limit,
            jurisdiction=args.jurisdiction, mode=args.mode, verbose=args.verbose,
        ))

    if "legislators" in categories:
        from test_rag_legislators import run_tests as run_legislator_tests
        all_results.extend(await run_legislator_tests(
            client, ground_truth=ground_truth, limit=args.limit,
            jurisdiction=args.jurisdiction, mode=args.mode, verbose=args.verbose,
        ))

    if "organizations" in categories:
        from test_rag_organizations import run_tests as run_org_tests
        all_results.extend(await run_org_tests(
            client, ground_truth=ground_truth, limit=args.limit,
            jurisdiction=args.jurisdiction, mode=args.mode, verbose=args.verbose,
        ))

    if "ddp" in categories:
        all_results.extend(await run_ddp_tests(
            client, mode=args.mode, verbose=args.verbose,
        ))

    if "out_of_system_votes" in categories:
        all_results.extend(await run_out_of_system_vote_tests(
            client, mode=args.mode, verbose=args.verbose,
        ))

    # ── Phase 3: Unified report ──────────────────────────────────────────
    print("\n" + "=" * 70)
    print("PHASE 3: UNIFIED REPORT")
    print("=" * 70)

    report = generate_report(all_results)
    print_report(report, verbose=args.verbose)

    if args.output:
        save_report(report, args.output)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
