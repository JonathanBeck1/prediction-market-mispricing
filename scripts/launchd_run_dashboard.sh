#!/bin/zsh
# Resolve repo root relative to this script's location
SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"
cd "$ROOT"
source "$ROOT/.venv/bin/activate"
exec python3 -m app.dashboard --port 8777 --no-open
