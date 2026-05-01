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
import statistics
import sys
from datetime import datetime, timedelta, timezone
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
# Phase 2 — denominator-correct metrics + pinned headline block
# ---------------------------------------------------------------------------

# Sentinel from button_cache.py — entries with this grounding_status are
# legacy v1 cache hits and are excluded from grounding/citation/retrieval-miss
# rates per the Phase 1 → Phase 2 contract documented in plan §1.2.
LEGACY_GROUNDING_STATUS = "legacy_unknown"


def _is_legacy_cache_hit(event: dict) -> bool:
    """True if this event lacks attributable retrieval/grounding metadata
    and should be excluded from grounding/citation/retrieval-miss rate
    denominators.

    Three sentinel patterns, any one of which is sufficient (defense in
    depth — the agent code sets all three together for legacy v1 hits,
    but checking each independently means a single field corruption or
    upstream bug can't silently mis-classify the event):

    1. ``grounding_status == "legacy_unknown"`` — the canonical sentinel.
    2. ``cache_hit is True AND retrieval_count is None`` — a cache hit
       without retrieval metadata.
    3. ``retrieval_count is None`` (regardless of cache_hit) — defends
       against any future code path that emits None-valued retrieval_count
       for a non-cache event. PM v5 build review v3 flagged this edge:
       without it, such events would silently inflate the RAG denominator
       and skew retrieval-miss + citation rates downward.

    The function name preserves the original "legacy_unknown" framing
    because that is the dominant case in production data; it's effectively
    "should be excluded from RAG-attribution rates" but with a more
    descriptive sentinel name.
    """
    return (
        event.get("grounding_status") == LEGACY_GROUNDING_STATUS
        or event.get("retrieval_count") is None
    )


def _compute_headline_metrics(
    all_entries: list[dict],
    start_date: datetime,
    end_date: datetime,
    days: int,
) -> dict:
    """Compute the pinned headline JSON block (plan §2.8).

    Denominators are sliced by event_type first (plan §2.1). Cache-hit
    semantics:
    - retrieval_miss_rate_excl_cache excludes both cache hits and legacy
      v1 entries — only "real" RAG queries with zero retrievals count as
      misses (plan §2.2).
    - p50/p95 latency reported twice: across all query_processed events,
      and across the RAG-only subset (plan §2.3).
    - citation/grounding rates exclude legacy v1 hits per the §1.2
      contract — they have no metadata to attribute.
    """
    qp = [e for e in all_entries if e.get("event_type", "query_processed") == "query_processed"]
    mr = [e for e in all_entries if e.get("event_type") == "message_received"]
    ce = [e for e in all_entries if e.get("event_type") == "conversation_ended"]
    n_qp = len(qp)

    # Citation/grounding rates: exclude legacy v1 hits (no metadata).
    qp_attributable = [e for e in qp if not _is_legacy_cache_hit(e)]
    n_attr = len(qp_attributable)

    citation_count = sum(1 for e in qp_attributable if e.get("has_citations"))
    citation_rate = citation_count / n_attr if n_attr else 0.0

    confidences = [e.get("confidence") for e in qp_attributable if e.get("confidence") is not None]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    # Cache-hit rate: across all query_processed events (cache hits ARE part
    # of user-facing traffic; this measures adoption, not quality).
    cache_hits = [e for e in qp if e.get("cache_hit") is True]
    cache_hit_rate = len(cache_hits) / n_qp if n_qp else 0.0

    # Retrieval miss rate: exclude cache hits + legacy entries (plan §2.2).
    # ``retrieval_count == 0`` is intentional zero ("we tried, got nothing").
    # ``retrieval_count is None`` is "unknown" (legacy) — already filtered.
    rag_only = [e for e in qp if not e.get("cache_hit") and not _is_legacy_cache_hit(e)]
    retrieval_misses = [e for e in rag_only if e.get("retrieval_count") == 0]
    retrieval_miss_rate_excl_cache = (
        len(retrieval_misses) / len(rag_only) if rag_only else 0.0
    )

    # Fallback / web search rates over query_processed.
    fallback = [e for e in qp if e.get("fallback_used")]
    fallback_rate = len(fallback) / n_qp if n_qp else 0.0
    web_search = [e for e in qp if e.get("web_search_used")]
    web_search_rate = len(web_search) / n_qp if n_qp else 0.0

    # Latency two ways (plan §2.3). Linear interpolation between adjacent
    # ranks (same algorithm as numpy.percentile / Excel PERCENTILE.INC) so
    # P50 on small N doesn't drift one rank high. PM v5 build review v3.
    def _percentile(values: list[float], pct: float) -> int:
        if not values:
            return 0
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        if n == 1:
            return int(sorted_vals[0])
        k = (n - 1) * pct
        f = int(k)
        c = min(f + 1, n - 1)
        if f == c:
            return int(sorted_vals[f])
        return int(sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f]))

    latencies_all = [e.get("duration_ms", 0) for e in qp if e.get("duration_ms")]
    latencies_rag = [e.get("duration_ms", 0) for e in rag_only if e.get("duration_ms")]

    # Bill-history leak canary.
    leak_count = sum(
        1 for e in qp if "bill-history" in (e.get("retrieval_sources") or [])
    )

    # Pass rate is computed over validated TestResults later — we don't have
    # them here. Caller injects pass_rate after validation completes.
    return {
        "window_days": days,
        "window_start": start_date.strftime("%Y-%m-%d"),
        "window_end": end_date.strftime("%Y-%m-%d"),
        "n_query_processed": n_qp,
        "n_message_received": len(mr),
        "n_conversation_ended": len(ce),
        "n_attributable": n_attr,  # qp minus legacy_unknown
        # Computed directly via _is_legacy_cache_hit() rather than
        # n_qp - n_attr so this metric stays accurate if future filters
        # add other exclusion reasons. PM v5 build review v3.
        "n_legacy_cache_hits": sum(1 for e in qp if _is_legacy_cache_hit(e)),
        "pass_rate": None,  # injected later from TestReport
        "citation_rate": round(citation_rate, 4),
        "avg_confidence": round(avg_confidence, 4),
        "fallback_rate": round(fallback_rate, 4),
        "web_search_rate": round(web_search_rate, 4),
        "cache_hit_rate": round(cache_hit_rate, 4),
        "retrieval_miss_rate_excl_cache": round(retrieval_miss_rate_excl_cache, 4),
        "p50_latency_ms_all": _percentile(latencies_all, 0.50),
        "p95_latency_ms_all": _percentile(latencies_all, 0.95),
        "p50_latency_ms_rag_only": _percentile(latencies_rag, 0.50),
        "p95_latency_ms_rag_only": _percentile(latencies_rag, 0.95),
        "bill_history_leak_count": leak_count,
    }


