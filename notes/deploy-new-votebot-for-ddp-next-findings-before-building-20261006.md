# VoteBot for ddp-next: read-only findings from the new server, and two decisions needed before I build (prod agent)

Replies to `deploy-new-votebot-for-ddp-next-20261006.md`. Nothing has been installed or changed on this host for VoteBot; only the clone below and read-only checks. Times 2026-10-06 UTC.

## Done
- Prerequisite check: `origin/main` is `72279d8` and contains the merges of PR #17 and PR #19. Passes.
- Cloned the repo to `/opt/votebot` (HTTPS, as the `bitnami` user), main, clean, only so I could read the runbook. This is not the `/home/votebot/votebot` layout from step 3.1; I have not done the SSH deploy-key check from that step, so "the server can read the repo with a deploy key" is NOT yet verified.

## Step 1 results (read-only)
- Memory: 7,879 MB total, 2,467 MB available (549 MB truly free), **no swap**. Biggest processes: ddp-sync 830 MB, broker celery 444 MB, tmux 410 MB, host Redis 401 MB, dockerd 287 MB. Two idle Claude background processes (about 440 MB and 270 MB) are also resident.
- CPU/disk: 2 CPUs; 296 GB disk, 177 GB free (38% used).
- Listening ports: 80 and 443 (broker nginx), 8001 (ddp-sync), **8002 (api-v3)**. 6379 is a Redis on the host itself (the broker's; not to be touched). 6380 is free.
- Services: everything runs in Docker, none under systemd (the `systemctl` listing for broker/openstates/sync/redis/nginx is empty). Containers up: ddp-sync (healthy), api-v3 (healthy), broker web (unhealthy: the known `DisallowedHost` healthcheck), celery, celery-beat, nginx, two Redis containers.
- Host Python: **3.9.2**.
- Not obtained: the nginx hostnames (`sudo -n nginx -T` returned nothing; it needs an interactive sudo, and nginx is in the broker's container anyway), whether Cloudflare is in front, the broker's local URL.

## Two things that block the plan as written
1. **Port 8002 is already api-v3.** The note's VoteBot unit also uses 8002. Pick another (I suggest 8003).
2. **Python 3.9.2 on the host; VoteBot needs 3.11+.** Options: (a) install Python 3.11 on the host (a package change on the production box), or (b) run VoteBot in a container like the other services here (the broker uses python 3.12 in its image). Which do you want? I would prefer (b), but it changes the systemd-unit steps in 3.5.

Also, since nothing here is systemd and nginx lives inside the broker's container, step 3.6 (a new server block) means editing the broker's nginx template and recreating/restarting that container, which interrupts the live broker. I have not planned that yet; please confirm whether a separate host nginx is intended instead.

## Memory is the other concern
Available memory is about 2.4 GB with no swap. The unit asks for up to 1 GB plus a 256 MB Redis. That fits only barely, next to heavy jobs on the same host. Please confirm that is acceptable, or say if a swap file should be added first.

## Still needed from you before I start step 3
The port, the Python approach, the hostname and certificate, the Pinecone namespace, and the secrets (a new `API_KEY`, a dedicated OpenAI project key, the Pinecone key, the api-v3 bearer token). I will not write any key value into a note. Steps 2 (freeze of the old server) is on the old civic server, which this host does not reach; that has to be done from there.

Reply on this branch either way.
