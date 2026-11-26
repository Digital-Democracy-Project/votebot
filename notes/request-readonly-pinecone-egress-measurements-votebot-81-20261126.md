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
3. **Who else uses the Pinecone account.** From the hosts you can see: any other process, cron job or script that holds the Pinecone key and reads (any `query`, `fetch` or `list`), and whether `build_legislator_votes.py` (ddp-sync, manual) or any ad-hoc script ran this month. Say "none I can see" if so. Also say which indexes serve live traffic today (the legacy index, the knowledge-base index, or both) and which service reads which.
4. **For Ramon (the account owner), not for you:** the Pinecone console's **Usage** page for November, read units and egress **by day and by index**. That is the one direct answer to "who used it". I will ask him in session; if you can see it, report it.
5. **Reply** with the time (UTC), the host and how you measured, and the values above. If a measurement would need more than one query, would write anything, or you cannot tell what you are looking at, say so and stop.

## Stop and tell me
If a step would change anything, or exposes text you think should not be shared (cut it and say what you cut). Do not raise any limit, restart anything, or run a "load test" to get a number: one query per shape is the whole measurement.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
