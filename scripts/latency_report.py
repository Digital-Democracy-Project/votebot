#!/usr/bin/env python3
"""Latency report from VoteBot's query logs (VOTEBOT-53): where the time goes, against the README targets.

Reads the `query_processed` events in `logs/queries/*.jsonl` (the fields from VOTEBOT-44: `first_token_ms`,
`marks_ms.model_call_start`, `phase_ms`, next to `duration_ms`) and prints p50 / p90 / p95 and the sample size for
`duration_ms`, `first_token_ms`, the wait before the model is asked, and every phase, cut by what the message was.
Needs no network and no keys; run it on the host's log directory, once before a change and once after, and keep the
two reports with the SHAs that were deployed.

    python scripts/latency_report.py --log-dir /opt/votebot-logs/queries --days 7
    python scripts/latency_report.py --log-dir logs/queries --json > report.json

Phases overlap since VOTEBOT-45 and VOTEBOT-47 (they run at the same time), so their durations are NOT parts of one
total: read `first_token_ms` and `model_call_start_ms` for what a visitor waits.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

# README "Performance Targets": p50 end to end, p95 end to end, first token
TARGETS_MS = {"duration_ms": {"p50": 2500, "p95": 5000}, "first_token_ms": {"p50": 1500}}
SMALL_SAMPLE = 20


def percentile(values: list[float], pct: float) -> float:
    """Linear interpolation between adjacent ranks (numpy.percentile's default), so p50 on a small sample does not
    drift one rank high. Not rounded: a verdict against a strict target compares this, and only the display rounds."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def read_events(log_dir: Path, days: int | None = None, today: date | None = None) -> tuple[list[dict], int]:
    """`query_processed` events from the date-partitioned files (`YYYY-MM-DD.jsonl`), the last `days` days if given,
    and how many lines were not JSON (skipped, and counted so a damaged log does not look complete)."""
    cutoff = (today or date.today()) - timedelta(days=days - 1) if days else None
    events: list[dict] = []
    skipped = 0
    for path in sorted(log_dir.glob("*.jsonl")):
        if cutoff is not None:
            try:
                if date.fromisoformat(path.stem) < cutoff:
                    continue
            except ValueError:
                continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(entry, dict) and entry.get("event_type") == "query_processed":
                events.append(entry)
    return events, skipped


def cuts(events: list[dict]) -> dict[str, list[dict]]:
    """The groups the report is split by. The cuts OVERLAP: a message can be in several (a button on a bill page that
    used the live tool), so they are views of the same messages, not segments that add up."""
    page = lambda e: (e.get("page_context") or {}).get("type")  # noqa: E731
    groups = {
        "all": events,
        "bill page, not from the saved answer": [e for e in events if page(e) == "bill" and not e.get("cache_hit")],
        "live bill lookup used": [e for e in events if e.get("bill_votes_tool_used")],
        "saved answer (button cache hit)": [e for e in events if e.get("cache_hit")],
        "general page": [e for e in events if page(e) == "general"],
    }
    for button in sorted({e["button_type"] for e in events if e.get("button_type")}):
        groups[f"button: {button}"] = [e for e in events if e.get("button_type") == button]
    return {name: rows for name, rows in groups.items() if rows}


def metric_values(events: list[dict]) -> dict[str, list[float]]:
    """Each metric's values over the events that have it."""
    metrics: dict[str, list[float]] = {}

    def add(name: str, value: object) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            metrics.setdefault(name, []).append(value)

    for e in events:
        add("duration_ms", e.get("duration_ms"))
        add("first_token_ms", e.get("first_token_ms"))
        add("model_call_start_ms", (e.get("marks_ms") or {}).get("model_call_start"))
        for phase, ms in (e.get("phase_ms") or {}).items():
            add(f"phase: {phase}", ms)
    return metrics


def summarize(events: list[dict]) -> dict[str, dict[str, dict[str, float]]]:
    return {
        cut: {
            metric: {"n": len(v), "p50": percentile(v, 0.5), "p90": percentile(v, 0.9), "p95": percentile(v, 0.95)}
            for metric, v in metric_values(rows).items()
        }
        for cut, rows in cuts(events).items()
    }


def label(metric: str) -> str:
    """first_token_ms is measured on the server, from the agent starting the message to its first text: it leaves out
    the time before the agent starts (the frame's arrival), so the visitor's own wait is a little longer."""
    return "first_token_ms (server side)" if metric == "first_token_ms" else metric


def verdicts(summary: dict) -> list[str]:
    """Each README target, said plainly: met, not met, or no data."""
    overall = summary.get("all", {})
    lines = []
    for metric, wanted in TARGETS_MS.items():
        stats = overall.get(metric)
        for pct, limit in wanted.items():
            if not stats:
                lines.append(f"{label(metric)} {pct} < {limit} ms: NO DATA")
                continue
            got = stats[pct]  # unrounded, against a strict "<"
            verdict = "MET" if got < limit else "NOT MET"
            lines.append(f"{label(metric)} {pct} < {limit} ms: {verdict} ({got:.0f} ms, n={stats['n']})")
    return lines


def render(summary: dict) -> str:
    out: list[str] = []
    for cut, metrics in summary.items():
        out.append(f"\n### {cut}\n")
        out.append("| metric | n | p50 ms | p90 ms | p95 ms |")
        out.append("| --- | ---: | ---: | ---: | ---: |")
        ordered = sorted(metrics, key=lambda m: (m.startswith("phase:"), m))
        for metric in ordered:
            s = metrics[metric]
            note = " (small sample)" if s["n"] < SMALL_SAMPLE else ""
            out.append(f"| {metric}{note} | {s['n']} | {s['p50']:.0f} | {s['p90']:.0f} | {s['p95']:.0f} |")
    out.append("\n### README targets (all messages)\n")
    out += [f"- {line}" for line in verdicts(summary)]
    out.append(
        "\nPhases overlap: read first_token_ms and model_call_start_ms for the visitor's wait, not the sum of phases. "
        "The cuts overlap too: a message can be in several."
    )
    return "\n".join(out).lstrip("\n")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--log-dir", type=Path, default=Path("logs/queries"))
    parser.add_argument("--days", type=int, help="only the last N days (by file date)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--skip-button", action="append", default=[], metavar="NAME",
                        help="leave out messages from this quick-action button (repeatable), e.g. a button whose saved answers would skew a comparison")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.log_dir.is_dir():
        print(f"no such log directory: {args.log_dir}", file=sys.stderr)
        return 2
    events, skipped = read_events(args.log_dir, args.days)
    if args.skip_button:
        events = [e for e in events if e.get("button_type") not in args.skip_button]
    if skipped:
        print(f"warning: {skipped} line(s) in {args.log_dir} were not JSON and were skipped", file=sys.stderr)
    if not events:
        print(f"no query_processed events in {args.log_dir}" + (f" for the last {args.days} days" if args.days else ""))
        return 1
    summary = summarize(events)
    shown = {cut: {m: {k: round(v, 1) for k, v in stats.items()} for m, stats in metrics.items()} for cut, metrics in summary.items()}
    print(json.dumps({"events": len(events), "skipped_lines": skipped, "summary": shown, "targets": verdicts(summary)}, indent=1) if args.json
          else f"{len(events)} query_processed events\n\n{render(summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
