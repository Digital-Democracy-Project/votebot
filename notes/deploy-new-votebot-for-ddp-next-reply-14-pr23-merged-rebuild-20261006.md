# Reply 14: PR #23 is merged. Rebuild and run the diagnosis from reply 13.

- votebot `main` is now **493f362** (PR #23, the bill-from-title fix).
- Do now, per the runbook (3.2 and the rollback tag): tag the running image `votebot-ddp-next:ba86a14` (already done at build; confirm), check out `493f362`, build, `up -d --no-build votebot`, record the new image ID. The `Error resolving bill from title` warning should be gone from the log.
- Then the steps in reply 13 (3): the FL case alone, three runs, with the container log lines for each. Also run the full smoke once. Report the results and the log lines.
- Everything else in reply 13 still applies. Gate 5 (nginx) stays held until the smoke passes and the user names a window.
