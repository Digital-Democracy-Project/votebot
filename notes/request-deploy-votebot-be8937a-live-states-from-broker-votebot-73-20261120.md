# REQUEST (not a GO): deploy VoteBot be8937a to production (`votebot-ddp-next`): its states sentence comes from the broker's list (VOTEBOT-73). 2026-11-20 (Mac side, Onboard New States)

**Nothing here runs without a GO note (Ramon).**
* **Why now:** Pennsylvania, Nebraska, Louisiana and New Mexico are going live (soak waived by Ramon, 2026-11-20). VoteBot's "states with voting now" sentence must follow the broker's list, not the typed list deployed on 10-30.
* **Who:** written by the Onboard New States session; no VoteBot session is running. Same procedure as your `c691a4c` deploy (request `80726e4`, your reply `7f6ae1b`).

## What is running, what to deploy
* **Running:** `c691a4c2dcb89bb5db5f80544fd503f0bf94e5ab` (deployed 10-30, your `7f6ae1b`). Rollback image: `votebot-ddp-next:c691a4c`.
* **To deploy:** **`be8937a31ef0e07afacb9327b8f5fa2e660e4d4a`** (`main`).
* **No compose, `prod.env.defaults` or `.env` change:** `git diff --stat c691a4c be8937a -- infrastructure/` is empty.
    * `DDP_BROKER_API_ROOT=https://mapapp.digitaldemocracyproject.org` is already in `infrastructure/docker/prod.env.defaults`.
    * VoteBot's content route already uses it.

## What changes (seven merges since c691a4c)
* **#97 VOTEBOT-73 (the one that matters):**
    * The system prompt's "states with voting now" sentence is read from the broker (`GET /api/jurisdictions/`). It is cached 5 minutes, or 45 seconds after a failure.
    * The federal jurisdiction is left out, and each name is written from VoteBot's own 50-state table.
    * If the broker can't be read, the prompt says nothing about covered states. It never falls back to a typed list.
    * Files: `src/votebot/services/broker_lookup.py`, `src/votebot/core/prompts.py`, `src/votebot/core/agent.py`.
* **#98 VOTEBOT-74:** two RAG training documents in the repo no longer name states.
    * That changes repo files only, **not** the Pinecone knowledge base.
    * Your earlier search found 0 of 60 typed lists there (`a184a3f`), so no re-ingest is needed.
* **#95, #96:** dead code deleted: `scripts/refresh_openstates_cache.py`, `src/votebot/utils/legislative_calendar.py`, broken RAG tests. If any runbook or cron of yours calls `refresh_openstates_cache.py`, say so.
* **#94, #99, #100:** docs and a test guard only.

## Please (after the GO)
1. **Preflight** (nothing written), as for `c691a4c`:
    * `git cat-file -t be8937a31ef0e07afacb9327b8f5fa2e660e4d4a` prints `commit`;
    * `infrastructure/render-env.sh --check`: the same secret names; names only;
    * the `:c691a4c` image tag exists;
    * `free -m` and `df -h /`;
    * no chat in the last 10 minutes.
2. **Deploy** as for `c691a4c` (runbook 3.2 to 3.5):
    * check out `be8937a`, then `render-env.sh`, `build`, tag `votebot-ddp-next:be8937a`, `up -d`;
    * Slack stays off, and no site is repointed;
    * record the UTC times.
    * **Rollback:** retag `:c691a4c` as `:local`, then `up -d --no-build votebot`.
3. **Checks** (pass or fail, with the actual output):
    * **A. Health:** `/votebot/v1/health/ready`: pinecone, openai, ddp_openstates_replica and redis all healthy; restarts 0.
    * **B. Smoke:** `scripts/smoke_ws.py` inside the network with `--retrieval` (expect 6/6), and public (expect 5/5), as before.
    * **C. Startup log:** one `Warm-up finished`; no new Traceback.
    * **D. The states sentence comes from the broker** (read only, no chat):
      ```
      docker exec votebot-ddp-next python - <<'PY'
      import asyncio
      from votebot.services.broker_lookup import BrokerLookup
      print(asyncio.run(BrokerLookup().get_active_states()))
      PY
      ```
      Paste the list it prints, and the broker's own `GET /api/jurisdictions/` (the iso2 codes) beside it. The names must match those codes minus `US`. I don't predict the list: it is whatever the broker serves today. If the class or method name differs in `be8937a`, say so and paste what you ran instead.
    * **E.** `/votebot/v1/content/resolve` for the FL HB 5601E address still returns `"id":"HB 5601E"`, as in `c691a4c`'s check E.
    * **F. Anything that differed.**

## Not authorized
* Any code, settings, broker, nginx or Slack change.
* Pointing a site at a different VoteBot.
* Touching the old server or a widget bundle.
* A knowledge-base re-ingest.

## Report back
A `reply-...` note on this branch: the deployed SHA and image id with UTC times; A to F with evidence. VOTEBOT-73 stays In Review until then.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
