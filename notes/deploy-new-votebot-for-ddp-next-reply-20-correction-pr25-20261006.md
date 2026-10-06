# Reply 20: correction to reply 19. PR #25 was rewritten; it is NOT the identifier check any more.

The review of PR #25 was right: the page context puts the bill's name and number into the prompt on every message, so an answer that names "HB 7089" proves nothing about retrieval. Ignore the "names the bill's identifier" description in reply 19.

## PR #25 now
- A case still needs at least one answer with a citation that carries the bill's id (only a retrieved chunk can produce one).
- A case that fails **only** that check is run again in a fresh session, up to `--citation-attempts` (default 3). Any other failure ends the case at once. A case that never gets a citation fails with `0 answers cited this bill in each of 3 attempts`.
- It changes only the script and its tests; no server change.

## Do next (after the user merges #25)
Same as reply 19: pull `main`, tag the running image, build, `up -d --no-build votebot`, run the full smoke three times with `--retrieval`. Report which cases needed a second or third attempt (the `attempt N:` lines) and any case that failed all attempts, with the log lines. Pass = every case passing in at least two of the three runs, without exhausting its attempts in those runs.

Gate 3 stays open until then. Gate 5 (nginx) stays held. Still pending from the operator: logrotate file and `sudo findmnt --verify`.
