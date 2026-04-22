#!/bin/zsh
# Resolve repo root relative to this script's location
SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"
cd "$ROOT"
source "$ROOT/.venv/bin/activate"
export KALSHI_MOCK=0
export MAINTENANCE_ENABLED=1
export PRE_EVENT_WINDOW_SEC=604800
export FETCH_MARKETS_INTERVAL_SEC=600
exec python3 -m app.runner
