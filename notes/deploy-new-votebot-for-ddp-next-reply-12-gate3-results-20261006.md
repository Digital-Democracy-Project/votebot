# Reply 12: gate 3 results: VoteBot is up and healthy inside the Docker network; the smoke test did NOT pass (2/6, then 3/6); a code error found (prod agent, 2026-10-06)

Follows reply 11 and the operator's decision (see below). **STOP per your rule: I have not touched nginx, the stack stays up on the broker network with no public route.** Times UTC.

## What changed since reply 11
- **The operator decided to reuse ddp-sync's credentials** (`ddp-sync/credentials` holds the same keys the old VoteBot uses). I ran your render script from the pushed branch (`feat/VOTEBOT-14-render-env-from-secrets-manager`, `1fee264`) against real AWS with `VOTEBOT_SECRET_ID=ddp-sync/credentials`: `--check` passed, then it wrote `/opt/votebot/.env` (mode 600, atomic, no duplicate names, no value printed): `API_KEY`, `OPENAI_API_KEY`, `PINECONE_API_KEY` (all three are ddp-sync's, by the operator's decision) and `DDP_OPENSTATES_BEARER_TOKEN` (the shared api-v3 key), plus the 14 non-secret defaults. So the script works against real Secrets Manager. The reuse means the OpenAI key is shared by three services, the Pinecone key can write (VoteBot only reads), and `API_KEY` equals ddp-sync's own inbound key. Slack tokens empty on purpose.
- The operator created `/opt/votebot-logs/queries` (owner uid 1000). The logrotate file and `sudo findmnt --verify` are still pending from the operator.

## Preflight (before `up -d`)
No `votebot*` containers; network `ddp-broker-py_default` present; `.env` `bitnami:bitnami` 600 with the four secret names filled; logs dir owner uid 1000 and a write test as uid 1000 passed; 2,560 MB available, 174 GB disk free; broker, ddp-sync, api-v3 all 200.

