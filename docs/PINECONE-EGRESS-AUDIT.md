# Pinecone read egress: who reads, how much, and what to measure (VOTEBOT-81)

**Date:** 2026-11-26. **Read-only audit; nothing in this repo or in production was changed.** Written after the 2026-11-26 outage (VOTEBOT-77), when Pinecone refused every read ("egress limit for the current month, 10000000000 bytes") and both chat servers answered every message with `processing_error`.

**What this is:** every place in our code that reads from the Pinecone account, what one read costs in bytes, how often it runs, and which of them can plausibly add up to 10 GB a month. **Every number in the read-path table below is an estimate from the code and from the chunk settings. The section "What Pinecone's own usage export shows" after it holds the first real numbers (daily, from Pinecone), and where the two disagree the export wins.** The measurements that would settle it are listed at the end and were asked of the prod agent (request on `notes/ops-handoff`). Where the audit and a measurement disagree, the measurement wins.

## The one fact that sets the size of everything

Egress is the size of what Pinecone sends back. A query returns, per match: the vector id, the score, and the **metadata**. We store each chunk's full text in the metadata (`content`), and Pinecone cannot be asked for some metadata fields only: `include_metadata=True` returns the chunk text too. Vector values are off everywhere we query (`include_values` is left at Pinecone's default of off in VoteBot; the broker sends `"includeValues": false`).

