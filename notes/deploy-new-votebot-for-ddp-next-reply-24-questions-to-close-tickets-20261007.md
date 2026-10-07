# Reply 24: thank you for gates 3 and 5; read-only questions that would let us close VOTEBOT-14 and VOTEBOT-15 (dev agent, on Ramon's instruction, 2026-10-06 evening)

Replies 22 and 23 were exactly what was needed: 6/6 three times, nginx recreated cleanly, public wss smoke 6/6. Nothing here asks for a change. Everything is read-only unless it says otherwise; names only for anything secret.

## For VOTEBOT-14 (so it can close)
1. **`DDP_BROKER_API_ROOT`.** Is it set in `/opt/votebot/.env`, and to what URL (a URL is fine; no keys)? Then one call from inside the container and one through nginx, both with a real bill from the smoke discovery (any of the five): `GET /votebot/v1/content/resolve?url=<the ddp-next /explore URL for that bill, for example https://dev.digitaldemocracyproject.org/explore/FL/2024/HB%207089>` (the exact query parameter name is in `src/votebot/api/routes/content.py`). Expected: 200 with the bill's `ocd_bill_id` (not 503, which means the root is unset). Paste the status and the first 200 characters.
2. **`legacy-webflow`.** Do the branch and tag `legacy-webflow` exist in `/opt/votebot` (the votebot checkout) and in `/opt/ddp-sync`? If not, do not create them yet; just tell me which are missing and what the checkouts' current HEADs are.
3. **Operator items.** Status of the logrotate file for `/opt/votebot-logs/queries/*.jsonl` and `sudo findmnt --verify`. If still pending, how big are the query logs now (`du -sh`)?
4. **The render-env branch** (`feat/VOTEBOT-14-render-env-from-secrets-manager`): has the script been used again since the first run (for example after the rebuilds), or was the `.env` left as first written?

## For VOTEBOT-15 (so it can close): two live runs, through the container, against the real api-v3
Use the same style as `smoke_ws.py` (a short script or the existing `ask()` helper); keep the answers to 300 characters and paste the container log lines for each (no keys).
5. **A legislator question.** On a general page (no bill context), ask: "Who is Nancy Pelosi and which district does she represent?" and, separately, a typo form: "Who is Nancy Pelsoi?". Expect the live api-v3 `/people` read to run (look for the legislator lookup log line), the answer to name her district, and no `UNAVAILABLE` text. Then the same on a legislator page context if you can build one cheaply (`type: legislator`, `id` an `ocd-person/...` id taken from api-v3 for Pelosi).
6. **A "what changed" question.** Pick a bill from the smoke discovery that has two versions (for example FL HB 7089 if api-v3 shows more than one version with a stage; if not, any of the five that does), page context as in the smoke cases, and ask: "What changed between the first and the latest version of this bill?" Expect the live diff read (api-v3 `diff_from_previous_version`) to run, the answer to describe real changes (not "no stored comparison"), and no `bill-version-diff` search. Tell me which versions were compared and the diff size in characters from the log if it is logged.
7. **`page_context.id`.** In the smoke runs the page context carries the bill id as the identifier (for example "HB 7089"). Does the live bill-organization positions lookup (broker, VOTEBOT-15 part 2) run on a bill page? Ask "Which organizations support or oppose this bill?" on one bill and tell me whether the log shows the broker call and whether it returned positions or "none".

## Small things
8. Any warning or error line in the VoteBot container log since 00:06 UTC (a count and the first of each kind is enough).
9. Reply 23 says the broker `web` container still shows `unhealthy` because of the known `DisallowedHost` healthcheck. Is there an open ticket for that? If not, give me the one-line healthcheck command and the Host it sends, and I will file BROKER-xxx.

Reply on this branch either way. Nothing here blocks the cutover plan; these are the evidence needed to close tickets.
