# Reply: VoteBot 4f10564 deployed on the broker host (votebot-ddp-next). Checks A to G: all pass

Prod agent, Thu 2026-10-29 01:45 UTC. The operator said "Option A, run the preflight and deploy now". I read your follow-up `a888568` (the api-v3 sentence) before deploying. Output in `/home/bitnami/ops/votebot-4f10564-20261029T0138Z/`.

## Preflight (nothing written)
* `git cat-file -t 4f1056486f34...` = `commit`; it is the tip of `origin/main`; **`infrastructure/` and `scripts/` are unchanged since `740fc1d`** (0 files); the source files that changed are `content.py`, `config.py`, `prompts.py`, `query_logger.py`, `slack.py`, `ddp_urls.py`, `page_address.py`.
* `render-env.sh --check` from a throwaway worktree of `4f10564`: OK, the same 4 secret names (`API_KEY`, `DDP_OPENSTATES_BEARER_TOKEN`, `OPENAI_API_KEY`, `PINECONE_API_KEY`) + 12 defaults. `:740fc1d` tag exists (`2878c1dff616`); 9.5 GB memory available, 149 GB disk free; no chat in 10 minutes.
* **`DDP_SITE_BASE_URL` is set to `https://dev.digitaldemocracyproject.org`.**

## Deploy (UTC)
Old log saved; the live `.env` backed up owner-only. 01:38:41 `git checkout 4f1056486f3456a53f0ca920b1d7b65610365ca3` (HEAD confirmed); `render-env.sh` wrote 4 secret names + 12 defaults (mode 600); `build` **59 s**, rc 0, memory never below 9.4 GB; new image **`c5c45bb9abf4`** tagged **`votebot-ddp-next:4f10564`** (and `:local`); 01:39:41 `up -d`: **healthy after 32 s**, started 01:39:43, restarts 0. Rollback images `:740fc1d` (`2878c1dff616`), `:4472180`, `:d3742ae` remain. No site repointed, Slack untouched.

## Checks
* **A. PASS.** `/health/ready`: `pinecone`, `openai`, `ddp_openstates_replica`, `redis` all **healthy**; one "Started server process"; restarts 0; memory **101.1 MiB / 1 GiB**.
* **B. PASS.** Inside the network with `--retrieval`: **6/6** (FL, WA, US, VA, MI and "index holds no bill-votes or bill-version-diff"); public `wss://mapapp.digitaldemocracyproject.org/ws/chat`: **5/5**.
* **C. PASS.** One `Warm-up finished` (`failed: []`, `pinecone_index_found: true`, 376 ms); no Traceback, `NOT writable` or `Redis: failed`.
* **D. PASS** (`/votebot/v1/content/resolve`, statuses): `/bill/FL/2026E/HB%205601E` **200**; `.../higher-education` **200**; `/explore/FL/2026E/HB%205601E` **200**; `/bills/FL/2026E/HB%205601E` **200**: all four `"id":"HB 5601E"`, `"ocd_bill_id":"a57c98e5-4b65-4f72-ade0-9c967528a181"`. `/organization/5155` **400** with "Unable to parse DDP URL ... Expected format: /bill/{jurisdiction}/{session}/{gov_id}, /legacy/bills/{broker_id}, /bills/{slug}, /legislators/{slug}, or /member-organizations/{slug}". `/legacy/bills/1` **200** (a bill: `HR8035`, US 118). Known and unchanged: `/member-organizations/<slug>` **500** (VOTEBOT-31).
* **E. PASS.** One test chat (session `handoffcheck-521a0d`, `stream_end`). **No query-log file contains `SECRET123` or `user:pw`; the container log count is 0; the logged `page_url` is exactly `https://handoffcheck.example.test:8443/organization/5155`.**
* **F. PASS.** `DDP_SITE_BASE_URL` is set, so the check applies: 868 frames, `stream_end`; the only link of the form is **`https://dev.digitaldemocracyproject.org/bill/FL/2026E/HB%205601E`**; **no `/explore/` link** (count 0). Frames saved in the output folder.
* **G. PASS.** `/votebot/v1/features` with `Origin: https://dev.digitaldemocracyproject.org`: **200**.

## Notes
The two test chats (E and F) wrote one `handoffcheck-521a0d` entry and one `billcheck-*` entry to today's query log; no real person was involved. Nothing else changed; the container is running.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
