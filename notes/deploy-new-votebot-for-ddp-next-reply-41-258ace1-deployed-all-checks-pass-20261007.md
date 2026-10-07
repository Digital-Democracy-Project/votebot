# Reply 41: 258ace1 deployed, all checks pass (answers your second reply 40)

Deployed `main` 258ace1a4e80c2ec0aef9799e46db7ecf452be23 on the broker host, 2026-10-07 about 23:35 UTC.
Image `4e55de7258c0` (tag `258ace1`). Healthy in 34 s, 0 restarts. Rollback tag kept: `votebot-ddp-next:758a1df` = `b08291e6fe18`.
Startup log shows `quick_action_buttons_enabled true`; public `/votebot/v1/features` returns true.

## Housekeeping
- `render-env.sh --check`: OK (no re-render).
- `query-log-cleanup.sh --dry-run` as bitnami, no sudo: 0 files to compress, 0 to delete, today's file not listed.
  Not installed to `/etc/cron.daily` yet (that needs sudo; operator step).

## A. Status & votes button (websocket, FL HB 7089)
All asks (button with id only, button with title, typed question, each twice) used the vote tool.
No web or `openstates.org` citations. Our `/explore/FL/2024/HB%207089` page is linked. 0 `Bill not found in OpenStates`.

## B. Saved answers
- FL Summary and Pros&cons, and VA HB 177 second asks: from cache, 3 frames, 103-205 ms, confidence 0.9 on every hit.
- Fresh VA answers: 0.783 (Summary), 0.774 (Pros&cons); expected about 0.795 / 0.777, close enough.
- Log: 6 `ButtonCache: hit`, 2 `ButtonCache: set`, 0 warnings.

## C. "What changed" heading
5 of 5 answers start with exactly `## What changed: H 7089 e2 -> H 7089 er`.
Log shows `Live version diffs` with versions=1, chunks=4 on each. Confidence 0.78 to 0.86.
Note: every answer opens with a sentence saying only the e2 to er comparison was reviewed. It is accurate, just worded a little differently each time.

## D. Regression
- Bill page, 3 asks: all cite only their own chunk.
- Vote question, 3 asks: 0 contain `openstates.org`.
- "Who is Nancy Pelosi?": lookup worked, 11th district named, not UNAVAILABLE.
- Full smoke `--citation-attempts 1`: 6/6 cases passed (including the "index holds no bill-votes or bill-version-diff" check).

## One thing still open
`Vote verification returned empty - could not find legislator or vote` still logs as a warning (6 times in this run, on the Status & votes asks).
The answers themselves are correct and use the vote tool, so it may only be noise, but I have not traced the cause. I changed no code or settings.

Check E (positions, organization page, log facts) was done earlier on `45815a6`; I did not re-run it on this image.
