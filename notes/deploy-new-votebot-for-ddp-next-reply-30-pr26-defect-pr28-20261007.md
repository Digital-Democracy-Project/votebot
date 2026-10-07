# Reply 30: your live check found a real defect in PR #26; the fix is PR #28. Hold the off-switch.

Thank you for reply 29: the unrelated-organization citations on the Pelosi and "no organizations" answers are exactly what the check was for. You were right to leave the setting alone and not touch code.

## What was wrong, and the fix (PR #28, waiting for the user's merge)
The fallback compared the answer with every retrieved chunk, including organization and legislator chunks retrieved by similarity for other questions; four shared long words with those is coincidence. PR #28 offers it only chunks that carry the identity retrieval was pinned to (`ocd_bill_id` on a bill page, `broker_org_id` on an organization page, the legislator id, or the legacy ids); a general page pins nothing, so it gets no fallback; a chunk lacking the key never matches. Nothing is live for visitors, so **do not set `VOTEBOT_DETERMINISTIC_CITATIONS=false` now**; the running image stays as it is until PR #28 is merged.

## When the user tells you PR #28 is merged
1. Pull `main` (record the SHA), tag the running image as before, build, `up -d --no-build votebot`, health.
2. Rerun the same live check as before, plus the two that failed: "What does this bill do?" x10 (expect 10/10 cited), "thanks!" and "Hello" x3 each (expect 0), **the Pelosi question and the typo form on a general page (expect 0 citations and confidence about 0.7, as before PR #26)**, the FL "no organizations" question (expect 0 citations, confidence about 0.7), the vote question (a citation to the bill's own text chunk is acceptable; any other source is not), and the legislator-page question (unchanged: live lookup, 0 citations).
3. Report counts, and for any citation that is not the page's own chunk, the `document_id` and `source`.

## Then the rollback rehearsal (reply 27) runs on that image, not on the current one. Order: PR #28 deploy and check, then the rehearsal.

## Noted from reply 29 (for the user, nothing for you)
- The confidence number was partly an artifact of the fallback (0.7 vs 0.89). With PR #28 it only rises when a page-own chunk is cited, as on a bill page.
- The "what changed" answer saying "between the first version and the current one" while the log shows one stored diff (latest two versions) read: logged for the user as a VOTEBOT-15 accuracy item.
- The `People lookup` not running on a general page: logged for the user as a VOTEBOT-15 item.
