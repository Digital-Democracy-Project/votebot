# Request: copy the new chat widget bundle to the OLD server's static directory, so Webflow bill pages show "America's" and not "America&#39;s" (VOTEBOT-1; dev agent, 2026-10-11)

**Who this is for:** whoever has shell access to the **old server** (the civic/ddp-api host that serves `votebot.digitaldemocracyproject.org`, running the frozen `legacy-webflow` code) and, for step 4, the Cloudflare zone. Earlier request to the same address, still unanswered: `old-server-legacy-webflow-label-request-20261007.md`. **This request changes one static file; it does not touch the old VoteBot code, its branch, its `.env` or any service.**

## Why
The Webflow bill pages load the chat widget from `https://votebot.digitaldemocracyproject.org/dist/ddp-chat.min.js`. Their embed ("VoteBot page context for bills") puts the bill title into `window.DDPChatConfig.pageContext` from a CMS merge field inside a `<script>`; Webflow HTML-escapes it, so visitors see `Making America&#39;s Food Safer Act (S 4974)` in the welcome message and in the Status & votes question. The fix is in the widget (votebot PRs #55 and #56, merged; `main` is `462eb96b837923af4e528c58955e61105d8fb883`): it decodes the title, and its link renderer no longer makes a link of a `javascript:` URL. Nothing merged to `main` reaches this server by itself, so the bundle has to be copied.

## Facts read just now (read-only `curl`, 2026-10-11 ~22:30 UTC)
| | served today | the new bundle (`main`, `chat-widget/dist/ddp-chat.min.js`) |
|---|---|---|
| sha256 | `20e4d7a45122e137bddc2ee5a9828d6adf9bb8c068171730f61234ff29952504` | `28b8b187035b6d321c4a88d1dbb1a8962c394c7fe9858953fc253ffa078fcc4f` |
| size | 113921 bytes | 119079 bytes |
| built | 2026-06-10 (banner line) | 2026-10-11 |
| has `decodeEntities` / `safeHref` | 0 / 0 | 1 / 1 |

Response headers today: `server: cloudflare`, `cf-cache-status: HIT`, `age: 128825`, **`cache-control: public, max-age=604800, immutable`** (7 days, immutable), `last-modified: Wed, 10 Jun 2026 22:14:31 GMT`. **The new file is a bigger jump than this fix:** it also carries the widget changes since June (the ddp-next page-context fields, quick-action buttons, session persistence). The widget tests cover the old Webflow page-context shape and it is what the runbook requires of any change to this file, but it is a different file, so step 5 matters.

## Do
1. **Find where the file is served from** (do not guess; the repo's nginx sample says `/var/www/votebot/`, the URL path is `/dist/`): `sudo nginx -T 2>/dev/null | grep -n -B2 -A12 'votebot.digitaldemocracyproject.org'` and note the `root`/`alias` and the real path of `ddp-chat.min.js` (`ls -l`). Report that path: the widget README says it is undocumented.
2. **Get the new bundle without touching the checkout's branch** (the old VoteBot checkout stays on `legacy-webflow`): from a scratch directory, `git clone --depth 1 --branch main git@github.com:Digital-Democracy-Project/votebot.git /tmp/votebot-main` (or `git -C <checkout> fetch origin main && git -C <checkout> show origin/main:chat-widget/dist/ddp-chat.min.js > /tmp/ddp-chat.new.js`). Then `sha256sum /tmp/votebot-main/chat-widget/dist/ddp-chat.min.js` and **confirm it is `28b8b187...fcc4f`** from the table. If it is not, stop and report.
3. **Back up, then copy:** `sudo cp -a <served path> <served path>.bak-$(date +%Y%m%d-%H%M)`; `sudo install -m 644 -o <the file's current owner> /tmp/votebot-main/chat-widget/dist/ddp-chat.min.js <served path>`; record the backup name. (Rollback = copy the backup back and purge again.)
4. **Purge Cloudflare for that one URL** (`https://votebot.digitaldemocracyproject.org/dist/ddp-chat.min.js`), whoever holds the zone: single-file purge, not purge-everything.
5. **Verify from outside:** `curl -s https://votebot.digitaldemocracyproject.org/dist/ddp-chat.min.js | sha256sum` (expect `28b8b187...fcc4f`; if Cloudflare still answers with the old file, repeat the purge and say so), `curl -sI` the same URL and report `cf-cache-status`, `age` and `cache-control`.

## Decision for the operator and Ramon (not for this agent to make): the 7-day `immutable` cache
Even after the copy and the purge, **a returning visitor's browser may keep the old file for up to 7 days** (`max-age=604800, immutable`: it will not even revalidate). New visitors and anyone whose cache has expired get the new one. Two ways to avoid the wait, both outside this repo: (a) in the Webflow Designer add a version to the script tag on the bill template (`.../dist/ddp-chat.min.js?v=20261011`), which needs Designer access; (b) on this server drop `immutable` and shorten `max-age` for this one file in nginx (for example `Cache-Control: public, max-age=300`), which also stops the next fix from waiting a week. I recommend (b), and (a) once if the fix is wanted sooner. If neither is done, say so and the fix is simply fully live in about a week.

## Report back (on this branch, a new file)
The served path (step 1); the sha256 of what you copied (step 2); the backup file name (step 3); who purged and when (step 4); the three outputs of step 5 including the headers. **I will then open a Webflow bill page (S 4974) in a browser and confirm the welcome message reads "Making America's Food Safer Act (S 4974)", and compare the served hash myself; VOTEBOT-1 stays open until I have.** Do not change anything else on the old server; if something looks wrong, restore the backup and tell me what you saw.
