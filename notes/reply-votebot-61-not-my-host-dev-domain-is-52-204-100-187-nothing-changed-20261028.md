# Reply: VOTEBOT-61 nginx request. This is NOT the broker-host agent's host. The dev domain is served from 52.204.100.187. Nothing changed

Prod agent (the EC2 broker host, 172.31.78.173), Wed 2026-10-28 01:48 UTC. Read-only checks only; no backup, no edit, no reload.

* **This host:** public IP **44.210.190.195** (instance metadata), which is the A record of `mapapp.digitaldemocracyproject.org`. The A record of **`dev.digitaldemocracyproject.org` resolves to 52.204.100.187: a different machine.**
* **Why I am sure it is not served here:** this host has **no host-level nginx at all** (no `nginx` binary, no `/etc/nginx`). The only nginx is the broker's container (`ddp-broker-py-nginx-1`), whose `server_name` is `mapapp.digitaldemocracyproject.org` only; the dev domain appears there twice, but not as a server of its own (I did not read those two lines in context beyond seeing that they are not a `server_name`). So there is no dev-domain server block here to back up or edit.
* **Which host it is, as far as I can tell:** 52.204.100.187 is also the single outside address that called `https://mapapp.digitaldemocracyproject.org/votebot/v1/health/ready` twice at 00:52 UTC today (it appears in this host's broker nginx log as a client), which fits the ddp-api / old-server host you list as the other candidate. I have no access to it. Please send this request to that host's agent.
* I did **not** touch the `mapapp` server block, its `.env`, VoteBot 2.0, the Next.js app, or this host's VoteBot (3.0, now `740fc1d`, reported in my earlier note).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
