# Request: deploy VoteBot 4f10564 to production (`votebot-ddp-next`): page-address trimming, the new site's bill addresses, the organization-page prompt

From: the VoteBot agent. To: **the prod agent on the EC2 broker host (VoteBot 3.0, container `votebot-ddp-next`)**. Date: 2026-10-29. Requested by Ramon. VoteBot 2.0 (old server) is **not** touched.

## What is running, what to deploy
- Running now: `740fc1d8` (image `2878c1dff616`, tagged `:local` and `:740fc1d`; rollback images `:4472180` = `2a0f548f3ab2` and `:d3742ae`). api-v3 on this host: `f9dde9192` (OPEN-398: revert of the rank pool and recency index, on `ddp-open-states/api-v3` `main`; checked in the nested repo).
- To deploy: **`4f1056486f3456a53f0ca920b1d7b65610365ca3`** (`main`, PR #83 merged). Everything after `740fc1d` is below. **Nothing in it needs a change to the compose file, `prod.env.defaults` or the `.env`** (`.env.example` and `config.py` changed one comment word only). `infrastructure/` and `scripts/` are unchanged since `740fc1d`.
- **VoteBot's calls to api-v3 did not change** since `740fc1d` (no change to any OpenStates client file), and api-v3 `f9dde9192` only touches `/ddp/search` (`api/ddp_search.py`), which VoteBot does not call, so neither side needs anything from the other; check A below proves the replica answers.

## What changes (all server side)
- **VOTEBOT-63 (#81):** the query log and the Slack handoff link keep only the origin and path of the page address the widget sends (no credentials, query string or fragment; a port stays, it is part of the origin). Old cached widgets still send the full address, so the server does the trimming.
- **VOTEBOT-64 (#82):** `/votebot/v1/content/resolve` accepts the new site's bill addresses `/bill/{JURISDICTION}/{SESSION}/{ID}[/slug]` and `/legacy/bills/{id}[-slug]`, and keeps every older form. **Bill links in answers are now built as `{DDP_SITE_BASE_URL}/bill/...`, not `/explore/...`.** (This only matters if `DDP_SITE_BASE_URL` is set: report its value in the preflight.)
- **VOTEBOT-66 (#85):** the organization-page prompt shows the page address as host and path only.
- Tests and docs only: #84 (widget tests), #83 (plan), README. The widget file `chat-widget/dist/ddp-chat.min.js` in the repo is rebuilt too, but **this deploy does not change which bundle any site loads** (ddp-next has its own copy; the Voatz page is on the old server).

## Please
1. **Preflight** (nothing written), as for `740fc1d`: `cd /opt/votebot && git fetch origin && git cat-file -t 4f1056486f3456a53f0ca920b1d7b65610365ca3` (must print `commit`), `infrastructure/render-env.sh --check` (same 4 secret names; names only, never values), `docker image ls votebot-ddp-next` (the `:740fc1d` tag must exist), `free -m`, `df -h /`. Also read the **value** of `DDP_SITE_BASE_URL` from the live `.env` (a public URL, safe to report) and say whether it is set and where it points. No chat in the last 10 minutes.
2. **Deploy** as you did `740fc1d` (runbook 3.2 to 3.5): `git checkout 4f1056486f3456a53f0ca920b1d7b65610365ca3` in `/opt/votebot`, `infrastructure/render-env.sh`, `docker compose -f infrastructure/docker/docker-compose.prod.yml build`, tag `votebot-ddp-next:4f10564`, `up -d`. Slack stays off; no site is repointed. Record the UTC times. **Rollback** if anything below fails: retag `votebot-ddp-next:740fc1d` as `:local` and `docker compose -f infrastructure/docker/docker-compose.prod.yml up -d --no-build votebot`.
3. **Checks.** Report each as pass or fail with the evidence.
   - **A. Health:** `docker exec votebot-ddp-next python -c "import httpx; print(httpx.get('http://localhost:8000/votebot/v1/health/ready').text)"`: `pinecone`, `openai`, `ddp_openstates_replica`, `redis` all healthy; one "Started server process"; restarts 0; memory.
   - **B. Smoke:** inside the network `docker exec votebot-ddp-next python scripts/smoke_ws.py --url ws://localhost:8000/ws/chat --cases scripts/smoke_cases.json --retrieval` (expect 6/6); public `... --url wss://mapapp.digitaldemocracyproject.org/ws/chat --cases scripts/smoke_cases.json` (expect 5/5).
   - **C. Startup log:** `docker logs votebot-ddp-next 2>&1 | grep -E "Warm-up|Traceback|NOT writable|Redis: failed"`: one `Warm-up finished`, none of the others (the shutdown traceback you reported on `740fc1d` is known and not part of this).
   - **D. The new address forms (read only).** Each is a GET; before this deploy the first two answer **400 "Unable to parse DDP URL"**:
     ```
     R=https://mapapp.digitaldemocracyproject.org/votebot/v1/content/resolve
     B=https://dev.digitaldemocracyproject.org
     curl -s -G $R --data-urlencode "url=$B/bill/FL/2026E/HB%205601E"
     curl -s -G $R --data-urlencode "url=$B/bill/FL/2026E/HB%205601E/higher-education"
     curl -s -G $R --data-urlencode "url=$B/explore/FL/2026E/HB%205601E"
     curl -s -G $R --data-urlencode "url=$B/bills/FL/2026E/HB%205601E"
     curl -s -G $R --data-urlencode "url=$B/organization/5155"
     ```
     Expect: the first four return JSON with `"id":"HB 5601E"` and `"ocd_bill_id":"a57c98e5-4b65-4f72-ade0-9c967528a181"` (the same bill four ways; the last two worked before and must still work); the fifth is a **400** whose text lists `/bill/{jurisdiction}/{session}/{gov_id}`. Also report the status of `.../legacy/bills/1` (informational: a bill, or a clear 404 if that broker id does not exist here; a 500 is a failure). **Known and unchanged, not a failure:** `/member-organizations/<slug>` answers 500 (VOTEBOT-31, open).
   - **E. Trimming in the query log (writes one test chat, no real person involved).** Run inside the container, then check the host log:
     ```
     docker exec -i votebot-ddp-next python - <<'EOF'
     import asyncio, json, uuid, websockets
     U = "https://user:pw@handoffcheck.example.test:8443/organization/5155?token=SECRET123#frag"
     async def main():
         sid = "handoffcheck-" + uuid.uuid4().hex[:6]
         async with websockets.connect(f"ws://localhost:8000/ws/chat?session_id={sid}") as c:
             await c.send(json.dumps({"type": "user_message", "payload": {"message": "What is this organization about?", "page_url": U,
                 "page_context": {"type": "organization", "id": "5155", "jurisdiction": "FL", "url": U}}}))
             while True:
                 m = json.loads(await asyncio.wait_for(c.recv(), 90))
                 if m.get("type") in ("stream_end", "error"):
                     break
         print(sid)
     asyncio.run(main())
     EOF
     grep -l -E "SECRET123|user:pw" /opt/votebot-logs/queries/*.jsonl; docker logs votebot-ddp-next 2>&1 | grep -c -E "SECRET123|user:pw"
     grep -h handoffcheck /opt/votebot-logs/queries/*.jsonl | grep -o '"page_url": *"[^"]*"' | sort -u
     ```
     Expect: no file listed; count `0`; `page_url` reads exactly `https://handoffcheck.example.test:8443/organization/5155`. (The `:8443` stays: a port is part of the origin.) If the answer is generic because organization 5155 is not in this broker, that is fine; only the log lines matter.
   - **F. Bill links point at `/bill/...`** (only if `DDP_SITE_BASE_URL` is set; otherwise say so and skip). Same script shape with a bill page; print the frames and look for our links:
     ```
     docker exec -i votebot-ddp-next python - > /tmp/billcheck.json <<'EOF'
     import asyncio, json, uuid, websockets
     CTX = {"type": "bill", "id": "HB 5601E", "jurisdiction": "FL", "session": "2026E", "ocd_bill_id": "a57c98e5-4b65-4f72-ade0-9c967528a181"}
     async def main():
         async with websockets.connect(f"ws://localhost:8000/ws/chat?session_id=billcheck-{uuid.uuid4().hex[:6]}") as c:
             await c.send(json.dumps({"type": "user_message", "payload": {"message": "What does this bill do?", "page_context": CTX}}))
             while True:
                 m = json.loads(await asyncio.wait_for(c.recv(), 90))
                 print(json.dumps(m))
                 if m.get("type") in ("stream_end", "error"):
                     break
     asyncio.run(main())
     EOF
     grep -o 'https://[^" )]*/\(bill\|explore\)/[^" )]*' /tmp/billcheck.json | sort -u; rm /tmp/billcheck.json
     ```
     Expect links of the form `{DDP_SITE_BASE_URL}/bill/FL/2026E/HB%205601E` and **none** with `/explore/`.
   - **G. Anything that differed**, including any 4xx or 5xx on `/votebot/v1/features` from the dev domain (CORS is unchanged; this is only a sanity check): `curl -s -o /dev/null -w '%{http_code}\n' -H 'Origin: https://dev.digitaldemocracyproject.org' https://mapapp.digitaldemocracyproject.org/votebot/v1/features`.

## Do not
Change code, settings, the broker, nginx or Slack. Do not point any site at a different VoteBot. Do not touch the old server or the widget bundle (the Voatz page moves later under VOTEBOT-54, which needs this deploy first). Leave the container running.

## Report back
As a `reply-...` note on this branch: the deployed SHA and image ID with the UTC times; the value of `DDP_SITE_BASE_URL`; A to G with evidence (the actual HTTP statuses and the grep outputs, not only pass or fail); anything that differed from this note. After your reply I will re-run the browser checks on `dev.digitaldemocracyproject.org` (which talks to this container) and update VOTEBOT-63, -64, -66 and -62.
