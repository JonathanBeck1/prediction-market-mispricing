#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
RUN_DIR="${ROOT_DIR}/data/run"
LOG_DIR="${ROOT_DIR}/data/logs"
RUNNER_PID_FILE="${RUN_DIR}/runner.pid"
DASH_PID_FILE="${RUN_DIR}/dashboard.pid"
RUNNER_LOG="${LOG_DIR}/runner.local.log"
DASH_LOG="${LOG_DIR}/dashboard.local.log"
PORT="${PORT:-8777}"

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

usage() {
  cat <<'EOF'
Usage:
  scripts/local_stack.sh [command]

Commands:
  start        Start runner + dashboard in background.
  fresh-start  Refresh caches, then start both services.
  stop         Stop both services.
  restart      Stop, then start both services.
  status       Show process status and log paths.

Examples:
  scripts/local_stack.sh start
  scripts/local_stack.sh restart
  scripts/local_stack.sh fresh-start
EOF
}

require_python() {
  if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Missing venv python at ${PYTHON_BIN}"
    echo "Create/install first:"
    echo "  python3 -m venv .venv"
    echo "  source .venv/bin/activate"
    echo "  python3 -m pip install -r requirements.txt"
    exit 1
  fi
}

is_running() {
  local pid_file="$1"
  [[ -f "${pid_file}" ]] || return 1
  local pid
  pid="$(<"${pid_file}")"
  [[ -n "${pid}" ]] || return 1
  kill -0 "${pid}" >/dev/null 2>&1
}

start_runner() {
  if is_running "${RUNNER_PID_FILE}"; then
    echo "runner already running (pid $(<"${RUNNER_PID_FILE}"))"
    return
  fi
  nohup env \
    KALSHI_MOCK=0 \
    MAINTENANCE_ENABLED=1 \
    PRE_EVENT_WINDOW_SEC=604800 \
    FETCH_MARKETS_INTERVAL_SEC=600 \
    "${PYTHON_BIN}" -m app.runner \
    >>"${RUNNER_LOG}" 2>&1 &
  echo $! > "${RUNNER_PID_FILE}"
  sleep 1
  if is_running "${RUNNER_PID_FILE}"; then
    echo "runner started (pid $(<"${RUNNER_PID_FILE}"))"
  else
    rm -f "${RUNNER_PID_FILE}"
    echo "runner failed to start (check ${RUNNER_LOG})"
  fi
}

start_dashboard() {
  if is_running "${DASH_PID_FILE}"; then
    echo "dashboard already running (pid $(<"${DASH_PID_FILE}"))"
    return
  fi
  if lsof -tiTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "dashboard port ${PORT} already in use (existing server likely running)"
    return
  fi
  nohup "${PYTHON_BIN}" -m app.dashboard --port "${PORT}" --no-open \
    >>"${DASH_LOG}" 2>&1 &
  echo $! > "${DASH_PID_FILE}"
  sleep 1
  if is_running "${DASH_PID_FILE}"; then
    echo "dashboard started (pid $(<"${DASH_PID_FILE}"), port ${PORT})"
  else
    rm -f "${DASH_PID_FILE}"
    echo "dashboard failed to start (check ${DASH_LOG})"
  fi
}

stop_one() {
  local name="$1"
  local pid_file="$2"
  if ! [[ -f "${pid_file}" ]]; then
    echo "${name} not running (no pid file)"
    return
  fi
  local pid
  pid="$(<"${pid_file}")"
  if [[ -z "${pid}" ]]; then
    rm -f "${pid_file}"
    echo "${name} not running (empty pid file)"
    return
  fi
  if kill -0 "${pid}" >/dev/null 2>&1; then
    kill "${pid}" >/dev/null 2>&1 || true
    sleep 1
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill -9 "${pid}" >/dev/null 2>&1 || true
    fi
    echo "${name} stopped"
  else
    echo "${name} not running (stale pid ${pid})"
  fi
  rm -f "${pid_file}"
}

status_one() {
  local name="$1"
  local pid_file="$2"
  if is_running "${pid_file}"; then
    echo "${name}: running (pid $(<"${pid_file}"))"
  else
    echo "${name}: stopped"
  fi
}

do_start() {
  require_python
  start_runner
  start_dashboard
  echo "logs:"
  echo "  ${RUNNER_LOG}"
  echo "  ${DASH_LOG}"
}

do_fresh_start() {
  require_python
  (cd "${ROOT_DIR}" && "${PYTHON_BIN}" scripts/fetch_markets.py && "${PYTHON_BIN}" scripts/fetch_polymarket.py && "${PYTHON_BIN}" scripts/fetch_wallet_flow.py && "${PYTHON_BIN}" scripts/fetch_news_signals.py)
  do_start
}

CMD="${1:-start}"
case "${CMD}" in
  start)
    do_start
    ;;
  fresh-start)
    do_fresh_start
    ;;
  stop)
    stop_one "runner" "${RUNNER_PID_FILE}"
    stop_one "dashboard" "${DASH_PID_FILE}"
    ;;
  restart)
    stop_one "runner" "${RUNNER_PID_FILE}"
    stop_one "dashboard" "${DASH_PID_FILE}"
    do_start
    ;;
  status)
    status_one "runner" "${RUNNER_PID_FILE}"
    status_one "dashboard" "${DASH_PID_FILE}"
    echo "logs:"
    echo "  ${RUNNER_LOG}"
    echo "  ${DASH_LOG}"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: ${CMD}"
    usage
    exit 1
    ;;
esac
