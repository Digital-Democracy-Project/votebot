# Found: "Unknown" party breakdown on federal bill vote-history answers

**Date:** 2026-09-20
**Branch:** cut from `main` @ `503fb99` (VOTEBOT-2 DDP OpenStates replica routing).
**Audience:** dev agent picking up a fix for this.

## What was found

Production query log (`logs/queries/2026-09-20.jsonl`, 19:07 UTC) shows a user asking "What is
the latest status and vote history for ... (HR 9576)?" on the bill page. The bot's answer,
generated via the live Bill Info Tool (`bill_votes_tool_used: true`, not the RAG path), included:

```
Party Breakdown:
- Democratic: 112 Yes, 53 No, 2 Other
- Republican: 165 Yes, 0 No, 4 Other
- Unknown: 75 Yes, 19 No, 3 Other
```

97 of 433 recorded votes (~22%) came back with no party affiliation.

## Root cause

Confirmed by pulling the live vote data for this bill through this environment's actual
configured route (`use_ddp_openstates_replica=True` -> `api.digitaldemocracyproject.org/openstates`,
per `openstates_client.py`) -- not the public `v3.openstates.org` API.

`src/votebot/services/bill_votes.py::_parse_votes()` gets each voter's party two ways:

1. From the nested `voter` object OpenStates embeds per vote record (`voter_obj.get("party")`).
2. When `voter` is `None`, falls back to `legislator_parties.get(voter_name.lower(), "")`, a dict
   built by `_get_legislator_parties()` from `/people?jurisdiction=us`, keyed by **full name**
   (e.g. `"aaron bean" -> "Republican"`).

For HR 9576's House passage vote, OpenStates returns `voter: None` for any member whose last name
collides with another current member, and disambiguates `voter_name` with a state suffix instead
(e.g. `"Bean (FL)"`, `"Carter (GA)"`, `"Garcia (TX)"`, `"Davis (IL)"`). That string never matches
a full-name key like `"aaron bean"`, so the fallback lookup misses every time, `party` stays `""`,
and `format_bill_info_document()` (line ~415: `party = v.party or "Unknown"`) buckets it as
"Unknown".

Verified directly: of 433 vote records on this bill, exactly **97 have `voter: None`**, split
**75 Yes / 19 No / 3 Not Voting** -- an exact match to the "Unknown" counts the bot reported.

## The fix already exists, just isn't wired in here

`src/votebot/utils/federal_legislator_cache.py` solves this precise problem -- it generates name
variants per legislator (`"Bean"`, `"Bean (R-FL)"`, `"Bean (R)"`) specifically to match OpenStates'
disambiguated federal vote-record format. But it's only wired into `core/retrieval.py` (the
RAG/`legislator-votes` ingestion path). `bill_votes.py` -- the live real-time tool path that
actually answered this HR 9576 query -- has its own separate, naive full-name-only matcher in
`_get_legislator_parties()` and never touches `FederalLegislatorCache`.

## Suggested fix

In `bill_votes.py::_parse_votes()`, when `voter` is `None` and the `legislator_parties` full-name
lookup misses, fall back to `FederalLegislatorCache.lookup_with_info(voter_name)` (already handles
the `"Bean (FL)"` / `"Bean"` formats) before defaulting to `"Unknown"`. Only applies when
`jurisdiction == "us"` -- state bills go through a different `voter_obj`-populated path and
weren't observed to have this gap.

## Not yet done

- No code changes made on this branch -- diagnosis only.
- Haven't checked whether this also affects the RAG-ingested `bill-votes`/`legislator-votes`
  documents (DDP-Sync side, `src/ddp_sync/sync/build_legislator_votes.py`) or is scoped to
  VoteBot's live tool path only.
- Haven't checked whether the same collision-driven `voter: None` gap shows up on Senate votes
  (this bill only had a House roll call).
