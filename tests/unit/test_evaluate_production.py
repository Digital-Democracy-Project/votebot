"""Unit tests for Phase 2 of PLAN-eval-and-cache-hit-logging.md.

Covers the headline metrics computation: denominator slicing by event_type
(§2.1), cache-hit + legacy exclusion from retrieval-miss (§2.2), latency
two-ways (§2.3), and the pinned headline JSON block (§2.8).
"""

from datetime import datetime
from pathlib import Path
import sys

# scripts/ isn't on sys.path by default at test time
sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))

from evaluate_production import (  # noqa: E402
    LEGACY_GROUNDING_STATUS,
    _compute_headline_metrics,
    _is_legacy_cache_hit,
)


def _qp_event(**overrides) -> dict:
    """Build a query_processed event with sensible defaults."""
    base = {
        "event_type": "query_processed",
        "timestamp": "2026-04-30T12:00:00+00:00",
        "session_id": "sess-1",
        "visitor_id": "v_1",
        "page_context": {"type": "bill", "slug": "hr-1"},
        "message": "Summarize this bill",
        "response": "Bill X does Y...",
        "primary_intent": "bill",
        "sub_intent": "summary",
        "confidence": 0.8,
        "retrieval_count": 4,
        "retrieval_sources": ["bill", "bill-webflow"],
        "has_citations": True,
        "citations_count": 2,
        "grounding_status": "grounded",
        "duration_ms": 4500,
        "cache_hit": False,
        "fallback_used": False,
        "web_search_used": False,
    }
    base.update(overrides)
    return base


def _conv_ended(**overrides) -> dict:
    base = {
        "event_type": "conversation_ended",
        "timestamp": "2026-04-30T12:30:00+00:00",
        "session_id": "sess-1",
        "turn_count": 3,
        "duration_seconds": 180,
        "terminal_state": "inactive_end",
    }
    base.update(overrides)
    return base


def _msg_received(**overrides) -> dict:
    base = {
        "event_type": "message_received",
        "timestamp": "2026-04-30T12:00:00+00:00",
        "session_id": "sess-1",
        "visitor_id": "v_1",
        "message": "Summarize this bill",
    }
    base.update(overrides)
    return base


def _build_fixture() -> list[dict]:
    """5 cache misses, 5 cache hits (3 v2 + 1 legacy + 1 v2 with no citations),
    plus 5 message_received and 5 conversation_ended. Used for denominator math.
    """
    events = []

    # 5 cache misses — RAG queries with retrievals + citations
    for i in range(5):
        events.append(_qp_event(
            session_id=f"sess-miss-{i}",
            cache_hit=False,
            retrieval_count=3,
            grounding_status="grounded",
            has_citations=True,
            citations_count=2,
            duration_ms=5000 + i * 100,
        ))

    # 3 cache hits — v2 entries, grounded with citations preserved
    for i in range(3):
        events.append(_qp_event(
            session_id=f"sess-hit-v2-{i}",
            cache_hit=True,
            retrieval_count=4,  # cached metadata, not actual retrieval
            grounding_status="grounded",
            has_citations=True,
            citations_count=2,
            button_type="summary",
            duration_ms=10,  # cache hits are fast
        ))

    # 1 legacy v1 cache hit — grounding_status=legacy_unknown, retrieval_count=None
    events.append(_qp_event(
        session_id="sess-legacy",
        cache_hit=True,
        retrieval_count=None,
        retrieval_sources=None,
        grounding_status=LEGACY_GROUNDING_STATUS,
        has_citations=False,
        citations_count=0,
        button_type="summary",
        duration_ms=8,
    ))

    # 1 cache miss with retrieval_count=0 (genuine retrieval miss)
    events.append(_qp_event(
        session_id="sess-zero-retrieval",
        cache_hit=False,
        retrieval_count=0,
        grounding_status="ungrounded",
        has_citations=False,
        citations_count=0,
        duration_ms=3000,
    ))

    # Non-query event types (must be filtered out of per-query denominators)
    for i in range(5):
        events.append(_msg_received(session_id=f"sess-mr-{i}"))
    for i in range(5):
        events.append(_conv_ended(session_id=f"sess-ce-{i}"))

    return events


