"""Prune stale market_snapshots, action_cards, and rotate JSONL logs.

Keeps the last N days of data in SQLite and truncates raw logs.
Safe to run while the scoring engine is active (SQLite WAL mode).

Design: uses incremental batch deletes so it always makes forward progress
even when the DB is large.  Skips expensive COUNT(*) queries — reports the
rowcount returned by DELETE directly.

Usage:
    python3 scripts/prune_snapshots.py          # default 1-day retention
    python3 scripts/prune_snapshots.py --days 3 # keep 3 days
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect as _db_connect  # noqa: E402

DB_PATH = Path("data/edge.db")
JSONL_PATHS = [
    Path("data/raw/kalshi_snapshots.jsonl"),
    Path("data/action_cards.jsonl"),
]
LOG_PATHS = [
    Path("data/logs/runner.err.log"),
    Path("data/logs/dashboard.err.log"),
    Path("data/logs/runner.local.log"),
    Path("data/logs/dashboard.local.log"),
]
JSONL_MAX_MB = 50
LOG_MAX_MB   = 10
BATCH_SIZE   = 100_000   # rows per DELETE batch — keeps each transaction short

# Hard cap: keep at most this many snapshots per market regardless of age.
# At a 5-min heartbeat, 500 rows ≈ 41 hours of history — well above the 24h
# price-velocity window. Prevents runaway growth when the watcher throttle
# doesn't fully contain writes (e.g. active-market price churn).
SNAP_MAX_ROWS_PER_MARKET = 500


def _truncate_if_large(path: Path, max_mb: float) -> None:
    if not path.exists():
        return
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        path.write_text("")
        print(f"  Truncated {path} ({size_mb:.1f} MB → 0 MB)")
    else:
        print(f"  {path}: {size_mb:.1f} MB (under {max_mb} MB limit, keeping)")


def _batch_delete(conn: sqlite3.Connection, table: str, days: int) -> int:
    """Delete rows older than `days` days in batches.

    Returns total rows deleted.  Uses rowid-based batching which is fast
    even on very large tables — no full table scan required.
    """
    cutoff = f"-{days} days"
    total_deleted = 0
    while True:
        # Find the max rowid to delete in this batch
        row = conn.execute(
            f"SELECT MAX(id) FROM (SELECT id FROM {table} WHERE ts < datetime('now', ?) LIMIT ?)",
            (cutoff, BATCH_SIZE),
        ).fetchone()
        if not row or row[0] is None:
            break
        max_id = row[0]
        deleted = conn.execute(
            f"DELETE FROM {table} WHERE id <= ? AND ts < datetime('now', ?)",
            (max_id, cutoff),
        ).rowcount
        conn.commit()
        total_deleted += deleted
        if deleted == 0:
            break
        time.sleep(0.05)   # yield to the watcher between batches
    return total_deleted


def _archive_bets_before_prune() -> None:
    """Run archive_bet_decisions.py BEFORE pruning so we never lose BUY card history."""
    import subprocess
    import sys
    script = Path(__file__).parent / "archive_bet_decisions.py"
    if not script.exists():
        return
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Bet archiver: {result.stdout.strip()}")
    else:
        print(f"Bet archiver WARNING: {result.stderr.strip()[:200]}")


def _cap_snapshots_per_market(conn: sqlite3.Connection) -> int:
    """Delete oldest rows that exceed SNAP_MAX_ROWS_PER_MARKET per market.

    Uses a single-pass window-function query so it scales with the number of
    distinct markets, not the total row count.  Returns total rows deleted.
    """
    t0 = time.time()
    deleted = conn.execute(
        """
        DELETE FROM market_snapshots
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY market_id ORDER BY id DESC
                       ) AS rn
                FROM market_snapshots
            ) ranked
            WHERE rn > ?
        )
        """,
        (SNAP_MAX_ROWS_PER_MARKET,),
    ).rowcount
    conn.commit()
    if deleted:
        print(
            f"Snapshots: hard-cap trimmed {deleted:,} rows "
            f"(>{SNAP_MAX_ROWS_PER_MARKET} per market)  [{time.time()-t0:.0f}s]"
        )
    return deleted


def prune(days: int = 1) -> None:
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}; nothing to prune.")
        return

    # Archive bet decisions BEFORE pruning action_cards
    _archive_bets_before_prune()

    db_mb_before = DB_PATH.stat().st_size / (1024 * 1024)
    print(f"Database size before: {db_mb_before:,.0f} MB")

    conn = _db_connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")    # FULL prevents B-tree corruption on kill
    conn.execute("PRAGMA busy_timeout=30000")  # wait up to 30s if runner holds write lock

    # Strip raw_json from all non-latest rows FIRST — raw_json (full API payload,
    # ~3-4 KB/row) is only needed for the current snapshot per market.
    # Historical rows only need yes_ask/no_ask for price-velocity calculations.
    t0 = time.time()
    stripped = conn.execute(
        """
        UPDATE market_snapshots SET raw_json = '{}'
        WHERE raw_json != '{}'
          AND id NOT IN (
              SELECT MAX(id) FROM market_snapshots GROUP BY market_id
          )
        """,
    ).rowcount
    conn.commit()
    if stripped:
        print(f"Snapshots: stripped raw_json from {stripped:,} non-latest rows  [{time.time()-t0:.0f}s]")

    t0 = time.time()
    snap_deleted = _batch_delete(conn, "market_snapshots", days)
    print(f"Snapshots: deleted {snap_deleted:,} rows (kept last {days}d)  [{time.time()-t0:.0f}s]")

    # Hard cap: enforce per-market row limit as a safety net regardless of age.
    # This catches the case where all snapshots are < 1 day old (fresh runner restart).
    _cap_snapshots_per_market(conn)

    t0 = time.time()
    card_deleted = _batch_delete(conn, "action_cards", days)
    print(f"Action cards: deleted {card_deleted:,} rows (kept last {days}d)  [{time.time()-t0:.0f}s]")

    conn.execute("PRAGMA optimize")
    conn.commit()

    # Only VACUUM if the DB is above a threshold — VACUUM is exclusive and slow (exclusive lock)
    db_mb_current = DB_PATH.stat().st_size / (1024 * 1024)
    if db_mb_current > 500:
        print(f"Running VACUUM (DB is {db_mb_current:,.0f} MB)…")
        t0 = time.time()
        conn.execute("VACUUM")
        conn.close()
        db_mb_after = DB_PATH.stat().st_size / (1024 * 1024)
        print(f"Database size after VACUUM: {db_mb_after:,.0f} MB (freed {db_mb_current - db_mb_after:,.0f} MB)  [{time.time()-t0:.0f}s]")
    else:
        conn.close()
        print(f"Database size after prune: {db_mb_current:,.0f} MB (no VACUUM needed)")

    print("\nJSONL logs:")
    for p in JSONL_PATHS:
        _truncate_if_large(p, JSONL_MAX_MB)

    print("\nError/app logs:")
    for p in LOG_PATHS:
        _truncate_if_large(p, LOG_MAX_MB)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prune stale data and logs")
    parser.add_argument("--days", type=int, default=1, help="Days of data to keep (default: 1)")
    args = parser.parse_args()
    prune(args.days)
