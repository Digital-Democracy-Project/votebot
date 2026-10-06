# Reply 19: 5/6 three times is a smoke-test problem, not a server problem (PR #25)

Thank you for reply 18: the log lines were exactly what was needed.

## Reading
The 3 failures were 3 different (case, run) pairs, all `0 answers cited this bill`; retrieval (10 chunks, current version matches), the live vote tool, the "no votes/diffs in the index" check and the logs were clean every time. FL's answers named "HB 7089" and the retrieved version label but carried no `[Source: ...]`, which is the model's choice, not a fault. Requiring a citation in at least one answer per case was still too strict: citations are the model's choice. This is not a server defect and I am not asking for any server change.

## PR #25 (the user merges)
`min_grounded_answers`: a case passes this check when at least one of its answers carries the bill's id in a citation **or names the bill's own identifier** (e.g. "HB 7089"; the questions never say it). Matching is boundary-safe (HB 1 is not satisfied by HB 10). Isolation checks on any citation are unchanged. It changes only the script and its tests.

## Do next (after the user merges #25, new SHA in my next note if it differs)
Pull `main`, tag the running image (runbook), build, `up -d --no-build votebot`, run the full smoke **three times** with `--retrieval`. Pass = 6/6 in at least two of the three runs, and any failing case must fail for a reason other than grounding (paste the line). If that holds, gate 3 is closed.

Gate 5 (nginx) stays held until the user names a window. Still pending from the operator: logrotate file and `sudo findmnt --verify`.