def test_is_legacy_cache_hit_recognizes_legacy_marker():
    """Either the grounding_status sentinel OR (cache_hit + None retrieval_count)
    triggers the legacy classification."""
    # Both signals present
    assert _is_legacy_cache_hit({
        "cache_hit": True,
        "retrieval_count": None,
        "grounding_status": LEGACY_GROUNDING_STATUS,
    }) is True
    # Just the grounding sentinel
    assert _is_legacy_cache_hit({
        "cache_hit": True,
        "retrieval_count": 5,
        "grounding_status": LEGACY_GROUNDING_STATUS,
    }) is True
    # Just the retrieval_count=None on a cache hit
    assert _is_legacy_cache_hit({
        "cache_hit": True,
        "retrieval_count": None,
        "grounding_status": "grounded",
    }) is True
    # Normal v2 cache hit — neither signal
    assert _is_legacy_cache_hit({
        "cache_hit": True,
        "retrieval_count": 4,
        "grounding_status": "grounded",
    }) is False
    # Cache miss with zero retrievals — NOT legacy
    assert _is_legacy_cache_hit({
        "cache_hit": False,
        "retrieval_count": 0,
        "grounding_status": "ungrounded",
    }) is False


def test_headline_denominator_excludes_non_query_events():
    """Plan §2.1 — citation/confidence rates must use query_processed events only.

    Fixture has 10 query_processed + 5 message_received + 5 conversation_ended = 20.
    Citation rate must be denominated by 9 (10 query_processed minus 1 legacy hit
    excluded per §2.2 contract), not 20.
    """
    events = _build_fixture()
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )

    assert headline["n_query_processed"] == 10
    assert headline["n_message_received"] == 5
    assert headline["n_conversation_ended"] == 5
    # 1 legacy cache hit excluded from attributable count.
    assert headline["n_attributable"] == 9
    assert headline["n_legacy_cache_hits"] == 1


def test_headline_citation_rate_excludes_legacy():
    """Citation rate denom = n_attributable (9), not n_query_processed (10).

    8 of 9 attributable have citations (5 misses + 3 v2 hits, the
    zero-retrieval miss has no citations). Rate = 8/9 = 0.889.
    """
    events = _build_fixture()
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )
    assert headline["citation_rate"] == round(8 / 9, 4)


def test_headline_retrieval_miss_excludes_cache_and_legacy():
    """Plan §2.2 — retrieval-miss denom is RAG-only (cache-misses, non-legacy).

    Fixture: 5 cache misses (retrieval_count=3) + 1 cache miss with
    retrieval_count=0 + 3 v2 cache hits + 1 legacy hit = 10 total.
    RAG-only = 6 (5 misses + 1 zero-retrieval miss; 3 v2 hits + 1 legacy excluded).
    Misses among RAG-only = 1 (the explicit retrieval_count=0).
    Rate = 1/6.
    """
    events = _build_fixture()
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )
    assert headline["retrieval_miss_rate_excl_cache"] == round(1 / 6, 4)


def test_headline_cache_hit_rate_uses_qp_denominator():
    """Cache-hit rate IS over all query_processed (cache adoption is the metric).

    4 cache hits / 10 query_processed = 0.4.
    """
    events = _build_fixture()
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )
    assert headline["cache_hit_rate"] == 0.4


def test_headline_latency_two_ways():
    """Plan §2.3 — P50/P95 reported across all queries AND RAG-only."""
    events = _build_fixture()
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )
    # All queries: includes the very-fast cache hits (skews P50 down).
    # RAG-only: excludes cache hits + legacy, so only the 5 misses + 1 zero-retrieval miss.
    # Cache-hit latencies are 8-10ms; RAG miss latencies are 3000-5400ms.
    # P50 all should be much lower than P50 rag_only.
    assert headline["p50_latency_ms_all"] < headline["p50_latency_ms_rag_only"]
    # RAG-only P50 should be in the miss-latency range.
    assert 3000 <= headline["p50_latency_ms_rag_only"] <= 5500


