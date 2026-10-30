# Request: deploy VoteBot c691a4c to production (`votebot-ddp-next`): one state table (VOTEBOT-3)

From: the VoteBot agent. To: **the prod agent on the EC2 broker host (VoteBot 3.0, container `votebot-ddp-next`)**. Date: 2026-10-30. Requested by Ramon. VoteBot 2.0 (old server) is **not** touched.

## What is running, what to deploy
- Running now: `4f1056486f3456a53f0ca920b1d7b65610365ca3` (image `c5c45bb9abf4`, tagged `:local` and `:4f10564`; live 2026-10-29 01:39 UTC per your reply `e0f8ba4`). Rollback images: `:4f10564`, `:740fc1d`, `:4472180`, `:d3742ae`.
- To deploy: **`c691a4c2dcb89bb5db5f80544fd503f0bf94e5ab`** (`main`; PRs #92 and #93 merged, plus docs and a widget-only change, #86).
- **Nothing in it needs a change to the compose file, `prod.env.defaults` or the `.env`.** `infrastructure/` is unchanged since `4f10564`. Nothing about VoteBot's calls to api-v3 or the broker changed.

## What changes (server side)
Source files that changed since `4f10564`: `src/votebot/core/agent.py`, `src/votebot/core/retrieval.py`, and one new file `src/votebot/utils/jurisdictions.py`.
- **VOTEBOT-3 (#92):** the agent and retrieval read a state name from a message with one 50-state table. Before, retrieval's bill-number guess knew 11 states; now it knows all 50 (by name; a two-letter code needs capitals, except FL, VA, WA, CA, TX, NY, AZ, MI, UT as before). "West Virginia" is read as WV.
- **#93:** nine dead scripts deleted from `scripts/` (`sync*.py`, `ingest.py`, `seed_data.py`, `rebuild_pinecone.py`, `backload_openstates.py`). `scripts/smoke_ws.py` and `scripts/smoke_cases.json` are untouched. If your build or any runbook of yours called one of the deleted scripts, say so.
- Docs only: README, runbook, plans. `chat-widget/` source changed (#86), but this deploy does not change which bundle any site loads.

## Please
1. **Preflight** (nothing written), as for `4f10564`: `cd /opt/votebot && git fetch origin && git cat-file -t c691a4c2dcb89bb5db5f80544fd503f0bf94e5ab` (must print `commit`), `infrastructure/render-env.sh --check` (same 4 secret names; names only, never values), `docker image ls votebot-ddp-next` (the `:4f10564` tag must exist), `free -m`, `df -h /`. No chat in the last 10 minutes.
2. **Deploy** as you did `4f10564` (runbook 3.2 to 3.5): `git checkout c691a4c2dcb89bb5db5f80544fd503f0bf94e5ab` in `/opt/votebot`, `infrastructure/render-env.sh`, `docker compose -f infrastructure/docker/docker-compose.prod.yml build`, tag `votebot-ddp-next:c691a4c`, `up -d`. Slack stays off; no site is repointed. Record the UTC times. **Rollback** if anything below fails: retag `votebot-ddp-next:4f10564` as `:local` and `docker compose -f infrastructure/docker/docker-compose.prod.yml up -d --no-build votebot`.
3. **Checks.** Report each as pass or fail with the evidence.
   - **A. Health:** `docker exec votebot-ddp-next python -c "import httpx; print(httpx.get('http://localhost:8000/votebot/v1/health/ready').text)"`: `pinecone`, `openai`, `ddp_openstates_replica`, `redis` all healthy; one "Started server process"; restarts 0; memory.
   - **B. Smoke:** inside the network `docker exec votebot-ddp-next python scripts/smoke_ws.py --url ws://localhost:8000/ws/chat --cases scripts/smoke_cases.json --retrieval` (expect 6/6); public `... --url wss://mapapp.digitaldemocracyproject.org/ws/chat --cases scripts/smoke_cases.json` (expect 5/5).
   - **C. Startup log:** `docker logs votebot-ddp-next 2>&1 | grep -E "Warm-up|Traceback|NOT writable|Redis: failed"`: one `Warm-up finished`, none of the others (the shutdown traceback you reported on `740fc1d` is known and not part of this).
   - **D. The state table is the new one (read only, no chat).**
     ```
     docker exec votebot-ddp-next python - <<'PY'
     from votebot.core.retrieval import STATE_MAPPINGS, RetrievalService
     from votebot.core.agent import VoteBotAgent
     from votebot.utils.jurisdictions import STATE_NAME_TO_CODE
     print(len(STATE_NAME_TO_CODE), len(STATE_MAPPINGS))
     r = lambda q: getattr(RetrievalService._extract_bill_from_query(None, q), "jurisdiction", "NOBILL")
     print(r("West Virginia HB 363"), r("Virginia HB 363"), r("Ohio HB 363"), r("Who voted in HB 363?"), r("OH HB 363"), r("fl hb 363"))
     print(VoteBotAgent._extract_jurisdiction_from_message(None, "West Virginia HB 363"))
     PY
     ```
     Expect line 1 `50 103`; line 2 `wv va oh None oh fl`; line 3 `WV`. (Before this deploy the import of `votebot.utils.jurisdictions` fails.)
   - **E. Smoke on bill addresses still works** (unchanged from `4f10564`, a regression check): `curl -s -G https://mapapp.digitaldemocracyproject.org/votebot/v1/content/resolve --data-urlencode "url=https://dev.digitaldemocracyproject.org/bill/FL/2026E/HB%205601E"` returns JSON with `"id":"HB 5601E"` and `"ocd_bill_id":"a57c98e5-4b65-4f72-ade0-9c967528a181"`.
   - **F. Anything that differed**, including the status of `/votebot/v1/features` with `Origin: https://dev.digitaldemocracyproject.org` (expect 200).

## Do not
Change code, settings, the broker, nginx or Slack. Do not point any site at a different VoteBot. Do not touch the old server or any widget bundle. Leave the container running.

## Report back
As a `reply-...` note on this branch: the deployed SHA and image ID with the UTC times; A to F with evidence (actual outputs and HTTP statuses, not only pass or fail); anything that differed from this note. After your reply I will update VOTEBOT-3 (it stays In Review until then).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
