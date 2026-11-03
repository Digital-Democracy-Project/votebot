# Request (read-only): does production's knowledge base hold the hand-typed list of live states? (VOTEBOT-74), 2026-11-03

**Say "not my host" and stop if you cannot read VoteBot's production knowledge base (the Pinecone index `ddp-knowledge-base`) from where you work.** **Read-only: change, delete and re-ingest nothing.** No keys in the reply.

## Why
VOTEBOT-73 (votebot #97, merged 2026-11-03) made VoteBot's system prompt read the live states from the broker. Two documents in the repo's `RAG training docs/` folder still carry a typed list ("DDP is actively running statewide legislative voting for: Florida, Virginia, Washington, Utah, Arizona, Michigan, Massachusetts"): `VoteBot Agent Instructions.txt` and `Digital Democracy Project overview 24 Jan 2026.txt`. VoteBot never loads that folder, but ddp-sync can ingest it by hand (its training handler, one file per call; no schedule found). **We do not know whether anyone ever ingested them.** If they are in the index, VoteBot can retrieve the stale list and quote it. votebot #98 removes the list from the files; it changes nothing in the index.

## What I need (each with its value)
1. **Search the index read-only with VoteBot's own retrieval code or client** (inside the VoteBot container if that is where its settings live; never print a key or a token): query for each of these texts and take the top 20 matches for each:
   - `DDP is actively running statewide legislative voting for: Florida Virginia Washington Utah Arizona Michigan Massachusetts`
   - `Federal Legislation All U.S. voters nationwide may participate in voting on Congressional bills State Legislation DDP is actively running statewide legislative voting`
   - `Expansion Roadmap DDP plans to support all 50 state legislatures by the 2027 legislative session`
2. **Report, per hit that contains the phrase `actively running statewide legislative voting for` or the seven state names in a list:** the match score, the vector id, the metadata field NAMES and their values for source type, source name or file, and any ingested-at or updated date (not the whole chunk: the first 300 characters of the text are enough), and which namespace it is in. **Also say how many of the 60 results were such hits, and "none" if there are none.**
3. **Context, read-only:** the index's namespaces and total vector count (the index's own stats call), and whether any vector's source metadata says `training` or `training_docs`.
4. **Reply** with the time (UTC), the host and the client you used, and the values above. If any query fails or you cannot tell hits from other text, say so and stop.

## Stop and tell me
If a query would write, change or delete anything; if you cannot read the index from your host; or if the hits include text you think should not be shared. Do not delete or re-ingest anything, even if you find stale chunks: that is a separate request with Ramon's go (VOTEBOT-74 step 3).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
