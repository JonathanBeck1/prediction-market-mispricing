#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import connect
from app.outcome_tracker import build_outcome_rows, load_outcome_map, upsert_outcome_rows

DB_PATH = Path("data/edge.db")
OUTCOMES_PATH = Path("data/kalshi_outcomes.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record settled outcomes using decision-time BUY cards before market close"
    )
    parser.add_argument(
        "--decision-mode",
        choices=("first_buy", "last_buy", "best_ev"),
        default="first_buy",
        help="Card selection mode per market (default: first_buy)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing outcome_reviews rows before insert",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    if not OUTCOMES_PATH.exists():
        raise SystemExit(f"Outcomes cache not found: {OUTCOMES_PATH} (run make fetch-outcomes)")

    conn = connect(DB_PATH)
    try:
        outcome_map = load_outcome_map(OUTCOMES_PATH)
        if args.replace:
            conn.execute("DELETE FROM outcome_reviews")
            conn.commit()
        rows = build_outcome_rows(conn, outcome_map, decision_mode=args.decision_mode)
        inserted = upsert_outcome_rows(conn, rows)
    finally:
        conn.close()

    print(f"Decision mode: {args.decision_mode}")
    print(f"Replaced existing rows: {int(args.replace)}")
    print(f"Resolved outcomes available: {len(outcome_map)}")
    print(f"Candidate reviews built: {len(rows)}")
    print(f"New outcome review rows inserted: {inserted}")


if __name__ == "__main__":
    main()