def _print_headline_summary(headline: dict) -> None:
    """One-line-per-row top-of-stdout summary (plan §2.7).

    The cron pipeline tails the first ~10 lines of stdout to answer
    "did anything regress?" without parsing JSON. Keep this compact and
    scannable.
    """
    print()
    print("=" * 70)
    print(
        f"=== VoteBot eval — last {headline['window_days']} days "
        f"({headline['window_start']} → {headline['window_end']}) ==="
    )
    print(
        f"N={headline['n_query_processed']} query_processed "
        f"(+{headline['n_message_received']} message_received, "
        f"+{headline['n_conversation_ended']} conversation_ended)"
    )
    if headline['n_legacy_cache_hits']:
        print(
            f"  (excluded from rates: {headline['n_legacy_cache_hits']} "
            f"legacy v1 cache hits)"
        )
    cite_pct = headline['citation_rate'] * 100
    print(f"Citation rate: {cite_pct:.1f}%   (target ≥60%)")
    print(
        f"Avg confidence: {headline['avg_confidence']:.2f}   "
        f"Avg latency P50/P95: {headline['p50_latency_ms_all']/1000:.1f}s / "
        f"{headline['p95_latency_ms_all']/1000:.1f}s   "
        f"(RAG-only: {headline['p50_latency_ms_rag_only']/1000:.1f}s / "
        f"{headline['p95_latency_ms_rag_only']/1000:.1f}s)"
    )
    print(
        f"Fallback rate: {headline['fallback_rate']*100:.1f}%    "
        f"Cache hit rate: {headline['cache_hit_rate']*100:.1f}%"
    )
    leak = headline['bill_history_leak_count']
    print(f"bill_history_leak_count: {leak} {'✓' if leak == 0 else '*** REGRESSION ***'}")
    print("=" * 70)


