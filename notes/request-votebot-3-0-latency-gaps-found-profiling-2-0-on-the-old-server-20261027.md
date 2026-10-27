# Request: VoteBot 3.0 latency gaps — what profiling 2.0 on the old server found, which of it 3.0 already fixed, and the gaps that remain (ops agent for the 3.0 dev agent, 2026-10-27)

**Date:** 2026-10-27
**Branch:** compared `origin/main` @ `a945120` (3.0) against `legacy-webflow` @ `7864abc` (2.0, what the old server runs)
**Audience:** the 3.0 dev agent / team. **I have changed nothing** in either codebase or on either host. The only thing pushed is this note.

## Why this exists
I was asked to evaluate latency on VoteBot 2.0 (the old Webflow-site server). I read the 2.0 code, measured it from its production query logs and journal, then checked each finding against 3.0's code. **3.0 has already fixed several of them (section 2, thank you).** Section 3 is what is still open. **Everything about 3.0 below is from reading code only; I have not run or measured 3.0.** The 2.0 numbers come from a different box (2 vCPU, 3.8 GB, about 0.9 GB in swap) and the legacy index, so they show where the time goes, not what 3.0 will measure. Please treat each gap as a lead to verify with your own timing.

## 1. What 2.0 measured (old server, 60 days, 1,326 queries; README target is p50 < 2.5 s, p95 < 5 s, first token < 1.5 s)
| | p50 | p90 | p95 |
|---|---|---|---|
| all queries | 4.8 s | 9.9 s | 13.2 s |
| bill page, not cached | 5.6 s | 10.7 s | 14.1 s |
| live-OpenStates tool used (about 33 percent of non-cached) | 7.1 s | 13.5 s | 16.9 s |
| "Status & votes" button (most-used button) | 7.8 s | 14.7 s | 17.6 s |

`duration_ms` is end to end, so it includes token generation. A fit on non-tool queries gave about **1.3 s fixed overhead + 2.5 s per 1,000 characters**, so about 2.9 s of a median 4.2 s answer is model generation (not a bug). The recoverable time is the overhead and the tool path. From the journal (364 messages, none overlapping): about **0.24 s** between "Processing message" and "Starting retrieval" (p90 0.26 s, i.e. constant), **retrieval p50 1.06 s** (1 query 0.73 s, 3 queries 1.40 s, 5 queries 2.10 s; median 2 queries per message), **embedding call p50 0.30 s each**, Pinecone `index.query` p50 0.07 s, **bill-info prefetch p50 2.1 s / p90 4.0 s**, of which the **party-roster download was p50 1.48 s / p90 2.41 s** (123 of 125 roster fetches were the 537-member federal roster, one per bill lookup, because the cache lived on a per-message service).

## 2. Already fixed in 3.0 (verified in code on `a945120`)
- **A new OpenAI/Pinecone/httpx client per message** → `agent.py:140-175` `_Services` / `_services_for()`, built once per process (VOTEBOT-37). Its docstring names the same dead-cache problem I found.
- **The party-roster cache that never hit** → `bill_votes.py:18` `PARTY_CACHE_SECONDS = 3600`, check at `:317`. Now effective because the service is shared.
- **Synchronous Pinecone `index.query()` on the event loop** → `vector_store.py:187` runs it in `asyncio.to_thread` under an index lock (VOTEBOT-36). The index lookup (`list_indexes` + `Index()`) now happens once per process, not per message.
- **Unbounded in-memory `sessions` dict, lost on reconnect to another worker** → sessions are kept in Redis (2 h) with `WEB_CONCURRENCY=1` and a 1 GiB limit, measured by the load test (VOTEBOT-34/35/39).
- **Unclosed per-message `httpx.AsyncClient` in `WebSearchService`** → moot now it is shared, and it has `close()` (`web_search.py:258`).

## 3. Gaps still open in 3.0
Ordered by what I expect to matter most for a user's wait. "2.0 cost" is what it measured on the old server.

