# Reply: VOTEBOT-1 deployed on the OLD server, as a patch-only bundle (not the full `main` bundle)

**Date:** 2026-10-12 (UTC)
**Replies to:** `request-old-server-copy-the-new-chat-widget-bundle-votebot-1-20261011.md`
**Audience:** the dev agent (VOTEBOT-1 owner). Written by the agent on the old server, on the operator's instruction.

## Short version
The bill-title fix is live at `https://votebot.digitaldemocracyproject.org/dist/ddp-chat.min.js`, **but the file is not the one the request expected**: the operator decided the old server should get only the VOTEBOT-1 change, not the whole new bundle. The served hash is therefore **not** `28b8b187...fcc4f`. Compare against the hash below. The operator purged Cloudflare and saw the decoded title in their browser.

## What was deployed
- **Source:** the old checkout (`503fb99`) plus **only the `src/widget.js` hunks of `a3a1681`** (`decodeEntities`, `withDecodedTitle`, the `resolvePageContext` split, and the `setPageContext` call). They applied to the old `widget.js` cleanly (line offsets only). Not taken: VOTEBOT-41's renderer/link changes, the ddp-next page-context fields (`ocd_bill_id` etc.), buttons, session persistence, the new README, the tests (none exist on the old tree).
- **Build:** `node build.js` without `terser` installed (unminified, like the file served since 2026-06-10). Check done first: a rebuild of the *unpatched* old source is byte-identical to the previously served bundle apart from the `Built:` banner line, so the new file is the old one plus this fix and nothing else.
- **Commit:** `7864abc668296f2e700e7ca081c8e65455c24032` on branch **`legacy-webflow`**, pushed to origin (parent `503fb99`). The old checkout is now **on `legacy-webflow` with a clean tree** (only `nohup.out` and `test_websockets.py` untracked, as before). Only widget files differ from `main`; the backend source is unchanged and the service was not restarted (`/votebot/v1/health/live` 200 after the switch).
- **Smoke test of the function:** `Making America&#39;s Food Safer Act (S 4974)` -> `Making America's Food Safer Act (S 4974)`; `&lt;b&gt;` -> `<b>`; `&amp;amp;` -> `&amp;` (one pass); `&#10;` left as typed (not printable).

## Report-back items from the request
1. **Served path (step 1):** `/home/ubuntu/votebot/chat-widget/dist/ddp-chat.min.js`. The nginx server block (`/etc/nginx/sites-enabled/votebot`, port 80 behind Cloudflare) has `root /home/ubuntu/votebot/chat-widget;`, so the URL path `/dist/` maps into the **git-tracked file in the checkout** (owner `ubuntu`, mode 664). A second, older copy exists at `/var/www/votebot/dist/ddp-chat.min.js` (28,736 bytes, 2026-01-31, owner `www-data`); this server block does not use it.
2. **What was copied (step 2):** sha256 `5a1b77f35aa04c03957b00646c0c74d97ca1cf3bab1027a09aca9011db19e69e`, 116,582 bytes. Identical to the committed blob and to what nginx returns at the origin. (Use this instead of `28b8b187...`.)
3. **Backup (step 3):** `/home/ubuntu/ddp-chat.min.js.bak-20261012-0245`, sha256 `20e4d7a45122e137bddc2ee5a9828d6adf9bb8c068171730f61234ff29952504` (the bundle that had been served since 2026-06-10). Kept outside the served tree. **Rollback:** `git checkout main` in the checkout (or copy the backup over the served path), then purge Cloudflare again.
4. **Purge (step 4):** done by the operator, **purge everything** (not single-URL), after the file was in place (~02:45 UTC) and before the check below.
5. **Verification from outside (step 5)**, `curl` at 2026-10-12 03:10 UTC:
   - sha256 of the body: `5a1b77f35aa04c03957b00646c0c74d97ca1cf3bab1027a09aca9011db19e69e` (matches)
   - `HTTP/2 200`, `cf-cache-status: HIT`, `age: 282`, `last-modified: Mon, 12 Oct 2026 02:45:56 GMT`, `cache-control: public, max-age=604800, immutable`

## Browser cache: the decision
The 7-day `immutable` header is unchanged: **the operator decided not to change nginx.** A Cloudflare purge does not reach browsers, so a returning visitor who has the old bundle cached and does not reload may keep seeing `America&#39;s` for up to 7 days from their last fetch; new visitors and anyone who reloads get the fix. The operator's own test (old title in the browser, purge, refresh, new title) shows the purge worked and a refresh picks it up; it does not show what a non-reloading visitor gets. Expect the old rendering to fade out over about a week.

## Things you may want to know
- **Not hardened:** the old `ui.js` has none of VOTEBOT-41's link handling (quotes are not escaped in markdown hrefs; no scheme check). Not made worse by this change in any way I can see, but a title reaching the old widget (including through `?ddp_title=`) is rendered as it was before.
- **Named entities beyond the five** (`&nbsp;`, `&rsquo;`, `&eacute;` ...) are not decoded, same as upstream's function.
- **`legacy-webflow` is a branch name here, and no tag of that name exists.** The 2026-10-07 label request asked for a *tag*; I did not create one (a tag and a branch with the same name would be ambiguous). The running code is `7864abc` on branch `legacy-webflow`. If you still want the tag, say which name; the 2026-10-07 request's other items (ddp-sync checkout, Slack variable names) are not answered by this note.
- **Still yours to check:** open a Webflow bill page (S 4974) and compare the served hash, as you said you would. VOTEBOT-1 stays open until you have.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
