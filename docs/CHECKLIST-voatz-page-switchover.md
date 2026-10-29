# Checklist: move the Voatz chat page to the new website (VOTEBOT-54), January 1st

Written 2026-10-29 for Ramon, who said he will set up the front server himself. Plain steps, exact commands, and the way back.

## What this does, in four lines

Voters open `https://votebot.digitaldemocracyproject.org/?ddp_url=<a bill's page>` from a Voatz poll, in the phone's built-in browser. Today that address is the old page on the old server (VoteBot 2.0). On January 1st it becomes the new website's page `/voatz-chat` (merged in ddp-next #87): the chat opens by itself on that bill. The address and the link form do not change. Decisions already made (Ramon, 2026-10-29): the switchover is January 1st with only VoteBot 3.0 addresses (every poll expires at the end of the year, so old Webflow-style links need nothing); the new React page only, no interim old page.

## What I assumed, and could not check

* **The Voatz address is served by the same ddp-next that serves `dev.digitaldemocracyproject.org` today** (the Mac Studio, `10.0.0.8:3000`, reached over WireGuard from the front server `52.204.100.187`). If the production website is moved to another host before January 1st, do the front-server steps on that host's front server instead.
* **I cannot read the front server or Cloudflare.** The nginx commands below follow the dev site's file (`ddp-next/deployment/nginx/dev.digitaldemocracyproject.org`), not the live config. Look at the live one first.
* **I do not know the current Cloudflare record for the Voatz address** (value, proxied or not, TTL). Step 0 writes it down; the way back depends on it.
* **I do not know whether Voatz accepts a link to a host other than `votebot.digitaldemocracyproject.org`** in a test poll. Step 1 says what to do either way.

## Who does what

| Step | Who |
|---|---|
| 0 Write down today's setting; 3 front server; 4 TTL; 5 cutover; 6 way back | Ramon |
| 1 Test in the real Voatz app | Ramon (needs a Voatz test poll) |
| 2 Allow the Voatz address on production VoteBot | a small pull request (the VoteBot agent prepares it on request), then the prod agent deploys it |

## When

* **By mid-December:** steps 0, 1, 2 and 3a to 3c (nothing is visible to voters).
* **December 31st:** step 4 (lower the DNS time-to-live).
* **January 1st:** step 5. All polls have expired, so no voter is mid-poll and a few minutes of a certificate warning is harmless.
* Do not send any poll with the VoteBot link from the broker's new builder (BROKER-334) before step 5 is done and verified.

## Step 0. Write down today's setting (5 minutes)

In Cloudflare, open the DNS record for `votebot.digitaldemocracyproject.org` and write down: type, value, proxy status (orange or grey cloud), TTL. Paste it into VOTEBOT-54. **This is the way back.**

## Step 1. Test the page in the real Voatz app (mid-December)

The page works on any address of the new website at `/voatz-chat`, so you can test before touching DNS:

1. In a Voatz **test** event (the test precinct), set a link to
   `https://dev.digitaldemocracyproject.org/voatz-chat?ddp_url=https://dev.digitaldemocracyproject.org/bill/FL/2026E/HB%205601E`
   (If Voatz refuses a link to that host, wait and run this step after step 5, with the real `votebot.` address, before the first real poll.)
2. Open it from the app on **an iPhone and an Android phone**. Check each of these:
   - the page opens inside the app, with no browser buttons;
   - the chat opens by itself and says it can answer questions about **HB 5601E**;
   - type a question: an answer streams in and finishes;
   - the three quick buttons (Summary, Pros & cons, Status & votes) appear and one works;
   - the chat fills the screen, the keyboard does not hide the text box, turning the phone sideways still works;
   - close the chat: the landing page with a button shows; reopen it;
   - closing the pop-up and opening the link again starts a fresh chat (no error).
3. Write the result (pass, or what failed, with a screenshot) on VOTEBOT-54. **This is the hard gate: if any check fails, stop and tell the VoteBot agent.**

## Step 2. Allow the Voatz address on production VoteBot

The page calls two things on VoteBot from the browser (`/votebot/v1/content/resolve` and `/votebot/v1/features`), and VoteBot only answers addresses on its allowed list. The list is one line in `infrastructure/docker/prod.env.defaults`:

```
ALLOWED_ORIGINS=["https://dev.digitaldemocracyproject.org"]
```

It becomes (the production website's own address is a separate cutover item, NEXT-36):

```
ALLOWED_ORIGINS=["https://dev.digitaldemocracyproject.org","https://votebot.digitaldemocracyproject.org"]
```

That is a pull request on the votebot repo, then the prod agent re-renders the env and restarts the container (runbook sections 3.2 to 3.5). It can ship any time before January 1st: an extra allowed address is harmless. **Check it worked** (nothing changes, safe from any machine):

```
curl -s -D - -o /dev/null -H 'Origin: https://votebot.digitaldemocracyproject.org' \
  https://mapapp.digitaldemocracyproject.org/votebot/v1/features | grep -i access-control-allow-origin
```

Expect `access-control-allow-origin: https://votebot.digitaldemocracyproject.org`. No line means the address is not allowed yet.

## Step 3. The front server (`52.204.100.187`), before DNS moves

**3a. Look first.** Open the live nginx config for the dev site and confirm it matches the repo file (it was never installed from the repo; the repo file says so). Check `$connection_upgrade` is defined (the dev block uses it).

**3b. Create the Voatz block.** `/etc/nginx/sites-available/votebot.digitaldemocracyproject.org`:

```
server {
  listen 80;
  server_name votebot.digitaldemocracyproject.org;

  location / {
    proxy_pass http://10.0.0.8:3000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 120s;
  }
}
```

Three things this block deliberately does **not** have, so do not copy them from the dev file:

* **`proxy_set_header Host $host;` stays.** The website shows the Voatz page only when it sees this exact Host (`src/middleware.ts`, `VOATZ_CHAT_HOST`). Without it every visit gets the normal home page.
* **No `X-DDP-Edge` line.** On the dev site that header belongs to the website's admin proxy (`src/middleware.ts`), which is switched off; the website redirects `/admin` to the admin's own address anyway (see the check below). Do not add the header: the Voatz address should never serve the admin.
* **No `/ws/chat` or `/votebot/` sections.** The dev file contains two (from VOTEBOT-61) that send chat traffic to the Mac's dev VoteBot; they were never installed and must not be here. The page's chat connects straight to `wss://mapapp.digitaldemocracyproject.org`, not through this server.

**3c. Switch it on and test before DNS moves.**

```
sudo ln -s /etc/nginx/sites-available/votebot.digitaldemocracyproject.org /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
curl -s --resolve votebot.digitaldemocracyproject.org:80:127.0.0.1 \
  'http://votebot.digitaldemocracyproject.org/?ddp_url=x' | grep -o '<title>[^<]*</title>'
curl -s -D - -o /dev/null --resolve votebot.digitaldemocracyproject.org:80:127.0.0.1 \
  http://votebot.digitaldemocracyproject.org/admin/ | grep -i -E '^HTTP|^location'
```

Expect `<title>VoteBot - Digital Democracy Project</title>` and, for the admin, `307` with a `location:` pointing at the admin's own address (today `https://admin-dev.digitaldemocracyproject.org/admin/`), never a Django page. (When I ran the same checks directly against the live website on the Mac, the page title and the 307 were exactly that.) (The old page has the same title: the next step tells them apart.) Then confirm the page is the new one: the same `curl` piped to `grep -c '/_next/'` prints a number above 0 (the old page has `./dist/ddp-chat.min.js` and no `/_next/`).

**3d. Check the website's setting** (names only, never print values):

```
grep -c '^VOTEBOT_WS_URL=' ~/Developer/repos/ddp-next/.env.local
```

Expect `1`. Nothing else is needed: the page reads it on each request.

## Step 4. December 31st: make DNS fast to change

In Cloudflare, set the record's TTL to the lowest it allows (1 minute) so both the move and the way back take effect within minutes.

## Step 5. January 1st: move the address

1. Change the record for `votebot.digitaldemocracyproject.org` to **A, `52.204.100.187`, DNS only (grey cloud)**, as the dev site's record is (the dev file says its DNS points straight at this host with no CDN). Wait a few minutes (`dig +short votebot.digitaldemocracyproject.org` shows the new address).
2. Get the certificate and switch on HTTPS: `sudo certbot --nginx -d votebot.digitaldemocracyproject.org` (it edits the block and adds the redirect, as for the dev site). Until it finishes, browsers show a certificate warning: expected.
3. **Verify** (from any machine):
   ```
   curl -s 'https://votebot.digitaldemocracyproject.org/?ddp_url=x' | grep -c '/_next/'     # above 0: the new page
   curl -s -D - -o /dev/null https://votebot.digitaldemocracyproject.org/admin/ | grep -i -E '^HTTP|^location'   # 307 to the admin's own address, not a Django page
   ```
4. Open the link on a phone in a normal browser, then once more in the Voatz app (step 1's checks, with the real address). Write the result on VOTEBOT-54.
5. Only now: BROKER-334 may ship (polls with the VoteBot link), VOTEBOT-70 (delete the old widget from the repo) is unblocked.

## The way back (if anything in step 5 fails)

1. In Cloudflare, set the record back to **exactly what you wrote down in step 0** (value, proxy status, TTL). With the 1-minute TTL it takes effect within minutes.
2. Nothing to restart: the old page keeps running on the old server, frozen on the branch `legacy-webflow`. If a stale copy shows, purge Cloudflare's cache for that address.
3. Leave the front-server block in place (nothing points at it) and write what failed on VOTEBOT-54.

## After it is done

Close VOTEBOT-54 with the step 1 and step 5 results. Unblocked by it: BROKER-334, VOTEBOT-70 (then the chat widget epic VOTEBOT-65 can close).

## Evidence for the facts above

The route and its host rule: ddp-next #87 (`src/middleware.ts`, `src/app/voatz-chat/`, `docs/primitives.md`); the dev front-server file and its comments: `ddp-next/deployment/nginx/dev.digitaldemocracyproject.org`; the allowed-origins line: `infrastructure/docker/prod.env.defaults` and `docs/RUNBOOK-civic-host-ddp-next.md` 4.1; the decisions: VOTEBOT-54 comments of 2026-10-29. I ran the route and the host rule in a real browser against a production build on a mapped hostname (23 of 23 checks), and the page's Host check against the live dev site's server (`Host: votebot.digitaldemocracyproject.org` returns the VoteBot page title). I have not run any step of this checklist on the front server, in Cloudflare, or in the Voatz app.