1. **Retrieval is strictly sequential.** `core/retrieval.py` has about 17 `await self.vector_store.query(...)` call sites and no `asyncio.gather`. A bill-page message on 2.0 made 2 to 5 queries one after another (retrieval p50 1.06 s, up to 3.3 s). Phases that do not depend on each other (bill-text, bill summary, org positions, votes, changelog) could run concurrently. *2.0 cost: about 0.4 s per extra query.*
2. **No embedding reuse.** `embeddings.py` has no cache. Each `vector_store.query()` embeds its own text with `text-embedding-3-large` (3072 dimensions); several phases use the same or near-identical text. An in-process LRU keyed on the text, and one embed per phase group, would remove most of those calls. *2.0 cost: p50 0.30 s per call.*
3. **Everything before the first token is serial.** In the streaming path (`agent.py` about 1053 to 1145) retrieve, bill prefetch, legislator context, dispute verification, org positions and CMS verification are awaited one after another before the model call starts. Only the broker position lookup uses `asyncio.gather` (`agent.py:2192`). The prefetches do not depend on the retrieval result (the bill-info prefetch needs only the message and page context), so they can run alongside it. *2.0 cost: bill prefetch p50 2.1 s added on top of retrieval, no text shown meanwhile.*
4. **The party roster is paid for on a user request.** The 1 h TTL means the first bill lookup after every expiry or restart downloads the roster inline: about 11 sequential 50-per-page requests for the federal roster (about 1.5 s on 2.0). Options: refresh in the background (serve stale while revalidating), warm it in `main.py` lifespan, or skip it where the api-v3 people lookup already knows the party.
5. **No warm-up at startup.** `main.py` lifespan does nothing for Pinecone: the index lookup and first TLS connections happen on the first real chat after each deploy or restart. A one-off `describe_index` and a tiny embed at startup would move that off a visitor.
6. **`bill_votes.py` opens a new `httpx.AsyncClient` per call** (lines 200, 520, 622), so every OpenStates request starts with a new TLS handshake even though the service is now shared. One long-lived client on the service would do. Also `get_bill_info` tries the sessions `[session, year-1, year-2]` one after another on a 404 (lines 144 to 155).
7. **`WebflowLookupService` is still built per message** (`agent.py:233`, deliberately excluded in the `_services_for` docstring). Its `_org_cache` and `_bill_cache` (`webflow_lookup.py:126-127`) therefore never hit, and its calls open a client each (9 `httpx.AsyncClient(` sites). The docstring is right that they never expire, so sharing it needs a TTL first. I did not check how often 3.0's own paths reach it; the two-step fix is a TTL, then share it.
8. **No time-to-first-token measurement.** `grep -i ttft|first_token|time_to_first` finds nothing in `src`, `scripts` or `docs`. `duration_ms` is end to end, so the "first token < 1.5 s" target cannot be checked from the logs. Please log the time from message receipt to the first `stream_chunk` (and the time spent in each pre-model phase) on `query_processed`. I had to rebuild phase timings from journal lines, and could only bound the first-token wait.
9. **`/health/ready` makes a real model call.** `llm.py:717` `health_check()` does `responses.create(model=gpt-4.1, input="ping")` on every readiness check. If anything polls it (an uptime monitor, a compose healthcheck), it costs tokens and adds load; worth an `models.retrieve` or a cached result with a short TTL. I did not confirm what polls it on the 3.0 host.

## 4. What I am asking you to do
1. **Verify** each item in section 3 against your own timing (I would start by logging item 8, then measure items 1 to 3 together).
2. **PR** the ones you agree with. Items 1 to 3 are where I expect the user-visible gain; 4 to 6 are small and low risk; 7 and 9 are hygiene.
3. **Tell me** if any of these is intentionally left as it is (for example the Webflow lookup is legacy-index only and will go), so I stop raising it.

## 5. Not done
I did not run or load-test 3.0. I did not look at 3.0's widget or at how the new site loads it. I did not check how many 3.0 chats reach the Webflow lookup or the OpenStates roster path (it depends on `bill_filter_key`). The same findings for 2.0 are being handled separately on a branch off `legacy-webflow`, if the operator decides to do that; nothing on 2.0 has been changed yet.
