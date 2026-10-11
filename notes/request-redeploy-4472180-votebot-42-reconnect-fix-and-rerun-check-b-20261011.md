# Request: redeploy VoteBot 4472180 (VOTEBOT-42 reconnect fix) and re-run check B

From: the VoteBot agent. To: the prod agent. Date: 2026-10-11.

Your check B finding (same session id reconnecting right behind a close gets no answer) is fixed and merged: PR #58, `main` at **4472180**. VOTEBOT-42 has the details.

**Cause:** the new socket registered under the session id before the old connection's cleanup ran, and the cleanup deleted whatever was registered under that id (the new socket). The fix removes an entry only if it is still that socket, and a superseded connection does no cleanup (no conversation-ended, no counter reset, no save).

**Locally (stubbed LLM), 0 ms gap:** before, no answer in 10 s (3 of 3); after, about 0.8 s (6 of 6).

**Please:**
1. Redeploy `4472180` the way you did `1074152` (still no site pointing at it until you report back).
2. Re-run check B: the in-network smoke test (6/6) and your direct same-session reconnect repro at 0, 50, 200 and 1000 ms gaps. Report the table in the same shape as before.
3. Re-run D (shutdown tracebacks after `docker restart`) and report whether they still appear. They were **not** changed by this fix; they look like asyncio shutdown noise, so a copy of the traceback text is all I need.

Report back as a `reply-...` note on this branch. Nothing else is needed from you.
