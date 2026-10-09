#!/usr/bin/env bash
# install-votebot-dev-daemon.sh: install the LaunchDaemon (com.ddp.votebot-dev) for the LOCAL DEV VoteBot on the Mac Studio.
# VOTEBOT-32. Run it from an ADMIN account (the service account agentsmith has no sudo):
#
#   sudo bash /Users/agentsmith/Developer/repos/votebot/infrastructure/install-votebot-dev-daemon.sh
#   bash    /Users/agentsmith/Developer/repos/votebot/infrastructure/install-votebot-dev-daemon.sh --check      (no sudo, changes nothing)
#   sudo bash .../install-votebot-dev-daemon.sh --uninstall
#
# It does what the plist header describes, in this order, and is safe to run again: check everything first, stop a
# hand-started copy of VoteBot that holds the port, replace the installed plist, load it, then wait until
# /votebot/v1/health/ready says healthy. Nothing else on the machine is touched. It runs as root, so read it first.
set -euo pipefail

LABEL="com.ddp.votebot-dev"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
SRC_PLIST="$SCRIPT_DIR/launchd/$LABEL.plist"
DEST_DIR="${DEST_DIR:-/Library/LaunchDaemons}"
DEST="$DEST_DIR/$LABEL.plist"
SERVICE_USER="${SERVICE_USER:-agentsmith}"   # the account the daemon runs as (the plist says agentsmith; tests override it)
PORT="${PORT:-8010}"
HEALTH_URL="http://127.0.0.1:${PORT}/votebot/v1/health/ready"
WAIT_SECONDS="${WAIT_SECONDS:-120}"
LAUNCHCTL="${LAUNCHCTL:-launchctl}"
TEST_MODE="${VOTEBOT_INSTALL_TEST:-0}"   # tests only: skips the root check and the chown

MODE="install"
case "${1:-}" in
    "") ;;
    --check) MODE="check" ;;
    --uninstall) MODE="uninstall" ;;
    *) echo "usage: $0 [--check | --uninstall]" >&2; exit 2 ;;
esac

say()  { echo "[install-votebot-dev-daemon] $*"; }
fail() { echo "[install-votebot-dev-daemon] ERROR: $*" >&2; exit 1; }
need_root() { [ "$TEST_MODE" = "1" ] || [ "$(id -u)" -eq 0 ] || fail "run this with sudo from an admin account (it writes to $DEST_DIR)"; }
loaded()    { "$LAUNCHCTL" print "system/$LABEL" >/dev/null 2>&1; }
healthy()   { curl -sf -m 5 "$HEALTH_URL" 2>/dev/null | grep -q '"status":"healthy"'; }
listener_pids() { lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true; }

preflight() {
    say "checking before changing anything"
    [ -f "$SRC_PLIST" ] || fail "missing $SRC_PLIST (run this from a votebot checkout on main)"
    plutil -lint "$SRC_PLIST" >/dev/null || fail "$SRC_PLIST is not a valid plist"
    local program
    program="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' "$SRC_PLIST")"
    [ -x "$program" ] || fail "the plist runs $program, which is missing or not executable"
    [ "$program" = "$PROJECT_DIR/infrastructure/start-votebot-dev.sh" ] \
        || say "WARNING: the plist runs $program, not this checkout's script; the plist paths are fixed to that location"
    id "$SERVICE_USER" >/dev/null 2>&1 || fail "user $SERVICE_USER does not exist"
    [ -f "$PROJECT_DIR/.env" ] || fail "$PROJECT_DIR/.env is missing (the daemon would exit and retry forever)"
    [ -x "$PROJECT_DIR/.venv/bin/python" ] || fail "$PROJECT_DIR/.venv/bin/python is missing"
    [ -d "$PROJECT_DIR/logs" ] || fail "$PROJECT_DIR/logs is missing (launchd does not create it)"
    [ "$(stat -f %Su "$PROJECT_DIR/logs")" = "$SERVICE_USER" ] || fail "$PROJECT_DIR/logs is not owned by $SERVICE_USER"
    local pid cmd
    for pid in $(listener_pids); do
        cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
        case "$cmd" in
            *"uvicorn votebot.main:app"*) say "port $PORT is held by a hand-started VoteBot (pid $pid); it will be stopped" ;;
            *) fail "port $PORT is held by something that is not VoteBot (pid $pid: ${cmd:-unknown}); not touching it" ;;
        esac
    done
    if [ -f "$DEST" ]; then
        if cmp -s "$SRC_PLIST" "$DEST"; then say "an identical plist is already installed"; else say "a different plist is installed and will be replaced"; fi
    fi
    if loaded; then say "$LABEL is already loaded and will be reloaded"; fi
    say "checks passed"
}

stop_hand_started() {
    local pid cmd waited=0
    for pid in $(listener_pids); do
        cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
        case "$cmd" in *"uvicorn votebot.main:app"*) say "stopping hand-started VoteBot (pid $pid)"; kill "$pid" 2>/dev/null || true ;; esac
    done
    while [ -n "$(listener_pids)" ] && [ "$waited" -lt 15 ]; do sleep 1; waited=$((waited + 1)); done
    [ -z "$(listener_pids)" ] || fail "port $PORT is still held after 15 s; stop it by hand and run this again"
}

case "$MODE" in
check)
    preflight
    say "check only: nothing was changed. To install: sudo bash $0"
    ;;
uninstall)
    need_root
    if loaded; then say "unloading $LABEL"; "$LAUNCHCTL" bootout "system/$LABEL"; fi
    if [ -f "$DEST" ]; then rm -f "$DEST"; say "removed $DEST"; fi
    say "uninstalled; VoteBot is no longer supervised (start it by hand with infrastructure/start-votebot-dev.sh)"
    ;;
install)
    need_root
    preflight
    if loaded; then say "unloading the loaded copy first"; "$LAUNCHCTL" bootout "system/$LABEL"; sleep 2; fi
    stop_hand_started
    say "installing $DEST"
    cp "$SRC_PLIST" "$DEST"
    [ "$TEST_MODE" = "1" ] || chown root:wheel "$DEST"
    chmod 644 "$DEST"
    say "loading $LABEL"
    "$LAUNCHCTL" bootstrap system "$DEST"
    say "waiting up to ${WAIT_SECONDS}s for VoteBot to report healthy (it first waits for Docker, the dev broker and api-v3)"
    waited=0
    until healthy; do
        waited=$((waited + 3))
        if [ "$waited" -ge "$WAIT_SECONDS" ]; then
            echo "--- launchd state:" >&2; "$LAUNCHCTL" print "system/$LABEL" 2>&1 | grep -E "state|pid|last exit|runs" >&2 || true
            echo "--- last log lines ($PROJECT_DIR/logs/dev-server.log):" >&2; tail -15 "$PROJECT_DIR/logs/dev-server.log" >&2 || true
            fail "not healthy after ${WAIT_SECONDS}s. The daemon stays installed and keeps retrying every 30 s; the log above says what it waits for. Remove it with: sudo bash $0 --uninstall"
        fi
        sleep 3
    done
    say "healthy:"; curl -s -m 5 "$HEALTH_URL"; echo
    "$LAUNCHCTL" print "system/$LABEL" 2>&1 | grep -E "state|pid" || true
    say "done. It now starts at every boot. Restart: sudo launchctl kickstart -k system/$LABEL    Remove: sudo bash $0 --uninstall"
    ;;
esac
