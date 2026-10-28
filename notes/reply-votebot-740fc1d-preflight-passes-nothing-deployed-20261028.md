# Reply: VoteBot 740fc1d preflight on the broker host: all green; nothing deployed yet

Prod agent, Wed 2026-10-28 01:08 UTC. The operator said "Option A, run the preflight now": **read-only, no build, no checkout in /opt/votebot, no restart.** Output in `/home/bitnami/ops/votebot-preflight-*.txt`.

* **Host state:** `/opt/votebot` at `4472180`, clean. `votebot-ddp-next` running image `2a0f548f3ab2` (tags `:local` and `:4472180`; `:d3742ae` = `4b6ffceb9a4c` is also there for rollback), healthy, up since 2026-10-12 00:46, restarts 0. No chat query in the last 30 minutes.
* **`740fc1d`** is an ancestor of current `main` (`5809422`, so main has moved on; I would deploy exactly `740fc1d8b40a66b6…`, as you wrote).
* **`render-env.sh --check`, run twice (names only, never values):**
  1. from the current checkout (`4472180`): OK, would write 4 secret names `['API_KEY', 'DDP_OPENSTATES_BEARER_TOKEN', 'OPENAI_API_KEY', 'PINECONE_API_KEY']` + 13 defaults;
  2. from a throwaway worktree of **`740fc1d`** (so the new script and the new `prod.env.defaults` were the ones checked; the worktree is removed, the live checkout untouched): OK, same 4 secret names + **12** defaults.
  * **Answer to your env question:** `DDP_OPENSTATES_BEARER_TOKEN` = **yes** (rendered from the shared secret), `DDP_OPENSTATES_API_ROOT` = **yes** (in the new defaults and in the live `.env`). The root is `http://10.0.0.11:8002` (this host's api-v3, not openstates.org), the same value as the live file. The only name that leaves the file is `USE_DDP_OPENSTATES_REPLICA` (in the live `.env`, not in the new defaults: the new code no longer reads it, I assume).
* **api-v3 check with the key the render will write:** the shared secret has the key; `GET /bills?jurisdiction=AZ&per_page=1` on `10.0.0.11:8002` with it answered **HTTP 200** (status only; note api-v3 was rebuilt at 00:46 UTC, see my report `cfc8cd1` on the ddp-open-states branch).
* **Resources:** memory 10.3 GB available (needs about 800 MB), `/` 153 GB free (46% used); `docker system df`: 40 GB of images, 96% of it reclaimable. The `:4472180` tag exists.
* **Not done and waiting for the operator's go:** checkout, render, build, tag, `up -d`, checks A to G (including the timed `docker stop`, a brief VoteBot restart).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
