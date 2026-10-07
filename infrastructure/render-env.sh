#!/usr/bin/env bash
# VOTEBOT-14: writes VoteBot's production /opt/votebot/.env (the compose file's `env_file`) on the HOST,
# from AWS Secrets Manager plus the committed non-secret defaults in infrastructure/docker/prod.env.defaults.
# Nobody types or pastes a secret, and none ever passes through a note, a ticket or shell history.
#
# Same approach as ddp-sync's infrastructure/render-env.sh, and for the same reason: the host's instance
# role already has secretsmanager:GetSecretValue, so the host resolves the secrets once and the container
# only sees a plain env file. VoteBot's own code is unchanged and has no boto3 dependency.
#
# Secrets (JSON object, snake_case keys; a missing or empty one makes the script fail BEFORE touching .env).
# By decision (2026-10-07) VoteBot reads the SAME shared secret as ddp-sync, `ddp-sync/credentials`
# (VOTEBOT_SECRET_ID), which holds: api_key, openai_api_key, pinecone_api_key, rds_openstates_api_key
# (-> DDP_OPENSTATES_BEARER_TOKEN: the setting name is historical, it is sent as X-API-Key) and, for
# --with-slack, votebot_slack_bot_token and votebot_slack_app_token (a bare slack_bot_token in the same
# secret belongs to something else and is never read). To give VoteBot a secret of its own later, set
# VOTEBOT_SECRET_ID=votebot/credentials (same key names), and API_SOURCE_SECRET_ID=ddp-sync/credentials
# to keep reading the api-v3 key from the shared one (API_SOURCE_SECRET_ID= empty reads
# ddp_openstates_api_key from VoteBot's secret instead).
# Other keys in a secret are ignored: only the names mapped below are ever written.
#
# THE FILE IS REWRITTEN IN FULL on every run: a hand edit of .env is lost. Anything that is not a secret
# (DDP_SITE_BASE_URL, ALLOWED_ORIGINS, ...) is changed in infrastructure/docker/prod.env.defaults, by a PR,
# and re-rendered; the cutover edits are made that way.
#
# Idempotent: re-run any time to pick up a rotated secret, then `docker compose ... up -d` (a restart ends
# open chats). The file is written atomically (temp file, then rename) with mode 600 and the invoking user as
# owner, because `docker compose` reads `env_file` as that user. Never prints a value: only key NAMES.
#
# Usage:  infrastructure/render-env.sh [--check] [--with-slack] [--out FILE]
#   --check       fetch and validate, print which names would be written, write nothing
#   --with-slack  also write SLACK_BOT_TOKEN and SLACK_APP_TOKEN (cutover day only: one app token may be held
#                 by ONE running VoteBot, so stop the old copy's Slack connection first; VOTEBOT-14)
#   --out     write somewhere other than <repo>/.env
set -euo pipefail
set +x  # never trace: expanded lines would print values (and this script keeps secrets out of bash anyway)

REGION="${AWS_REGION:-us-east-1}"
VOTEBOT_SECRET_ID="${VOTEBOT_SECRET_ID:-ddp-sync/credentials}"
API_SOURCE_SECRET_ID="${API_SOURCE_SECRET_ID-$VOTEBOT_SECRET_ID}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULTS_FILE="${DEFAULTS_FILE:-$REPO_DIR/infrastructure/docker/prod.env.defaults}"
OUT_FILE="$REPO_DIR/.env"
CHECK=0
WITH_SLACK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --with-slack) WITH_SLACK=1 ;;
    --out) shift; OUT_FILE="${1:?--out needs a file}" ;;
    *) echo "[render-env] unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -r "$DEFAULTS_FILE" ] || { echo "[render-env] missing $DEFAULTS_FILE" >&2; exit 1; }

# The secrets are fetched INSIDE python (subprocess to the aws CLI): they never pass through a bash
# variable, an `export`, a command line or a here-doc, so tracing or `ps` cannot show them.
export VOTEBOT_SECRET_ID API_SOURCE_SECRET_ID REGION WITH_SLACK
python3 - "$OUT_FILE" "$DEFAULTS_FILE" "$CHECK" <<'PYEOF'
import json
import os
import re
import subprocess
import sys
import tempfile

