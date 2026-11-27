# REQUEST (not a GO): deploy VoteBot 4a85cf0 to production (`votebot-ddp-next`): a health check that runs one tiny real query, and a clear "service unavailable" error (VOTEBOT-78, VOTEBOT-79). 2026-11-26 (votebot session)

**Nothing here runs without a GO note (Ramon).**
* **Why:** on 2026-11-26 Pinecone refused every read (monthly read limit, VOTEBOT-77) while `/health/ready` said healthy, and the chat answered every message with the same generic error. These two merges make the health check follow a real read, and make the chat say plainly when a dependency is refusing.
* **Which server:** the VoteBot 3.0 container on the broker host only. **The legacy 2.0 server is not affected** and gets nothing from this: do not touch it.
* **Who:** written by the votebot session; no other VoteBot session is running. Same procedure as your `be8937a` deploy (request `20261120`, your reply `reply-deploy-votebot-be8937a-20261120`).

## What is running, what to deploy
* **Running:** `be8937a31ef0e07afacb9327b8f5fa2e660e4d4a` (your 11-20 deploy, image `f2f82140d089`, also tagged `votebot-ddp-next:be8937a`). Rollback image: `votebot-ddp-next:be8937a`.
* **To deploy:** **`4a85cf062e969f4421b3aef546f8e3140c16e584`** (`main`, carrying both merges).
* **No compose, `prod.env.defaults`, `.env` or dependency change:** `git diff --stat be8937a 4a85cf0 -- infrastructure requirements.txt pyproject.toml` is empty. No new setting.

## What changes (everything between be8937a and 4a85cf0)
* **#103 VOTEBOT-78:** `/votebot/v1/health/ready` now runs **one real, tiny Pinecone query** instead of an index-statistics call: one match, no metadata, no values, a fixed vector (so **no OpenAI call**). The result is kept **60 s when healthy, 10 s when failing**, and checks that arrive together share one query. Cost: at most **one Pinecone read a minute per process while healthy (1,440 a day), at most one every 10 seconds while failing**, a few dozen bytes each. The route also now uses the process's shared vector store (before, it built a new client on every poll). Files: `src/votebot/services/vector_store.py`, `src/votebot/api/routes/health.py`.
* **#104 VOTEBOT-79:** when a message fails because a service VoteBot depends on is refusing (a limit or quota: HTTP 402, 429) or down (5xx, connection error, timeout), the websocket sends the error code **`service_unavailable`** ("VoteBot can't answer right now. Please try again in a little while.") instead of `processing_error`. Anything else is unchanged. Files: `src/votebot/utils/dependency_errors.py` (new), `src/votebot/api/routes/websocket.py`. The matching widget text is in the ddp-next PR (#384, merged; it goes live through that repo's own deploy, not this one); an old widget just shows the new sentence with "Error:" in front.
* **Docs and tests only:** the Pinecone read audit (`docs/PINECONE-EGRESS-AUDIT.md`), README and primitives lines, new tests.

## Please (after the GO)
1. **Preflight** (nothing written), as for `be8937a`:
    * `git cat-file -t 4a85cf062e969f4421b3aef546f8e3140c16e584` prints `commit`;
    * `infrastructure/render-env.sh --check`: the same secret names; names only;
    * the `:be8937a` image tag exists;
    * `free -m` and `df -h /`;
    * no chat in the last 10 minutes.
2. **Deploy** as for `be8937a` (runbook 3.2 to 3.5):
    * check out `4a85cf0`, then `render-env.sh`, `build`, tag `votebot-ddp-next:4a85cf0`, `up -d`;
    * Slack stays off, and no site is repointed;
    * record the UTC times.
    * **Rollback:** retag `:be8937a` as `:local`, then `up -d --no-build votebot`.
3. **Checks** (pass or fail, with the actual output):
    * **A. Health:** `/votebot/v1/health/ready` answers **200** with pinecone, openai, ddp_openstates_replica and redis all healthy; container healthy, restarts 0.
    * **B. A normal chat answers:** `scripts/smoke_ws.py` inside the network with `--retrieval` (expect 6/6), and public (expect 5/5), as before. This is the check that the new health query and the new error path did not break a normal answer.
    * **C. Startup log:** one `Warm-up finished`; no new Traceback.
    * **D. The health check now runs one tiny query (read only, one query):**
      ```
      docker exec votebot-ddp-next python - <<'PY'
      import asyncio, time
      from votebot.services.vector_store import VectorStoreService
      async def main():
          vs = VectorStoreService()
          for label in ("first (a real query)", "second (kept, no query)"):
              t = time.monotonic()
              print(label, await vs.health_check(), round((time.monotonic() - t) * 1000), "ms")
      asyncio.run(main())
      PY
      ```
      Paste the three lines. Expect `True` twice, the first taking a network round trip (tens to hundreds of ms), the second about 0 ms. This builds its own client, so it makes exactly one query and does not touch the server's state; the server's own kept result is separate.
    * **E. (Optional, only if you judge it safe) how a refused read shows as unhealthy.** A real 429 cannot be produced safely, so this makes Pinecone refuse a wrong-sized probe instead (a 400, which the old statistics call would not have noticed). Pinecone returns no data for the refused probe and nothing is written or changed; the index is opened **before** the probe size is changed, so it cannot create anything. Skip it if you have any doubt:
      ```
      docker exec votebot-ddp-next python - <<'PY'
      import asyncio
      from votebot.services.embeddings import EmbeddingService
      from votebot.services.vector_store import VectorStoreService
      async def main():
          vs = VectorStoreService()
          vs.index                      # open the existing index first (a missing index would be created: do not skip this line)
          print("statistics still answer:", vs.index.describe_index_stats().total_vector_count, "vectors")
          EmbeddingService.get_dimension = classmethod(lambda cls: 8)   # a wrong-sized probe
          try:
              await vs.health_check()
              print("UNEXPECTED: healthy")
          except Exception as e:
              print("unhealthy, as expected:", type(e).__name__)
      asyncio.run(main())
      PY
      ```
      Expect "statistics still answer" and then "unhealthy, as expected". Stop and say so if the first line errors (then the index was not found: do not continue).
    * **F. Anything that differed.**
* **Not asked:** provoking the `service_unavailable` frame on production (it needs a real refusal). It is covered by tests that use the real exception classes of Pinecone, OpenAI, httpx and Redis. If a refusal ever happens, the chat should answer "VoteBot can't answer right now. Please try again in a little while." and `/health/ready` should show `pinecone: unhealthy` within about a minute; please say so in a note if you see either.

## Not authorized
* Any code, settings, broker, nginx or Slack change.
* Pointing a site at a different VoteBot.
* Touching the old server or a widget bundle.
* A knowledge-base re-ingest, or any change to the Pinecone account.

## Report back
A `reply-...` note on this branch: the deployed SHA and image id with UTC times; A to F with evidence. VOTEBOT-78 and VOTEBOT-79 stay In Review until then.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