def _print_cache_hit_breakdown(qp: list[dict]) -> None:
    """Cache-hit breakdown section (plan §2.4)."""
    n_qp = len(qp)
    if n_qp == 0:
        return

    cache_hits = [e for e in qp if e.get("cache_hit") is True]
    cache_misses = [e for e in qp if e.get("cache_hit") is False]
    if not cache_hits:
        return  # nothing to break down

    by_button: dict[str, int] = {}
    for e in cache_hits:
        bt = e.get("button_type") or "unknown"
        by_button[bt] = by_button.get(bt, 0) + 1

    hit_lat = [e.get("duration_ms", 0) for e in cache_hits if e.get("duration_ms")]
    miss_lat = [e.get("duration_ms", 0) for e in cache_misses if e.get("duration_ms")]

    hit_cited = sum(1 for e in cache_hits if e.get("has_citations"))
    miss_cited = sum(1 for e in cache_misses if e.get("has_citations"))

    print(f"\n--- Cache-hit breakdown ---")
    print(
        f"  Cache hits:      {len(cache_hits)} of {n_qp} query_processed "
        f"({len(cache_hits)/n_qp*100:.1f}%)"
    )
    button_str = ", ".join(f"{k} {v}" for k, v in sorted(by_button.items()))
    print(f"  By button:       {button_str}")
    if hit_lat or miss_lat:
        avg_hit = (sum(hit_lat) / len(hit_lat)) if hit_lat else 0
        avg_miss = (sum(miss_lat) / len(miss_lat)) if miss_lat else 0
        print(
            f"  Avg latency_ms:  {avg_hit:.0f} (cache hits) vs "
            f"{avg_miss:.0f} (cache misses)"
        )
    if cache_hits:
        print(
            f"  Citation rate on hits:  {hit_cited/len(cache_hits)*100:.1f}% "
            f"(should match miss rate after Phase 1: "
            f"{miss_cited/len(cache_misses)*100 if cache_misses else 0:.1f}%)"
        )


def _print_subintent_breakdown(qp: list[dict]) -> None:
    """Per-sub-intent + per-button citation rate cross-tabs (plan §2.5)."""
    if not qp:
        return

    qp_attr = [e for e in qp if not _is_legacy_cache_hit(e)]

    # Citation rate by sub_intent
    by_sub: dict[str, dict[str, int]] = {}
    for e in qp_attr:
        si = e.get("sub_intent") or "none"
        d = by_sub.setdefault(si, {"n": 0, "cited": 0})
        d["n"] += 1
        if e.get("has_citations"):
            d["cited"] += 1
    if by_sub:
        print(f"\n--- Citation rate by sub_intent ---")
        for si, d in sorted(by_sub.items(), key=lambda x: -x[1]["n"]):
            rate = d["cited"] / d["n"] * 100 if d["n"] else 0
            print(f"  {si:<25s} n={d['n']:3d}  cited={d['cited']:3d}  rate={rate:5.1f}%")

    # Citation rate by button_type
    by_btn: dict[str, dict[str, int]] = {}
    for e in qp_attr:
        bt = e.get("button_type") or "none"
        d = by_btn.setdefault(bt, {"n": 0, "cited": 0})
        d["n"] += 1
        if e.get("has_citations"):
            d["cited"] += 1
    if len(by_btn) > 1:  # only interesting if buttons were used
        print(f"\n--- Citation rate by button_type ---")
        for bt, d in sorted(by_btn.items(), key=lambda x: -x[1]["n"]):
            rate = d["cited"] / d["n"] * 100 if d["n"] else 0
            print(f"  {bt:<15s} n={d['n']:3d}  cited={d['cited']:3d}  rate={rate:5.1f}%")


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

    # --- Retrieval Miss (cache hits + legacy excluded per plan §2.2) ---
    # Cache hits have retrieval_count=0 by design; legacy v1 entries have None.
    # Neither is a "real" retrieval miss — they didn't run the retrieval path.
    rag_only = [e for e in query_events if not e.get("cache_hit") and not _is_legacy_cache_hit(e)]
    retrieval_miss = [e for e in rag_only if e.get("retrieval_count") == 0]
    if rag_only:
        print(
            f"  Retrieval miss rate: {len(retrieval_miss)} of {len(rag_only)} RAG-only "
            f"({len(retrieval_miss)/len(rag_only)*100:.1f}%) "
            f"[excludes {total - len(rag_only)} cache-hits + legacy entries]"
        )
    else:
        print(f"  Retrieval miss rate: 0 / 0 RAG-only (all events are cache hits or legacy)")

    # --- Grounding Distribution (legacy_unknown shown separately) ---
    grounding_counts: dict[str, int] = {}
    for e in query_events:
        gs = e.get("grounding_status")
        if gs:
            grounding_counts[gs] = grounding_counts.get(gs, 0) + 1
    if grounding_counts:
        print(f"\n--- Grounding Distribution ---")
        # Exclude legacy_unknown from the rate calculation; report separately.
        attributable = sum(c for s, c in grounding_counts.items() if s != LEGACY_GROUNDING_STATUS)
        for status, count in sorted(grounding_counts.items(), key=lambda x: -x[1]):
            if status == LEGACY_GROUNDING_STATUS:
                print(f"  {status}: {count} (excluded from rate denominator)")
            elif attributable:
                print(f"  {status}: {count} ({count/attributable*100:.1f}% of attributable)")
            else:
                print(f"  {status}: {count}")

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

    # --- Bill-history leak canary (Fix F2 in PLAN-quick-action-buttons) ---
    # Permanent regression check: any non-zero count means stale bill-history
    # chunks are being retrieved despite the data-layer removal. Investigate
    # immediately if this surfaces — likely indicates ddp-sync regenerated
    # the docs (Fix F1 regressed) or a new code path is producing them.
    leak_count = sum(
        1 for e in query_events
        if "bill-history" in (e.get("retrieval_sources") or [])
    )
    print(f"\n--- Bill-history leak canary ---")
    if leak_count == 0:
        print(f"  bill_history_leak_count: 0  (clean)")
    else:
        print(f"  bill_history_leak_count: {leak_count}  *** REGRESSION — investigate ***")
        # Surface a few examples so the operator has something to grep on
        examples = [
            e for e in query_events
            if "bill-history" in (e.get("retrieval_sources") or [])
        ][:3]
        for ex in examples:
            ts = ex.get("timestamp", "")[:19]
            pc_type = (ex.get("page_context") or {}).get("type") or "none"
            msg = (ex.get("message") or "")[:60]
            print(f"    {ts} [{pc_type}] {msg!r}")


