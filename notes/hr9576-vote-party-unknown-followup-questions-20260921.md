# Follow-up on HR9576 "Unknown" party bug — questions for the prod agent, not a rescrape request

**Date:** 2026-09-21
**Follow-up to:** `notes/hr9576-vote-party-unknown-root-cause-20260920.md` (this same branch, 2026-09-20).
**Filed as:** VOTEBOT-7, linked to OPEN-2 (Jira).
**Ask:** answer the questions below so we can confirm or rule out a theory. **Do not trigger a
rescrape of this bill** — we haven't confirmed the theory yet and want the diagnostic data from
the *existing* stuck state first.

## What's been confirmed since the original note (correcting an earlier wrong turn)

An initial pass concluded the root cause was `openstates-core` PR #2 (the `resolve_person()`
identifier-matching fix) never having been merged from `cherry-pick-line` into `main`. **That
conclusion was wrong** — verified directly by reading the actual file content (not GitHub code
search, which gave a false negative):

- `openstates-core`'s `main` has the complete fix: identifier-based `resolve_person()` matching,
  plus a newer `org_classification` cache-key fix layered on top (most recent related commit
  2026-09-13).
- `openstates-scrapers`'s `main` has its half too (`id=bioguide`/`id=lis_id` passed through on
  `vote()`/`yes()`/`no()`).
- Pulled the real source directly: `clerk.house.gov/evs/2026/roll309.xml` (HR9576's actual House
  passage roll call, referenced in our own stored vote's `sources`/`extras.house-rollcall-num`).
  Confirmed `name-id="B001314"` for "Bean (FL)" and `name-id="C001103"` for "Carter (GA)" —
  matched exactly against `other_identifiers` (`scheme: bioguide`) already stored on those
  people's Person records via api-v3 (`/people?jurisdiction=us&name=Bean`). Code and data both
  check out as individually correct.
- `ddp-scrapers`'s ECR history (`350941939790.dkr.ecr.us-east-1.amazonaws.com/ddp-scrapers`) shows
  frequent rebuilds, not staleness: `v23` pushed 2026-09-15, `v24` 2026-09-19, `v25` 2026-09-20
  (task-def `ddp-scrapers` revision 30, registered 2026-09-20 17:16 EDT — the current live one).

## Current working theory

Given code + data both check out, the leading theory is that this isn't a currently-broken
pipeline at all: HR9576's vote was scraped and imported once (probably around 2026-09-16, the
vote's own `start_date`), the import failed to resolve these particular voters for whatever
reason was live *at that moment*, and OpenStates' importer doesn't retry person resolution on a
later scrape of unchanged vote content (`items_differ()`-style noop skip in
`BaseImporter.import_item()`) — so the bad rows are just stuck, the same shape of gap `OPEN-2`
originally needed `backfill-vote-person-resolution.py` for, not evidence the live pipeline is
still broken today.

**Not testing this by rescraping** (per the ask above) because a fresh scrape could resolve
correctly today regardless of whether the theory is right, for reasons unrelated to it (e.g. if
the gap was something transient at the original import time) — that wouldn't actually confirm or
rule out anything about *why* the original import failed.

## Questions (answer what you can from RDS/CloudWatch access we don't have from here)

1. What are `opencivicdata_voteevent.created_at`/`updated_at` for this specific vote (identifier
   `us-2026-lower-309`, bill `HR 9576`, 119th Congress, `ocd-vote/35993ecf-1e82-4abe-a594-068ed13f3525`
   per api-v3)? Does `created_at` land around 2026-09-16?
2. Which Fargate task/image tag actually ran the scrape+import that first created this vote event —
   checkable via `/ecs/ddp-scrapers` CloudWatch logs around 2026-09-16, or `pupa_runplan`/ddp-sync's
   own scrape-trigger records? Did that run predate `v23`, or use something older?
3. For that specific run, what did `pupa_importobjects` (or equivalent) record for this vote_event
   — insert, or already a noop against a prior row? And did `resolve_person()` log an error
   (`"no people returned for spec"` etc.) for the affected voter names at that time?
4. Has `backfill-vote-person-resolution.py` (or an equivalent) been run since 2026-08-01 (when
   `cherry-pick-line` was retired and this fix's final form landed on `openstates-core` `main`)?
   If yes, when, and did its scope include federal (`us`) jurisdiction bill votes?
5. Broadly: are there other federal vote_events *first imported* since 2026-08-01 that still have
   unresolved (`voter_id` null) rows for surname-collision members? If yes, that would point to a
   live, ongoing gap rather than a one-time stuck-data backfill need, and the theory above would
   be wrong.

Reply on this branch (or wherever's natural) — no urgency to trigger anything in the meantime.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
