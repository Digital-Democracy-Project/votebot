# REPLY (done): VoteBot `be8937a31ef0e07afacb9327b8f5fa2e660e4d4a` deployed to production (VOTEBOT-73). 2026-11-20 14:22 UTC (EC2 prod agent)

Reply to `f02690a` (+ fix `a4e5fe2`). Ramon approved in session ("you have my approval for all 4").

**Deployed:** checkout `c691a4c` -> `be8937a31ef0e07afacb9327b8f5fa2e660e4d4a` (detached). Preflight: commit exists; `render-env.sh --check` OK (names only: API_KEY, DDP_OPENSTATES_BEARER_TOKEN, OPENAI_API_KEY, PINECONE_API_KEY + 12 defaults); `:c691a4c` image exists (edc8f5a85695); memory available ~8.9 GB, 113 GB free; 0 chat lines in the last 10 min. `git diff --stat c691a4c be8937a -- infrastructure/` empty. 14:19:07 start; render-env wrote `.env` (mode 600); build 14:20:03; new image **`f2f82140d089`** tagged `votebot-ddp-next:be8937a` (and `:local`); `up -d` 14:20:06 (container recreated; the open chats on the old container ended). Slack off, nothing repointed. Rollback: retag `:c691a4c` as `:local`, `up -d --no-build votebot`.

**Checks:**
- **A. Health:** `/ready` healthy: pinecone, openai, ddp_openstates_replica, redis all healthy; container healthy, restarts 0.
- **B. Smoke:** inside the network with `--retrieval`: **6/6 cases passed**; public (wss://mapapp...): **5/5 passed**. Outputs in /home/bitnami/ops/votebot-be8937a-smoke-{internal,public}-20261120.txt.
- **C. Startup log:** one `VoteBot started` (pinecone_index_name=ddp-knowledge-base, bill_filter_key=ocd_bill_id), one `Warm-up finished` (failed [], pinecone_index_found true); 0 Tracebacks.
- **D. States sentence from the broker:** `BrokerLookupService().get_active_states()` printed `['Arizona', 'Florida', 'Michigan', 'Utah', 'Virginia', 'Washington']`. The broker's `GET /api/jurisdictions/` (200) iso2 codes: `AZ, FL, MI, US, UT, VA, WA`. Names match the codes minus `US`. (Broker served this list today; NE, PA, LA, NM, NC, AL are not on it yet.)
- **E. Content resolve:** `/votebot/v1/content/resolve?url=https://digitaldemocracyproject.org/bill/FL/2026E/HB%205601E` -> 200, `"id":"HB 5601E"` (also the dev.* address: 200).
- **F. Differences:** none in the deploy. My first check E attempt used a made-up slug address (`/bills/florida-hb-5601e`) and got `500 {"detail":"Webflow collection not configured for bill"}`: that is the legacy-slug path with no Webflow settings (by design, "No WEBFLOW_* variables at all"), not a regression; the bill address above answers 200.

VOTEBOT-73 can move on.
