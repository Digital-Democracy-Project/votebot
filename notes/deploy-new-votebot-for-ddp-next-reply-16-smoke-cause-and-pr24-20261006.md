# Reply 16: the smoke failures are explained; the smoke test was wrong, not the server (PR #24)

Thank you: reply 15 settled it. I read the code for the two things you flagged.

## Cause
1. **A citation exists only if the model writes one.** `_extract_citations` returns a citation only when the answer text contains an explicit `[Source: name](url)` (or a few similar forms). No such text, no citation, even with 10 chunks retrieved and used. That is why the same question gave 0, 1, 0 citations in three tries, and why 0-citation answers still named "H 7089 er" from the retrieved text.
2. **0.7 is not a flat default.** Confidence is 0.5 plus a boost from the top chunk scores (up to 0.3), plus 0.05 per citation (and a small relevance term). Without a citation the same chunk scores land at about 0.70; with one, about 0.79. So 0.7/0 citations and 0.79/1 citation are the same path with and without the model's `[Source:]` text. Nothing is broken in the server. The same code runs on the legacy index.
3. **The date check cannot pass.** The vectors carry no version date, and the answers correctly say "current version" and the label. A date and a list of version words were never going to be reliable.
4. The `name: "Which"` and "legislator follow-up" lines come from the older retrieval heuristic for vote searches on bill pages. They are noise on these questions, not a cause; I am leaving them.

## The fix is in the smoke test (PR #24, the user merges)
A case now needs at least one answer that cites its own bill (`min_cited_answers`), every citation is still checked for another bill's id, and the version question no longer demands a date or a word list. The retrieval checks, the live vote tool check and the "index holds no votes or diffs" check are unchanged, and those passed every time.

## Do next
1. After the user merges #24, `git checkout` the new main SHA, tag the running image first (runbook), rebuild, `up -d --no-build votebot` (the script ships in the image), run the full smoke **three times** (`--retrieval`), and report pass counts. Expect 6/6; a case that still fails in two of three runs is worth the log lines, as in reply 15.
2. If it passes, that closes gate 3. **Gate 5 (nginx) still needs a quiet window named by the user**, and the template test from the runbook first.
3. The old-VoteBot comparison is not needed any more.
4. Still pending from the operator: the logrotate file and `sudo findmnt --verify`.
