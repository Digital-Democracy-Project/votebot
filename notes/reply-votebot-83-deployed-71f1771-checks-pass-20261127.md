# REPLY (prod agent): VoteBot `71f177175435a94b2fa03ff81b7df003ca1cf6f8` deployed (VOTEBOT-83). 2026-11-27 21:36 to 21:37 UTC (production host)

Ramon's go in my session. Preflight: the commit is on `main`, `git diff --stat 4a85cf0 71f1771` lists exactly the five files (95 insertions, 6 deletions); `render-env.sh --check` OK (the same four secret names); rollback image `votebot-ddp-next:4a85cf0` exists; memory and disk fine (96 GB free). **Chat check:** no chat message in the last 15 minutes; there were 22 WebSocket connects of about 2 seconds each with no messages (probe-like), so I proceeded and the recreate dropped nothing real.
* **Deploy:** `render-env.sh`, build, tag `votebot-ddp-next:71f1771`, `up -d`: recreated and healthy in 34 s. Slack off, no site repointed, old server untouched.
* **A. Rendered value:** `ALLOWED_ORIGINS` is a JSON list of exactly two `https://` origins (the dev site and the stage site), no path, no slash, no wildcard; `.env` mode 600.
* **B. Health:** `/votebot/v1/health/ready` 200, pinecone, openai, replica, redis all healthy, restarts 0; smoke test inside the network **6/6** (`--retrieval`), public **5/5**.
* **C. Startup log:** one `Warm-up finished`; no Traceback.
* **D. CORS:** `GET /votebot/v1/features` with each listed origin returned `200` and exactly that origin in `access-control-allow-origin`; `https://example.invalid` got `200` with no header (`None`).
* **Also:** the checkout of `/opt/votebot` had moved to local `main` when I posted notes earlier today (it was on `4a85cf0`); the running container was not affected, and it is now detached at `71f1771`. Rollback: retag `:4a85cf0` as `:local`, check out `4a85cf0`, `render-env.sh`, `up -d --no-build`.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
