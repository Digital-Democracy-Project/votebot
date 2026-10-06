# Reply 11: gate 2 built; the api-v3 key is in place; a proposal to render the other secrets from Secrets Manager (PR ready) (prod agent, 2026-10-06)

Replies to replies 8, 9 and 10. **Nothing is running: no `up -d`, no nginx change.** Times UTC.

## Gate 2 (build): done
- Built from votebot `ba86a14eab9d41d8976bc0877ccafab6e9d6d6e0` (checked out detached in `/opt/votebot`), `docker compose -f infrastructure/docker/docker-compose.prod.yml build`, 118 s, exit 0. Image `votebot-ddp-next:local` = `votebot-ddp-next:ba86a14` = `6eea052e122d`, 251 MB. No earlier image existed, so there is no rollback tag for this first build.
- Preflight: 2,361 MB available (runbook: 800), 176 GB disk free, no existing `votebot*` containers, network `ddp-broker-py_default` present. During the build the lowest available memory I saw was 2,139 MB (sampled every 3 s; abort at 500 MB never triggered). After: 2,340 MB available, swap 22 MB used.
- A throwaway container from the image with no env file (`--entrypoint python`): `import aiofiles, websockets, httpx` prints `deps ok`; Python 3.11.17; user `votebot` uid 1000.
- Broker, ddp-sync and api-v3 all returned 200 afterward.

## Reply 10 (api-v3 key): done
- **Where ddp-sync's key lives, names only:** the variable `RDS_OPENSTATES_API_KEY` in `/opt/ddp-sync/.env`, which `infrastructure/render-env.sh` regenerates at every ddp-sync start from the Secrets Manager secret `ddp-sync/credentials` (JSON key `rds_openstates_api_key`; I confirmed it is non-empty, no value read into any note).
- I copied it host-side with a script into `DDP_OPENSTATES_BEARER_TOKEN` in `/opt/votebot/.env`, not printed and not through any note or command line. The copy is a snapshot: it does not follow a rotation of the Secrets Manager value.
- **Tier: `unlimited`** (read-only, earlier today): `profiles_profile` has two rows, both `unlimited`, and ddp-sync's key is one of them. By your rule that is not a stop; it means api-v3 cannot throttle VoteBot through that key.

## `/opt/votebot/.env`
Non-secret values from replies 8 and 9 (including `DDP_OPENSTATES_AUTH_HEADER=x-api-key`, `VOTEBOT_QUICK_ACTION_BUTTONS=false`). By name, filled: everything except `API_KEY`, `OPENAI_API_KEY`, `PINECONE_API_KEY` (empty) and the two Slack tokens (empty on purpose). Git ignores it.
**Deviation from the runbook:** owner `bitnami`, mode 600, not `root` 600. `docker compose` reads `env_file` as the invoking user (`bitnami`), so a root-only file would stop `up -d`; ddp-sync's `.env` is `bitnami:bitnami` 600 for the same reason.

## Proposal and PR: render the secrets from Secrets Manager, like ddp-sync does
The operator asked why not read the secrets from Secrets Manager as ddp-sync does. I agree, and propose the same host-side approach: no code change to VoteBot (it has no boto3 and reads the environment only), nobody types a secret, rotation is "change the secret, re-run the script, `up -d`".
- **Branch pushed, PR to open:** `feat/VOTEBOT-14-render-env-from-secrets-manager` (commit `1fee264`, one commit on `ba86a14`): https://github.com/Digital-Democracy-Project/votebot/pull/new/feat/VOTEBOT-14-render-env-from-secrets-manager (this host has no `gh`; the operator opens the PR). Adds `infrastructure/render-env.sh`, the committed non-secret defaults `infrastructure/docker/prod.env.defaults`, runbook 3.3.1, and 7 standard-library tests (all pass here under host Python 3.9: `python3 -m unittest tests.unit.infrastructure.test_render_env`).
- **Behaviour:** reads `votebot/credentials` (`api_key`, `openai_api_key`, `pinecone_api_key`) and the shared api-v3 key from `ddp-sync/credentials` (`rds_openstates_api_key`); fails before touching `.env` if any is missing or empty; writes atomically with mode 600; never prints a value; `--check` writes nothing; never reads ddp-sync's OpenAI or Pinecone keys (a test pins that). Not run against real AWS yet.
- **Needs, and I cannot do either from here:** (1) the secret `votebot/credentials` created in Secrets Manager (a dedicated OpenAI project key with a budget limit, a read-only Pinecone key if the plan supports it, and a generated `api_key`); (2) this host's instance role allowed `secretsmanager:GetSecretValue` on it. This role is denied `ListSecrets` and `DescribeSecret` on `votebot/credentials`, so I cannot tell whether the secret exists or only the permission is missing. I did read `ddp-sync/credentials` successfully (that is how I checked the key name).
- **Question:** do you want this (option 1) over the operator typing the three values into `.env`? If yes, who creates the secret and the policy? I will not start VoteBot until the three secrets are filled either way.

## Still needed from the operator (sudo)
`sudo install -d -o 1000 -g 1000 /opt/votebot-logs/queries` (does not exist yet), the logrotate file for `/opt/votebot-logs/queries/*.jsonl`, and `sudo findmnt --verify` for the swap line. Next after those: the runbook preflight and gate 3.

Reply on this branch either way.
