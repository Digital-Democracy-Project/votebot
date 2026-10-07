# Reply 31: PR #28 is merged. Go: deploy it and run the checks in reply 30.

PR #28 (VOTEBOT-21: fallback citations only from the page's own chunks) is merged to `main` (squash). I reviewed it: the unit suite passes, and removing the new restriction makes 6 of the new tests fail.

1. Do steps 1 to 3 of reply 30 exactly as written (pull main and record the SHA, tag the running image, build, `up -d --no-build votebot`, health, then the live checks and the counts).
2. One correction to an expectation in reply 30: on the FL bill page, "which organizations support or oppose this bill" may still carry ONE citation if it is the bill's own text chunk (the answer's words can overlap it). That is acceptable. Any citation to an organization or legislator chunk is not, and means the fix failed: report its `document_id` and `source`, and leave the setting on.
3. Also check, if you can: a cached button answer (the preset question buttons) still returns its stored citations.
4. If every case passes, run the smoke test once with `--citation-attempts 1` and report. Then do the rollback rehearsal from reply 27 on this image.

Report back here as reply 32. Names only, no secret values.
