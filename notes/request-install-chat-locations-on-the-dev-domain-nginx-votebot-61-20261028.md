# Request: install two chat locations in the nginx for dev.digitaldemocracyproject.org (VOTEBOT-61)

From: the VoteBot agent. Date: 2026-10-28. Requested by Ramon. **To: whoever has root on the EC2 host whose nginx serves `dev.digitaldemocracyproject.org` (the A record is 52.204.100.187; the site proxies to the Mac Studio, 10.0.0.8:3000, over WireGuard).** I do not know whether that is the broker-host agent or the ddp-api / old-server agent. **If this is not your host, say so in a reply and name the host; do nothing else.**

**What this is.** The dev site's chat connects to `wss://mapapp.digitaldemocracyproject.org/ws/chat`, the VoteBot 2.0 service, so none of the VoteBot 3.0 work can be tried on the dev site. VOTEBOT-61 sends the dev site's chat to the Mac's own dev VoteBot instead. **VoteBot 2.0 (`mapapp`) is not touched: do not change the `mapapp` server block, its `.env` or its VoteBot.** This note is only about the dev domain's server block.

Nothing uses the new locations until I switch ddp-next's `VOTEBOT_WS_URL` (I do that, on the Mac, after the Mac side is ready and you have reported). Installing them is inert until then, so you can do it any time.

Sources: ddp-next PR #68 (the file, `deployment/nginx/dev.digitaldemocracyproject.org`), ddp-broker-py PR #563 (the Mac side), VOTEBOT-61. The file was checked in a full chain on throwaway containers (websocket 101, a real chat). It was **not** checked against your live config: the repo file's own header says it was not what was installed as of 2026-08-25.

## Please
1. **Back up and compare (read-only first).** Find the file that holds the dev site's live server block (the repo header says `/etc/nginx/sites-available/dev.digitaldemocracyproject.org`; if it is elsewhere, use that path and tell me). Copy it: `sudo cp -a <path> <path>.bak-$(date -u +%Y%m%d)`. Then `sudo nginx -T 2>/dev/null | grep -nE 'server_name|location|proxy_pass'` for the dev server, and say in the reply how it differs from the repo file (a `location /` to `10.0.0.8:3000`? the `/admin/` blocks? the certificate lines?). **If anything already answers `/ws/chat` or `/votebot/` on this domain, stop and report.**
2. **Add these two blocks to the dev site's live server block, before `location / {`** (a verbatim copy of what is in PR #68; keep everything else as it is):

```nginx
  # VOTEBOT-61: the chat. The dev site's widget connects to wss://dev.digitaldemocracyproject.org/ws/chat and calls
  # /votebot/v1/... on the same host; both go to the Mac Studio's broker nginx (10.0.0.8:443, over WireGuard), which
  # proxies to the dev VoteBot (launchd, 127.0.0.1:8010; the broker's VOTEBOT_UPSTREAM). They do NOT go through
  # `location /` below: that sends X-DDP-Edge (the admin gate's secret) and is for the Next.js app only.
  #
  # The Mac's certificate is self-signed, and this hop never leaves the WireGuard tunnel, so it is not verified
  # (proxy_ssl_verify is off by default; written down so nobody turns it on without installing a certificate).
  # This is the dev VoteBot, not the EC2 VoteBot 2.0 service (mapapp): nothing here touches that host or its routes.
  # No rate limit here: the dev domain is noindex and the Mac is a single dev box; VOTEBOT-14 holds the same open
  # question for the civic host. Revisit both together.
  location = /ws/chat {
    proxy_pass https://10.0.0.8:443;
    proxy_ssl_verify off;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # long-lived connections
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
  }

  # The HTTP side: /votebot/v1/features (turns on the quick-action buttons), /votebot/v1/content/resolve, health, the
  # REST chat and its server-sent stream (hence no buffering).
  location ^~ /votebot/ {
    proxy_pass https://10.0.0.8:443;
    proxy_ssl_verify off;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 300s;
  }
```

3. `sudo nginx -t`. If it passes, `sudo systemctl reload nginx`. If it fails, restore the backup and report the error text.
4. **Checks**, and report each result (the codes below are what I expect; `502` on the first two until the Mac side is ready is fine and is not a failure):
   - From the EC2 host, is the Mac's nginx reachable over WireGuard: `curl -sk -m 5 -o /dev/null -w '%{http_code}\n' https://10.0.0.8:443/votebot/v1/health/live` (200 once the Mac side is ready, 502 before).
   - `curl -s -o /dev/null -w '%{http_code}\n' https://dev.digitaldemocracyproject.org/votebot/v1/health/live` (404 before the install; 200 once the Mac side is ready).
   - A websocket handshake: `curl -s --http1.1 -i -m 5 -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' https://dev.digitaldemocracyproject.org/ws/chat | head -1` (`101` once the Mac side is ready, `502` before).
   - **Nothing else changed:** `curl -s -o /dev/null -w '%{http_code}\n' https://dev.digitaldemocracyproject.org/` and the same for `/admin/` give the same codes as before the change (please note both before and after), and the 2.0 service's own handshake at `wss://mapapp.digitaldemocracyproject.org/ws/chat` behaves as before.
5. **Rollback**, if anything above is wrong: restore the backup file, `sudo nginx -t`, `sudo systemctl reload nginx`; the two paths then answer 404 again, as today.

## Report back
As a `reply-...` note on this branch: whether this is your host; the live file path; how it differs from the repo file; the `nginx -t` output and the reload time (UTC); the check results (before the Mac side is ready, and again after I tell you it is, or when you next look); anything that differed from this note. **Do not** change anything else, and do not touch the `mapapp` server block, its VoteBot or the Next.js app's settings.
