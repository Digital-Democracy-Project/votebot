# Reply 10: api-v3 key, the user's decision

The user points out that ddp-sync on this EC2 already has an api-v3 key, possibly from AWS Secrets Manager. Decision: **reuse ddp-sync's key for the first deployment** instead of creating a dedicated one. No database row is needed.

## What to do
1. Find where ddp-sync gets its api-v3 key (its env file, its task/compose config, or Secrets Manager). **Report the location and the variable or secret name only, never the value.**
2. Put that value into `DDP_OPENSTATES_BEARER_TOKEN` in `/opt/votebot/.env` (the setting name is historical; with `DDP_OPENSTATES_AUTH_HEADER=x-api-key` it is sent as `X-API-Key`). Copy it host-side (e.g. `sudo -e` or a root-run command that reads from the source), never through a note, ticket or shell history.
3. Check its tier in `profiles_profile` (read-only: `SELECT api_tier FROM profiles_profile WHERE api_key = ...` run by an operator, without printing the key). If it is `unlimited`, VoteBot cannot throttle ddp-sync through it. If it is anything else, stop and tell the user: a public chat could exhaust ddp-sync's quota.

## Trade-off (the user accepted it)
Both services share one credential, so rotating it means updating both, and usage cannot be told apart per service. A dedicated key stays an option later.

## Still open
The other secrets (OPENAI, PINECONE, API_KEY): how they are provisioned is still the user's call. If they are also in Secrets Manager under ddp-sync, say so, but **do not reuse ddp-sync's OpenAI or Pinecone keys** without asking: the plan is a dedicated OpenAI project key and a read-only Pinecone key for VoteBot.
