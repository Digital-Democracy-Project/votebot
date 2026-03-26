#!/usr/bin/env python3
"""
Offline evaluation of production query logs against ground truth.

Reads JSONL query logs produced by QueryLogger, classifies each query,
matches against Webflow CMS ground truth, and generates a quality report.

Usage:
    python scripts/evaluate_production.py
    python scripts/evaluate_production.py --date 2026-02-08
    python scripts/evaluate_production.py --days 7 --jurisdiction FL --verbose
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure scripts/ is on sys.path for rag_test_common / rag_ground_truth
sys.path.insert(0, str(Path(__file__).parent))

from rag_test_common import (
    TestResult,
    TestReport,
    fetch_ground_truth,
    generate_report,
    print_report,
    save_report,
    validate_response,
)


# ---------------------------------------------------------------------------
# Query classification
# ---------------------------------------------------------------------------

BILL_PATTERN = re.compile(
    r"\b(HB|SB|HR|S|HJ|SJ|HCR|SCR|HJR|SJR)\s*\d+", re.IGNORECASE
)

ORG_KEYWORDS = [
    "organization", "organizations", "org ", "who supports", "who opposes",
    "which groups", "support", "oppose", "backed", "endorses",
]

LEGISLATOR_KEYWORDS = [
    "senator", "representative", "legislator", "congress",
    "voted", "vote", "sponsor", "cosponsor",
]

OUT_OF_SCOPE_KEYWORDS = [
    "weather", "recipe", "joke", "hello", "hi ", "hey ",
    "thanks", "thank you", "bye", "goodbye",
]


def classify_query(entry: dict) -> str:
    """Classify a log entry by entity type.

    Returns one of: bill, organization, legislator, general, out_of_scope.
    """
    page_type = entry.get("page_context", {}).get("type", "general")
    message = (entry.get("message") or "").lower()

    # Page context is the strongest signal
    if page_type == "bill":
        return "bill"
    if page_type == "organization":
        return "organization"
    if page_type == "legislator":
        return "legislator"

    # Fall back to message content analysis
    if BILL_PATTERN.search(message):
        return "bill"

    if any(kw in message for kw in ORG_KEYWORDS):
        return "organization"

    if any(kw in message for kw in LEGISLATOR_KEYWORDS):
        return "legislator"

    if any(kw in message for kw in OUT_OF_SCOPE_KEYWORDS):
        return "out_of_scope"

    return "general"


# ---------------------------------------------------------------------------
# Ground truth matching
# ---------------------------------------------------------------------------

def match_bill_ground_truth(entry: dict, bills_gt: list) -> object | None:
    """Match a log entry to a bill ground truth record."""
    pc = entry.get("page_context", {})
    slug = pc.get("slug")
    webflow_id = pc.get("webflow_id")
    message = (entry.get("message") or "").lower()

    # Match by webflow_id or slug first (most reliable)
    for bill in bills_gt:
        if webflow_id and bill.webflow_id == webflow_id:
            return bill
        if slug and bill.slug == slug:
            return bill

    # Match by bill identifier in message text
    match = BILL_PATTERN.search(message)
    if match:
        bill_id_query = match.group(0).replace(" ", "").upper()
        for bill in bills_gt:
            gt_id = f"{bill.bill_prefix}{bill.bill_number}".upper()
            if gt_id == bill_id_query:
                return bill

    return None


def match_org_ground_truth(entry: dict, orgs_gt: list) -> object | None:
    """Match a log entry to an organization ground truth record."""
    pc = entry.get("page_context", {})
    slug = pc.get("slug")
    webflow_id = pc.get("webflow_id")

    for org in orgs_gt:
        if webflow_id and org.webflow_id == webflow_id:
            return org
        if slug and org.slug == slug:
            return org

    return None


def match_legislator_ground_truth(entry: dict, legislators_gt: list) -> object | None:
    """Match a log entry to a legislator ground truth record."""
    pc = entry.get("page_context", {})
    slug = pc.get("slug")
    webflow_id = pc.get("webflow_id")

    for leg in legislators_gt:
        if webflow_id and leg.webflow_id == webflow_id:
            return leg
        if slug and leg.slug == slug:
            return leg

    return None


# ---------------------------------------------------------------------------
# Validation per entity type
# ---------------------------------------------------------------------------

def validate_bill_entry(entry: dict, bill_gt) -> TestResult:
    """Validate a bill query response against ground truth."""
    response = entry.get("response", "")

    # Validate bill title keywords
    expected = bill_gt.name_keywords()
    passed, found, missing = validate_response(
        response, expected, validation="contains_any", min_matches=2,
    )

    return TestResult(
        test_id=f"prod-bill-{bill_gt.slug}",
        category="bills",
        entity_type="bill",
        entity_name=bill_gt.name,
        entity_slug=bill_gt.slug,
        prompt=entry.get("message", ""),
        response_text=response,
        response_preview=response[:500],
        confidence=entry.get("confidence", 0.0),
        has_citations=len(entry.get("citations", [])) > 0,
        citation_count=len(entry.get("citations", [])),
        latency=entry.get("duration_ms", 0) / 1000.0,
        passed=passed,
        expected_data=expected,
        found_data=found,
        missing_data=missing,
        validation_mode="contains_any",
        data_source="webflow_cms",
        jurisdiction=bill_gt.jurisdiction,
    )


def validate_org_entry(entry: dict, org_gt) -> TestResult:
    """Validate an organization query response against ground truth."""
    response = entry.get("response", "")

    # Validate org name is mentioned
    expected = [org_gt.name]
    passed, found, missing = validate_response(response, expected, validation="contains")

    return TestResult(
        test_id=f"prod-org-{org_gt.slug}",
        category="organizations",
        entity_type="organization",
        entity_name=org_gt.name,
        entity_slug=org_gt.slug,
        prompt=entry.get("message", ""),
        response_text=response,
        response_preview=response[:500],
        confidence=entry.get("confidence", 0.0),
        has_citations=len(entry.get("citations", [])) > 0,
        citation_count=len(entry.get("citations", [])),
        latency=entry.get("duration_ms", 0) / 1000.0,
        passed=passed,
        expected_data=expected,
        found_data=found,
        missing_data=missing,
        validation_mode="contains",
        data_source="webflow_cms",
    )


def validate_legislator_entry(entry: dict, leg_gt) -> TestResult:
    """Validate a legislator query response against ground truth."""
    response = entry.get("response", "")

    # Validate legislator name
    expected = [leg_gt.name]
    passed, found, missing = validate_response(response, expected, validation="contains")

    return TestResult(
        test_id=f"prod-leg-{leg_gt.slug}",
        category="legislators",
        entity_type="legislator",
        entity_name=leg_gt.name,
        entity_slug=leg_gt.slug,
        prompt=entry.get("message", ""),
        response_text=response,
        response_preview=response[:500],
        confidence=entry.get("confidence", 0.0),
        has_citations=len(entry.get("citations", [])) > 0,
        citation_count=len(entry.get("citations", [])),
        latency=entry.get("duration_ms", 0) / 1000.0,
        passed=passed,
        expected_data=expected,
        found_data=found,
        missing_data=missing,
        validation_mode="contains",
        data_source="webflow_cms",
        jurisdiction=leg_gt.jurisdiction,
    )


def make_unvalidated_result(entry: dict, query_type: str) -> TestResult:
    """Create an unvalidated TestResult for queries without ground truth."""
    response = entry.get("response", "")
    return TestResult(
        test_id=f"prod-{query_type}-{entry.get('session_id', 'unknown')[:8]}",
        category=query_type,
        entity_type=query_type,
        entity_name="",
        prompt=entry.get("message", ""),
        response_text=response,
        response_preview=response[:500],
        confidence=entry.get("confidence", 0.0),
        has_citations=len(entry.get("citations", [])) > 0,
        citation_count=len(entry.get("citations", [])),
        latency=entry.get("duration_ms", 0) / 1000.0,
        passed=None,
        jurisdiction=entry.get("page_context", {}).get("jurisdiction", ""),
    )


# ---------------------------------------------------------------------------
# JSONL loading
# ---------------------------------------------------------------------------

def load_log_entries(
    log_dir: str,
    start_date: datetime,
    end_date: datetime,
    jurisdiction: str | None = None,
    visitor: str | None = None,
    event_type: str | None = None,
) -> list[dict]:
    """Load log entries from date-partitioned JSONL files."""
    log_path = Path(log_dir)
    entries = []

    current = start_date
    while current <= end_date:
        file_path = log_path / f"{current.strftime('%Y-%m-%d')}.jsonl"
        if file_path.exists():
            with open(file_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Filter out human_active entries
                    if entry.get("human_active"):
                        continue

                    # Filter by event_type if specified
                    if event_type:
                        entry_event = entry.get("event_type", "query_processed")
                        if entry_event != event_type:
                            continue

                    # Filter by jurisdiction if specified
                    if jurisdiction:
                        entry_jurisdiction = entry.get("page_context", {}).get("jurisdiction", "")
                        if entry_jurisdiction and entry_jurisdiction.upper() != jurisdiction.upper():
                            continue

                    # Filter by visitor if specified
                    if visitor:
                        if entry.get("visitor_id") != visitor:
                            continue

                    entries.append(entry)
        current += timedelta(days=1)

    return entries


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def _print_analytics_report(entries: list[dict], verbose: bool = False) -> None:
    """Print analytics metrics from structured event log entries.

    This is a reporting layer over logs, not the source of truth —
    all core metric definitions live in the logger and agent code.
    """
    # Separate event types
    query_events = [e for e in entries if e.get("event_type", "query_processed") == "query_processed"]
    conversation_events = [e for e in entries if e.get("event_type") == "conversation_ended"]
    total = len(query_events)

    if total == 0:
        return

    print(f"\n{'='*70}")
    print("ANALYTICS REPORT")
    print(f"{'='*70}")

    # --- Unique Visitors ---
    visitor_ids = {e.get("visitor_id") for e in query_events if e.get("visitor_id")}
    print(f"\n--- Visitor Metrics ---")
    print(f"  Unique visitors: {len(visitor_ids)}")
    if visitor_ids:
        queries_per_visitor = total / len(visitor_ids)
        print(f"  Avg queries per visitor: {queries_per_visitor:.1f}")

    # --- Success Tiers ---
    system_ok = [e for e in query_events if not e.get("error") and not e.get("handoff_triggered")]
    citation_grounded = [e for e in query_events
                         if e.get("retrieval_count", 0) > 0 and e.get("has_citations")]
    heuristic_ok = [e for e in query_events
                    if e.get("confidence", 0) >= 0.5
                    and not e.get("fallback_used")
                    and not e.get("handoff_triggered")
                    and e.get("has_citations")]

    print(f"\n--- Success Tiers ({total} queries) ---")
    print(f"  System success: {len(system_ok)} ({len(system_ok)/total*100:.1f}%)")
    print(f"  Citation-grounded success: {len(citation_grounded)} ({len(citation_grounded)/total*100:.1f}%)")
    print(f"  Heuristic answer success: {len(heuristic_ok)} ({len(heuristic_ok)/total*100:.1f}%)")

    # --- Intent Distribution ---
    intent_counts: dict[str, int] = {}
    sub_intent_counts: dict[str, int] = {}
    for e in query_events:
        pi = e.get("primary_intent")
        si = e.get("sub_intent")
        if pi:
            intent_counts[pi] = intent_counts.get(pi, 0) + 1
        if si and si != "unknown":
            sub_intent_counts[f"{pi}.{si}"] = sub_intent_counts.get(f"{pi}.{si}", 0) + 1

    if intent_counts:
        print(f"\n--- Intent Distribution ---")
        for intent, count in sorted(intent_counts.items(), key=lambda x: -x[1]):
            print(f"  {intent}: {count} ({count/total*100:.1f}%)")
        if verbose and sub_intent_counts:
            print(f"  Sub-intents:")
            for si, count in sorted(sub_intent_counts.items(), key=lambda x: -x[1])[:15]:
                print(f"    {si}: {count}")

    # --- Fallback & Web Search ---
    fallback_entries = [e for e in query_events if e.get("fallback_used")]
    web_search_entries = [e for e in query_events if e.get("web_search_used")]
    print(f"\n--- Fallback & Web Search ---")
    print(f"  Fallback rate: {len(fallback_entries)} ({len(fallback_entries)/total*100:.1f}%)")
    print(f"  Web search rate: {len(web_search_entries)} ({len(web_search_entries)/total*100:.1f}%)")
    if fallback_entries:
        reasons: dict[str, int] = {}
        for e in fallback_entries:
            r = e.get("fallback_reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason}: {count}")

    # --- Retrieval Miss ---
    retrieval_miss = [e for e in query_events if e.get("retrieval_count", 0) == 0]
    print(f"  Retrieval miss rate: {len(retrieval_miss)} ({len(retrieval_miss)/total*100:.1f}%)")

    # --- Grounding Distribution ---
    grounding_counts: dict[str, int] = {}
    for e in query_events:
        gs = e.get("grounding_status")
        if gs:
            grounding_counts[gs] = grounding_counts.get(gs, 0) + 1
    if grounding_counts:
        print(f"\n--- Grounding Distribution ---")
        for status, count in sorted(grounding_counts.items(), key=lambda x: -x[1]):
            print(f"  {status}: {count} ({count/total*100:.1f}%)")

    # --- Handoff Rates (multi-level) ---
    query_handoffs = [e for e in query_events if e.get("handoff_triggered")]
    print(f"\n--- Handoff Rates ---")
    print(f"  Query handoff rate: {len(query_handoffs)} ({len(query_handoffs)/total*100:.1f}%)")

    session_ids = {e.get("session_id") for e in query_events if e.get("session_id")}
    sessions_with_handoff = {e.get("session_id") for e in query_handoffs if e.get("session_id")}
    if session_ids:
        print(f"  Session handoff rate: {len(sessions_with_handoff)}/{len(session_ids)} "
              f"({len(sessions_with_handoff)/len(session_ids)*100:.1f}%)")

    # --- Conversation Metrics (from conversation_ended events) ---
    if conversation_events:
        turn_counts = [e.get("turn_count", 0) for e in conversation_events]
        durations = [e.get("duration_seconds", 0) for e in conversation_events]
        single_turn = [e for e in conversation_events if e.get("turn_count", 0) == 1]

        print(f"\n--- Conversation Metrics ({len(conversation_events)} conversations) ---")
        if turn_counts:
            print(f"  Avg turns per conversation: {sum(turn_counts)/len(turn_counts):.1f}")
        print(f"  Drop-off rate (1 turn): {len(single_turn)} ({len(single_turn)/len(conversation_events)*100:.1f}%)")
        if durations:
            print(f"  Avg duration: {sum(durations)/len(durations):.0f}s")

        # Terminal state distribution
        terminal_counts: dict[str, int] = {}
        for e in conversation_events:
            ts = e.get("terminal_state", "unknown")
            terminal_counts[ts] = terminal_counts.get(ts, 0) + 1
        print(f"  Terminal states:")
        for state, count in sorted(terminal_counts.items(), key=lambda x: -x[1]):
            print(f"    {state}: {count}")

    # --- Device Distribution ---
    device_counts: dict[str, int] = {}
    for e in query_events:
        dt = e.get("device_type")
        if dt:
            device_counts[dt] = device_counts.get(dt, 0) + 1
    if device_counts:
        print(f"\n--- Device Distribution ---")
        for device, count in sorted(device_counts.items(), key=lambda x: -x[1]):
            print(f"  {device}: {count} ({count/total*100:.1f}%)")


async def evaluate(
    log_dir: str,
    start_date: datetime,
    end_date: datetime,
    jurisdiction: str | None = None,
    verbose: bool = False,
    output: str | None = None,
    visitor: str | None = None,
    event_type: str | None = None,
) -> TestReport:
    """Run offline evaluation against ground truth."""
    # Load log entries
    print(f"Loading query logs from {log_dir}...")
    print(f"  Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    entries = load_log_entries(
        log_dir, start_date, end_date, jurisdiction,
        visitor=visitor, event_type=event_type,
    )
    print(f"  Loaded {len(entries)} entries")

    if not entries:
        print("No entries found. Nothing to evaluate.")
        return TestReport(timestamp=datetime.now().isoformat())

    # Classify queries
    classified: dict[str, list[dict]] = {
        "bill": [],
        "organization": [],
        "legislator": [],
        "general": [],
        "out_of_scope": [],
    }
    for entry in entries:
        query_type = classify_query(entry)
        classified[query_type].append(entry)

    print(f"\nQuery classification:")
    for qtype, elist in classified.items():
        print(f"  {qtype}: {len(elist)}")

    # Determine which ground truth to fetch
    need_bills = len(classified["bill"]) > 0
    need_orgs = len(classified["organization"]) > 0
    need_legs = len(classified["legislator"]) > 0

    entity_types = []
    if need_bills:
        entity_types.append("bills")
    if need_orgs:
        entity_types.append("organizations")
    if need_legs:
        entity_types.append("legislators")

    bills_gt, legislators_gt, orgs_gt = [], [], []
    if entity_types:
        print(f"\nFetching ground truth for: {', '.join(entity_types)}...")
        bills_gt, legislators_gt, orgs_gt = await fetch_ground_truth(
            entity_types=entity_types,
            jurisdiction=jurisdiction,
        )
        print(f"  Bills: {len(bills_gt)}, Legislators: {len(legislators_gt)}, Organizations: {len(orgs_gt)}")

    # Evaluate each entry
    results: list[TestResult] = []

    for entry in classified["bill"]:
        gt = match_bill_ground_truth(entry, bills_gt)
        if gt:
            results.append(validate_bill_entry(entry, gt))
        else:
            results.append(make_unvalidated_result(entry, "bill"))

    for entry in classified["organization"]:
        gt = match_org_ground_truth(entry, orgs_gt)
        if gt:
            results.append(validate_org_entry(entry, gt))
        else:
            results.append(make_unvalidated_result(entry, "organization"))

    for entry in classified["legislator"]:
        gt = match_legislator_ground_truth(entry, legislators_gt)
        if gt:
            results.append(validate_legislator_entry(entry, gt))
        else:
            results.append(make_unvalidated_result(entry, "legislator"))

    for entry in classified["general"]:
        results.append(make_unvalidated_result(entry, "general"))

    for entry in classified["out_of_scope"]:
        results.append(make_unvalidated_result(entry, "out_of_scope"))

    # Generate report
    report = generate_report(results)

    # Print confidence analysis
    print(f"\n--- Confidence Analysis ---")
    low_confidence = [r for r in results if r.confidence < 0.5]
    print(f"  Low confidence (<0.5): {len(low_confidence)} of {len(results)} queries")
    if low_confidence and verbose:
        for r in low_confidence[:10]:
            print(f"    [{r.confidence:.2f}] {r.prompt[:60]}...")

    # Print citation analysis
    with_citations = [r for r in results if r.has_citations]
    print(f"\n--- Citation Analysis ---")
    print(f"  Queries with citations: {len(with_citations)} of {len(results)} ({len(with_citations)/len(results)*100:.1f}%)")
    if with_citations:
        avg_count = sum(r.citation_count for r in with_citations) / len(with_citations)
        print(f"  Average citation count: {avg_count:.1f}")

    # Print the standard report
    print_report(report, verbose=verbose)

    # Print analytics report (for entries with structured event fields)
    _print_analytics_report(entries, verbose=verbose)

    # Save to JSON if output path specified
    output_path = output or f"eval_report_{start_date.strftime('%Y-%m-%d')}.json"
    save_report(report, output_path)

    return report


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Evaluate production query logs against ground truth.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Specific date to evaluate (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of days to evaluate (counting back from --date). Default: 1.",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default="logs/queries",
        help="Path to query log directory. Default: logs/queries.",
    )
    parser.add_argument(
        "--jurisdiction",
        type=str,
        default=None,
        help="Filter by jurisdiction code (e.g., FL, VA, US).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON report path.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed failure information.",
    )
    parser.add_argument(
        "--visitor",
        type=str,
        default=None,
        help="Filter to a specific visitor_id.",
    )
    parser.add_argument(
        "--event-type",
        type=str,
        default=None,
        help="Filter by event type (e.g., query_processed, conversation_ended).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.date:
        end_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        end_date = datetime.now()

    start_date = end_date - timedelta(days=args.days - 1)

    asyncio.run(
        evaluate(
            log_dir=args.log_dir,
            start_date=start_date,
            end_date=end_date,
            jurisdiction=args.jurisdiction,
            verbose=args.verbose,
            output=args.output,
            visitor=args.visitor,
            event_type=args.event_type,
        )
    )


if __name__ == "__main__":
    main()
