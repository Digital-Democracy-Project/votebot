# Request (read-only): measure what one Pinecone read costs in bytes, and how many reads ran this month (VOTEBOT-81), 2026-11-26

**Say "not my host" and stop for any part you cannot do from where you work.** **Read-only: change, delete, restart, redeploy and re-ingest nothing.** No keys or tokens in the reply. Reads are cheap but not free (the account has just been raised, but a limit still exists): each measurement below is **one** query, not a loop.

## Why
The 2026-11-26 outage (VOTEBOT-77) was Pinecone's monthly read-egress limit (10 GB). We do not know what used it. VOTEBOT-81 audited the code (the table is in the votebot repo, `docs/PINECONE-EGRESS-AUDIT.md`, in the PR for VOTEBOT-81): the chat's retrieval and the broker's site search both read the chunk text with every match, so a read costs an estimated 3 to 5 KB per match, and the two big suspects are the broker's semantic search (100 matches a search) and the chat (about 56 matches a message, 100 more when a message names a bill). **Those sizes are estimates. I need real bytes and real counts.** (The ddp-sync embedding passes only write, so they are not on the list.)

## What I need (each with its value)
1. **The size of one read, three shapes, from where each service runs** (use the service's own client or settings; never print a key):
   - **The broker's search query:** one query with `topK` 100, metadata on, values off, filter `document_type` in `bill-text, bill-changelog, bill` (whatever `SEARCHED_DOCUMENT_TYPES` is in the broker's `search/semantic_search.py`), on the index the broker's search reads, with a plain query vector from any short phrase. Report the **response size in bytes** (the HTTP body length, not the Python object), the number of matches, and the index and namespace name it hit.
   - **The chat's bill-page text query:** one query with `top_k` 10 and metadata on, through VoteBot's own `VectorStoreService.query` or the equivalent raw call, on each chat server's index (the 3.0 container's and the legacy 2.0 server's). Bytes, matches, index name.
   - **One `describe_index_stats` call**, on each index: the response body in bytes and the number of namespaces in it.
2. **How many reads ran.** For this calendar month (1 to 26 November, UTC), as far as the logs reach (say how far back they go):
   - **The broker:** the number of site searches (requests to the search endpoint) per day, and how many of them reached the semantic leg (not answered with "semantic (rate limited)" or without it). Also the top 10 client addresses or user agents by search count **as counts only, no addresses** (say "10 callers account for N% of searches, the biggest is a search-engine crawler / a script / ordinary visitors" in words).
   - **Each chat server:** the number of "Querying Pinecone" log lines per day and the share with `top_k` 100 (the bill-name lookup), and the number of chat messages per day.
   - **The stats call:** how often `describe_index_stats` is called per hour on each host and by what (the health check, a monitor, the docker health check), as a count.
2b. **The broker's limits, as deployed:** how many broker worker processes serve the search endpoint, and whether a shared cache (not the per-process default) holds the semantic ceilings' counters, and the values of the two semantic rate settings if they are not the defaults (30 a minute per client, 1,000 an hour).
3. **Who else uses the Pinecone account.** From the hosts you can see: any other process, cron job or script that holds the Pinecone key and reads (any `query`, `fetch` or `list`), and whether `build_legislator_votes.py` (ddp-sync, manual) or any ad-hoc script ran this month. Say "none I can see" if so. Also say which indexes serve live traffic today (the legacy index, the knowledge-base index, or both) and which service reads which.
4. **For Ramon (the account owner), not for you:** the Pinecone console's **Usage** page for November, read units and egress **by day and by index**. That is the one direct answer to "who used it". I will ask him in session; if you can see it, report it.
5. **Reply** with the time (UTC), the host and how you measured, and the values above. If a measurement would need more than one query, would write anything, or you cannot tell what you are looking at, say so and stop.

## Stop and tell me
If a step would change anything, or exposes text you think should not be shared (cut it and say what you cut). Do not raise any limit, restart anything, or run a "load test" to get a number: one query per shape is the whole measurement.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>

## Update 2026-11-26 evening: aim the counts at 23 to 26 November (UTC)
Pinecone's usage export (1 to 24 November) shows the month's egress was a **burst**, not steady traffic: an ordinary day reads about 11 MB and 700 read units; **23 Nov read 0.86 GB (57,000 read units) and 24 Nov read 4.02 GB (276,000 read units), almost all on the knowledge-base index**, and the same two days wrote about 300,000 write units each. The export does not have 25 or 26 November yet, and the 10 GB limit was reached on 26 November, so about 4.9 GB was probably read on 25 and 26 November as well. So item 2 above matters most for **those four days, per hour if the logs allow**, and the answer I need is "what read the knowledge-base index then", not a monthly average. Read-only, counts only, no addresses:
- **The broker:** site searches per hour on 23, 24, 25 and 26 Nov (UTC), how many answered with "semantic (rate limited)" and how many reached the semantic leg; whether one caller (as "one address made N% of searches", no address) or a user-agent family (crawler, script, browser) dominates. If the ceiling counter's refusals show on those days, say how many.
- **Each chat server:** "Querying Pinecone" lines per hour on the same four days, with the share at `top_k` 100.
- **Anything that read the knowledge-base index by hand or on a timer on those days:** a script, benchmark, load test, backfill or probe, from shell history, cron, scheduled tasks or ECS task runs on any host you can see (names and times only). In particular: a run of `build_legislator_votes.py`, a search-coverage measurement (`measure_search_coverage`), or any bulk question run against the chat or the search endpoint.
- **For converting read units to queries:** the knowledge-base index's size in GB (`describe_index_stats`, one call) and its dimension, so I can turn 276,000 read units into a number of queries.
If the log retention does not reach 23 Nov, say how far it does.
