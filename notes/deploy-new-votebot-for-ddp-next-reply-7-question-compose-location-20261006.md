# Reply 7: a question before I create anything: where should VoteBot's compose file live? (prod agent, 2026-10-06)

Follows reply 6. The operator asked this, and I agree it is worth settling before gate 2. **I am holding the `sudo` steps (item 1, 4, 5 of reply 5) until you answer. Nothing has been created.** `/opt/votebot-ddp-next/` does not exist; I only staged three files in a temp directory.

## The question
Reply 2 has the compose file in a host-only directory, `/opt/votebot-ddp-next/` (not a git repo, not in `main`), with the `.env` in it. Every other compose project on this host keeps its compose file **in its repo checkout**:
- broker: `/opt/ddp-broker-py/infra/compose/prod.yml`, with its `.env` at `/opt/ddp-broker-py/.env` (outside git);
- ddp-sync: `/opt/ddp-sync/infrastructure/docker-compose.prod.yml`;
- api-v3: `/opt/ddp-open-states/deploy/docker-compose.rds.yml`.
A compose file outside git has no review and no rollback, and ddp-sync's uncommitted local compose edits already cost a hand-merge risk on every pull.

## Proposal
Add a production compose file to the votebot repo (for example `infrastructure/docker/docker-compose.prod.yml`) in a PR, run as its **own compose project** (`-p votebot-ddp-next`) so broker deploys still never recreate it (your reason in reply 2). The `.env` would live at `/opt/votebot/.env` (git-ignored, owner root, mode 600, like the broker's). Deploy would then be the same routine as the others: `git pull --ff-only`, `build`, `up -d`. The query-log folder would sit in a directory outside git (for example `/opt/votebot-logs`, owned by uid 1000) so the repo checkout stays clean.

It needs a PR you review and merge, so it fits beside votebot PR #21, which gate 2 already waits for. I would also use `docker compose build` from `/opt/votebot` at the merged SHA, which is what reply 2 asked for anyway.

## What I need
Which do you want: (A) the repo-based compose above, or (B) keep `/opt/votebot-ddp-next/` as in reply 2? If A, please say who writes the PR (I can draft it from the staged file, but I would not merge it) and what to do with `VOTEBOT_QUICK_ACTION_BUTTONS` (the old server's value; I left it empty in the template).

Everything else in reply 5 is unchanged. Reply on this branch either way.
