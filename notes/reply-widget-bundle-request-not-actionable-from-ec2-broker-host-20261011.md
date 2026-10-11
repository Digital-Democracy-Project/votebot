# Reply: the widget-bundle request (VOTEBOT-1) cannot be done from the EC2-broker host; it needs the ddp-api instance

From: prod agent (EC2 broker host, ip-172-31-78-173), 2026-10-11 UTC. Operator confirmed the point below.

- The request `request-copy-the-new-chat-widget-bundle-votebot-1-20261011.md` targets the old server that serves `votebot.digitaldemocracyproject.org/dist/ddp-chat.min.js`. That is the **ddp-api EC2 instance**, not this host. The work can only be done there, by whoever has a shell on it (and, for step 4, access to the Cloudflare zone).
- **I did nothing for this request.** I changed no file on this host or elsewhere, and I did not look for the served path (step 1), so I have no path, hash, backup name or headers to report.
- Please re-address it to the session or person with access to the ddp-api instance (and to Ramon for the Cloudflare single-file purge).
- The 7-day `immutable` cache decision (Webflow `?v=` on the script tag, or a shorter `max-age` for this one file in nginx) is still open with the operator and Ramon. I have not been asked to decide it.

Separate and unchanged: my VoteBot 1074152 reply (`reply-votebot-1074152-deployed-checks-a-to-e-direct-reconnect-hang-20261011.md`) still waits on your direction about check B (same-session reconnect hang on the direct path).
