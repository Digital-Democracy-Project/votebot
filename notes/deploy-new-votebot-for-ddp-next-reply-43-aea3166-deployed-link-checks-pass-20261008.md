# Reply 43: aea3166 deployed, link checks pass (answers reply 42)

Deployed `main` aea316614195f89ffe3a3c6d68776397ba5ff8c1 on the broker host, 2026-10-08 about 01:08 UTC.
Image `cda6366f34fc` (tag `aea3166`). Healthy in 34 s, 0 restarts, 0 warnings at start.
Startup line: `ddp-knowledge-base`, `ocd_bill_id`, `quick_action_buttons_enabled true`. `/votebot/v1/health/ready` 200; public `/votebot/v1/features` returns true.
`render-env.sh --check` OK (no re-render). Rollback tag kept: `votebot-ddp-next:258ace1` = `4e55de7258c0`.
Note: the 12 warning-level log lines since the deploy are all 404s from my own guesses at the health path (`/health/ready` and similar); the real path is `/votebot/v1/health/ready`.

## A. Links on FL 2026E HB 5601E (context from `/content/resolve`)
Every `http` link in the text and in the citations was listed for 10 answers.
- Organizations question x5: 3 of 5 had a bill link, all exactly `https://dev.digitaldemocracyproject.org/explore/FL/2026E/HB%205601E`. 2 of 5 had none (answers 1 and 2).
- "Tell me about this bill" x5: 5 of 5 had a bill link, all exactly that same URL.
- So 8 of 10 answers had a bill link and all 8 were ours. 0 `digitaldemocracyproject.org/bills/...`, 0 `/legislators/...`, 0 `openstates.org`, 0 `webflow.io`.
- Other links, all outside our site: the news article `wusf.org/text/education/2026-02-19/usf-faculty-oppose-sarasota-manatee-campus-handover-new-college-limayem-pledges-transparency` and the two organization sites `www.protectringling.org` and `www.saveusfsm.org` (organization answers, from the organization rows).
- One more link worth your eye: `https://digitaldemocracyproject.org/vote` appeared in 3 of the 10 answers (organizations #1 and #5, "tell me about" #2), and once more on the organization page in B (4 of 15 answers in all). It is not a bill or legislator link, but it is the old-site host. I do not know where the model got it.

## B. Other pages
- FL HB 7089 "Tell me about this bill" x2 and "How did the vote on this bill go?" x2: all 4 linked exactly `https://dev.digitaldemocracyproject.org/explore/FL/2024/HB%207089`. The vote asks used the vote tool.
- Organization 5155 "What bills does this organization support or oppose?" x1: links were `https://dev.digitaldemocracyproject.org/explore/organizations/5155` and a bill link `https://dev.digitaldemocracyproject.org/explore/FL/2026E/HB5601E` (no space, no `%20`; it is ours but not in the form A expects, and probably comes from the organization's own data). `https://digitaldemocracyproject.org/vote` appeared here too. I built this page's context by hand (type organization, id 5155), because `/content/resolve` cannot resolve an organization yet (it answers 500 "Webflow collection not configured for organization").
- "Who is Nancy Pelosi?" on a general page: 0 links; 11th district named; no made-up legislator or bill link.

## C. Stored chunk sample (read-only; counts and ids only, no content)
- bill-text: 24 chunks across FL, MI, US, UT, VA and WA (NC returned none in my sample): **0** contain `digitaldemocracyproject.org/bills/`, `/legislators/` or `webflow.io`.
- organization (with content): 21 chunks, ids 10688, 12148 and about 18 others: **0** contain them.
- I also looked at the chunk metadata: 0 matches. Organization chunks carry `ddp_url` in the form `/organizations/<id>`, a path with no host. Bill-text chunks carry no `ddp_url` at all.

## D. Regression
- Smoke `--citation-attempts 1`: 6/6 passed.
- Status & votes button with the title (HB 7089): vote tool used, confidence 0.78, 0 `openstates.org`, 0 `Bill not found`.
- Container log: 0 real warnings or errors since the deploy. `Vote verification returned empty` did not appear at all in this run, including on the Status & votes button whose text contains "What is the latest...". I have not looked into why it did not fire this time.

I changed no code or settings.
