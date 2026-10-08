#!/usr/bin/env bash
# start-votebot-dev.sh: run the LOCAL DEV copy of VoteBot on the Mac Studio (127.0.0.1:8010) against the dev stack.
# Used by launchd (com.ddp.votebot-dev) and safe to run by hand. Mirrors ddp-open-states/start-os-api.sh and
# ddp-sync/scripts/start-ddp-sync.sh. Not the civic EC2 VoteBot and not the production container.
#
# At boot this daemon can start BEFORE Colima, the dev broker or api-v3 are up, so it waits (bounded) for each and
# exits non-zero on timeout: launchd (KeepAlive, ThrottleInterval) relaunches it until they are, instead of a VoteBot
# that started against nothing. Written for macOS's bash 3.2 (no ${!var}, no mapfile).
#
# Env overrides (for tests): WAIT_ATTEMPTS (default 60, 5 s apart = 300 s), BROKER_URL, API3_URL, PORT, REDIS_CONTAINER.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/Users/agentsmith/Developer/repos/votebot}"
PORT="${PORT:-8010}"
BROKER_URL="${BROKER_URL:-http://localhost:8080}"
API3_URL="${API3_URL:-http://localhost:8002}"
REDIS_CONTAINER="${REDIS_CONTAINER:-votebot-dev-redis}"
REDIS_PORT="${REDIS_PORT:-6380}"
WAIT_ATTEMPTS="${WAIT_ATTEMPTS:-60}"

cd "$PROJECT_DIR"
log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [start-votebot-dev] $*"; }

# Reach Docker through Colima's socket directly, independent of the docker "colima" context (a colima bounce can drop
# the context meta.json), as a system LaunchDaemon has no GUI docker context. Same as start-os-api.sh / start-cams.sh.
if [ -z "${DOCKER_HOST:-}" ]; then
    for _sock in "$HOME/.colima/default/docker.sock" "/Users/agentsmith/.colima/default/docker.sock"; do
        [ -S "$_sock" ] && export DOCKER_HOST="unix://$_sock" && break
    done
fi

# wait_for "what" "command that succeeds when ready"
wait_for() {
    local what="$1" check="$2" attempts=0
    until eval "$check" >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        [ "$attempts" -ge "$WAIT_ATTEMPTS" ] && { log "ERROR: $what not ready after $((attempts * 5))s"; exit 1; }
        log "waiting for $what... (${attempts}/${WAIT_ATTEMPTS})"; sleep 5
    done
}

wait_for "the Docker daemon (Colima)" "docker info"

# VoteBot's own Redis (button cache, handoff map). Created once, then `--restart unless-stopped` keeps it across
# reboots; this makes the script idempotent if it is missing or stopped. 127.0.0.1 only, never the CAMS Redis (6379).
if [ -z "$(docker ps -aq --filter "name=^${REDIS_CONTAINER}\$")" ]; then
    log "creating $REDIS_CONTAINER on 127.0.0.1:$REDIS_PORT"
    docker run -d --name "$REDIS_CONTAINER" --restart unless-stopped -p "127.0.0.1:${REDIS_PORT}:6379" \
        redis:7-alpine redis-server --maxmemory 128mb --maxmemory-policy allkeys-lru >/dev/null
elif [ -z "$(docker ps -q --filter "name=^${REDIS_CONTAINER}\$")" ]; then
    log "starting $REDIS_CONTAINER"
    docker start "$REDIS_CONTAINER" >/dev/null
fi
wait_for "$REDIS_CONTAINER" "docker exec $REDIS_CONTAINER redis-cli ping | grep -q PONG"

# The dev services VoteBot calls. Any HTTP answer from api-v3 counts as up (it answers 403 without a key).
wait_for "the dev broker ($BROKER_URL)" "curl -sf -m 3 $BROKER_URL/api/status/"
wait_for "api-v3 ($API3_URL)" "[ \"\$(curl -s -o /dev/null -m 3 -w '%{http_code}' '$API3_URL/jurisdictions?per_page=1')\" != 000 ]"

[ -f "$PROJECT_DIR/.env" ] || { log "ERROR: $PROJECT_DIR/.env is missing (see votebot memory / README: local dev setup)"; exit 1; }

log "Starting VoteBot (dev) on 127.0.0.1:$PORT"
# VoteBot reads .env itself (cwd is the project dir). exec, so launchd supervises uvicorn directly.
exec "$PROJECT_DIR/.venv/bin/python" -m uvicorn votebot.main:app --app-dir src --host 127.0.0.1 --port "$PORT" --log-level info
