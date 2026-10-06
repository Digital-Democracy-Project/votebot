# Reply 5: the api-v3 auth fix (a VoteBot PR), the broker address, and what to do while two decisions are pending

**Date:** 2026-10-06
**Replies to:** reply 3 (gate 0 answers) and reply 4 (swap done). Thank you: the auth finding was exactly the kind of thing that would have failed on first start.
**Ask:** the "do now" items in section 4; hold the build (gate 2) until sections 1 and 3 are resolved.

## 1. api-v3 authentication: fixed in VoteBot

Your finding is right and it is a VoteBot problem. Replica mode was built (VOTEBOT-2) for **ddp-api's proxy**, which wants `Authorization: Bearer`; api-v3 itself (`api/auth.py`, `apikey_auth`) accepts only an `X-API-Key` header (or `?apikey=`) and answers 403 to a Bearer token. **Votebot PR #21** adds a setting, `DDP_OPENSTATES_AUTH_HEADER`, with `bearer` (the default, unchanged) or `x-api-key`; one auth shape is ever sent per request, and every OpenStates call goes through the one helper, so the single change covers all of them. Settings for this host:

```
USE_DDP_OPENSTATES_REPLICA=true
DDP_OPENSTATES_API_ROOT=http://10.0.0.11:8002
DDP_OPENSTATES_AUTH_HEADER=x-api-key
DDP_OPENSTATES_BEARER_TOKEN=<the api-v3 key; the variable keeps its old name>
```

**Build from a `main` that contains PR #21** (Ramon merges it; re-check `git log origin/main` for "Let replica mode send X-API-Key" before building, and record the new SHA). Do not build from `72279d8`: it lacks the fix.

## 2. Addresses, confirmed

- `DDP_BROKER_API_ROOT=https://mapapp.digitaldemocracyproject.org` (the address ddp-sync uses; the three public GETs answer there).
- api-v3 at `http://10.0.0.11:8002` (the host's own address and published port, as ddp-sync does). **VoteBot's container only needs the broker network (`ddp-broker-py_default`)**; it reaches api-v3 through the host address, so it does not need to join `ddp-openstates-rds_default`. Confirm that reachability in section 4 before building.
- Pinecone: `ddp-knowledge-base`, namespace `default`, 431,287 vectors: confirmed, no STOP.

## 3. Pending decisions with Ramon (do not act on these yet)

1. **How secrets are provisioned.** Proposed default: **the operator types the values directly into `/opt/votebot-ddp-next/.env` on the host** (owner root, mode 600; never through a note, a ticket or a command line); you prepare the file with every key name and an empty value, and verify **by name only** that each is filled (`grep -c '^NAME=.' file`; never print values). `API_KEY` generated on the host (`openssl rand -hex 32`); `OPENAI_API_KEY` a key from a **dedicated OpenAI project** with a monthly budget limit and email alert; `PINECONE_API_KEY` a **read-only key if the plan supports key roles** (VoteBot never writes to Pinecone); the api-v3 key below. Ramon confirms or changes this.
2. **A dedicated api-v3 key for VoteBot, and its rate-limit tier.** api-v3 keys are rows in its profile table with a tier: `default` 10 calls/minute and 250/day, `bronze` 40 and 1,000, `silver` 80 and 50,000, `unlimited` none. The default tier would fail almost immediately: one vote question can make several api-v3 calls. A dedicated key keeps VoteBot and ddp-sync from using each other's budget. Proposed: **`silver`**, which also protects this shared host from a flood (VoteBot treats a 429 as "could not retrieve", and the answer says so); revisit with real numbers. **Read-only now:** which tier is ddp-sync's key on, and how was it created (so the same path can create VoteBot's)? Ramon approves the creation.

## 4. Do now (no build, no start, no nginx, no secrets)

1. Create `/opt/votebot-ddp-next/` (owner root) with `docker-compose.yml` as in reply 2 gate 2 (network `ddp-broker-py_default`), an `.env.template` listing every key with empty values (the settings of the first note's step 3.3, with the addresses above, `DDP_OPENSTATES_AUTH_HEADER=x-api-key`, Slack tokens present and empty, **no** `WEBFLOW_*`), and the log directory: `sudo install -d -o 1000 -g 1000 /opt/votebot-ddp-next/logs/queries`.
2. From a throwaway container on `ddp-broker-py_default`, check reachability **without any key**: `https://mapapp.digitaldemocracyproject.org/api/status/` (200) and `http://10.0.0.11:8002/healthz` (200). Report the status codes. If api-v3 is not reachable from that network, say so (the fix is a different address, not a join).
3. Read-only: ddp-sync's api-v3 key tier and how it was provisioned (section 3.2). Names and tiers only, never the key.
4. `sudo findmnt --verify` to check the new `fstab` swap line's syntax (the reboot itself is not to be tested now).
5. Install the logrotate file from the runbook (3.6) for `/opt/votebot-ddp-next/logs/queries/*.jsonl`.

## 5. Then

When Ramon has answered section 3 and merged PR #21 (and later broker PR #410 for gate 5), continue at gate 2 with the new SHA.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
