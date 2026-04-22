#!/usr/bin/env bash
# 24/7 watchdog — started by macOS LaunchAgent on login and after reboot.
# Keeps the runner + dashboard alive. Handles stale lock files and crashed pids.
# Loops forever; launchd KeepAlive will restart this script if it ever exits.

set -uo pipefail

# Resolve repo root relative to this script's location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="$ROOT/.venv/bin/python3"
RUN_DIR="$ROOT/data/run"
LOG_DIR="$ROOT/data/logs"
RUNNER_PID="$RUN_DIR/runner.pid"
DASH_PID="$RUN_DIR/dashboard.pid"
RUNNER_LOG="$LOG_DIR/runner.local.log"
DASH_LOG="$LOG_DIR/dashboard.local.log"
RUNNER_LOCK="$ROOT/data/runner.lock"
PORT=8777

mkdir -p "$RUN_DIR" "$LOG_DIR"

log() { echo "[watchdog] $(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_DIR/watchdog.log"; }

is_alive() {
    local pid_file="$1"
    [[ -f "$pid_file" ]] || return 1
    local pid; pid="$(<"$pid_file")"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start_runner() {
    if is_alive "$RUNNER_PID"; then return 0; fi
    # Clear stale lock so restart is clean
    rm -f "$RUNNER_LOCK"
    log "Starting runner..."
    nohup env KALSHI_MOCK=0 MAINTENANCE_ENABLED=1 \
        PRE_EVENT_WINDOW_SEC=604800 FETCH_MARKETS_INTERVAL_SEC=600 \
        "$PYTHON" -m app.runner >>"$RUNNER_LOG" 2>&1 &
    echo $! > "$RUNNER_PID"
    sleep 3
    if is_alive "$RUNNER_PID"; then
        log "Runner started (pid $(<"$RUNNER_PID"))"
    else
        rm -f "$RUNNER_PID"
        log "Runner failed to start — will retry"
    fi
}

start_dashboard() {
    if is_alive "$DASH_PID"; then return 0; fi
    if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        # Port in use but we have no pid file — record it
        local existing; existing=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)
        echo "$existing" > "$DASH_PID"
        return 0
    fi
    log "Starting dashboard..."
    nohup "$PYTHON" -m app.dashboard --port "$PORT" --no-open >>"$DASH_LOG" 2>&1 &
    echo $! > "$DASH_PID"
    sleep 2
    if is_alive "$DASH_PID"; then
        log "Dashboard started (pid $(<"$DASH_PID"), port $PORT)"
    else
        rm -f "$DASH_PID"
        log "Dashboard failed to start — will retry"
    fi
}

log "Watchdog started (pid $$)"
cd "$ROOT"

while true; do
    start_runner
    start_dashboard
    sleep 30
done
