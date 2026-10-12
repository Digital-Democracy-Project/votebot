# Reply: VoteBot 4472180 deployed; check B passes (smoke 6/6 and 5/5, reconnect repro 12/12 at every gap); D shows no shutdown tracebacks

From: prod agent (EC2 broker host), 2026-10-12 UTC. Changed no code or settings. The container is left running (healthy, no site points at it). The rollback image `d3742ae` (`4b6ffceb9a4c`) is still on the host and was not needed.

## Deploy
- `/opt/votebot` detached at `447218050050d3c508c4d4b453073afb669649c1` (PR #58, VOTEBOT-42).
- Same steps as `1074152`: `render-env.sh` (4 secret names + 13 defaults), `docker compose ... build`, tag `votebot-ddp-next:4472180` = image ID `2a0f548f3ab2` (= `:local`), `up -d`.
- Times (UTC, 2026-10-12): render 00:41:36, build done 00:42:11, recreate 00:42:13, healthy by 00:42:43 (30 s).
- `WEB_CONCURRENCY=1` (1 "Started server process"), memory limit 1 GiB, restarts 0.

## B (smoke and reconnect repro): PASS
- Inside the network, `--retrieval`: **6/6 cases passed** (was 1/6). Index `ddp-knowledge-base`, namespace `default`, filter key `ocd_bill_id`; the index holds no bill-votes or bill-version-diff documents.
- Public wss: **5/5 passed**.
- Same-session reconnect repro (FL HB 7089, first answer, client closes, same session id reconnects after the gap, 3 attempts each, 20 s timeout):

| gap before reconnect | result (3 attempts) |
|---|---|
| 0 ms | answered in 4.2 s, 4.4 s, 4.2 s |
| 50 ms | 4.3 s, 4.6 s, 4.1 s |
| 200 ms | 4.0 s, 4.6 s, 3.8 s |
| 1000 ms | 3.5 s, 3.9 s, 3.7 s |

Before the fix, 0 ms gap: no answer in 20 s on both attempts. Now 12/12 answered. These times are real LLM and retrieval answers, not stubs. I did not run `scripts/load_ws.py`, and did not test a browser reload.

## D (restart): no tracebacks
- After `docker restart votebot-ddp-next` (00:46:28; healthy 00:47:00, 32 s): **0 `Traceback` lines** in the container log (the log persists across restarts, so this covers the start and the restart), 0 `Redis: failed`, 0 `NOT writable`. Restart counter 0.
- The 2 httpcore2 async-generator tracebacks seen on 1074152 did not appear this time. I have one restart of data on this image; that earlier case was also a single restart, so I would not call it fixed.
- Memory 118 MiB (limit 1 GiB), measured after the repro runs, not under load.

## State left
`4472180` running and healthy. Scratch files removed from the host and from the container. Check C (session restore) and E were not re-run, as the request listed B and D only.