def test_headline_pinned_keys():
    """Plan §2.8 — the JSON contract must include exactly these keys."""
    events = _build_fixture()
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )
    expected_keys = {
        "window_days",
        "window_start",
        "window_end",
        "n_query_processed",
        "n_message_received",
        "n_conversation_ended",
        "n_attributable",
        "n_legacy_cache_hits",
        "pass_rate",
        "citation_rate",
        "avg_confidence",
        "fallback_rate",
        "web_search_rate",
        "cache_hit_rate",
        "retrieval_miss_rate_excl_cache",
        "p50_latency_ms_all",
        "p95_latency_ms_all",
        "p50_latency_ms_rag_only",
        "p95_latency_ms_rag_only",
        "bill_history_leak_count",
    }
    assert set(headline.keys()) == expected_keys


def test_headline_bill_history_leak_canary_zero():
    """Fixture has no bill-history sources — canary must read 0."""
    events = _build_fixture()
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )
    assert headline["bill_history_leak_count"] == 0


def test_headline_bill_history_leak_canary_fires_on_regression():
    """If bill-history sneaks back into retrieval_sources, the canary catches it."""
    events = _build_fixture()
    # Inject a regression
    events.append(_qp_event(
        session_id="sess-leak",
        retrieval_sources=["bill", "bill-history"],
    ))
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )
    assert headline["bill_history_leak_count"] == 1


def test_legacy_detection_handles_orphan_null_retrieval_count():
    """PM v5 build review v3 #3 — defense in depth.

    Hypothetical edge case: an event has retrieval_count=None but
    cache_hit=False. Current Phase 1 code only emits this combination
    on legacy v1 cache hits, but if a future bug or upstream change
    creates it, the eval script must still exclude it from RAG denoms
    rather than silently inflating them.
    """
    orphan = _qp_event(
        session_id="sess-orphan",
        cache_hit=False,
        retrieval_count=None,
        # NOTE: grounding_status is "grounded" — no sentinel — but
        # retrieval_count=None alone is enough for the helper to
        # exclude this event from RAG-only denominators.
        grounding_status="grounded",
    )
    assert _is_legacy_cache_hit(orphan) is True


def test_headline_window_dates_are_iso_strings():
    """PM v5 build review v3 — Phase 3 cron parser needs reliable ISO format
    on window_start / window_end. This test locks the contract.
    """
    events = _build_fixture()
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )
    # Strings, not datetimes (after JSON round-trip they'd be strings anyway).
    assert isinstance(headline["window_start"], str)
    assert isinstance(headline["window_end"], str)
    # Parsable as YYYY-MM-DD.
    assert datetime.strptime(headline["window_start"], "%Y-%m-%d").date() == datetime(2026, 4, 24).date()
    assert datetime.strptime(headline["window_end"], "%Y-%m-%d").date() == datetime(2026, 4, 30).date()


def test_percentile_interpolates_correctly_on_small_n():
    """PM v5 build review v3 #2 — the prior int(len*pct) percentile was
    off-by-one on small N. Linear interpolation matches numpy.percentile.
    """
    from evaluate_production import _compute_headline_metrics

    # Build a fixture with 5 RAG-only events at known latencies so we can
    # check the percentile math directly. Only the latencies matter here.
    events = []
    for ms in [1000, 2000, 3000, 4000, 5000]:
        events.append(_qp_event(
            session_id=f"sess-{ms}",
            cache_hit=False,
            retrieval_count=3,
            duration_ms=ms,
            grounding_status="grounded",
            has_citations=True,
        ))
    headline = _compute_headline_metrics(
        events,
        start_date=datetime(2026, 4, 24),
        end_date=datetime(2026, 4, 30),
        days=7,
    )
    # 5 values [1000, 2000, 3000, 4000, 5000].
    # P50 (median) = 3000 (linear interpolation: k = (5-1)*0.5 = 2.0, exact).
    # P95 between idx 3 (4000) and idx 4 (5000), k = (5-1)*0.95 = 3.8, so 4000 + 0.8*1000 = 4800.
    assert headline["p50_latency_ms_all"] == 3000
    assert headline["p95_latency_ms_all"] == 4800