out_file, defaults_file, check = sys.argv[1], sys.argv[2], sys.argv[3] == "1"


def fetch(secret_id):
    print(f"[render-env] fetching {secret_id}...")
    done = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--region", os.environ["REGION"], "--secret-id", secret_id,
         "--query", "SecretString", "--output", "text"],
        capture_output=True, text=True,
    )
    if done.returncode != 0:  # the CLI's own error names the secret and the reason; it never holds the value
        sys.exit(f"[render-env] could not read {secret_id}: {done.stderr.strip()[:300]}")
    try:
        doc = json.loads(done.stdout)
    except ValueError:
        sys.exit(f"[render-env] {secret_id} is not a JSON object")  # never echo the content
    if not isinstance(doc, dict):
        sys.exit(f"[render-env] {secret_id} is not a JSON object")
    return doc


votebot = fetch(os.environ["VOTEBOT_SECRET_ID"])
api_source_id = os.environ["API_SOURCE_SECRET_ID"]
USE_API_SOURCE = bool(api_source_id)
api_source = votebot if api_source_id == os.environ["VOTEBOT_SECRET_ID"] else (fetch(api_source_id) if USE_API_SOURCE else {})

# (secret, secret key) -> env var name. Only these are ever written.
REQUIRED = [
    (votebot, "api_key", "API_KEY"),
    (votebot, "openai_api_key", "OPENAI_API_KEY"),
    (votebot, "pinecone_api_key", "PINECONE_API_KEY"),
]
# The setting name is historical: with DDP_OPENSTATES_AUTH_HEADER=x-api-key it is sent as X-API-Key.
if USE_API_SOURCE:
    REQUIRED.append((api_source, "rds_openstates_api_key", "DDP_OPENSTATES_BEARER_TOKEN"))
else:
    REQUIRED.append((votebot, "ddp_openstates_api_key", "DDP_OPENSTATES_BEARER_TOKEN"))

if os.environ["WITH_SLACK"] == "1":
    REQUIRED += [
        (votebot, "votebot_slack_bot_token", "SLACK_BOT_TOKEN"),
        (votebot, "votebot_slack_app_token", "SLACK_APP_TOKEN"),
    ]

SAFE_VALUE = re.compile(r"[A-Za-z0-9._~+/=:@-]+")
secret_lines, written, missing = [], [], []
for source, key, env_name in REQUIRED:
    value = source.get(key)
    if value in (None, ""):
        missing.append(f"{env_name} (secret key {key})")
        continue
    value = str(value)
    # An env file has no lossless escaping that every parser agrees on (spaces, quotes, `#`, `$`, backslashes
    # are all read differently by docker compose), so a value outside the characters real API keys use is
    # refused by name instead of being written wrong.
    if not SAFE_VALUE.fullmatch(value):
        sys.exit(f"[render-env] refusing: the value for {env_name} has a character outside A-Z a-z 0-9 . _ ~ + / = : @ -")
    secret_lines.append(f"{env_name}={value}\n")
    written.append(env_name)

if missing:
    sys.exit("[render-env] missing or empty, nothing written: " + ", ".join(missing))

with open(defaults_file) as f:
    defaults = f.read()
defaults_names = sorted(
    line.split("=", 1)[0] for line in defaults.splitlines() if "=" in line and not line.lstrip().startswith("#")
)
clash = [n for n in defaults_names if n in written]
if clash:
    sys.exit("[render-env] the defaults file must not define secret names: " + ", ".join(clash))

if check:
    print(f"[render-env] --check OK: would write {len(written)} secret names {sorted(written)} + {len(defaults_names)} defaults to {out_file}")
    sys.exit(0)

body = "# Generated by infrastructure/render-env.sh -- do not edit by hand, do not commit.\n" + defaults
if not body.endswith("\n"):
    body += "\n"
body += "".join(secret_lines)

directory = os.path.dirname(os.path.abspath(out_file))
fd, tmp = tempfile.mkstemp(prefix=".env.", dir=directory)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.replace(tmp, out_file)
except BaseException:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise
print(f"[render-env] wrote {len(written)} secret names {sorted(written)} + {len(defaults_names)} defaults to {out_file} (mode 600)")
PYEOF
