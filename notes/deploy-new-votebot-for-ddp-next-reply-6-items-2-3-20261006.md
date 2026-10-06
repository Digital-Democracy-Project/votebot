# Reply 6: reply 5 section 4, items 2 and 3 done; items 1, 4 and 5 staged for the operator's sudo (prod agent, 2026-10-06)

Replies to reply 5. No build, no start, no nginx, no secrets. I will not build until sections 1 and 3 of reply 5 are resolved and `main` has votebot PR #21 (`origin/main` here is still `72279d8`).

## Item 2: reachability from the broker network, no key
A throwaway container on `ddp-broker-py_default` (the `ddp-sync:prod` image, run with `--rm`): `https://mapapp.digitaldemocracyproject.org/api/status/` -> **200** (`{"status": "ok", "service": "ddp-broker", ...}`); `http://10.0.0.11:8002/healthz` -> **200** (`OK`). api-v3 is reachable from the broker network through the host address; no network join is needed. The network's subnet is 172.30.0.0/16.

## Item 3: ddp-sync's api-v3 key tier (names and tiers only)
- api-v3 keeps keys in one table, `profiles_profile`, with three columns: `id`, `api_key`, `api_tier`. It has **two rows, both tier `unlimited`**.
- ddp-sync's `RDS_OPENSTATES_API_KEY` is one of those two rows: **`unlimited`**. Its `OPENSTATES_API_KEY` (the public openstates.org key) matches no row, as expected. I did not identify the other row's owner.
- **How the keys were created: I cannot tell.** The table has no creation date or owner column, and I have no record of the standup step. Creating VoteBot's dedicated key (the `silver` tier you propose) would mean inserting a row (`id`, `api_key`, `api_tier`) into `profiles_profile`. That is a database write; this host's read-only path cannot do it. It needs your approval and a decision on who writes it and how the key value reaches `/opt/votebot-ddp-next/.env` without passing through a note or a command line.

## Items 1, 4, 5: staged, waiting for the operator's sudo
- Item 1: `docker-compose.yml` (from reply 2 gate 2, network `ddp-broker-py_default`) and an `.env.template` with every key from step 3.3, empty values, `DDP_OPENSTATES_AUTH_HEADER=x-api-key`, no `WEBFLOW_*`. `VOTEBOT_QUICK_ACTION_BUTTONS` is left empty because the old server's value is not known here; please say what it should be. The directory and log folder (owner uid 1000, which on this host is the `bitnami` user) need sudo.
- Item 4: `sudo findmnt --verify` for the swap line. My earlier non-root run only showed the normal swap-file warnings.
- Item 5: logrotate for `/opt/votebot-ddp-next/logs/queries/*.jsonl`, the runbook's file with the path changed and `su bitnami bitnami` (uid and gid 1000 on this host). The app writes one file per day (`YYYY-MM-DD.jsonl`), so rename-style rotation is safe and I did not add `copytruncate`.
I will report the results of 1, 4 and 5 in the next note once they are run.

Reply on this branch either way.
