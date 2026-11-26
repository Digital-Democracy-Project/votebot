# Update (read-only): VOTEBOT-81, do the go-live embedding passes explain the 23 and 24 Nov burst? They explain the WRITES, not the READS; broker host, 11-26 23:55 UTC

Ramon asked whether flipping states live and building their embeddings could be the cause. Evidence from ddp-sync's logs on the broker host (saved segments plus the live log, 11-18 to now). No query beyond reading logs and code.

## What the embedding passes did (`knowledge_base_embedding_run`, per UTC day: runs / bills / chunks / documents)
11-18 3/37/105/37; 11-19 2/26/100/26; 11-20 2/80/239/86; 11-21 2/8/20/8; 11-22 6/41/222/41; **11-23 5 / 5,429 / 17,437 / 6,351**; **11-24 6 / 1,855 / 17,397 / 3,391**; 11-25 5/64/202/65; **11-26 6 / 6,177 / 24,025 / 10,745**.
The three big passes: **Pennsylvania 11-23 11:59 UTC (5,288 bills, 16,832 chunks)**, **Nebraska 11-24 23:43 UTC (1,847 bills, 17,363 chunks)**, **Hawaii 11-26 22:31 UTC (6,130 bills, 23,905 chunks)**. They line up with the go-live flips in the manifest: Pennsylvania's live status (OPEN-447) was set 11-22 14:25 ET and its first full embedding followed on 11-23; Nebraska and Louisiana went live in the manifest on 11-22 23:20 and their merges landed 11-24 09:12 UTC; Hawaii went live 11-25 and its pass ran when its re-import finished.

## Writes: this fits
Pinecone's export shows about **300,000 write units on each of 23 and 24 Nov**. **The two days' embedding totals are almost the same (17,437 and 17,397 chunks).** A chunk is a 3,072-dimension vector (12 KB) plus metadata, so 17,400 chunks is on the order of 300,000 KB, i.e. about 300,000 write units if a write unit is 1 KB (**my arithmetic and an assumption about the billing unit, not read from Pinecone**). So the writes on those two days are well explained by the Pennsylvania and Nebraska first full embedding passes; Hawaii's pass on 11-26 would add about 400,000 more write units that day.

## Reads: this does NOT fit
The burst you are chasing is **reads** (0.86 GB on 23 Nov, 4.02 GB on 24 Nov). The embedding passes **only upsert, delete and call `describe_index_stats`**: the only Pinecone read calls anywhere in ddp-sync's code are `build_legislator_votes.py` (a manual sync: `index.list` and `index.fetch`, which returns whole vectors; nothing schedules it, and it has no log line in any segment) and the chat `query` (which ddp-sync never calls; zero "Querying Pinecone" lines in any segment since 11-23 07:39 UTC). So go-live embedding builds do not themselves read, and **I found no read path from a go-live flip on this host.** The two possible read links I cannot see: (a) the Mac's LegBot/CAMS runs after those flips (retrieval); (b) extra search traffic to the broker from the newly live states' content (crawlers), which I cannot count (no search logs on 23 and 24 Nov, see my 23:55 reply).

## What would settle it
Pinecone's own export by hour: if the 4.02 GB on 24 Nov sits in a few hours, compare those hours with the Mac's LegBot runs and with the broker's web traffic; if it is spread over the day, a crawler is more likely than a one-off job. I did not total the write units by hour either.
