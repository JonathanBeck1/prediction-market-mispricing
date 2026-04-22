#!/usr/bin/env python3
"""Start the Kalshi Edge engine — cooperates with the launchd watchdog.

Instead of starting the runner directly (which created duplicate processes),
this script:
  1. Removes the pause file so the watchdog is allowed to start the runner
  2. Waits for the watchdog to notice and start the runner (within ~30s)
  3. Confirms the runner is alive

The launchd watchdog is the SOLE process that starts the runner.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PAUSE_FILE = Path.home() / "Library" / "Logs" / "kalshi-edge" / "runner.paused"
WATCHDOG_RUNNER_PID = Path.home() / "Library" / "Logs" / "kalshi-edge" / "runner.pid"

_RUNNER_PATTERNS = ["app.runner", "kalshi_edge_launch_app_runner"]


def _find_runner_pids() -> list[int]:
    pids: list[int] = []
    own = os.getpid()
    for pattern in _RUNNER_PATTERNS:
        try:
            r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
            for p in r.stdout.strip().split():
                if p.strip().isdigit():
                    pid = int(p)
                    if pid != own:
                        pids.append(pid)
        except Exception:
            pass
    return list(set(pids))


def main() -> None:
    existing = _find_runner_pids()
    if existing:
        print(f"Runner is already running (pid={', '.join(str(p) for p in existing)}).")
        # Make sure it's not paused
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
            print("  Removed watchdog pause file.")
        return

    # Remove pause file so the watchdog can start the runner.
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
        print("Removed watchdog pause file — watchdog will start the runner.")
    else:
        print("Watchdog is not paused — it should start the runner shortly.")

    # Check if the watchdog itself is running.
    try:
        r = subprocess.run(["pgrep", "-f", "watchdog.sh"], capture_output=True, text=True)
        watchdog_pids = [p for p in r.stdout.strip().split() if p.strip().isdigit()]
    except Exception:
        watchdog_pids = []

    if not watchdog_pids:
        print("\n[WARNING] The launchd watchdog is not running!")
        print("  Run: launchctl load ~/Library/LaunchAgents/com.kalshi-edge.watchdog.plist")
        print("  Then try Start Engine again.")
        return

    print(f"Watchdog is running (pid={watchdog_pids[0]}). Waiting for runner to start...")
    print("  (The watchdog checks every 30 seconds)")

    for i in range(12):
        time.sleep(5)
        pids = _find_runner_pids()
        if pids:
            print(f"\nEngine started successfully (pid={pids[0]}).")
            print("  Dashboard auto-refreshes every 5s — markets will appear shortly.")
            return
        sys.stdout.write(".")
        sys.stdout.flush()

    print("\n\n[TIMEOUT] Runner did not appear within 60s.")
    print("  Check watchdog log: ~/Library/Logs/kalshi-edge/watchdog.log")
    print("  Or try: Repair System")


if __name__ == "__main__":
    main()