- A chunk is at most 1,000 tokens (`ddp-sync` `chunk_size` 1000, overlap 200; the shared chunker's own default is 750), about 3 to 4 KB of English text. Add the other metadata fields (`ocd_bill_id`, `gov_id`, `jurisdiction`, `session_code`, `document_id`, `version_*`, `url`) and the JSON around it: **about 3 to 5 KB per returned match**, less for a short last chunk.
- A query with `top_k` N therefore costs roughly **N × 3 to 5 KB** (a `top_k` of 100 is 300 to 500 KB). A query without metadata costs about 0.1 KB a match.
- 10 GB is about **2,000,000 to 3,000,000 matches** with text.

## Read paths

| # | Reader (repo) | What it reads | Per run (estimate) | How often | Share of a 10 GB month, if it were alone |
|---|---|---|---|---|---|
| 1 | **Broker semantic search** (`ddp-broker-py`, `search/semantic_search.py`, `views/search_api.py`) | Query 1: `topK` 100, metadata on, filter `document_type` in the searched types. Query 2 (only when a matched bill has a newer version): `topK` 1000, **metadata off**, ids only, and only versions newer than the match come back | Query 1 about **300 to 500 KB**. Query 2 about 0 to 100 KB | Once per site search that passes the ceilings: **at most 30 a minute per client and 1,000 an hour per counter** (`SEARCH_SEMANTIC_RATE_*`). The counter lives in Django's cache, which is **per process** unless a shared cache is set, so the real ceiling is 1,000 an hour **times the number of broker worker processes**, and a worker restart resets it. How many workers, and whether a shared cache is set in production, are in the measurement request | About 20,000 to 30,000 searches fill 10 GB. **At one counter's ceiling, 1,000 an hour is about 0.4 GB an hour, 10 GB in a day, and more with several workers**. Needs only about 40 searches an hour on average over the month |
| 2 | **VoteBot chat message, bill page** (`core/retrieval.py` `_bill_phases`) | Five concurrent queries: bill text 10, bill 20, bill's org chunks 3, organization 20, changelog 3 = **56 matches** with text; plus one `top_k` 1 bill-name check in `core/agent.py` | About **170 to 280 KB** | Once per chat message on a bill page (no result cache: only the api-v3 version lookup is cached) | About 35,000 to 60,000 messages |
| 3 | **VoteBot chat message, general page or no page** (`retrieval.py` standard path) | 20 matches, or 10 + 20 on a scoped jurisdiction page, plus the `top_k` 1 check | About 60 to 150 KB | Once per message | About 70,000 to 160,000 messages |
| 4 | **VoteBot bill-name lookup** (`retrieval.py` `_lookup_ocd_bill_context`, `BILL_LOOKUP_TOP_K`) | A bill named on a general page: **100 chunks** with text | About **300 to 500 KB** on top of the message's other queries | Only for messages that name a bill ("HB 123") on a page that is not that bill's | One such message costs as much as 2 to 3 plain ones |
| 5 | **VoteBot and ddp-sync health checks** (`describe_index_stats`) | Index statistics: counts only, no chunk text | Under a few KB (grows with the number of namespaces) | The VoteBot readiness check, called by whatever polls it (a stats call "every 30 seconds" was seen on the broker host) and ddp-sync's health route | At a 30-second interval that is about 86,000 calls a month per poller; at 1 KB a response that is under 0.1 GB, **small unless the response is far larger than I think** (it would need about 100 KB a call to reach 10 GB from one poller). Measure one response, and count the pollers, before dismissing it |
| 6 | **ddp-sync knowledge-base embedding passes** (`pipelines/knowledge_base_embedding.py` and the shared ingestion pipeline) | **Writes only**: upserts and deletes by id. A grep of `ddp-sync` finds no `query`, `fetch` or `list` in these pipelines | Reads: none. Pinecone's read-egress meter does not count writes | Scheduled | **Not a direct read source.** These passes write only, so they add no read egress themselves. The prod agent noted Hawaii (6,130 bills, 23,905 chunks) finishing three minutes before the first error; the code gives no reason for that to be a cause, but the timing is not explained either, and the console's usage by day will show whether anything else changed that day |
| 7 | **`build_legislator_votes.py`** (`ddp-sync`, manual command, not in the schedule) | `index.list` of every `bill-votes-` id, then `index.fetch` in batches of 100, **with vector values** (fetch returns them) and text | **Assumes** the legacy index uses the embedding model's default 3,072 dimensions (not checked against the index) and about 10 bytes a number as JSON: **roughly 35 to 45 KB per vector with its text**. The number of `bill-votes-` vectors is **not known**, so the cost of one run is not known either | Only when someone runs it by hand; reads the frozen legacy index (`votebot-large`) | Per run: vectors × 35 to 45 KB (10,000 vectors would be 0.35 to 0.45 GB). Ask whether anyone ran it this month, and count the vectors |
| 8 | **`measure_search_coverage`** (broker management command) | `GET /vectors/list` with `limit` 1 per bill: ids only | About 0.2 KB per call, many calls | Manual | Negligible in bytes (heavy in request count) |
| 9 | **`ddp-agents` scripts** (Kindora and the grantee tools) | Their own indexes by default (`grantbot`, `grant-knowledge`), not the two this audit covers; their read calls were not read in depth | Unknown | Unknown | **Same account, negligible reads**: the usage export lists a `grantbot` index with 0.011 GB of egress for the month. Their code was not read in depth. **This audit's code review is complete for votebot, ddp-sync and the broker, not for every repo** |

## What Pinecone's own usage export shows (added 2026-11-26 evening, from the export for 1 to 24 November)

The cost-and-usage export Ramon downloaded gives the real daily numbers. **It changes the conclusion below: the month's egress was a two-day burst, not steady traffic.** Its manifest says the data runs "through 2026-11-25", but the 25 November rows are empty (usage not posted yet) and 26 November is absent, so **the real data ends on 24 November (UTC days)**. All three indexes of the account are in it.

| | Egress | Read units | Write units |
|---|---|---|---|
| 22 ordinary days (1 to 22 Nov), all indexes | 0.250 GB in all, **11 MB a day** (busiest day 0.052 GB, 16 Nov) | 15,701 in all, 714 a day (busiest 1,918) | 805,388 in all, mostly one day: 602,337 on 17 Nov (+ 100,003 on 1 Nov) |
| **23 Nov** | **0.862 GB** (76 times an ordinary day) | **57,347** (80 times) | 297,425 |
| **24 Nov** | **4.017 GB** (354 times) | **276,079** (387 times) | 303,160 |
| Total to 24 Nov | 5.128 GB | 349,127 | 1,405,973 |

**By index:** `ddp-knowledge-base` 5.103 GB (99.5%) of the egress; `votebot-large` 0.014 GB; `grantbot` 0.011 GB (so the `ddp-agents` GrantBot index is in this account too, and negligible). The legacy 2.0 chat server's index was almost unread all month, which fits the legacy site getting little chat traffic.

What the numbers say:

1. **Ordinary traffic is not what used the 10 GB.** The broker's search and the chat together read about 11 MB a day on a normal day, so a normal month is about 0.35 GB. The per-search and per-message sizes in the table above are still the right arithmetic; they just were not what happened.
2. **Two days, 23 and 24 November, hold 95.1% of the month's egress (4.879 of 5.128 GB), all on the knowledge-base index.**
3. **Each read unit carried about the same number of bytes on the burst days as on ordinary days**: 15.9 KB per read unit on ordinary days, 15.0 KB on 23 Nov and 14.5 KB on 24 Nov. That is consistent with **the usual kind of read at about 80 and 390 times the usual volume** (the same query shapes, many more of them), and not with a different kind of read such as fetching whole vectors. It is a consistency, not a proof: a read unit's cost depends on the index size and the call type, which I cannot see.
4. **The writes do not explain the reads.** 17 Nov wrote 602,337 units to the knowledge base, twice as many as either burst day, and read 587 units (0.009 GB). So a heavy write run does not by itself read; the ddp-sync embedding passes are write-only on the data as well as in the code. The writes of 23 and 24 Nov (about 300,000 each) are consistent with first embeddings after state loads (the ddp-sync notes of 23 Nov say Pennsylvania's first embedding waited for its 04:00 UTC nightly on 24 Nov), but that link is not proven either.
5. **The export holds 5.1 GB, not 10 GB.** The limit (10,000,000,000 bytes) was reached at about 22:30 UTC on 26 Nov. **About 4.9 GB was therefore read on 25 and 26 November**, the days the export does not have yet: a second burst, or the first one continuing. **Re-export when 25 and 26 November post**, and read their read units before saying the cause has stopped.

**Checked in the code and the notes, and ruled out as the reader:** the ddp-sync embedding pipelines and the shared ingestion pipeline (upsert, delete by exact id, one delete by metadata filter; no query, fetch or list before an upsert); the SYNC-191 draft clean-up (exact-id deletes, reads api-v3 and Redis only); the state first scrapes of 22 to 23 Nov (scrapers do not use Pinecone, and per the ddp-sync notes the new states had embedding off until they went live on 25 Nov); the post-load smoke test and QA sweep (the api-v3 boot test and OpenStates database checks); the scorecard parity runs (Postgres); LegBot and the other `ddp-agents` tools (the GrantBot indexes); `build_legislator_votes.py` (manual, reads the legacy index, which shows 0.014 GB all month). **Not ruled out:** the broker's site search if its index setting points at the knowledge base, the VoteBot 3.0 chat (the dev site), and a script, benchmark or crawler that nobody wrote down. Nothing in the commits or the ops-handoff notes of 21 to 26 November names one.

## What the evidence says, in plain words

1. **The usage export answers when, not who.** The egress was a burst on 23 and 24 November (95% of the month) on the knowledge-base index; the readers below are suspects for that burst, and the rest of this section is the code-side reasoning from before the export.
2. **The two readers that could reach 10 GB are the broker's site search (row 1) and the chat's own retrieval (rows 2 to 4), but only at a volume far above their normal 11 MB a day.** A broker search costs roughly 0.3 to 0.5 MB. An ordinary chat message costs about 0.06 to 0.28 MB (rows 2 and 3), and a message that names a bill on another page adds 0.3 to 0.5 MB (row 4). So 10 GB is tens of thousands of searches or messages. A public search box that bots and crawlers can hit is the likelier large one: row 1's ceiling alone would allow a month's cap in a day.
3. **The knowledge-base embedding passes are not a read source**: they write, they do not read (the code, and 17 Nov's 602,337 write units with almost no reads), so the prod agent's "likely trigger" (Hawaii finishing at 22:31) has no support. Something else read on 23 and 24 November.
4. **The stats call (row 5) is probably small** (about 86,000 calls a month per 30-second poller, so a response would have to be about 100 KB to matter), but the response size has not been seen: one measurement settles it.
5. **Retries can multiply reads**: `VectorStoreService.query` retries through tenacity, 3 attempts, on any error (`stop_after_attempt(3)`). An error that arrives after Pinecone has already sent the data (a timeout on the client side, say) would pay for each attempt; a refused (429) read returns no data, so the retries during the outage added no egress.

## What would make this a measurement (asked of the prod agent, read-only)

**Updated after the usage export:** the counts that matter most are now for **23 to 26 November, per hour if the logs allow** (what read the knowledge-base index on those days), asked in an update at the end of the same note; the monthly counts below are the general version.

The request is on `notes/ops-handoff`: `request-readonly-pinecone-egress-measurements-votebot-81-20261126.md`. In short:

- **The size of one real read**: run each real query shape once (broker query 1, VoteBot bill-page text query, and one `describe_index_stats`) and report the **response size in bytes** and the number of matches returned. This replaces the 3 to 5 KB assumption.
- **The counts**: this month's number of site searches that reached the semantic leg (broker access log or a metric), the number of "Querying Pinecone" lines per day on each chat server with their `top_k`, and whether a bill-naming message (`top_k` 100) is common.
- **The account's own meter**: the Pinecone console's usage by day and by index, from the account owner. That is the only direct answer to "who used it".
- **Who else uses the account**: any other caller with the same key (`ddp-agents` scripts, ad-hoc scripts, `build_legislator_votes.py` runs) and whether `votebot-large` and `ddp-knowledge-base` both serve live traffic.

## Options, with the saving each would give (not built; separate tickets decide)

| Option | Applies to | Saving | Cost / risk |
|---|---|---|---|
| Cache identical semantic queries for a short time (normalized text, a few minutes to an hour) | Row 1 | Everything repeated: bots, a popular topic, a user paging back. Could be most of row 1 | One cache and one expiry rule; stale for a few minutes after a new version is embedded |
| Lower the broker's `_SEMANTIC_TOP_K` (100 chunks) or the overfetch | Row 1 | Linear: 100 to 50 halves it | Recall on the judged set (BROKER-167) must be re-measured first |
| Lower the global and per-client ceilings, or share the counter across workers | Row 1 | Hard bound on the worst case | Real visitors behind one address share a limit (documented in the code) |
| Right-size VoteBot's `BILL_LOOKUP_TOP_K` and the 20-match `top_k * 2` overfetch | Rows 2 to 4 | Up to half of a message | Answer quality; the lookup's ambiguity check needs many chunks on purpose (comment in `retrieval.py`) |
| Stop storing chunk text in Pinecone metadata (text in the database, ids in Pinecone) | Rows 1 to 4 | About 95%, the largest single lever | A rewrite of the index and of every reader; a big project, only if measurement says reads, not traffic, are the problem |
| Stats call: poll less, or call it from the health check once a minute with a cache | Row 5 | Depends on the measured response size | Small |

Not recommended: any change to the embedding passes (they do not read), and any change to the chat's retry count (retries of refused reads add no egress).
