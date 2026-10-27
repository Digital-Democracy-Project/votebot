"""VOTEBOT-53: the latency report reads the query log and says plainly which README targets are met."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import latency_report as lr  # noqa: E402


def _event(duration=3000, first=1200, model_start=900, page="bill", tool=False, button=None, cache=False, phases=None, **extra):
    return {
        "event_type": "query_processed", "duration_ms": duration, "first_token_ms": first,
        "marks_ms": {"model_call_start": model_start, "first_token": first}, "phase_ms": phases or {"retrieval": 400},
        "page_context": {"type": page}, "bill_votes_tool_used": tool, "button_type": button, "cache_hit": cache, **extra,
    }


def _write(directory: Path, name: str, events: list[dict], junk: list[str] = ()):
    lines = [json.dumps(e) for e in events] + list(junk)
    (directory / f"{name}.jsonl").write_text("\n".join(lines) + "\n")


class TestPercentile:
    def test_it_interpolates_between_ranks_like_numpy(self):
        assert lr.percentile([100, 200], 0.5) == 150
        assert lr.percentile([10, 20, 30, 40], 0.5) == 25
        assert lr.percentile(list(range(1, 101)), 0.95) == 95  # 95.05 rounds to 95

    def test_one_value_and_none(self):
        assert lr.percentile([700], 0.95) == 700
        assert lr.percentile([], 0.5) == 0

    def test_the_input_order_does_not_matter(self):
        assert lr.percentile([3, 1, 2], 0.5) == 2


class TestReading:
    def test_only_query_processed_events_and_not_junk_lines(self, tmp_path):
        _write(tmp_path, "2026-10-27", [_event(), {"event_type": "message_received"}, {"event_type": "conversation_ended"}],
               junk=["not json {", ""])
        assert len(lr.read_events(tmp_path)) == 1

    def test_the_last_days_by_file_date(self, tmp_path):
        for day in ("2026-10-20", "2026-10-25", "2026-10-27"):
            _write(tmp_path, day, [_event(duration=int(day[-2:]))])
        got = lr.read_events(tmp_path, days=3, today=date(2026, 10, 27))
        assert sorted(e["duration_ms"] for e in got) == [25, 27]  # the 20th is older than three days

    def test_a_file_that_is_not_a_date_is_left_out_when_days_are_asked_for(self, tmp_path):
        _write(tmp_path, "notes", [_event()])
        assert lr.read_events(tmp_path, days=7, today=date(2026, 10, 27)) == []
        assert len(lr.read_events(tmp_path)) == 1  # but read when no window is asked for


class TestCuts:
    def test_each_message_lands_in_every_cut_it_belongs_to(self):
        events = [
            _event(page="bill", tool=True, button="status_votes"),
            _event(page="bill", cache=True, button="summary"),
            _event(page="general"),
        ]
        groups = lr.cuts(events)
        assert len(groups["all"]) == 3
        assert len(groups["bill page, not from the saved answer"]) == 1
        assert len(groups["live bill lookup used"]) == 1
        assert len(groups["saved answer (button cache hit)"]) == 1
        assert len(groups["general page"]) == 1
        assert len(groups["button: status_votes"]) == 1 and len(groups["button: summary"]) == 1

    def test_a_cut_with_no_messages_is_left_out(self):
        assert "general page" not in lr.cuts([_event(page="bill")])


class TestMetrics:
    def test_the_fields_phases_and_the_wait_before_the_model(self):
        values = lr.metric_values([_event(duration=4000, first=1500, model_start=1000, phases={"retrieval": 300, "legislator_lookup": 50})])
        assert values["duration_ms"] == [4000] and values["first_token_ms"] == [1500] and values["model_call_start_ms"] == [1000]
        assert values["phase: retrieval"] == [300] and values["phase: legislator_lookup"] == [50]

    def test_an_event_without_the_new_fields_still_counts_for_duration(self):
        old = {"event_type": "query_processed", "duration_ms": 5000, "page_context": {"type": "bill"}}
        values = lr.metric_values([old])
        assert values == {"duration_ms": [5000]}

    def test_non_numbers_are_ignored(self):
        assert lr.metric_values([{"duration_ms": "fast", "first_token_ms": None, "phase_ms": {"retrieval": True}}]) == {}


class TestTargets:
    def _summary(self, **kw):
        return lr.summarize([_event(**kw) for _ in range(30)])

    def test_met(self):
        text = "\n".join(lr.verdicts(self._summary(duration=2000, first=1000)))
        assert text.count("MET") == 3 and "NOT MET" not in text

    def test_not_met_says_so_with_the_numbers(self):
        text = "\n".join(lr.verdicts(self._summary(duration=6000, first=2000)))
        assert "duration_ms p50 < 2500 ms: NOT MET (6000 ms, n=30)" in text
        assert "duration_ms p95 < 5000 ms: NOT MET" in text and "first_token_ms p50 < 1500 ms: NOT MET" in text

    def test_no_first_token_data_is_said_to_be_no_data(self):
        old = [{"event_type": "query_processed", "duration_ms": 2000, "page_context": {"type": "bill"}}]
        assert "first_token_ms p50 < 1500 ms: NO DATA" in "\n".join(lr.verdicts(lr.summarize(old)))


class TestOutput:
    def test_a_small_sample_is_flagged_and_the_overlap_is_explained(self):
        text = lr.render(lr.summarize([_event() for _ in range(3)]))
        assert "(small sample)" in text and "Phases overlap" in text and "p90 ms" in text

    def test_main_prints_a_report(self, tmp_path, capsys):
        _write(tmp_path, "2026-10-27", [_event(duration=1000 + i) for i in range(25)])
        assert lr.main(["--log-dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "25 query_processed events" in out and "### all" in out and "README targets" in out

    def test_main_json(self, tmp_path, capsys):
        _write(tmp_path, "2026-10-27", [_event()])
        assert lr.main(["--log-dir", str(tmp_path), "--json"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body["events"] == 1 and "all" in body["summary"] and body["targets"]

    def test_no_events_and_no_directory_are_said(self, tmp_path, capsys):
        assert lr.main(["--log-dir", str(tmp_path)]) == 1
        assert "no query_processed events" in capsys.readouterr().out
        assert lr.main(["--log-dir", str(tmp_path / "missing")]) == 2
