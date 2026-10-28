# Reply: VoteBot 740fc1d deployed on the broker host (votebot-ddp-next). Checks A to G: all pass; one shutdown traceback noted

Prod agent, Wed 2026-10-28 01:27 UTC. The operator said "Option A, deploy VoteBot now". Before acting I re-read all four branches (nothing for this) and checked: no chat in 10 min, memory 9.5 GB available, 153 GB free. Output in `/home/bitnami/ops/votebot-740fc1d-20261028T0120Z/`.

## What was deployed (UTC)
* Old log saved to `log-archive/`; the live `.env` backed up owner-only (600) in the output folder.
* 01:20:09 `git checkout 740fc1d8b40a66b627b2af2e1907cb6afaf2f434` in `/opt/votebot` (HEAD confirmed). `render-env.sh`: wrote 4 secret names + 12 defaults, mode 600.
* 01:20:17 `docker compose ... build`: **47 s**, rc 0; lowest available memory during the build **9,347 MB**. New image **`2878c1dff616`**, tagged `votebot-ddp-next:740fc1d` (and `:local`). Rollback images `:4472180` (`2a0f548f3ab2`) and `:d3742ae` are still there.
* 01:21:14 `up -d`: healthy after **32 s**; restarts 0; policy `unless-stopped`; memory 103 MiB / 1 GiB.

## Checks
* **A. Health: PASS.** `/health/ready` (inside the container): `pinecone`, `openai`, `ddp_openstates_replica`, `redis` all **healthy**; one "Started server process"; restarts 0.
* **B. Smoke: PASS.** Inside the network with `--retrieval`: **6/6**; public `wss://mapapp.digitaldemocracyproject.org/ws/chat`: **5/5** (the MI case found SB 810, 3 questions answered with bill-id citations).
* **C. Startup log: PASS.** One `Warm-up finished` (`failed: []`, `pinecone_index_found: true`, 776 ms); no Traceback, `NOT writable` or `Redis: failed` at startup.
* **D. Timings: PASS.** Today's query log (`2026-10-28.jsonl`) has **30** lines with `first_token_ms` and **30** with `phase_ms` after the smoke runs.
* **E. Scorecard route on the production broker: HTTP 404** (expected until BROKER-325 is deployed).
* **F. Shutdown: PASS.** `docker stop votebot-ddp-next` took **1 s**, exit code **0** (not 137). Brought back with `up -d`: healthy after 31 s, `/health/ready` all healthy, one `Warm-up finished`. **One thing to look at:** after `Application shutdown complete` and `Finished server process [1]` the log shows a Python traceback ending `RuntimeError: aclose(): asynchronous generator is already running` (an httpx stream `aclose` at interpreter exit). It did not delay the stop or change the exit code, but it is new noise at shutdown.
* **G. Anything polling `/health/ready`? No recurring poller found.** The broker's nginx log (JSON, since 10-27 14:04) holds exactly **2** requests for it, both at 00:52 UTC today from the single external address 52.204.100.187 (21 and 24 seconds apart, status 200): one-off, not a monitor; no cron entry and nothing under `/opt/ddp-infra`. The nginx log does not go back further than 10-27 14:04, so I cannot rule out an older poller.

## Env names (answering your question)
`DDP_OPENSTATES_API_ROOT` and `DDP_OPENSTATES_BEARER_TOKEN` are both in the rendered `.env` (names only checked, never values). The root is `http://10.0.0.11:8002` (this host's api-v3); the old `USE_DDP_OPENSTATES_REPLICA` name no longer appears.

## Not done, and note
No code, settings or broker change; the container is left running. I have not run `latency_report.py` (you said not now). api-v3 on this host was rebuilt to `03fbb34` at 00:46 UTC today (OPEN-398, on hold by Ramon), which VoteBot's replica lookups now go through.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
