#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import connect

DB_PATH = Path("data/edge.db")


def _safe_ratio(a: int, b: int) -> float:
    return 0.0 if b <= 0 else a / b


def _print_summary(conn: sqlite3.Connection, days: int) -> None:
    rows = conn.execute(
        """
        SELECT
            side,
            COUNT(*) AS n,
            SUM(CASE
                    WHEN (side='BUY_YES' AND outcome='yes') OR (side='BUY_NO' AND outcome='no') THEN 1
                    ELSE 0
                END) AS wins,
            ROUND(AVG(CASE WHEN side='BUY_YES' THEN ev_yes ELSE ev_no END), 4) AS avg_model_ev,
            ROUND(SUM(realized_pnl), 4) AS pnl
        FROM outcome_reviews
        WHERE resolved_ts >= datetime('now', ?)
        GROUP BY side
        ORDER BY n DESC
        """,
        (f"-{days} days",),
    ).fetchall()
    print("=== Outcome Review Summary (by side) ===")
    if not rows:
        print("No rows found. Run: make record-outcomes")
        return
    for r in rows:
        n = int(r["n"])
        wins = int(r["wins"] or 0)
        wr = _safe_ratio(wins, n) * 100
        print(
            f"{r['side']:8s} n={n:4d} wins={wins:4d} "
            f"wr={wr:6.1f}% avg_ev={float(r['avg_model_ev'] or 0):+0.4f} pnl={float(r['pnl'] or 0):+0.4f}"
        )


def _print_by_tag(conn: sqlite3.Connection, days: int) -> None:
    rows = conn.execute(
        """
        SELECT
            tag,
            COUNT(*) AS n,
            SUM(CASE
                    WHEN (side='BUY_YES' AND outcome='yes') OR (side='BUY_NO' AND outcome='no') THEN 1
                    ELSE 0
                END) AS wins,
            ROUND(SUM(realized_pnl), 4) AS pnl
        FROM (
            SELECT side, outcome, realized_pnl,
                   trim(value) AS tag
            FROM outcome_reviews, json_each('["' || replace(tags, ',', '","') || '"]')
            WHERE resolved_ts >= datetime('now', ?)
              AND tags <> ''
        )
        GROUP BY tag
        ORDER BY n DESC
        """,
        (f"-{days} days",),
    ).fetchall()
    print("\n=== Outcome Review Summary (by tag) ===")
    if not rows:
        print("No tagged rows yet.")
        return
    for r in rows:
        n = int(r["n"])
        wins = int(r["wins"] or 0)
        wr = _safe_ratio(wins, n) * 100
        print(f"{r['tag']:15s} n={n:4d} wins={wins:4d} wr={wr:6.1f}% pnl={float(r['pnl'] or 0):+0.4f}")


def _print_by_event(conn: sqlite3.Connection, days: int) -> None:
    rows = conn.execute(
        """
        SELECT
            event_ticker,
            COUNT(*) AS n,
            SUM(CASE
                    WHEN (side='BUY_YES' AND outcome='yes') OR (side='BUY_NO' AND outcome='no') THEN 1
                    ELSE 0
                END) AS wins,
            ROUND(SUM(realized_pnl), 4) AS pnl
        FROM outcome_reviews
        WHERE resolved_ts >= datetime('now', ?)
        GROUP BY event_ticker
        ORDER BY pnl DESC
        """,
        (f"-{days} days",),
    ).fetchall()
    print("\n=== Outcome Review Summary (by event) ===")
    if not rows:
        print("No event rows yet.")
        return
    for r in rows:
        n = int(r["n"])
        wins = int(r["wins"] or 0)
        wr = _safe_ratio(wins, n) * 100
        evt = r["event_ticker"] or "(unknown)"
        print(f"{evt:30.30s} n={n:4d} wins={wins:4d} wr={wr:6.1f}% pnl={float(r['pnl'] or 0):+0.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Report realized outcome metrics from outcome_reviews table")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    conn = connect(DB_PATH)
    try:
        _print_summary(conn, args.days)
        _print_by_tag(conn, args.days)
        _print_by_event(conn, args.days)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
