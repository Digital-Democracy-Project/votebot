# Interim 11-27 02:05 UTC: VoteBot 4a85cf0 deploy request (VOTEBOT-78 and 79) read; waits for Ramon's go

* Read `d188887`. Nothing started. It waits for Ramon's go in this session naming `4a85cf062e969f4421b3aef546f8e3140c16e584`.
* Preflight facts I can already read without changing anything: `votebot-ddp-next` runs image `f2f82140d089` (the `be8937a` build, started 11-20 14:20 UTC); the rollback tag `votebot-ddp-next:be8937a` exists on this host; `4a85cf0` is a commit in the checkout's fetched history. I will run the full preflight (secret names, memory and disk, no chat in the last 10 minutes) when he gives the go. Only this host's 3.0 container is touched; the legacy 2.0 server is not mine and is not involved.
* Note for check D/E: Pinecone's read limit was raised at about 22:47 UTC on 11-26 (VOTEBOT-77), so a real query will succeed now.
