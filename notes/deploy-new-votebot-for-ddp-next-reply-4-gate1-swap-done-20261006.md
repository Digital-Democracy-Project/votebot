# Reply 4: gate 1 (swap) done; gate 2 is blocked on two answers (prod agent, 2026-10-06)

Follows reply 2, gate 1. The operator ran the `sudo` commands; I verified the result. Nothing else changed.

## Gate 1: swap
- Backed up `/etc/fstab` first (copy named `/etc/fstab.bak-<date>-<time>`). `/swapfile` did not exist before and `/etc/fstab` had no swap lines.
- Created `/swapfile` with `fallocate` (the filesystem is ext4, so no `dd` fallback was needed): 2 GiB, owner root, mode 600; `mkswap`, `swapon`; added `/swapfile none swap sw 0 0` to `/etc/fstab`; wrote `vm.swappiness=10` to `/etc/sysctl.d/99-swappiness.conf` and applied it. Swappiness was 60 before.
- `free -m`, before: total 7,879 MB, used 4,683, free 1,209, available 2,954, Swap 0. After: total 7,879 MB, used 4,627, free 1,257, available 3,009, Swap total 2,047, used 0. (Available memory moves with ordinary usage; swap adds headroom, not RAM.)
- `/proc/swaps` shows `/swapfile`, file, 2,097,148 KB, used 0. `swapon` is not on the `bitnami` user's PATH (it is in `/usr/sbin`), so I read `/proc/swaps`.
- Not tested: a reboot, so the `fstab` and `sysctl.d` entries are unproven across one.
- Rollback: `sudo swapoff /swapfile && sudo rm /swapfile`, restore the `fstab` backup, remove `/etc/sysctl.d/99-swappiness.conf`.

## Baseline health after (200 each)
Broker `/api/status/` 200; ddp-sync `/ddp-sync/v1/health` 200; api-v3 `/healthz` 200. All containers up. The broker `web` container still reports unhealthy: the known `DisallowedHost` healthcheck problem, not new.

## Other changes today on this host (for your baseline table)
- ddp-broker `aab241ec` -> `1fcff46d` (21:07 UTC; reported on the broker branch).
- api-v3 `1b7fae0` -> `ae33226` (OPEN-316, PRs #18 to #20; recreated 21:22 UTC; rollback tag `ddp-openstates-api:pre-open316`). Search checks passed for `HB 1C`, `HB 1A` and `HB 99999999` in FL, UT and VA.

## Gate 2 is blocked on two answers from you
1. **How secrets are provisioned on this host.** I will not put a key value in a note or a shell history line. Which mechanism do you want for the `.env` (the OpenAI project key, the Pinecone key, a new `API_KEY`, and the api-v3 credential)?
2. **The api-v3 auth decision from reply 3.** VoteBot's replica mode sends `Authorization: Bearer`; the api-v3 here returns 403 for that and 200 for `X-API-Key`. Which route do you want (a Bearer-accepting URL, api-v3 accepting Bearer, or VoteBot sending `X-API-Key`)? This sets `DDP_OPENSTATES_API_ROOT` and the token.

I will not start the build until both are answered. Gates 3 to 5 are unchanged.
