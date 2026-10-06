# Reply 9: GO for gate 2 (build). All PRs are merged.

The user has merged every VOTEBOT-14 PR and asked you to deploy.

## Merged
- votebot `main` at **ba86a14** (PRs #20 runbook, #21 auth header, #22 prod compose + log-dir check are all in). **Build from this SHA** and record it in the ticket.
- Broker PR #410 (nginx routes) is merged as de734d7. **Merged is not applied**: the running nginx container still has the old template. It changes nothing until you recreate nginx (gate 5).

## Do now
1. Pull `main` in the votebot checkout (`/opt/votebot`), `git checkout ba86a14`.
2. Follow the updated runbook section 3 (`docs/RUNBOOK-civic-host-ddp-next.md`): swap (3.1) if not already done, preflight (800 MB free memory, 5 GB disk), build (3.2), and note the previous image tag for rollback.
3. Create `/opt/votebot-logs/queries` owned by uid 1000.
4. Create `/opt/votebot/.env` with the settings from reply 8, with `umask 077`. **Secret values: wait** (below). You may create the file with every non-secret value and report which secret names are still empty.

## Wait for the user (do not guess or reuse keys)
- How the secrets get in (operator types them on the host is the proposal) and the dedicated api-v3 key. Stop before `up -d` until the file is complete, then run gate 3 (verification inside the Docker network, including `scripts/smoke_ws.py`).

## Still gated
- Gate 5 (recreate only nginx with `--no-deps --force-recreate nginx`, template tested first): only in a quiet window the user names, after gate 3 passes.

Report back with: the SHA built, image ID, `free -m` before/after the build, and anything that differed from the runbook.
