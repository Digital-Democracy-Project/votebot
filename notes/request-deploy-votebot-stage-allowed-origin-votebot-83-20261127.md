# REQUEST (not a GO): deploy VoteBot with the stage site's address on its allowed list (`votebot-ddp-next`, VOTEBOT-83). 2026-11-27 (votebot session)

**Nothing here runs without a GO note (Ramon), and not before votebot PR #105 is merged.** Ramon merges; the full 40-character SHA of the `main` commit that carries it comes with the GO (this note will be amended with it).
* **Why:** Ramon wants the VoteBot chat on the stage site. Even after the stage site's own build turns the chat on (VOTEBOT-84, a separate request), the browser's calls to VoteBot from the stage site (`/votebot/v1/features` and `/votebot/v1/content/resolve`) are blocked unless VoteBot answers them with `access-control-allow-origin`, and VoteBot's allowed list names only the dev site today. Checked read-only on 11-27: a `GET /votebot/v1/features` with the stage site's origin returned 200 **without** that header; the dev site's origin got it. The WebSocket already connects from the stage origin (it has no origin check), so only the HTTP calls need this.
* **Which server:** the VoteBot 3.0 container on the broker host only. **The legacy 2.0 server is not affected**: do not touch it.
* **Safe to ship first:** an extra allowed address is harmless. The stage chat itself is not switched on by this (that is VOTEBOT-84, a request on the website's side).

## What is running, what to deploy
* **Running:** `4a85cf062e969f4421b3aef546f8e3140c16e584` (your 11-27 deploy, image `4016cfc000f1`, tagged `votebot-ddp-next:4a85cf0`). Rollback image: `votebot-ddp-next:4a85cf0`.
* **To deploy:** the `main` commit carrying PR #105 (full SHA with the GO). **Preflight must show the diff is only this:** `git diff --stat 4a85cf0 <that SHA>` lists `infrastructure/docker/prod.env.defaults`, three docs (`README.md`, `docs/CHECKLIST-voatz-page-switchover.md`, `docs/RUNBOOK-civic-host-ddp-next.md`) and one new test file. If it lists anything else, stop and say so.
* **No code, dependency, compose or secret change.** The only behaviour change is one line, `ALLOWED_ORIGINS` in `infrastructure/docker/prod.env.defaults`: the JSON list goes from the dev site's address alone to the dev site's address plus the stage site's address (two bare `https://` origins, nothing else).

## What it changes
* `render-env.sh` merges `prod.env.defaults` into `/opt/votebot/.env`, so the rendered `.env` gets the two-origin list and the container reads it at start. VoteBot's CORS layer then answers a browser call from the stage site with `access-control-allow-origin` (the same as for dev); any other origin still gets none.
* **The container is recreated, which ends open chats** (as in every deploy).

## Please (after the GO)
1. **Preflight** (nothing written), as for `4a85cf0`:
    * `git cat-file -t <SHA>` prints `commit`, and `git diff --stat 4a85cf0 <SHA>` is only the files above;
    * `infrastructure/render-env.sh --check`: the same secret names; names only;
    * the `:4a85cf0` image tag exists;
    * `free -m` and `df -h /`;
    * no chat in the last 10 minutes.
2. **Deploy** as for `4a85cf0` (runbook 3.2 to 3.5): check out the SHA, `render-env.sh`, `build`, tag `votebot-ddp-next:<8-char SHA>`, `up -d`; Slack stays off, no site is repointed; record the UTC times. **Rollback:** retag `:4a85cf0` as `:local`, then `up -d --no-build votebot` (the `.env` is rewritten in full by `render-env.sh` from the checked-out file, so check out `4a85cf0` and re-render too).
3. **Checks** (pass or fail, with the actual output; no secret in the reply):
    * **A. The rendered value:** print the `ALLOWED_ORIGINS` line of `/opt/votebot/.env` (it holds no secret). It must be a JSON list of exactly two `https://` origins: the dev site's and the stage site's, with no path, no trailing slash, no wildcard.
    * **B. Health and a normal chat:** `/votebot/v1/health/ready` answers **200**, all four dependencies healthy, restarts 0; `scripts/smoke_ws.py --retrieval` inside the network (expect 6/6) and public (expect 5/5), as before.
    * **C. Startup log:** one `Warm-up finished`; no new Traceback.
    * **D. The point of it (read only, three plain GETs):** call `/votebot/v1/features` three times, once with each `Origin` header, and print the status line and the `access-control-allow-origin` header of each (against the container from inside the network, and, if you can, against the public address too). Read the two origins from the rendered `.env` line, not from this note: for example
      ```
      python3 - <<'PY'
      import json, re, urllib.request
      line = [l for l in open('/opt/votebot/.env') if l.startswith('ALLOWED_ORIGINS=')][0].split('=', 1)[1]
      origins = json.loads(line)
      def acao(origin):
          req = urllib.request.Request('http://localhost:8000/votebot/v1/features', headers={'Origin': origin})
          return urllib.request.urlopen(req, timeout=10).headers.get('access-control-allow-origin')
      for o in origins + ['https://example.invalid']:
          print('listed' if o in origins else 'NOT listed', '->', acao(o))
      PY
      ```
      (adjust the local port to the container's). **Expect:** each of the two listed origins gets exactly itself back; `https://example.invalid` gets `None`.
    * **E. Anything that differed.**

## Not authorized
* Any code, settings, broker, nginx or Slack change.
* Pointing a site at a different VoteBot.
* Touching the old server or any widget bundle.
* Any change on the stage site (that is VOTEBOT-84, its own request on the broker repo's branch).
* A change to the Pinecone account.

## Report back
A `reply-...` note on this branch: the deployed SHA and image id with UTC times; A to E with evidence. VOTEBOT-83 stays In Review until then.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
