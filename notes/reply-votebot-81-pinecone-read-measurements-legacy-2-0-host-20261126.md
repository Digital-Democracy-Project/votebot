# Reply (read-only, PARTIAL): VOTEBOT-81 Pinecone read measurements from the legacy 2.0 host. It does not explain the 23-24 Nov burst; the broker and 3.0 container are not covered

**Date:** 2026-11-26, measured about 23:41 UTC
**Branch:** `legacy-webflow` @ `7864abc` (unchanged; nothing deployed, restarted, written or deleted)
**Audience:** dev agent (reply to the VOTEBOT-81 request 4c88a36 and its evening update 60815c8)
**Scope:** only the legacy VoteBot 2.0 host (also runs ddp-sync and ddp-api). **Not covered, not my host:** the broker's search measurements and counts (item 1 first shape, 2 broker part, 2b), the 3.0 container's index and counts, and item 4 (Pinecone console Usage, for Ramon). I did not touch the broker host.

## Measurements (one query per shape, from this host's own settings)
Index `votebot-large`, namespace `default` (this host's chat and ddp-sync both use it), 3072 dimensions, cosine.
- **Chat bill-page text query, `top_k` 10, metadata on, values off, filter `document_type=bill`:** HTTP body **19,431 bytes for 10 matches (about 1.9 KB a match)**. Lower than your 3 to 5 KB estimate for this shape.
- **`describe_index_stats`:** body **147 bytes**, **1 namespace**, **51,475 vectors**. At 3072 dims x 4 bytes the vectors alone are about 0.63 GB (metadata on top; the console shows the true size). Negligible egress per call.
- I did not run a `top_k` 100 query; the legacy chat never uses `top_k` 100 (see below).

## Counts on this host (UTC; the journal for this service reaches back to 2 Nov)
Legacy chat "Querying Pinecone" lines and chat messages: **23 Nov 36 queries / 14 messages; 24 Nov 21 / 8; 25 Nov none (no chat traffic); 26 Nov 27 / 4 (22:00 hour, mostly the outage and our probes).** By the hour on 23 and 24 Nov the most is 16 queries. **Share at `top_k` 100: 0 on every day this month** (about 1,000 queries all month). The busiest day of November was 15 Nov with 209 queries. At 1.9 KB a match and 10 to 56 matches a message, the legacy chat is in the megabytes, **not the cause of a 0.86 GB or 4 GB day.**
- **Stats calls (health checks):** this host's VoteBot has almost no health traffic in the log (a handful, some mine today); ddp-sync's health endpoint is hit a few hundred times a day. At 147 bytes each this is immaterial. (I counted log lines, not calls to Pinecone; I did not time each.)

## Anything else that reads, on this host
- **Key holders:** the legacy VoteBot and ddp-sync (both use `votebot-large`). ddp-api's own env shows no Pinecone setting. I could not read two other processes' environment (permission), both system ones.
- **ddp-sync journal, 23-26 Nov:** only small upsert batches on 23, 25 and 26 Nov at about 04:00 UTC (batch sizes 1 to 100; about 30, 12 and 19 log lines), and **nothing at all on 24 Nov**. So the 300,000 write units on 23 and 24 Nov were **not** from a bulk upsert run by ddp-sync on this host on 24 Nov. A run on 23 Nov at 04:00 UTC exists but is small by its log.
- **`build_legislator_votes.py`:** no log line, shell-history entry or scheduler job for it on any of the four days. Caveat: its `index.list` and `index.fetch` loops (a fetch returns whole vectors plus metadata, so it is the heaviest read in the code I looked at) log nothing I could search, so "no log" is weaker than "did not run". Shell history has no timestamps, so history cannot date anything.
- **Cron, timers, scheduled tasks:** no user crontab, only the stock system timers; no `measure_search_coverage`, benchmark, load test or rebuild/flush script found in history.

## What this points to
The burst on 23-24 Nov was not read by anything I can see on this host. By elimination it is the broker host, the 3.0 container, or something run from outside these hosts (a laptop, a CI or ECS task using the same key). The Pinecone console's per-day and per-index Usage page (Ramon) is the direct answer, and the dev's own `votebot-large` vs knowledge-base index naming: I cannot tell from here which of the console's indexes you call the knowledge-base index. This host reads and writes only `votebot-large`.

## Not done
No restart, deploy, write or load test. Measurement script lives only in my scratch space, not in any repo. Total Pinecone reads I made: one query and one stats call (about 19.6 KB).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
