#!/usr/bin/env python3
"""One-click system repair: heal the database, ensure schema, restart the engine.

This script is designed to be run from the dashboard Scripts tab whenever
the system is misbehaving. It performs these steps in order:

  1. Stop all engine processes (runner, watcher, scorer, etc.)
  2. Heal the database (WAL checkpoint, or safe sidecar removal with backup)
  3. Ensure all tables/indexes exist (handles restored backups or partial DBs)
  4. Verify DB integrity with PRAGMA quick_check
  5. Restart the engine
  6. Wait for confirmation that the engine is producing data

Safe to run at any time — even if the system is healthy.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "edge.db"
RUNNER_MODULE = "app.runner"
PAUSE_FILE = Path.home() / "Library" / "Logs" / "kalshi-edge" / "runner.paused"
WATCHDOG_RUNNER_PID = Path.home() / "Library" / "Logs" / "kalshi-edge" / "runner.pid"

ENGINE_TARGETS = [
    "app.runner",
    "app.watcher",
    "app.ingestor",
    "app.scorer",
    "app.maintenance",
    "kalshi_edge_launch_app_runner",
]


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



def step_stop_engine() -> None:
    print("=" * 60)
    print("STEP 1: Stopping engine (with watchdog pause)")
    print("=" * 60)

    # Pause the watchdog so it doesn't restart the runner mid-repair.
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text(f"paused for repair at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    print("  Watchdog paused (will not auto-restart during repair)")

    own_pid = os.getpid()
    to_kill: list[tuple[str, int]] = []

    for target in ENGINE_TARGETS:
        for pid in _find_pids(target):
            if pid != own_pid:
                to_kill.append((target, pid))

    if not to_kill:
        print("  No engine processes found. Clean slate.")
        return

    print(f"  Found {len(to_kill)} process(es) — sending SIGTERM...")
    for label, pid in to_kill:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"    [SIGTERM] {label} pid={pid}")
        except (ProcessLookupError, PermissionError):
            pass

    time.sleep(3)

    still_alive = [(l, p) for l, p in to_kill if _is_running(p)]
    if still_alive:
        print(f"  Force-killing {len(still_alive)} stubborn process(es)...")
        for label, pid in still_alive:
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"    [SIGKILL] {label} pid={pid}")
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1)

    # Clear the watchdog's PID file.
    if WATCHDOG_RUNNER_PID.exists():
        WATCHDOG_RUNNER_PID.unlink(missing_ok=True)

    print("  Engine stopped.\n")


def step_heal_db() -> bool:
    print("=" * 60)
    print("STEP 2: Healing database")
    print("=" * 60)

    if not DB_PATH.exists():
        print("  Database not found — will be created fresh.")
        return True

    wal_path = DB_PATH.with_suffix(DB_PATH.suffix + "-wal")
    shm_path = DB_PATH.with_suffix(DB_PATH.suffix + "-shm")
    print(f"  DB size: {DB_PATH.stat().st_size:,} bytes")
    if wal_path.exists():
        print(f"  WAL size: {wal_path.stat().st_size:,} bytes")
    if shm_path.exists():
        print(f"  SHM size: {shm_path.stat().st_size:,} bytes")

    # Try 1: normal open + quick_check
    try:
        c = sqlite3.connect(str(DB_PATH), timeout=10.0)
        c.execute("PRAGMA busy_timeout=10000")
        result = c.execute("PRAGMA quick_check").fetchone()[0]
        if result == "ok":
            print("  quick_check: OK")
            # Try to safely checkpoint
            try:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                print("  WAL checkpoint: OK (data merged into main DB)")
            except Exception as e:
                print(f"  WAL checkpoint: skipped ({e})")
            c.close()
            return True
        else:
            print(f"  quick_check: FAILED ({result})")
            c.close()
    except Exception as e:
        print(f"  DB open failed: {e}")

    # Try 2: checkpoint to save data
    print("  Attempting WAL recovery checkpoint...")
    try:
        c = sqlite3.connect(str(DB_PATH), timeout=10.0)
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result = c.execute("PRAGMA quick_check").fetchone()[0]
        c.close()
        if result == "ok":
            print("  Recovery checkpoint: OK (data preserved)")
            return True
    except Exception as e:
        print(f"  Recovery checkpoint failed: {e}")

    # Try 3: back up and remove sidecars (last resort)
    print("  LAST RESORT: backing up then removing WAL/SHM files...")
    import shutil
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for src in (DB_PATH, wal_path, shm_path):
        if src.exists():
            dst = src.parent / f"{src.name}.pre_repair_{stamp}"
            shutil.copy2(str(src), str(dst))
            print(f"    Backed up: {src.name} -> {dst.name}")

    for p in (shm_path, wal_path):
        if p.exists():
            p.unlink()
            print(f"    Removed: {p.name}")

    try:
        c = sqlite3.connect(str(DB_PATH), timeout=10.0)
        result = c.execute("PRAGMA quick_check").fetchone()[0]
        c.close()
        if result == "ok":
            print("  DB recovered after sidecar removal.")
            return True
        else:
            print(f"  DB STILL CORRUPT: {result}")
            return False
    except Exception as e:
        print(f"  DB STILL CORRUPT: {e}")
        return False


def step_clean_data() -> None:
    print("\n" + "=" * 60)
    print("STEP 2b: Cleaning corrupted data rows")
    print("=" * 60)

    try:
        c = sqlite3.connect(str(DB_PATH), timeout=10.0)
        c.execute("PRAGMA busy_timeout=10000")
        for table in ["market_snapshots", "action_cards", "transcripts"]:
            bad = c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE ts NOT LIKE '20%'"
            ).fetchone()[0]
            if bad > 0:
                c.execute(f"DELETE FROM {table} WHERE ts NOT LIKE '20%'")
                print(f"  {table}: deleted {bad} corrupted rows (bad timestamps)")
            else:
                print(f"  {table}: OK")
        c.commit()
        c.close()
    except Exception as e:
        print(f"  Data cleaning failed: {e}")


def step_ensure_schema() -> bool:
    print("\n" + "=" * 60)
    print("STEP 3: Ensuring database schema")
    print("=" * 60)

    sys.path.insert(0, str(REPO_ROOT))
    from app.db import SCHEMA_SQL

    try:
        c = sqlite3.connect(str(DB_PATH), timeout=10.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        c.executescript(SCHEMA_SQL)

        tables = [t[0] for t in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        print(f"  Tables present: {', '.join(tables)}")

        for t in ["markets", "action_cards", "transcripts", "events",
                   "market_snapshots", "outcome_reviews", "phrase_hits", "bet_journal"]:
            count = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"    {t}: {count:,} rows")
            if t == "action_cards" and count == 0:
                print("    ⚠ WARNING: action_cards is empty — data may have been lost")

        c.close()
        print("  Schema: OK\n")
        return True
    except Exception as e:
        print(f"  Schema failed: {e}")
        return False


def step_start_engine() -> bool:
    print("=" * 60)
    print("STEP 4: Unpausing watchdog (it will start the engine)")
    print("=" * 60)

    # Remove the pause file so the watchdog can restart the runner.
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
        print("  Watchdog unpaused — it will start the runner within ~30s.")
    else:
        print("  Watchdog was not paused.")

    # Check if the watchdog itself is running.
    try:
        r = subprocess.run(["pgrep", "-f", "watchdog.sh"], capture_output=True, text=True)
        wd_pids = [p for p in r.stdout.strip().split() if p.strip().isdigit()]
    except Exception:
        wd_pids = []

    if not wd_pids:
        print("  [WARNING] Watchdog is not running! Trying to load it...")
        plist = Path.home() / "Library" / "LaunchAgents" / "com.kalshi-edge.watchdog.plist"
        if plist.exists():
            subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
            time.sleep(3)
        else:
            print("  [ERROR] Watchdog plist not found — cannot auto-start.")
            return False

    own_pid = os.getpid()
    patterns = ["app.runner", "kalshi_edge_launch_app_runner"]
    print("  Waiting for runner to appear...")
    for i in range(12):
        time.sleep(5)
        for pat in patterns:
            pids = [p for p in _find_pids(pat) if p != own_pid]
            if pids:
                print(f"  Engine confirmed running (pid={pids[0]}).")
                return True
        sys.stdout.write(".")
        sys.stdout.flush()

    print("\n  [TIMEOUT] Runner did not appear within 60s.")
    print("  Check: ~/Library/Logs/kalshi-edge/watchdog.log")
    return False


def step_verify() -> None:
    print("\n" + "=" * 60)
    print("STEP 5: Verifying system health")
    print("=" * 60)

    time.sleep(5)

    try:
        c = sqlite3.connect(str(DB_PATH), timeout=10.0)
        c.execute("PRAGMA busy_timeout=10000")

        quick = c.execute("PRAGMA quick_check").fetchone()[0]
        print(f"  DB health: {quick}")

        cards = c.execute("SELECT COUNT(*) FROM action_cards").fetchone()[0]
        latest = c.execute("SELECT MAX(ts) FROM action_cards").fetchone()[0]
        print(f"  Action cards: {cards:,}  (latest: {latest or 'none'})")

        markets = c.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"  Markets: {markets:,}  Events: {events:,}")

        c.close()
    except Exception as e:
        print(f"  Verification failed: {e}")
        return

    pids = _find_pids(RUNNER_MODULE)
    own_pid = os.getpid()
    pids = [p for p in pids if p != own_pid]
    if pids:
        print(f"  Engine: RUNNING (pid={pids[0]})")
    else:
        print("  Engine: NOT RUNNING")

    print("\n" + "=" * 60)
    print("REPAIR COMPLETE")
    print("=" * 60)
    print("  Dashboard will auto-refresh in a few seconds.")
    print("  If issues persist, check data/logs/runner.err.log")


def main() -> None:
    print()
    print("*" * 60)
    print("  KALSHI EDGE — SYSTEM REPAIR")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("*" * 60)
    print()

    step_stop_engine()

    if not step_heal_db():
        print("\n[FATAL] Database could not be healed.")
        print("  Manual intervention required.")
        sys.exit(1)

    step_clean_data()

    if not step_ensure_schema():
        print("\n[FATAL] Schema could not be applied.")
        sys.exit(1)

    if not step_start_engine():
        print("\n[WARNING] Engine did not start cleanly.")
        print("  The database is repaired — try Start Engine from the dashboard.")

    step_verify()


if __name__ == "__main__":
    main()
