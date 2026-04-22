#!/usr/bin/env bash
set -euo pipefail

RUNNER_LABEL="com.kalshi-edge.runner"
DASH_LABEL="com.kalshi-edge.dashboard"
DOMAIN="gui/$(id -u)"

usage() {
  cat <<'EOF'
Usage:
  scripts/restart_24x7.sh [command] [target]

Commands:
  restart   Kickstart service(s). (default)
  status    Show whether service(s) are loaded.
  help      Show this help.

Targets:
  all       Runner + dashboard. (default)
  runner    Runner only.
  dashboard Dashboard only.

Examples:
  scripts/restart_24x7.sh
  scripts/restart_24x7.sh restart all
  scripts/restart_24x7.sh restart runner
  scripts/restart_24x7.sh status
EOF
}

is_loaded() {
  local label="$1"
  launchctl print "${DOMAIN}/${label}" >/dev/null 2>&1
}

job_info() {
  local label="$1"
  launchctl list "${label}" 2>/dev/null || true
}

job_pid() {
  local label="$1"
  job_info "${label}" | awk -F'= ' '/"PID"/ {gsub(";", "", $2); gsub(/^[[:space:]\"]+|[[:space:]\"]+$/, "", $2); print $2; exit}'
}

job_last_exit() {
  local label="$1"
  job_info "${label}" | awk -F'= ' '/"LastExitStatus"/ {gsub(";", "", $2); gsub(/^[[:space:]\"]+|[[:space:]\"]+$/, "", $2); print $2; exit}'
}

restart_one() {
  local label="$1"
  if is_loaded "${label}"; then
    launchctl kickstart -k "${DOMAIN}/${label}"
    echo "restarted: ${label}"
  else
    echo "not loaded: ${label}"
  fi
}

status_one() {
  local label="$1"
  if is_loaded "${label}"; then
    local pid
    local last_exit
    pid="$(job_pid "${label}")"
    last_exit="$(job_last_exit "${label}")"
    if [[ -n "${pid}" && "${pid}" != "0" ]]; then
      echo "running: ${label} (pid ${pid})"
    elif [[ -n "${last_exit}" && "${last_exit}" != "0" ]]; then
      echo "loaded but failing: ${label} (LastExitStatus=${last_exit})"
    else
      echo "loaded: ${label}"
    fi
  else
    echo "not loaded: ${label}"
  fi
}

run_for_target() {
  local action="$1"
  local target="$2"

  case "${target}" in
    runner)
      "${action}" "${RUNNER_LABEL}"
      ;;
    dashboard)
      "${action}" "${DASH_LABEL}"
      ;;
    all)
      "${action}" "${RUNNER_LABEL}"
      "${action}" "${DASH_LABEL}"
      ;;
    *)
      echo "unknown target: ${target}"
      usage
      exit 1
      ;;
  esac
}

CMD="${1:-restart}"
TARGET="${2:-all}"

case "${CMD}" in
  restart)
    run_for_target restart_one "${TARGET}"
    ;;
  status)
    run_for_target status_one "${TARGET}"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "unknown command: ${CMD}"
    usage
    exit 1
    ;;
esac

if [[ "${CMD}" == "restart" ]]; then
  if ! is_loaded "${RUNNER_LABEL}" || ! is_loaded "${DASH_LABEL}"; then
    cat <<'EOF'

Tip: If services are not loaded yet, install once:
  source .venv/bin/activate
  python3 scripts/manage_launchd.py install --repo-root . --with-dashboard
EOF
  fi
fi
