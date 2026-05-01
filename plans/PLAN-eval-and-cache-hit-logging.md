# Eval Script Fixes + Cache-Hit Logging Fidelity + Recurring Eval Cron

**Status:** Ready for implementation (final — v5)
**Created:** 2026-04-30
**Revised:** 2026-04-30 (v5 — adopts existing Zapier alerting pattern, bumps lock TTL margin to +300s, rejects unknown YAML keys)
**Source:** Production-log audit run on 2026-04-29 → 2026-04-30 (118 events / 45 query_processed events). Triage in this conversation surfaced two real bugs and three eval-script artifacts that misleadingly looked like regressions.

---

## Goal

Make the production eval report trustworthy and run it automatically every 7 days.

The 2026-04-30 audit showed apparent regressions (citation rate 10.2%, confidence 0.27, fallback 0%) that turned out to be a mix of:
- **Two real bugs** in cache-hit logging fidelity (cache hits silently report `grounding_status='ungrounded'` and lose `retrieval_sources`).
- **Three eval-script artifacts** (denominator includes non-query events; cache hits counted as retrieval misses; latency stats not carved out by code path).

Fix both, then put the eval on a weekly cron in ddp-sync so we don't have to remember to run it.

---

## Background: What the Audit Surfaced

### Real bugs

| # | Bug | Where | Effect |
|---|---|---|---|
| **B1** | Cache-hit `grounding_status` is computed from `retrieval_count=0` on the replay path → forces `'ungrounded'` regardless of whether the cached response was originally grounded | `core/agent.py` (cache-hit logging branch) + `services/button_cache.py` (cached payload schema) | Every cache hit logs as ungrounded even when its source response had citations |
| **B2** | Cache-hit `retrieval_sources` is `None` because `retrieval_result=None` is passed to `_log_query` | same | We can't see what document types the cached response was built from |

### Eval-script artifacts (not regressions)

| # | Symptom | Cause |
|---|---|---|
| **E1** | Citation rate 10.2%, confidence 0.27 | Denominator is all 118 events. Only 45 are `query_processed` — the other 73 are `message_received` + `conversation_ended` and don't carry these fields. Real rates: citation 26.7%, confidence 0.70. |
| **E2** | Retrieval miss rate 20% with 0% fallback | The 9 retrieval-miss events are all `cache_hit=True`. By design — cache bypasses retrieval. Eval should exclude cache hits from "retrieval miss." |
| **E3** | P50 latency 7.57s, P95 18.97s | Mixed RAG and cache-hit responses. Cache hits are sub-second; RAG responses dominate the tail. Need separate distributions. |

### What is real

- Citation rate ~27% on `query_processed` is roughly consistent with the 31.6% from the 2026-04-28 audit, both well below the ≥60% Deploy 2 target. **Pre-existing issue, not new.**
- 20% of bill-page traffic on day 1+2 of buttons hit the cache — adoption is real.
- Bill-history leak canary remained 0 ✓.

---

## Phase 1 — Cache-hit logging fidelity (votebot)

### 1.1 Extend cached payload schema

**File:** `src/votebot/services/button_cache.py`

Currently `ButtonCache.set()` stores `{response, citations, confidence, cached_at, button_type}`. Add three fields so the cache-hit path can restore the original event's metadata:

- `retrieval_count` — int
- `retrieval_sources` — list[str] | None (the normalized doc-type vocabulary used elsewhere)
- `grounding_status` — `"grounded"` | `"partial"` | `"ungrounded"`

These are computed on the cache-miss path right before logging — pass them into `_populate_button_cache()` and store alongside the response.

### 1.2 Versioned key prefix with dual-read fallback (preserves cache, marks legacy explicitly)

**File:** `src/votebot/services/button_cache.py`

Change `KEY_PREFIX` from `"votebot:button:"` to `"votebot:button:v2:"`. New writes always go to v2 with the full new schema. **On read miss in v2, fall back to a read-only lookup against the v1 prefix.** If a v1 entry is found, treat it as a legacy entry:
- Return the cached `response` and `citations` so the user still gets a fast path (no cold-start latency hit).
- Tag the returned dict with `grounding_status="legacy_unknown"` and `retrieval_count=None`, `retrieval_sources=None`.
- Do **not** rewrite the v1 entry into v2 (no double-write — keeps the migration window self-cleaning).

The eval script (Phase 2) treats `grounding_status="legacy_unknown"` as "exclude from citation/grounding rates" so the first 7 days of metrics aren't skewed by entries we can't reason about. After 7 days, v1 entries TTL out and the fallback path becomes dead code.

**Phase 1 → Phase 2 contract — null handling on cache-hit events:** legacy v1 cache-hit events emit `grounding_status="legacy_unknown"`, `retrieval_count=null`, `retrieval_sources=null` in the JSONL. The eval script (Phase 2) MUST distinguish these three sentinel values from their non-null counterparts:

