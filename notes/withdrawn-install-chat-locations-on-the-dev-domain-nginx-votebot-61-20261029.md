# Withdrawn: the request to install two chat locations on the dev domain's nginx (VOTEBOT-61) is cancelled; nobody needs to act

From: the VoteBot agent. To: whoever has root on the host behind 52.204.100.187, and the prod agent. Date: 2026-10-29. Decided by Ramon.

Ramon closed VOTEBOT-61 ("No, close it"): the dev site keeps using the production VoteBot (`mapapp`, deployed to `4f10564`), so a separate dev-only route to the Mac's VoteBot is not wanted.

* `request-install-chat-locations-on-the-dev-domain-nginx-votebot-61-20261028.md` (`0a53e39`), already on hold since `791e934`, is **withdrawn**. Do not install the two locations; if you already did, tell me and I will write the removal steps (the rollback in that note: remove the two sections).
* Nothing else changes. The merged repo pieces (ddp-broker-py #563 with its default unchanged, ddp-next #68 as a file only, votebot README) stay in place and unused.

No reply needed unless something was installed.
