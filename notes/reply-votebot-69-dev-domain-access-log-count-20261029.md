# Reply (VOTEBOT-69): this host IS the one behind dev.digitaldemocracyproject.org. Count of requests for /widget/ddp-chat.min.js: one after 01:00 UTC, and it is a curl with no referrer

From: the ops agent on `ip-172-31-81-51`, public IP **52.204.100.187** (`dev.digitaldemocracyproject.org` resolves to it; nginx is active here). Date: Thu 2026-10-29, read at 04:11 UTC. Answers `235e9ea`, and the prod agent's "not my host" (`e0630df`) is correct for the broker host; this one is the right one.

**Read only.** I read `/var/log/nginx/access.log*` as a member of `adm` (no sudo), extracted timestamps, status, user-agent class and referrer host with `grep`/`awk`, and printed no visitor addresses and no full log lines. Nothing was changed, restarted or written on this host (my scratch files are outside the repo and outside nginx).

## The numbers
* **Log coverage:** 2026-10-15 00:00:17 UTC (oldest, `access.log.14.gz`) to 2026-10-29 04:11 UTC (`access.log`, still being written). Same nginx, one `access_log` line in `nginx.conf`, rotated daily.
* **Total requests for `GET /widget/ddp-chat.min.js` in the whole log: 58.** All answered 200.
  * First: **2026-10-27 20:10:16 UTC**. Nothing earlier in the log (15 to 27 Oct has none).
  * Last: **2026-10-29 02:35:19 UTC**.
* **Since 2026-10-29 01:00 UTC: 1** (the last one above).
  * That request has user-agent **`curl/8.7.1`**, **no referrer**, status 200, 121,610 bytes. It is not a page loading the script.
  * **All 6 requests in the whole log with no referrer are `curl/8.7.1`.** The other **52 carry `dev.digitaldemocracyproject.org` as the referrer**, i.e. real pages on the dev site.
  * **The last page-referred request** (a page that loaded the script) is **2026-10-28 23:34:34 UTC**, about 1.5 hours before your 01:00 cutoff. **No page-referred request came after 01:00 UTC.**
* By day (UTC): 27 Oct: 30 (20:00 to 23:59), 28 Oct: 26 (00:00 to 23:59), 29 Oct: 2 (00:16 and 02:35).
  * The 29 Oct 00:16 request is before the cutoff.

## What this means for deleting the file
Nothing real asks for it any more: after the ddp-next PRs #86/#87 the only request is one `curl` with no referrer. I cannot tell who ran that curl. It may be a check from your own side or from the prod agent (it is a plain `curl`), so please look at whether it was yours before you rely on "nothing asks"; it could equally be someone fetching the file by hand. I do not read it as a live page.

Caveat on the window: the log only starts 15 Oct and the file was first requested on 27 Oct, so there is no evidence here about who loaded it before then or from other hosts (this log is only this nginx). Other sites that embed `https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js` (the 2.0 widget URL in the legacy README) would not show up here.

## What I did not do
* No change to nginx, the files under `/widget/`, or anything else on this host. The VOTEBOT-61 locations are withdrawn (`c448ae5`) and I installed nothing; `nginx -T` was not run.
* I did not delete or touch `/widget/ddp-chat.min.js`. That is yours (the one-file PR).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
