# Reply: VoteBot c691a4c deployed on the broker host (votebot-ddp-next). Checks A to F: all pass

Prod agent, Fri 2026-10-30 16:45 UTC. The operator said "Option A, run both". Before deploying I re-read all four branches (nothing for this) and checked: no chat in 10 minutes, 9.3 GB memory available, 148 GB disk. Output in `/home/bitnami/ops/votebot-c691a4c-20261030T1637Z/`.

## Preflight
`git cat-file -t c691a4c2dcb8...` = `commit`, the tip of `origin/main`; `infrastructure/` unchanged since `4f10564` (0 files); source files changed: `agent.py`, `retrieval.py`, `utils/jurisdictions.py`; **9 scripts deleted; nothing on this host (`/opt/ddp-infra`, my ops folder, the runbooks I could grep) calls any of them.** `render-env.sh --check`: OK, the same 4 secret names + 12 defaults. `:4f10564` tag exists (`c5c45bb9abf4`).

## Deploy (UTC)
Old log saved; live `.env` backed up owner-only. 16:37:19 `git checkout c691a4c2dcb89bb5db5f80544fd503f0bf94e5ab` (HEAD confirmed); `render-env.sh` wrote 4 secret names + 12 defaults (mode 600); `build` **27 s**, rc 0; new image **`edc8f5a85695`** tagged `votebot-ddp-next:c691a4c` (and `:local`); 16:37:47 `up -d`: **healthy after 32 s**, started 16:37:49, restarts 0. Rollback images `:4f10564` (`c5c45bb9abf4`), `:740fc1d`, `:4472180`, `:d3742ae` remain. No site repointed, Slack untouched.

## Checks
* **A. PASS.** `/health/ready`: `pinecone`, `openai`, `ddp_openstates_replica`, `redis` all **healthy**; one "Started server process"; restarts 0; memory **98.6 MiB / 1 GiB**.
* **B. PASS.** Inside the network with `--retrieval`: **6/6**; public `wss://mapapp.digitaldemocracyproject.org/ws/chat`: **5/5**.
* **C. PASS.** One `Warm-up finished` (`failed: []`, `pinecone_index_found: true`, 1,142 ms); no Traceback, `NOT writable` or `Redis: failed`.
* **D. PASS (the new table).** Line 1 **`50 103`**; line 2 **`wv va oh None oh fl`**; line 3 **`WV`**: exactly as you expected.
* **E. PASS.** `/votebot/v1/content/resolve` for `/bill/FL/2026E/HB%205601E`: **HTTP 200**, `"id":"HB 5601E"`, `"ocd_bill_id":"a57c98e5-4b65-4f72-ade0-9c967528a181"`.
* **F. PASS.** `/votebot/v1/features` with `Origin: https://dev.digitaldemocracyproject.org`: **200**.

Nothing else changed; the container is left running.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
