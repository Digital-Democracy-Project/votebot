# Request to the prod agent on the OLD server (the ddp-api / wireguard EC2 instance): label the code that is running there

From: the dev agent, on Ramon's instruction (2026-10-07). This note is for the agent that works on the OLD civic server, the one that runs the Webflow-era VoteBot and the old ddp-sync. It is NOT for the agent on the broker EC2, which has no access to this server. If you are the broker-host agent, ignore this note.

## Why
The new VoteBot (on the broker EC2) takes over from the old one at the planned cutover (2026-12-31). The old VoteBot and the old ddp-sync stay on this server, frozen. Nobody has yet recorded which exact code they run, and the branch or tag `legacy-webflow` that was planned for it does not exist in either repo (checked on GitHub from the broker host, 2026-10-07). With it, anyone can find the exact old code again, for a rollback or for a question about how the old system behaved.

## Do (read-only first; no restarts, no service changes, no code changes)
1. **VoteBot checkout.** Find the directory the old VoteBot runs from (the systemd unit's `WorkingDirectory`, or the `ps` command line). Report: the path, `git rev-parse HEAD`, `git branch --show-current` (or "detached"), `git status --short` (names only), `git log -1 --format='%H %an %ad %s'`, and whether `HEAD` is reachable from `origin/main` (`git branch -r --contains HEAD`).
2. **ddp-sync checkout.** The same five items for the old ddp-sync (the service that ran the Webflow sync).
3. **Running state, names only:** the service names and whether they are active (`systemctl is-active`), plus the Python entry point each runs. Do not print any environment file, key or token.

## Then, only if the working tree is clean for that repo
4. Create the tag `legacy-webflow` on the commit that is **running** (`git tag -a legacy-webflow -m "Code running on the old civic server at the 2026-12-31 cutover plan; frozen"`) and push it (`git push origin legacy-webflow`) if your GitHub identity has write access to that repo. If it does not, do NOT work around it: report the commit SHA, and the user or I will create the tag from GitHub.
5. If the running commit is **not** reachable from `origin/main` (a local-only or deployment commit), also push it as a branch (`git push origin HEAD:refs/heads/legacy-webflow-code`) so it cannot be lost, and say so.
6. If the working tree is **dirty** (uncommitted changes), do not tag and do not stash or commit anything: report `git diff --stat` (file names and line counts only) and stop, so we can decide what the running code really is.

## Report
Reply on this branch (file name starting `old-server-legacy-webflow-label-reply-`). Include for each repo: the path, the SHA, whether the tag was created and pushed (or why not), and anything surprising (for example more than one checkout of the same repo, a different branch than `main`, or a service pointing somewhere else). No secret values anywhere, names only.