async def evaluate(
    log_dir: str,
    start_date: datetime,
    end_date: datetime,
    jurisdiction: str | None = None,
    verbose: bool = False,
    output: str | None = None,
    visitor: str | None = None,
    event_type: str | None = None,
    days: int = 1,
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

    # Compute and print the headline summary FIRST so the cron log tail
    # answers "did anything regress?" without scrolling (plan §2.7).
    headline = _compute_headline_metrics(entries, start_date, end_date, days)
    _print_headline_summary(headline)

    # Slice by event_type before classification (plan §2.1). Without this,
    # message_received + conversation_ended events get classified as
    # "general" and inflate the TestResult denominator.
    query_events = [
        e for e in entries
        if e.get("event_type", "query_processed") == "query_processed"
    ]

    # Classify queries (only over query_processed events).
    classified: dict[str, list[dict]] = {
        "bill": [],
        "organization": [],
        "legislator": [],
        "general": [],
        "out_of_scope": [],
    }
    for entry in query_events:
        query_type = classify_query(entry)
        classified[query_type].append(entry)

    print(f"\nQuery classification (n={len(query_events)} query_processed events):")
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

    # New Phase 2 breakdowns (cache-hit + per-sub-intent + per-button)
    _print_cache_hit_breakdown(query_events)
    _print_subintent_breakdown(query_events)

    # Inject pass_rate into the headline now that validation has run.
    headline["pass_rate"] = round(report.pass_rate / 100, 4) if report.pass_rate else 0.0

    # Save the augmented report (plan §2.6 + §2.8). Filename includes the
    # window end date + days + UTC HHMMSS so successive runs don't collide.
    if output:
        output_path = output
    else:
        now_utc = datetime.now(timezone.utc)
        output_path = (
            f"eval_report_{end_date.strftime('%Y-%m-%d')}_last{days}d_"
            f"{now_utc.strftime('%H%M%S')}.json"
        )
    _save_augmented_report(report, headline, output_path)

    return report


def _save_augmented_report(report: TestReport, headline: dict, output_path: str) -> None:
    """Save report JSON with the pinned ``headline`` block at the top.

    The cron pipeline (Phase 3) parses the headline block as a stable
    contract — keeping all the existing report fields nested under
    ``report`` so future dashboard parsers don't need to fish keys out.
    """
    from dataclasses import asdict

    augmented = {
        "headline": headline,
        "report": {
            "timestamp": report.timestamp,
            "total_tests": report.total_tests,
            "passed": report.passed,
            "failed": report.failed,
            "not_validated": report.not_validated,
            "errors": report.errors,
            "pass_rate": report.pass_rate,
            "avg_confidence": report.avg_confidence,
            "avg_latency": report.avg_latency,
            "p95_latency": report.p95_latency,
            "citation_rate": report.citation_rate,
            "results_by_category": report.results_by_category,
            "results_by_entity_type": report.results_by_entity_type,
            "results_by_jurisdiction": report.results_by_jurisdiction,
            "results_by_mode": report.results_by_mode,
            "all_results": [asdict(r) for r in report.all_results],
        },
    }
    with open(output_path, "w") as f:
        json.dump(augmented, f, indent=2, default=str)
    print(f"\nReport saved to: {output_path}")


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
            days=args.days,
        )
    )


if __name__ == "__main__":
    main()
