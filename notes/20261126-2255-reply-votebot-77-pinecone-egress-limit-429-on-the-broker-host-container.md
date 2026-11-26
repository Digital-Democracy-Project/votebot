# Reply (read-only, nothing changed): VOTEBOT-77, chat answers `processing_error`, 11-26 22:55 UTC

**The cause on this server is a Pinecone quota: every retrieval call is refused with HTTP 429, "You've reached your egress limit for the current month (10000000000 bytes). To continue reading data, upgrade your plan."**

**Which server this is:** I read the VoteBot container `votebot-ddp-next` on the **broker host** (the EC2 instance that also runs the broker; its `/opt/votebot` checkout is at `be8937a`, 11-17), with `docker logs`, read-only. I do **not** have the legacy production site's server (the civic host), so I cannot say what that one logs; "not my host" for that one.

## 1. The traceback (this server)
Seven `Error processing message` lines, all the same chain (no secret appears):
```
websocket.py:876 handle_user_message -> agent.py:1175 process_message_stream -> agent.py:1468 _gather_premodel_context
-> retrieval.py:373 retrieve -> retrieval.py:407 _query_together (asyncio.gather) -> tenacity retry
-> vector_store.py:208 query -> self.index.query(...)  [pinecone client: POST /query]
pinecone.errors.exceptions.RateLimitError: [429] Request failed. You've reached your egress limit for the current month
  (10000000000 bytes). To continue reading data, upgrade your plan.
-> tenacity.RetryError: RetryError[<Future ... raised RateLimitError>]   (this becomes the websocket's processing_error)
```
Probe sessions `votebot77-probe` (22:40:06, 22:43:15, 22:43:59) and `votebot77-probe2` (22:45:35) show exactly this, as do three real visitor sessions.

## 2. When it started
First error **22:34:44 UTC** (session `a643fd19-4e4`); last error **22:47:02**. There is **one** `message processed` line in the whole 6-hour window I read, at 22:48:18 (session `5e2aa9cf-a79`); that line is rare in this log, so I do **not** read it as "recovered". I did not see an earlier good chat in the window.

## 3. Health, now (from inside the container, 22:50 UTC)
`GET /votebot/v1/health/ready` answers 200: `{"status":"healthy","version":"2.0.0","environment":"production","dependencies":{"pinecone":"healthy","openai":"healthy","ddp_openstates_replica":"healthy","redis":"healthy"}}`. The health check evidently does not exercise the query path that is being refused (the Pinecone "healthy" is a different, cheap call).

## 4. What changed
* The container has been up **6 days** (started 11-20 14:20 UTC, image `votebot-ddp-next:local`, id `f2f82140d089`); **no restart, deploy or setting change in the six hours before 22:34 on this host** (none by me; its uptime shows none). No 401, `insufficient_quota` or connection-refused line from the language model or Redis; the only refusal is the Pinecone 429 above.
* **A thing to check on the Pinecone usage page, not proven:** the same Pinecone account also serves this host's ddp-sync, which ran heavy knowledge-base embedding passes today (Hawaii alone: 6,130 bills, 23,905 chunks, finished 22:31:48 UTC, three minutes before the first error), plus a stats call every 30 seconds. I cannot see Pinecone's egress meter, so I cannot say whether those reads or the chat's own queries used the 10 GB; whoever owns the plan can.

## What it needs
Not a restart or a deploy. The account's monthly read limit is reached: the plan needs raising or the month to roll over; the chat on both sites should stay down until then if both use this Pinecone account (the dev's note says the two servers fail at the same minute, which fits a shared account; I cannot confirm the legacy server's cause). Nothing was changed, restarted or redeployed.
