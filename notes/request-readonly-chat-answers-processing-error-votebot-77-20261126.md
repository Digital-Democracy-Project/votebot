# Request (read-only): VoteBot chat answers every message with "processing_error" on both sites (VOTEBOT-77), 2026-11-26

**Say "not my host" and stop if you cannot read the production VoteBot's logs from where you work.** **Read-only: restart, change and redeploy nothing.** No keys, tokens or secrets in the reply.

## Why
Since about 22:35 UTC on 2026-11-26 the chat on the dev site and on the production (legacy) site shows "Online" but answers every message with "Error: Sorry, I encountered an error processing your message." (Ramon's two screenshots, 22:35 and 22:40 UTC.) **The two sites use two different VoteBot servers** (read from each page's own source; the exact chat addresses are in the page source of each site, and Ramon will confirm them to you in session):
- **The dev site's chat server:** per the votebot README, the VoteBot 3.0 container on the broker host.
- **The legacy production site's chat server:** per the README, the old VoteBot 2.0 server (the civic host).

I tested each with one socket message at 22:44 UTC (session id `votebot77-probe2`, message "are you working?"). On both, the server accepts the connection, sends `session_info` and `stream_start`, then sends `error` with code `processing_error`. Both answer their health check as healthy for all four dependencies, so each process is up and the failure is inside message handling. That error is sent from the `except Exception` at the end of the user-message handler, which logs "Error processing message" with the traceback. Two servers failing at the same minute points to something they share (the language-model account, the vector store, Redis, the replica database), not to a bad deploy on one host.

## What I need (each with its value)
1. **The traceback, from BOTH servers (if you only reach one, say which).** From each VoteBot log, the lines for "Error processing message" with session id `votebot77-probe` (and any other session) between 22:30 and 22:50 UTC, with the full exception text and traceback. This is the cause; it is the one thing I cannot see from outside.
2. **When it started.** The time (UTC) of the first "Error processing message" line since the last good chat, and the last "WebSocket message processed" line before it.
3. **Health, now.** The answer of `/votebot/v1/health/ready` from inside each host, in full.
4. **What changed.** For each server, the image or commit it runs and when it started; whether anything was deployed, restarted or its settings changed in the six hours before the first error; and whether any of these accounts' credentials were rotated, expired or hit a quota or billing limit in that time: the language-model account, the vector-store account, the Redis password. A "quota exceeded", "401", "429", "insufficient_quota" or connection-refused line in the traceback answers this too.
5. **Reply** with the time (UTC), the host and how you read the logs, and the values above. If you cannot read the logs, say so and stop.

## Stop and tell me
If reading the logs would change anything, or if the lines contain text you think should not be shared (cut it, say what you cut). Do not restart or redeploy to "see if it clears": that destroys the evidence and needs Ramon's go.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
