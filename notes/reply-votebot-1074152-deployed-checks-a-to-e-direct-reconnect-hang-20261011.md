# Reply: VoteBot 1074152 deployed; checks A, C, E pass, B fails on the direct path (same-session reconnect hang), D has 2 shutdown tracebacks

From: prod agent (EC2 broker host), 2026-10-11 UTC.
Changed no code or settings after the deploy. The new container is left running (healthy, no rollback needed; no website points at it).

## Deploy
- `/opt/votebot` detached at `107415240132cfaa6478f952ddbfc3cac273e045` (PRs #50 to #54).
- Image `votebot-ddp-next:1074152` = ID `1a906ab5727e` (= `:local`). Rollback image `d3742ae` = `4b6ffceb9a4c` is still on the host.
- Times (UTC): render-env 21:46:22, build 21:46:27 to 21:46:57, recreate 21:47:02, healthy 32 s later.
- `WEB_CONCURRENCY=1`, memory limit 1 GiB (1073741824).

## A (startup): PASS
1 "Started server process", startup line unchanged (`pinecone_index_name=ddp-knowledge-base`, `bill_filter_key=ocd_bill_id`, `quick_action_buttons_enabled: true`, no Slack). Readiness healthy (pinecone, openai, replica, redis). Public features true.

## B (smoke): FAIL on one of the two commands
- Public wss: 5/5 cases pass.
- Inside the network with `--retrieval`: 1/6. All five jurisdiction cases fail with `TimeoutError` (about 95 s each); only the index check passes.
- Per-question timing: on the direct path some questions never complete (100 s timeout: FL Q2, US Q2, US Q3). The public path answered all 6 in 1.4 to 5.7 s.
- The server did answer. In the log for a hung question, `WebSocket disconnected` for the session id appears about 2 ms after the new connection for the same id starts processing (22:05:57.9977 connected, 22:05:58.0004 disconnected), and the stream then continues with no receiver.

### Reproduction (observed, 22:15 to 22:17 UTC, FL HB 7089, same session id, client closes after the first answer)
| gap before reconnect | result (2 attempts each) |
|---|---|
| 0 ms | no answer in 20 s, both attempts |
| 50 ms | answered in 5.7 s and 4.1 s |
| 200 ms | 5.1 s and 3.7 s |
| 1000 ms | 4.3 s and 4.1 s |

So the hang reproduces with a reconnect that arrives right behind the close, on the same session id. The smoke script reconnects that fast; the public path's latency hides it.

### Likely cause (code reading, NOT proven by a fix)
`src/votebot/api/routes/websocket.py`: `connect` sets `active_connections[session_id] = websocket` (line 63). `disconnect(session_id)` (lines 107 to 111) deletes the entry by id, whichever socket is registered. `send_json` (line 115) silently drops the message when the id is missing. If the old connection's cleanup runs after the new connection has registered, it removes the NEW socket and the answer is dropped. #53 awaits `save_session` (Redis) in the disconnect path (lines 618 and 633), which widens that window.
I did not test a fix and did not run `scripts/load_ws.py`. A real browser reload that reconnects within milliseconds could hit the same thing; I have not tested that.
A guard such as "delete only if `active_connections.get(session_id) is websocket`" would be the obvious thing to try; that is your call.

## C (restart/session restore): PASS
Two messages on one session id (3 s gap): the second connection returned `session_info restored: True` then `session_restored`. Redis holds 20 `votebot:session:*` keys; my key TTL 7191. After `docker restart votebot-ddp-next` (healthy in about 31 s each time), the first connection with the same id gave `session_info restored=True` and `session_restored messages=4 roles=['user','assistant','user','assistant']`. A brand-new id gave `restored False`. Restart counter stays 0.
Note: my three `docker restart`s are in the log (that is where the shutdown traces below come from).

## D (memory, logs): PASS with a caveat and one item to look at
- Memory 71.85 MiB, limit 1 GiB. This is idle after restarts, NOT measured under load.
- 0 `Redis: failed`, 0 `NOT writable`, no warning or error level events.
- 2 `Traceback` lines at 22:12:46, during my first `docker restart`, after `VoteBot shutdown complete`: "an error occurred during closing of asynchronous generator ... RuntimeError: aclose(): asynchronous generator is already running" from httpcore2 `PoolByteStream.__aiter__`. You expected none; reporting as evidence. It looks like asyncio shutdown noise (my reading).

## E (rest of the host): PASS
Broker web healthy (up since 20:27:59, 0 restarts), celery ping ok, beat running, api-v3 and ddp-sync healthy (0 restarts), votebot-redis up since 19:49:12, public `/api/status/` and `/map/` 200, load about 0.68, about 9.9 GB free.

## State left
New container running on `1074152`. Scratch files removed. Waiting for your direction on the B finding before anyone points a site at it.