- `retrieval_count == null` (legacy v1 hit) → exclude from the retrieval-miss denominator entirely (we don't know if retrieval ran).
- `retrieval_count == 0` (cache-miss with zero retrievals, OR non-cache event with no retrieval) → counts as a retrieval miss.
- `grounding_status == "legacy_unknown"` → exclude from grounding-rate and citation-rate denominators.
- `grounding_status` in `{"grounded", "partial", "ungrounded"}` → include in rate calculations.

Phase 2's denominator filter (§2.2) must check both `cache_hit` AND `grounding_status != "legacy_unknown"` before counting an event toward the retrieval-miss/grounding/citation rates. The PM v5 build review v2 caught a None→0 coercion bug on this seam in Phase 1; the same care applies on Phase 2's read side.

This balances the v1 review concern (#3 — legacy entries shouldn't skew metrics) against the v2 review concern (cache cold-start would slow ~20% of bill-page taps for a week). PM v2 explicitly asked for "dual-read on miss" and this is that.

`reconcile_on_startup()` and `list_cached_keys()` continue to use `KEY_PREFIX` for the canonical namespace; legacy entries are not reconciled (intentional — they expire on their own).

#### Rollback procedure

If the v2 schema or the dual-read path causes unexpected production impact, rollback is:
1. `git revert` the commit that bumped `KEY_PREFIX` to `v2:` and the dual-read change.
2. `sudo systemctl restart votebot`.
3. Any v1 entries that haven't TTLed will be readable again under their original key. v2 entries written after the bump orphan and TTL out on their 7-day clock — same posture as the forward migration, in reverse.

No data loss in either direction. The two prefix namespaces never collide.

### 1.3 Wire cache-hit logging to use the cached metadata

**File:** `src/votebot/core/agent.py`

There are **exactly 4 call sites** of `_log_query` (verified by grep at plan time):

| Line | Context | cache_hit value |
|---|---|---|
| 393 | `process_message` cache-hit branch | True |
| 625 | `process_message` normal path | False (when button) / None |
| 735 | streaming cache-hit branch | True |
| 948 | streaming normal path | False (when button) / None |

All four call sites use keyword arguments only (no positional). Add a new **optional** parameter to `_log_query`:

```python
cache_hit_metadata: dict | None = None,
```

with default `None` so the two non-cache-hit call sites (625, 948) need no edits. Only the cache-hit branches (393, 735) gain a new kwarg pass.

Inside `_log_query`, when `cache_hit_metadata` is set, prefer its `grounding_status`, `retrieval_sources`, `retrieval_count` values over the recomputation derived from `retrieval_result`. When unset (the normal path), behavior is unchanged.

This addresses PM review concern #1 (signature change risks TypeErrors): the only way a TypeError could arise is if a caller positionally passes a value that lands on `cache_hit_metadata`'s slot. None of the 4 call sites do this, and the existing `_log_query` signature uses `*,` to enforce keyword-only — so the change is provably safe.

PR checklist: re-grep `_log_query` before merge to confirm no new call site has appeared between plan time and merge time.

### 1.4 Hand-test before deploy (per the cache-hit feedback memory)

After Phase 1 ships:
1. Restart votebot locally with `quick_action_buttons_enabled=true` and a Redis up
2. Tap "Summary" on a bill — confirms cache miss, populates cache
3. Tap "Summary" again on the same bill — confirms cache hit
4. Inspect the JSONL: cache-hit event must have `grounding_status` matching the miss event (not blindly `ungrounded`), and `retrieval_sources` populated

This is non-negotiable per the lesson from commit `88b9dd2` (the cache-hit hang) — server-side metrics aren't sufficient validation for cache paths.

### 1.5 Automated test for the streaming cache-hit branch

Per PM review concern #6: the streaming cache-hit path is the higher-risk branch and the hardest to manually verify. Add an automated test (Phase 4 row) that mocks Redis, primes a v2 cached payload, runs `process_message_stream` end-to-end, and asserts:
- the event's `grounding_status`, `retrieval_sources`, `retrieval_count` match the cached values
- the `cache_hit=True`, `button_type=...` fields are populated
- the streaming chunks are emitted in the expected order (text chunk before done chunk — the bug class fixed in `88b9dd2`)

---

## Phase 2 — Eval script denominator + cache-hit handling (votebot)

**File:** `scripts/evaluate_production.py`

### 2.1 Slice by event_type before computing per-query metrics

All metrics that require query-level fields (`has_citations`, `confidence`, `fallback_used`, `web_search_used`, `retrieval_count`) must use `events_qp = [e for e in events if e['event_type'] == 'query_processed']` as the denominator, not the whole event list.

Conversation-level metrics (avg turns, drop-off, terminal state) keep `conversation_ended` events as their denominator. Print both Ns explicitly in the report header so the reader can tell the difference.

### 2.2 Exclude cache hits from retrieval-miss denominator

A cache hit has `retrieval_count == 0` by design. The current "Retrieval miss rate" metric counts these as misses, conflating two different things. Recompute as:

```
events_qp_non_cached = [e for e in events_qp if not e.get('cache_hit')]
retrieval_miss_rate = sum(
    1 for e in events_qp_non_cached if (e.get('retrieval_count') or 0) == 0
) / len(events_qp_non_cached)
```

### 2.3 Carve out latency stats two ways

Compute and print both:
- **All queries** (P50/P95) — what users experience
- **RAG-only** (excluding `cache_hit=True`) — measures the actual response pipeline

Both are useful: cache adoption is a feature, not noise, but mixing them hides the RAG tail.

### 2.4 Add a cache-hit breakdown section

New section in the report:

```
--- Cache-hit breakdown ---
  Cache hits:      9 of 45 query_processed (20.0%)
  By button:       summary 7, pros_cons 2, status_votes 0
  Avg latency_ms:  X (cache hits) vs Y (cache misses)
  Citation rate on hits:  X% (sanity check — should match miss rate after Phase 1)
```

The "citation rate on hits should match miss rate" check is the feedback loop for whether Phase 1 actually fixed B1/B2 — once cache hits restore citation metadata, the rate of `has_citations=True` should be roughly the same on hits as on misses for the same button type. If it isn't, something is still wrong with the cache schema.

### 2.5 Per-sub-intent and per-button citation rates

Already partly present (the script breaks down by category/jurisdiction). Add explicit cross-tabs:

- citation rate by `sub_intent` (summary, pros_cons, status_votes, support_opposition, vote_history) — surfaces where Deploy 2 is failing
- citation rate by `cache_hit ∈ {True, False, None}` — sanity check
- citation rate by `button_type` (None / summary / pros_cons / status_votes) — measures button impact

### 2.6 Output filename — date + window + UTC timestamp

Currently saves to `eval_report_{start_date}.json`. New filename:

```
eval_report_{end_date}_last{days}d_{HHMMSS_utc}.json
```

The `HHMMSS_utc` suffix prevents collisions when a manual trigger fires close to the scheduled run (PM review concern #7). With the suffix, two runs on the same day produce two distinct files; without it, the second silently overwrites the first.

### 2.7 Add a one-line summary at the top of stdout

For the cron-driven flow, the first 10 lines of stdout should include the headline numbers so a tail of the cron log answers "did anything regress?" without parsing JSON:

```
=== VoteBot eval — last 7 days (2026-04-23 → 2026-04-30) ===
N=315 query_processed (+218 message_received, +97 conversation_ended)
Citation rate: 28.4%   (target ≥60%)
Avg confidence: 0.72   Avg latency P50/P95: 4.1s / 11.2s   (cache-excluded: 5.3s / 13.7s)
Fallback rate: 1.6%    Cache hit rate: 18.4%
bill_history_leak_count: 0 ✓
```

### 2.8 JSON report headline schema (pinned)

The cron pipeline will parse a small set of headline metrics from the saved JSON. Pin them in the JSON output as a top-level `headline` block so the parsing contract is stable:

```json
{
  "headline": {
    "window_days": 7,
    "window_start": "2026-04-23",
    "window_end": "2026-04-30",
    "n_query_processed": 315,
    "n_message_received": 218,
    "n_conversation_ended": 97,
    "pass_rate": 0.512,
    "citation_rate": 0.284,
    "avg_confidence": 0.72,
    "fallback_rate": 0.016,
    "web_search_rate": 0.012,
    "cache_hit_rate": 0.184,
    "retrieval_miss_rate_excl_cache": 0.061,
    "p50_latency_ms_all": 4127,
    "p95_latency_ms_all": 11214,
    "p50_latency_ms_rag_only": 5341,
    "p95_latency_ms_rag_only": 13701,
    "bill_history_leak_count": 0
  },
  "report": { ... existing structure ... }
}
```

This addresses PM review spec gap #8 — downstream parsers (the cron pipeline, future dashboards) get a stable contract instead of fishing keys out of the legacy nested structure.

---

## Phase 3 — Recurring eval cron via ddp-sync APScheduler

### 3.1 Job design

**Goal:** Run `evaluate_production.py --days 7` once a week, archive the JSON report, log a one-line summary, and surface non-zero `bill_history_leak_count` immediately.

**Why ddp-sync, not votebot:** the README and memory both flag that "VoteBot no longer runs a scheduler — it is a chat-only service." Sync scheduling already lives in ddp-sync's APScheduler. The eval is a maintenance task, same shape as the existing weekly Webflow CMS batch jobs.

**Why subprocess, not import:** ddp-sync should not depend on votebot's package or its venv. Subprocess into votebot's `.venv/bin/python` keeps the two services decoupled. Memory documents the venv paths: votebot `~/votebot/.venv/`, ddp-sync `~/ddp-sync/.venv/` — each has its own dependency tree.

**Co-residence assumption:** ddp-sync and votebot run on the same EC2 instance. This is the durable architecture per `project_deployment.md` memory and shows no sign of changing in the near term. PM review raised a "what if we containerize / split hosts later" concern (#2); the response is the env-driven path resolution in 3.9 — if the path doesn't exist, the job fails loudly with an actionable error message instead of silently misbehaving. We don't pre-build an RPC fallback for a deploy topology that isn't on any roadmap; if/when we split, the fix is to swap the subprocess call for an HTTP call against votebot's existing localhost API. That's a one-function change, not a re-architecture.

### 3.2 New pipeline module

**File:** `src/ddp_sync/pipelines/votebot_eval.py` (new)

Single function `run_votebot_eval(days: int = 7) -> dict[str, Any]`:

1. Resolve votebot path (see 3.9). If unresolvable, log error and return `{"success": False, "error": "votebot_path_missing"}` — do **not** raise.
2. Compute timeout: `timeout_s = max(600, 60 + days * 60)`. PM review concern #5: a 7-day eval typically completes in 1-3 minutes, but a 30-day run during heavy traffic can exceed 10 minutes. Scaling with window size keeps the safety margin without over-provisioning the typical case.
3. `subprocess.run([votebot_path/.venv/bin/python, "scripts/evaluate_production.py", "--days", str(days), "--output", output_path], cwd=votebot_path, capture_output=True, timeout=timeout_s, start_new_session=True)`. The `start_new_session=True` flag puts the child in its own process group so a timeout/SIGKILL propagates to any grandchildren it spawned (PM v2 concern: Python's subprocess `timeout` only kills the top process by default — child processes can leak if the script forked anything).
4. Parse the saved JSON's `headline` block (pinned in 2.8) for metrics. If parsing fails, log error + write failure status to Redis.
5. Compare against previous run (Redis `votebot:eval:last_run`):
   - **Hard alert (always):** `bill_history_leak_count > 0`
   - **Hard alert (configurable, defaults below current baseline so first run is clean):** `citation_rate < threshold_citation_rate` (default `0.20` — current production baseline is ~0.27; the threshold is set below baseline so we don't fire on every Sunday until Deploy 2 lands and the baseline lifts. Tighten via YAML as Deploy 2 rolls out — target end state is `0.50`).
   - **Hard alert (configurable):** `pass_rate < threshold_pass_rate` (default `0.40` — same logic; current production baseline is ~0.50).
   - **Soft alert (delta, always-on):** `citation_rate` dropped > 10pp from previous run
   - **Soft alert (delta, always-on):** `pass_rate` dropped > 10pp from previous run

   This addresses PM v2 concern #1: shipping with thresholds at `0.50/0.80` would fire `regression_detected` on the first run because production hasn't yet hit those targets. Defaults below baseline preserve the "fixed-target" alerting structure while keeping operators desensitized to false positives. Deploy 2's PLAN-log-quality-fixes work owns lifting the baseline; this plan owns wiring the threshold knob.

   Thresholds live in the YAML config block (see 3.3) so updating them post-Deploy-2 doesn't require a code change.
6. Write Redis status (schema in 3.6) and the metric event log (constants in 3.6).
7. Update `votebot:eval:last_run` with this run's headline.
8. **Post Zapier run-summary alert via `push_eval_alert(webhook_url, headline, regression_details)`** (see §3.7). Mirrors `pipelines/legislator_bio.py::push_bio_sync_alert`. Fire-and-forget — never raises, returns bool. Skipped silently if `ZAPIER_WEBHOOK_URL` is not set in env.
9. Return success/failure dict. Exceptions caught and logged — never raised.

### 3.3 Schedule registration

**File:** `src/ddp_sync/scheduler.py`

Register inside `_register_ddp_api_jobs()` (or a new `_register_votebot_eval_job()` for tidiness). Use a YAML-driven config block.

```yaml
# config/sync_schedule.yaml — new block
votebot_eval:
  enabled: true
  frequency: weekly                  # weekly only — eval is expensive
  sync_day: sunday                   # Sunday early UTC = Saturday evening EST
  sync_time_utc: "12:00"
  days: 7                            # window passed to --days
  max_days: 30                       # hard cap accepted by manual trigger (see 3.4)
  votebot_path: "/home/ubuntu/votebot"  # see 3.9 for resolution order
  thresholds:
    # Set below current production baselines so first run isn't a false positive.
    # Tighten as Deploy 2 (citation prompt) lifts the citation_rate baseline.
    citation_rate_floor: 0.20        # alert when citation_rate < this
    pass_rate_floor: 0.40            # alert when pass_rate < this
    delta_drop_pp: 10                # soft-alert when either rate drops by ≥ this many pp
```

Job parameters (PM review concern #7 — concurrency hardening):

```python
self.scheduler.add_job(
    run_votebot_eval_wrapper,
    trigger=CronTrigger(day_of_week="sun", hour=12),
    id="weekly_votebot_eval",
    name="Weekly VoteBot eval",
    replace_existing=True,
    max_instances=1,            # never run two at once
    coalesce=True,              # if multiple fires queue up, run once
    misfire_grace_time=3600,    # 1h grace if scheduler was paused
)
```

Use the daily/weekly toggle pattern from `legislator_bio_sync` so future cadence changes don't need code changes.

### 3.4 Manual trigger endpoint + symmetric lock

ddp-sync already exposes manual trigger endpoints. Add `POST /trigger/votebot-eval` that accepts `{ "days": int }` and runs the pipeline.

**Input validation:**
- `days` is required and must be an integer in `[1, max_days]` where `max_days` comes from the YAML config (default 30). Out-of-range returns `400 Bad Request`. PM v2 concern #4: an accidental `--days 365` would tie up the lock for hours; the cap is a cheap guardrail.
- Re-validate the votebot path (3.9 logic, but live — not just at startup) before invoking. If invalid, return `503 Service Unavailable` with the validation error. PM v2 concern: registration-time validation can go stale if the path is moved during a deploy.

**Concurrency lock — symmetric across both job paths:**

The `run_votebot_eval` function itself acquires the Redis NX lock as its first step, regardless of whether it was invoked by the scheduler or the manual endpoint. Lock semantics:

```python
# Pseudocode — lives inside run_votebot_eval, not in the trigger handler
run_id = uuid4().hex
# Lock TTL is bound to subprocess timeout + 300s safety margin (covers
# subprocess wall-clock + post-processing: JSON parse, Redis writes,
# Zapier POST, retention prune). Addresses PM v3 #1 + v4 lock-margin
# concern: a fixed 30-min TTL would expire mid-run on a 30-day window
# (subprocess timeout 1860s = 31 min). The 300s margin gives ~5 min of
# post-processing headroom — generous for the actual workload (Redis
# writes < 1s, Zapier POST < 30s, prune < 1s).
lock_ttl = timeout_s + 300
acquired = await redis.set("votebot:eval:running", run_id, nx=True, ex=lock_ttl)
if not acquired:
    existing = await redis.get("votebot:eval:running")
    return {"success": False, "error": "already_running", "current_run_id": existing}
try:
    ...do the work...
finally:
    # Only delete if we still own the lock (defensive — fence against TTL-then-takeover)
    current = await redis.get("votebot:eval:running")
    if current == run_id:
        await redis.delete("votebot:eval:running")
```

The manual endpoint's role is just input validation + return-code translation: if `run_votebot_eval` returns `error="already_running"`, the endpoint returns `409 Conflict` with the existing `run_id` and the lock's expiry. If it returns success, the endpoint returns `200 OK` with the headline metrics. PM v2 concern #5: putting the lock acquisition in the wrapper (not the endpoint handler) means scheduled and manual paths share the exact same lock semantics — no asymmetry, no race window between "manual handler checked the lock" and "scheduler started without checking."

`max_instances=1` on the APScheduler job is a belt-and-suspenders defense: the lock is the primary mutex, max_instances prevents APScheduler from queuing a second invocation if the first is still running.

### 3.5 Output archival + retention

- **Eval report files**: `~/votebot/eval_reports/eval_report_{end_date}_last{days}d_{HHMMSS_utc}.json`. Directory tracked via `.gitkeep`; reports gitignored. Cleanup: prune files older than 180 days as the last step of `run_votebot_eval`.
- **`flow_status:votebot_eval` Redis key**: single key, overwritten on each run — bounded size (~few KB), no cleanup needed.
- **`votebot:eval:last_run` Redis key**: single key, overwritten on each run — bounded size, no cleanup needed.
- **`votebot:eval:running` Redis key**: TTL = `subprocess timeout_s + 300s` (dynamic, bound to the actual run's expected duration plus 5-min post-processing margin so it can't expire mid-run). Self-cleaning if a process crashes between subprocess kickoff and the `finally` cleanup.

PM v2 spec gap: retention for `flow_status:*` keys is unbounded growth concern in general, but our schema uses one key per flow_id (a fixed set), so the keyspace under that prefix is O(1) in flow count, not O(N) in run count.

### 3.6 Redis schema + structured-log constants (pinned)

PM review spec gaps #8 and #9: pin the contract so dashboard parsers and log alert rules don't break on a typo.

**Redis keys (all under existing `flow_status:` infrastructure):**

```python
# Per-run flow status (consistent with set_flow_status pattern)
flow_status:votebot_eval = {
    "flow": "votebot_eval",
    "started_at": "2026-05-04T12:00:00Z",
    "completed_at": "2026-05-04T12:02:18Z",
    "duration_seconds": 138,
    "status": "completed",  # "completed" | "failed"
    "trigger": "scheduled",  # "scheduled" | "manual"
    "headline": { ...same shape as JSON headline block... },
    "regressions_detected": false,
    "regression_details": [],  # list of {type, metric, value, threshold}
    "report_path": "~/votebot/eval_reports/eval_report_2026-05-04_last7d_120218.json",
    "error": null,
}

# Persistent across runs — used for delta detection
votebot:eval:last_run = { ...same headline shape, plus completed_at... }

# Concurrency lock (NX + dynamic TTL = subprocess timeout_s + 300)
votebot:eval:running = "<run_id>"
```

**Structured-log metric strings (defined as module-level constants in `votebot_eval.py`):**

```python
METRIC_RUN_COMPLETED = "votebot_eval.scheduled_run_completed"
METRIC_REGRESSION = "votebot_eval.regression_detected"
METRIC_RUN_FAILED = "votebot_eval.run_failed"
# Zapier alert metrics — mirror legislator_bio_sync.alert_* naming
METRIC_ALERT_SENT = "votebot_eval.alert_sent"
METRIC_ALERT_SKIPPED = "votebot_eval.alert_skipped"
METRIC_ALERT_FAILED = "votebot_eval.alert_failed"
METRIC_UNKNOWN_YAML_KEY = "votebot_eval.unknown_yaml_key"
```

Tests assert these exact strings (see Phase 4) so accidental edits surface in CI before reaching production log alert rules.

### 3.7 Zapier alerting (mirrors existing `push_bio_sync_alert` pattern)

ddp-sync already has a Zapier integration that other pipelines (`legislator_bio_sync`, `voatz_brevo`, `webflow_batch::run_webflow_check_org_missing`) use for run-summary alerts. The webhook URL lives in `settings.zapier_webhook_url` (env: `ZAPIER_WEBHOOK_URL`), and Zapier handles routing to Slack and elsewhere on the receiver side. votebot_eval becomes another consumer of the same wire.

Why this beats a fresh Slack webhook:
- Zero new credentials to provision (the URL is already in production env).
- Routing/escalation is configured Zapier-side, not in the codebase, so changing the recipient channel doesn't require a deploy.
- Same `on_failure` / `on_regression` flag pattern Zapier already routes on for `legislator_bio_sync`.
- Avoids reusing VoteBot's `SLACK_BOT_TOKEN` (scoped to user-facing chat→handoff in `#votebot-support`); keeps ddp-sync ↔ votebot decoupled.

#### `push_eval_alert(webhook_url, headline, regression_details)` — new helper

**File:** `src/ddp_sync/pipelines/votebot_eval.py`

Mirror `pipelines/legislator_bio.py::push_bio_sync_alert` exactly:
- Sync HTTP via `requests.post(..., timeout=30)`. Zapier is fire-and-forget; latency doesn't matter; sync is fine.
- Returns `bool`; never raises.
- Skips silently with `metric="votebot_eval.alert_skipped", reason="no_webhook_url"` if URL not configured (don't block the cron when the webhook is down).
- On 2xx: log info with `metric="votebot_eval.alert_sent"` and key headline counts.
- On non-2xx or exception: log error with `metric="votebot_eval.alert_failed"`.

**Payload shape** (Zapier-routable; flat keys, no nested conditionals — Zapier doesn't support Mustache conditional sections):

```python
{
    "alert_type": "votebot_eval_complete",
    "summary": (
        f"n={n_query_processed} citation_rate={citation_rate:.1%} "
        f"pass_rate={pass_rate:.1%} cache_hit_rate={cache_hit_rate:.1%} "
        f"bill_history_leak_count={bill_history_leak_count}"
    ),
    "window_days": 7,
    "window_start": "2026-04-23",
    "window_end": "2026-04-30",
    "n_query_processed": 315,
    "citation_rate": 0.284,
    "pass_rate": 0.512,
    "avg_confidence": 0.72,
    "cache_hit_rate": 0.184,
    "fallback_rate": 0.016,
    "bill_history_leak_count": 0,
    "p50_latency_ms_rag_only": 5341,
    "p95_latency_ms_rag_only": 13701,
    # Threshold flags Zapier routes on (mirrors on_failure / on_large_changes
    # in push_bio_sync_alert). These map directly to Zapier filter rules.
    "on_failure": False,             # status == "failed" or run_votebot_eval errored
    "on_regression": False,          # any regression_details non-empty
    "on_bill_history_leak": False,   # bill_history_leak_count > 0 (specific canary)
    # Pre-formatted warning lines for the Slack template (concat unconditionally;
    # empty string when not active).
    "failure_warning": "",           # "⚠️ Eval run failed: <error>" if on_failure
    "regression_warning": "",        # human-readable regression list if on_regression
    "leak_warning": "",              # "🚨 bill_history_leak_count = N" if on_bill_history_leak
    "report_path": "~/votebot/eval_reports/eval_report_2026-05-04_last7d_120218.json",
    "synced_at": "2026-05-04T12:02:18Z",
    "trigger": "scheduled",          # "scheduled" | "manual"
}
```

The three flag keys (`on_failure`, `on_regression`, `on_bill_history_leak`) are the contract Zapier filters route on. Zapier-side routing config (channel, escalation, who gets pinged) lives in the Zap, not in this plan.

#### Operator pathway

- **Primary signal:** Zapier alert fires after every scheduled and manual run. Slack channel routing handled Zapier-side.
- **Backup / passive review:** Ramon's existing Monday-morning check-in pattern still applies — `redis-cli HGETALL flow_status:votebot_eval` and the saved JSON in `~/votebot/eval_reports/` are unchanged. Useful when reviewing trends across multiple weeks rather than single-run alerts.
- **Health-of-the-cron signal:** if the Zapier alert doesn't fire by Monday morning, the scheduler itself stopped — same observability Ramon already does for daily bill sync (`flow_status:daily_bill_sync` freshness check).

#### Configuration

```yaml
# config/sync_schedule.yaml — votebot_eval block extension
votebot_eval:
  notifications:
    enabled: true                    # set false to suppress Zapier alerts (still logs to Redis + structured log)
    alert_on_success: true           # send the green-path summary too, not just failures
    # Future: per-flag webhook overrides if Zapier-side routing isn't enough
```

The webhook URL itself is sourced from `settings.zapier_webhook_url` (env var), not the YAML — same pattern as the other pipelines.

### 3.8 YAML config validation (pinned contract)

PM v3 #3: malformed YAML must fail loudly, not silently fall through to undefined behavior.

**At job-registration time, validate:**

```python
# Required keys (validated, registration aborts if missing/wrong-type)
votebot_eval.enabled        # bool
votebot_eval.frequency      # "weekly" | "daily"
votebot_eval.sync_day       # one of monday..sunday (only checked when frequency=weekly)
votebot_eval.sync_time_utc  # "HH:MM"
votebot_eval.days           # int in [1, max_days]

# Optional keys (validated if present; falls back to defaults if missing)
votebot_eval.max_days                          # int, 1..90, default 30
votebot_eval.votebot_path                      # string, default /home/ubuntu/votebot
votebot_eval.thresholds.citation_rate_floor    # float in [0.0, 1.0], default 0.20
votebot_eval.thresholds.pass_rate_floor        # float in [0.0, 1.0], default 0.40
votebot_eval.thresholds.delta_drop_pp          # number in [0, 100], default 10
votebot_eval.notifications.enabled             # bool, default true
votebot_eval.notifications.alert_on_success    # bool, default true
```

**Behavior on failure:**
- Missing required key, wrong type, or out-of-range → log `error` with the specific validation failure, **do not register the job**, return early from `_register_votebot_eval_job`.
- Missing optional key → use default, log `info` with the substitution.
- Missing entire `thresholds:` or `notifications:` block → fields fall to their defaults (logged once at info level so the operator knows the defaults are in effect).
- **Unknown key under `votebot_eval:` (or any nested block)** → log `warning` with `metric="votebot_eval.unknown_yaml_key"` and the offending key name, but do not abort registration. Catches typos like `notifications.enabled_alerts` that would silently no-op otherwise. Address PM v4 #2: typo'd keys should be visible.

This is the same posture as path validation in 3.9 — fail loudly at startup on real errors, warn loudly on suspected typos.

### 3.9 Path resolution + loud failure

Resolve `votebot_path` in this order:
1. `VOTEBOT_PATH` env var (highest priority — supports per-deploy override without YAML edit)
2. YAML config `votebot_eval.votebot_path`
3. Default: `/home/ubuntu/votebot`

Validate the resolved path **at job-registration time** (during `scheduler.start()`), not at first run. Required checks:
- Directory exists
- `{path}/.venv/bin/python` exists and is executable
- `{path}/scripts/evaluate_production.py` exists

If any check fails, log a loud error (`logger.error("votebot_eval: path validation failed", ...)`) and **do not register the job**. This way an operator misconfiguration produces an immediate visible error at service start, not a silent 7-day wait followed by a buried subprocess failure.

This is the proportionate response to PM review concern #2 — we get the "fail loudly on misconfig" benefit without speculatively building an RPC fallback for a deploy topology that isn't planned.

---

## Phase 4 — Tests

| File | What it covers |
|---|---|
| `tests/unit/test_button_cache.py` | Extend existing tests: round-trip a payload with `grounding_status`, `retrieval_sources`, `retrieval_count` through `set` → `get` and confirm all fields survive serialization. (Dual-read covered separately below.) |
| `tests/integration/test_streaming_cache_hit.py` (new) | Per PM review concern #6: prime a v2 cached payload in a fakeredis instance, run `process_message_stream` end-to-end, assert (a) the JSONL event has cached `grounding_status` + `retrieval_sources`, (b) `cache_hit=True` + `button_type` fields populated, (c) chunks emit in `text-then-done` order to prevent a regression of the `88b9dd2` hang. |
| `tests/unit/test_evaluate_production.py` (new) | Feed a fixture JSONL that mixes 10 query_processed (5 cache hits, 5 misses) + 5 conversation_ended events. Assert: citation-rate denom = 10, retrieval-miss-rate denom = 5 (cache hits excluded), latency P50 computed twice (all + RAG-only), output JSON includes the pinned `headline` block (2.8). |
| `tests/unit/test_button_cache_dual_read.py` (new) | Cover the dual-read fallback: (a) v2 hit returns full schema, (b) v2 miss + v1 hit returns response with `grounding_status="legacy_unknown"` and no retrieval metadata, (c) v2 miss + v1 miss returns None, (d) writes only ever go to v2 (asserted via spy on `set`). |
| `ddp-sync tests/test_votebot_eval.py` (new) | Mock `subprocess.run` and the JSON file write. Assert: pipeline (a) parses headline metrics, (b) sets Redis flow status with the pinned schema, (c) emits the three pinned metric strings (`METRIC_RUN_COMPLETED`, `METRIC_REGRESSION`, `METRIC_RUN_FAILED`) — assert the literal string values to catch typos, (d) regression-detect branch fires on `bill_history_leak_count > 0`, on `citation_rate < threshold` (test with threshold=0.30 and citation_rate=0.20), AND on a 12pp citation-rate drop. (e) Threshold defaults are `0.20`/`0.40` (so a fresh production-baseline run does **not** fire the fixed-target alert — explicit assertion). (f) Lock is acquired/released by the wrapper, not just the endpoint. (g) Concurrent lock contention test — second invocation while first holds the lock returns `error="already_running"`. (h) Timeout scales with `days`. (i) `start_new_session=True` is set on the subprocess call. |
| `ddp-sync tests/test_scheduler_path_validation.py` (new) | Assert `_register_votebot_eval_job` skips registration + logs error when `votebot_path` doesn't exist or is missing the venv/script. Also assert the manual trigger endpoint re-validates path live and returns 503 if invalid. |
| `ddp-sync tests/test_manual_trigger_validation.py` (new) | Assert: (a) `days` outside `[1, max_days]` returns 400. (b) Lock-held path returns 409 with `current_run_id`. (c) Path-invalid live returns 503. |
| `ddp-sync tests/test_yaml_validation.py` (new) | Assert behavior for malformed `votebot_eval` YAML: (a) missing required key → registration aborts + error log. (b) `thresholds:` block missing → defaults applied + info log. (c) `thresholds.citation_rate_floor` non-numeric or out-of-range [0,1] → registration aborts + error log. (d) `days > max_days` in YAML at start → error + abort. |
| `ddp-sync tests/test_lock_ttl_binding.py` (new) | Assert TTL on `votebot:eval:running` Redis key equals `timeout_s + 300` (where `timeout_s` is the same value passed to `subprocess.run`). Mock a 30-day eval to force `timeout_s = 60 + 30*60 = 1860s` and assert the lock TTL is set to `2160s`, not the legacy `1800s`. PM v3 #1 + v4 lock-margin — prevents the silent double-run that the v3 plan would have allowed. |
| `ddp-sync tests/test_zapier_alert.py` (new) | Mock `requests.post`. Mirrors `tests/test_push_bio_sync_alert.py` pattern. Assert: (a) payload contains all required keys (alert_type, summary, the three on_* flags, the three *_warning lines, headline metrics, report_path, synced_at, trigger). (b) on_failure / on_regression / on_bill_history_leak are computed correctly from the headline + regression_details. (c) Empty webhook URL → log `votebot_eval.alert_skipped` and return False without raising. (d) Non-2xx → log `votebot_eval.alert_failed` and return False. (e) 2xx → log `votebot_eval.alert_sent` and return True. (f) Network exception → log `votebot_eval.alert_failed` and return False (never raise). (g) `notifications.enabled=false` → alert is not sent (caller skip). (h) `notifications.alert_on_success=false` → alert sent only when on_failure/on_regression/on_bill_history_leak is true. |

We deliberately do **not** add a cross-service Docker integration test (PM review concern #6 alt). The smoke test in Phase 5 (manual trigger via `POST /trigger/votebot-eval` against the deployed environment) covers the same surface at a fraction of the maintenance cost; the unit tests cover the logic shape; CI integration of two repos in Docker is overengineered for our deploy reality.

---

## Phase 5 — Deployment

Two deploys, in order:

### Deploy V (votebot)

1. Phase 1 + Phase 2 + Phase 4 unit tests merge to `main`.
2. SSH to EC2, `cd ~/votebot && git pull && sudo systemctl restart votebot`.
3. Hand-test cache-hit cycle (per 1.4) on production traffic — tap "Summary" on a bill twice, pull JSONL, verify the second event has populated `grounding_status` + `retrieval_sources` matching the first.
4. Run `./.venv/bin/python scripts/evaluate_production.py --days 2` and confirm:
   - Citation rate / confidence reflect query_processed-only denominator
   - Retrieval miss rate excludes cache hits
   - Cache-hit breakdown section prints
   - Output JSON has the pinned `headline` block
5. Confirm Redis no longer holds any `votebot:button:` (v1) keys after a few hours of natural turnover, OR run a one-shot `SCAN MATCH votebot:button:*` (without `:v2:`) to verify v1 keys are decaying as expected. If v1 is still substantial after 7 days, manually `DEL` the surviving keys.

### Deploy S (ddp-sync)

1. Phase 3 + tests merge to ddp-sync `main`.
2. Add the `votebot_eval` block to `~/ddp-sync/config/sync_schedule.yaml` on EC2.
3. `cd ~/ddp-sync && git pull && sudo systemctl restart ddp-sync`.
4. Verify path validation (3.9) passed by tailing the structlog output for `votebot_eval: registered` (or the loud error if not).
5. Hit `POST /trigger/votebot-eval { "days": 7 }` to smoke-test end-to-end.
6. Verify Redis `flow_status:votebot_eval` is populated with the pinned schema and the JSON report landed in `~/votebot/eval_reports/`.
7. Wait for the first scheduled Sunday run and confirm it ran on its own (check Redis + the structured-log metric event).

---

## Phase 6 — Audit-all-paths follow-up (per the audit memory)

Per the feedback memory `feedback_audit_all_paths_after_fix.md`: a fix targeting one path leaves latent bugs in parallel paths. After Deploy V:

- Re-grep `_log_query` and confirm only the 4 expected call sites exist. (Done at plan time; re-do at PR time to catch drift.)
- Grep for any other code path that constructs `AgentResult(cached=True, ...)`. Same check — does it correctly preserve grounding metadata?
- Grep `votebot:button:` (without `v2:`) anywhere else in the codebase that might construct keys outside `make_key()`. The bump only matters if it's the single source of truth.
- After the first weekly cron run, eyeball the cache-hit citation rate. If it's dramatically different from cache-miss for the same button_type, the metadata round-trip is broken somewhere we missed.

---

## Push-back on PM reviews (overengineering / out of scope)

### Declined from PM review v1

| Concern | Disposition | Why |
|---|---|---|
| **RPC fallback for non-co-resident topology** (high severity) | Declined. Replaced with env-driven path + loud-fail validation (3.9). | Co-residence is the durable architecture per `project_deployment.md`. Pre-building an RPC layer for a deploy topology that isn't on any roadmap is speculative work. The migration path if/when it changes is a one-function swap. |
| **Cross-service Docker integration test in CI** (medium severity) | Declined. Replaced with manual-trigger smoke test in Phase 5.5. | Setting up two-repo Docker CI for a single weekly cron is disproportionate. The unit tests cover the logic; the smoke test covers the integration; the structured-log metric covers the observability. |

### Adopted from PM reviews v2/v3/v4

The repeated reviewer push for an alerting integration was correct. v5 adopts the existing Zapier pattern (§3.7) — same wire as `legislator_bio_sync` and `voatz_brevo`. Declines from prior revisions are superseded:
- v2 + v3 "Slack/email" concern: **resolved via Zapier** (§3.7). The `on_failure` / `on_regression` / `on_bill_history_leak` flags map directly to Zapier filter rules that already route to Slack on the receiver side. No new credentials, no new code path beyond the helper function.

### Declined from PM review v2

| Concern | Disposition | Why |
|---|---|---|
| **Performance test measuring latency impact of prefix bump** (low severity) | Declined. Mitigation is built into the design (dual-read fallback in 1.2 means existing entries remain readable, so there's no cold-start period to measure). | The dual-read approach eliminates the latency-impact scenario that motivated the test. If real production traffic shows unexpected slowdown anyway, the rollback procedure (1.2) is one revert + restart away. |

### Declined from PM review v3

| Concern | Disposition | Why |
|---|---|---|
| **Write-through v1→v2 on dual-read fallback** (low severity) | Declined. | The "thrash" is two GETs per legacy hit for at most 7 days; on our scale (single EC2 instance, hundreds of bill-page taps/day) this is negligible Redis load. Adding a write-through introduces a third state (v1-and-v2-coexist) and complicates the invariant "v2 is the single source of truth." Simpler is better here. |

### Declined from PM review v4

| Concern | Disposition | Why |
|---|---|---|
| **Heartbeat-loop refresh on the lock TTL** (high severity per reviewer) | Declined. Replaced with bumped static margin (timeout_s + 300s). | A heartbeat loop that periodically extends the lock during a long subprocess adds threading complexity (or an asyncio task) for marginal benefit. The 300s margin already covers post-processing (Redis writes, Zapier POST, prune) by ~10x. If post-processing ever exceeds 5 min, the right fix is to investigate why, not to paper over it with a heartbeat. |
| **Fallback operator / OOO escalation path** (low severity) | Declined. | This is a single-operator project today. Documenting an escalation path would be fiction. If/when a second operator joins, the upgrade is a Zapier-side routing change (add a second recipient channel), not a code change. |
| **Integration test simulating clock-skewed long runs** (medium severity per reviewer) | Declined. | Unit tests assert the TTL value; the actual long-running behavior is exercised every Sunday in production. A clock-skew integration test would mock everything that matters and prove nothing the unit test doesn't. |
| **CloudWatch alert before ship** (low severity per reviewer) | Declined as a v1 requirement; covered by the §3.7 Zapier integration. | Zapier-side routing is the alerting layer; CloudWatch can be added later if Zapier proves insufficient. |

---

## Open Questions

1. **Eval window for the cron — 7 vs 14 days?** Started with 7 to match Ramon's existing 7/14/30 mental model and keep the cron output legible. YAML-tunable, easy to bump.
2. **Should the cron also surface low-confidence outliers?** Could add a "top 10 low-confidence queries" section. Defer — not a regression-detection requirement.
3. **When to tighten the threshold floors?** Defaults are set below current production baseline (citation `0.20`, pass `0.40`) so first run is clean. Once Deploy 2 lifts the citation rate above 0.40-ish stably, we should bump the floor to `0.30` then `0.50`. This is a small follow-up, not a code change — just a YAML edit on EC2 + restart.

---

## Out of Scope

- Fixing the underlying citation rate (the 28% citation rate on RAG-only paths is the Deploy 2 problem). Tracked under PLAN-log-quality-fixes Deploy 2.
- Splitting `evaluate_production.py` into smaller modules.
- Migrating cache invalidation tracking to PostgreSQL.
- Per-visitor or cohort-level metric breakdowns.
- Cross-service Docker integration testing in CI.
- RPC abstraction over the subprocess call.

---

## Success Criteria

After Deploy V + Deploy S:

- [ ] A user tapping "Summary" twice on the same bill produces two JSONL events where the cache-hit event has `grounding_status` and `retrieval_sources` matching the cache-miss event.
- [ ] No JSONL events emit `votebot:button:` (v1) cache reads after 7 days post-deploy.
- [ ] `evaluate_production.py --days 7` reports citation rate, confidence, fallback rate, retrieval miss rate using `query_processed`-only denominators, and exposes the pinned `headline` JSON block.
- [ ] The eval report breaks out cache-hit volume, per-button counts, and RAG-only latency.
- [ ] APScheduler runs the eval automatically every Sunday at 12:00 UTC with `max_instances=1` and `coalesce=True`.
- [ ] Redis `flow_status:votebot_eval` is populated with the pinned schema within 60 seconds of completion.
- [ ] Manual trigger fired during a scheduled run returns `409 Conflict` instead of double-running.
- [ ] Non-zero `bill_history_leak_count` produces an `error`-level structured log with `metric="votebot_eval.regression_detected"` (literal string).
- [ ] Citation rate < `citation_rate_floor` or pass rate < `pass_rate_floor` (configurable; defaults set below current production baseline) produces a `regression_detected` log even without a step-change from the previous run. **First post-Deploy-V/S Sunday run must NOT fire `regression_detected` from the threshold check** — only from delta or `bill_history_leak_count > 0`. If it fires from the threshold check, the YAML defaults are misconfigured.
- [ ] Manual trigger with `days > max_days` returns 400. Manual trigger fired during a scheduled run returns 409 with `current_run_id`. Manual trigger with invalid `votebot_path` returns 503.
- [ ] Existing v1 cached entries remain readable for 7 days post-deploy via the dual-read fallback (no user-visible cold-start).
- [ ] Each scheduled and manual eval run posts a Zapier alert with `alert_type="votebot_eval_complete"`. Confirmed by `metric="votebot_eval.alert_sent"` log entry within 60 seconds of subprocess completion.
- [ ] When `bill_history_leak_count > 0` or `regressions_detected=true`, the Zapier payload sets the corresponding flag (`on_bill_history_leak` / `on_regression` / `on_failure`) so Zapier's filter rules can route to Slack.
- [ ] When `ZAPIER_WEBHOOK_URL` is not set in env, the cron still completes successfully and logs `metric="votebot_eval.alert_skipped"` instead of crashing.
- [ ] Lock TTL is `timeout_s + 300s` (verified by the new test fixture; e.g. for `--days 30`, lock TTL = 2160s).
- [ ] Unknown YAML keys under `votebot_eval:` produce a `votebot_eval.unknown_yaml_key` warning at startup (typo guard).
