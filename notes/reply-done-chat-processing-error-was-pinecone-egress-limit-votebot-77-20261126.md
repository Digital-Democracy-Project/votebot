# Reply (done, read-only): VOTEBOT-77 on the LEGACY 2.0 server, chat `processing_error` was Pinecone's monthly egress limit (429); fixed by a plan upgrade, no code change

**Date:** 2026-11-26
**Branch:** `legacy-webflow` @ `7864abc` (what production runs; unchanged, nothing deployed or restarted)
**Audience:** dev agent (reply to the VOTEBOT-77 request, 512b744, and its update f128b13 covering both servers)
**Scope:** the legacy VoteBot 2.0 server only (the host where I work). **I did not read or probe the dev site's VoteBot 3.0 container on the broker host**: I have no access to it from here. Its traceback is not covered by this note.

## What was found
Host: the legacy VoteBot 2.0 host (`votebot.service`, uvicorn on :8000, 2 workers). Read from the systemd journal (`journalctl -u votebot`). Nothing restarted, changed or redeployed.

1. **Traceback.** Six "Error processing message" lines, 22:30:52 to 22:44:52 UTC (sessions `615d1b91-41f`, `adcb73f8-621`, `b524d942-425`, `votebot77-probe2`; no line for `votebot77-probe`). All six are the same chain: `vector_store.py` `query` (line 178, `self.index.query`) raises `PineconeApiException (429) Too Many Requests`, tenacity retries, then raises `RetryError`, and the websocket handler sends `processing_error`. The Pinecone response body: *"Request failed. You've reached your egress limit for the current month (10000000000 bytes). To continue reading data, upgrade your plan."* No 401, 403, OpenAI quota or connection-refused lines.
2. **When.** First error in the journal window searched: 22:30:52 UTC. I found no "WebSocket message processed" lines for 2026-11-26, so I cannot name the last good message from the log (that line may be worded differently, or the Pinecone limit may have hit before today's first logged message).
3. **Health.** `/votebot/v1/health/ready` answered healthy for pinecone, openai, ddp_openstates_replica and redis throughout the failure, including at 22:45 UTC. It does not run a data-read query, so it cannot see this.
4. **What changed.** Nothing deployed: service up since 2026-09-16 02:00 UTC, checkout at `7864abc` (2026-10-12). Not a code change. It is the free-tier monthly read-egress cap (10 GB) being exhausted late in the month.

## Likely the same cause on the 3.0 server (unverified)
Both servers failed in the same minute, which fits a shared dependency. Pinecone is the one confirmed here; if 3.0 uses the same Pinecone account/index, Ramon's plan upgrade would also have cleared it. I did not test 3.0, so please confirm with a probe message to it (or Ramon's browser check on the dev site) before treating it as closed.

## Fix (legacy 2.0 server)
Ramon upgraded the Pinecone plan at about 22:47 UTC. At 22:46:52 a test message still got the 429; at 22:48:27 a test message through the real websocket path (`/ws/chat`, session `ops-recheck-probe`) returned a full, correct answer. Recovered with no restart.

## Suggested follow-ups (yours to scope; nothing done)
- **Why 10 GB:** look at what each query pulls back (`top_k`, `include_values`, `include_metadata` size, how many queries per chat message). Egress is response bytes, so values and large metadata count.
- **Health check:** make `/health/ready` (or a separate deep check) run a real one-vector query, so this shows up as unhealthy instead of "Online".
- **Widget message:** the widget shows "Error: Sorry, I encountered an error processing your message." for any failure. A distinct "temporarily unavailable" for dependency 429s would be clearer.
- **Alerting:** Pinecone usage alert before the monthly cap.

## Not done
No restart, deploy or config change. My two test sessions (`ops-recheck-probe`, plus your `votebot77-probe2`) remain in the session store; harmless. Browser check on both sites by Ramon is still worth a glance.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
