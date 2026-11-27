# Reply (done): VoteBot `4a85cf0` deployed to `votebot-ddp-next` on the broker host; checks A to F, 11-27

Ramon's go in this session (a pop-up that named the full SHA `4a85cf062e969f4421b3aef546f8e3140c16e584`). **Only this host's 3.0 container was touched**: no legacy 2.0 server, no site repointed, no Slack, no `.env` key change, no Pinecone account change.

## Times (UTC) and what runs
* Preflight 01:36 to 01:37: the target is a commit on `origin/main`; the checkout was clean at `be8937a`; `git diff --stat be8937a 4a85cf0 -- infrastructure requirements.txt pyproject.toml` empty; `render-env.sh --check` OK (4 secret names, 12 defaults); rollback tag `votebot-ddp-next:be8937a` (image `f2f82140d089`) present; 8.2 GB memory available, 98 GB disk free; **no chat message in the last 30 minutes** (0 in the last 5 minutes right before the restart).
* Checked out `4a85cf0`; `render-env.sh` wrote `.env` (mode 600, 4 secret names plus 12 defaults); built 01:37:01 to 01:38:16 (exit 0); image **`4016cfc000f1`**, also tagged `votebot-ddp-next:4a85cf0`; `up -d` at **01:38:26** (container recreated); **healthy 01:38:59, restarts 0**. Startup lines: `VoteBot started (chat-only mode)`, `pinecone_index_name=ddp-knowledge-base`, `bill_filter_key=ocd_bill_id`.

## The checks
* **A, health:** `/votebot/v1/health/ready` **200**, `{"status":"healthy","version":"2.0.0",...,"pinecone":"healthy","openai":"healthy","ddp_openstates_replica":"healthy","redis":"healthy"}`; container healthy, restarts 0.
* **B, a normal chat answers:** `scripts/smoke_ws.py --cases scripts/smoke_cases.json --retrieval` inside the container: **6/6 cases passed** (ends "index holds no bill-votes or bill-version-diff", MI's current version matches the vectors' document id); public over `wss://mapapp.digitaldemocracyproject.org/ws/chat`: **5/5 cases passed**. Outputs saved on the host (`votebot-4a85cf0-smoke-internal-20261127.txt`, `...-public-...`).
* **C, startup:** one `Warm-up finished` (`failed=[]`, `pinecone_index_found=true`, 605 ms); **0 Tracebacks**.
* **D, the health check runs one tiny query:** `first (a real query) True 233 ms` and `second (kept, no query) True 0 ms`.
* **E (optional; I judged it safe: the index was opened first and the probe is a refused read):** `statistics still answer: 504897 vectors`, then Pinecone logged `Pinecone health check failed error='[400] Vector dimension 8 does not match the dimension of the index 3072'` and the check printed **`unhealthy, as expected: ApiError`**: so a refused read now shows as unhealthy. Nothing was written or changed.
* **F, anything that differed:** nothing. The `.env` was re-rendered as the runbook says (same names; I did not compare values).

## Not done
`service_unavailable` on a real refusal (as you said, covered by tests); I will note it if a real refusal ever happens. Rollback if ever needed: retag `:be8937a` as `:local`, then `up -d --no-build votebot`.
