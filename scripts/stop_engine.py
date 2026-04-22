#!/usr/bin/env python3
"""Stop the Kalshi Edge engine — kills ALL runner processes.

Works cooperatively with the launchd watchdog:
  1. Creates a pause file so the watchdog won't immediately restart
  2. Kills all forms of the runner (launcher scripts + direct invocations)
  3. The pause file persists until Start Engine or Repair System removes it

Safe to run at any time.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PAUSE_FILE = Path.home() / "Library" / "Logs" / "kalshi-edge" / "runner.paused"
WATCHDOG_RUNNER_PID = Path.home() / "Library" / "Logs" / "kalshi-edge" / "runner.pid"

_PATTERNS = [
    "app.runner",
    "app.watcher",
    "app.ingestor",
    "app.scorer",
    "app.maintenance",
    "kalshi_edge_launch_app_runner",
]

WAIT_SEC = 5


def _find_pids(pattern: str) -> list[int]:
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        return [int(p) for p in r.stdout.strip().split() if p.strip().isdigit()]
    except Exception:
        return []


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def main() -> None:
    own_pid = os.getpid()

    # Signal the watchdog to NOT restart the runner.
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text(f"paused at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    print(f"Watchdog pause file created: {PAUSE_FILE}")

    to_kill: list[tuple[str, int]] = []
    print("Scanning for ALL engine processes (direct + launcher-started)...")
    for pattern in _PATTERNS:
        for pid in _find_pids(pattern):
            if pid == own_pid:
                continue
            to_kill.append((pattern, pid))
            print(f"  Found: {pattern}  pid={pid}")

    if not to_kill:
        print("\nNo engine processes found. Nothing to stop.")
        return

    print(f"\nSending SIGTERM to {len(to_kill)} process(es)...")
    for label, pid in to_kill:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"  [SIGTERM] {label}  pid={pid}")
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.time() + WAIT_SEC
    still_alive = list(to_kill)
    while still_alive and time.time() < deadline:
        time.sleep(0.5)
        still_alive = [(l, p) for l, p in still_alive if _is_running(p)]

    if still_alive:
        print(f"\nForce-killing {len(still_alive)} stubborn process(es)...")
        for label, pid in still_alive:
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"  [SIGKILL] {label}  pid={pid}")
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1)

    # Clear the watchdog's PID file so it knows the runner is gone.
    if WATCHDOG_RUNNER_PID.exists():
        WATCHDOG_RUNNER_PID.unlink(missing_ok=True)

    print("\nDone. Engine stopped.")
    print("  Watchdog is paused — it will NOT auto-restart.")
    print("  To resume: use Start Engine or Repair System.")
    print("  Dashboard is still running at http://127.0.0.1:8777")


if __name__ == "__main__":
    main()