## Start and in-network checks (`up -d --no-build` at 22:52:36, from `ba86a14`, image `6eea052e122d`)
- Startup line: `{"version": "2.0.0", "environment": "production", "pinecone_index_name": "ddp-knowledge-base", "bill_filter_key": "ocd_bill_id", "quick_action_buttons_enabled": false, "event": "VoteBot started (chat-only mode)"}`. No warning about an unrecognised index; no "Query log directory is NOT writable" line.
- `votebot-ddp-next` healthy after about 40 s; no host port published.
- `import aiofiles, websockets, httpx` -> `deps ok`.
- `/votebot/v1/health/ready` -> `{"status":"healthy", ... "dependencies":{"pinecone":"healthy","openai":"healthy","ddp_openstates_replica":"healthy","redis":"healthy"}}` (so the api-v3 `X-API-Key` path from PR #21 works).
- `/votebot/v1/features` -> 200 `{"quick_action_buttons_enabled":false}`.
- Memory: VoteBot 92.8 MiB of 768, Redis 3.1 MiB of 256; host available 2,557 MB at that point and 2,822 MB after the tests.
- Baseline after (broker `/api/status/`, ddp-sync health, api-v3 `/healthz`): 200, 200, 200 before the start, after the start, and after both smoke runs.

## Smoke test: `docker exec votebot-ddp-next python scripts/smoke_ws.py --url ws://localhost:8000/ws/chat --cases scripts/smoke_cases.json --retrieval`
**Run 1 (22:53:38 to 22:54:55): 2/6 passed (exit 1).** I only kept the end of this output: WA FAIL, US FAIL, VA PASS, MI FAIL, index-holds-no-votes/diffs PASS (FL's result was not captured).
**Run 2 (22:55:04 onward): 3/6 passed (exit 1).** The result block, pasted as printed (the first 90 lines are the script's own debug log of its retrieval checks):

```
FAIL  FL
        discovered HB 7089 (f3dbc843-937d-4231-bc2d-e7ab4ff9a92b), session 2024
        'What does this bill do?': 3266 chars, confidence 0.7, 0/0 citations carry a bill id (isolation checked on those)
        'Which version of the bill text are you answering from, and what is its date?': 392 chars, confidence 0.7, 0/0 citations carry a bill id (isolation checked on those)
        'How did the vote on this bill go?': 1140 chars, confidence 0.7, 0/0 citations carry a bill id (isolation checked on those)
        this machine's index 'ddp-knowledge-base' namespace 'default' (filter key ocd_bill_id), 10 chunks; document types checked: bill-text
        current version 13717 matches the vectors' document_id
      ! 'What does this bill do?': 0 citations, expected at least 1
      ! 'What does this bill do?': no citation carries this bill's id: nothing shows the answer came from this bill
      ! 'Which version of the bill text are you answering from, and what is its date?': answer mentions none of ['introduced', 'engrossed', 'enrolled', 'chaptered', 'amendment', 'passed']
      ! 'Which version of the bill text are you answering from, and what is its date?': answer matches none of ['\\d{4}-\\d{2}-\\d{2}', '(January|February|March|April|May|June|July|August|September|October|November|December) \\d{1,2}, \\d{4}']
      ! 'Which version of the bill text are you answering from, and what is its date?': 0 citations, expected at least 1
      ! 'Which version of the bill text are you answering from, and what is its date?': no citation carries this bill's id: nothing shows the answer came from this bill
FAIL  WA
        discovered SB 5444 (65f49d8d-6937-4cf5-a7fe-3f0c4f463f34), session 2025-2026
        'What does this bill do?': 3653 chars, confidence 0.7924733, 1/1 citations carry a bill id (isolation checked on those)
        'Which version of the bill text are you answering from, and what is its date?': 588 chars, confidence 0.7, 0/0 citations carry a bill id (isolation checked on those)
        'How did the vote on this bill go?': 1158 chars, confidence 0.7, 0/0 citations carry a bill id (isolation checked on those)
        this machine's index 'ddp-knowledge-base' namespace 'default' (filter key ocd_bill_id), 10 chunks; document types checked: bill-text
        current version 59142 matches the vectors' document_id
      ! 'Which version of the bill text are you answering from, and what is its date?': answer matches none of ['\\d{4}-\\d{2}-\\d{2}', '(January|February|March|April|May|June|July|August|September|October|November|December) \\d{1,2}, \\d{4}']
      ! 'Which version of the bill text are you answering from, and what is its date?': 0 citations, expected at least 1
      ! 'Which version of the bill text are you answering from, and what is its date?': no citation carries this bill's id: nothing shows the answer came from this bill
PASS  US
        discovered HR 6855 (85021062-30fa-47e3-ac49-8d93f8a6b54b), session 118
        'What does this bill do?': 2365 chars, confidence 0.7975666106, 1/1 citations carry a bill id (isolation checked on those)
        'Which version of the bill text are you answering from, and what is its date?': 363 chars, confidence 0.7916541368, 1/1 citations carry a bill id (isolation checked on those)
        'How did the vote on this bill go?': 740 chars, confidence 0.780444473, 1/1 citations carry a bill id (isolation checked on those)
        this machine's index 'ddp-knowledge-base' namespace 'default' (filter key ocd_bill_id), 2 chunks; document types checked: bill-text
        current version 184011 matches the vectors' document_id
PASS  VA
        discovered HB 177 (e9edf414-4b0e-4df4-8d5b-4d3c9a82fa22), session 2026
        'What does this bill do?': 1642 chars, confidence 0.7837844044, 1/1 citations carry a bill id (isolation checked on those)
        'Which version of the bill text are you answering from, and what is its date?': 254 chars, confidence 0.7853271425, 1/1 citations carry a bill id (isolation checked on those)
        'How did the vote on this bill go?': 1230 chars, confidence 0.75, 0/1 citations carry a bill id (isolation checked on those)
        this machine's index 'ddp-knowledge-base' namespace 'default' (filter key ocd_bill_id), 1 chunks; document types checked: bill-text
        current version 67360 matches the vectors' document_id
FAIL  MI
        discovered HB 4127 (57115f81-e417-431a-9869-e9863abfed7c), session 2025-2026
        'What does this bill do?': 2493 chars, confidence 0.7847449183, 1/1 citations carry a bill id (isolation checked on those)
        'Which version of the bill text are you answering from, and what is its date?': 668 chars, confidence 0.7824819565, 1/1 citations carry a bill id (isolation checked on those)
        'How did the vote on this bill go?': 1012 chars, confidence 0.7, 0/0 citations carry a bill id (isolation checked on those)
        this machine's index 'ddp-knowledge-base' namespace 'default' (filter key ocd_bill_id), 2 chunks; document types checked: bill-text
        current version 98205 matches the vectors' document_id
      ! 'Which version of the bill text are you answering from, and what is its date?': answer matches none of ['\\d{4}-\\d{2}-\\d{2}', '(January|February|March|April|May|June|July|August|September|October|November|December) \\d{1,2}, \\d{4}']
PASS  index holds no bill-votes or bill-version-diff

3/6 cases passed
```
- The failures are about the answers, not about retrieval: the retrieval check reports 10, 10, 2, 1 and 2 chunks and `current version ... matches the vectors' document_id` for every sampled bill (FL 13717, WA 59142, US 184011, VA 67360, MI 98205). That settles the item "never seen on real data": it holds for these five.
- What fails: some answers come back with **0 citations and confidence 0.7**, and some "which version and what date" answers contain neither a version label nor a date. **The failing cases differ between the two runs** (US failed in run 1 and passed in run 2), so it is not stable. The first test had spend I did not measure (a handful of OpenAI chat and embedding calls and Pinecone reads per run); I did not read answer texts, only the script's own pass/fail lines.

## A code error I found in the container log (the more concrete lead)
Ten warnings, one per chat run: `Error resolving bill from title: 'BillVotesService' object has no attribute 'vector_store'` (`votebot.core.agent`).
- `src/votebot/core/agent.py:1661` calls `self.bill_votes.vector_store.query(...)`; `BillVotesService` (`src/votebot/services/bill_votes.py:74`) has no `vector_store` attribute, so the `AttributeError` is caught at `agent.py:1690` and logged as that warning: resolving a bill from its title silently does nothing.
- I do not know whether this explains the 0-citation answers; it is a real defect on `main` at `ba86a14` that I did not touch. Nothing else in the log was above `info` (584 info lines, 10 warnings, all this one).

## What I need from you
1. Is the smoke failure expected from the code error above, or is something else wrong? I will not change code; say if you want me to rerun after a fix, or run any specific case (a single bill, a `page_context` payload) by hand.
2. Stay running or `docker compose ... down`? I left it running (healthy, no public route).
3. Gate 5 (nginx) stays held until gate 3 passes and the operator names a quiet window.
4. Still pending from the operator: the logrotate file and `sudo findmnt --verify`.

Reply on this branch either way.
