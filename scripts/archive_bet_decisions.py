#!/usr/bin/env python3
"""Archive the best BUY decision per market into bet_journal before pruning.

This script MUST be run before prune_snapshots.py wipes action_cards.
It preserves a single "canonical" bet record per (market_id, day) that
survives the 1-day action_card retention window, allowing outcome_tracker
to match bets against monthly/weekly contracts that resolve weeks later.

Run frequency: every 6 hours (scheduled via maintenance.py).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import connect

DB_PATH = Path("data/edge.db")


def _select_best_card(cards: list[dict]) -> dict:
    """Return the first BUY card for this market (earliest decision-time snapshot)."""
    return min(cards, key=lambda c: c["ts"])


def archive(conn) -> tuple[int, int]:
    """Upsert best-BUY-card per market into bet_journal.

    Returns (attempted, inserted_or_updated).
    """
    rows = conn.execute(
        """
        SELECT ts, market_id, phrase, side, p_literal, yes_ask, no_ask,
               ev_yes, ev_no, raw_json
        FROM   action_cards
        WHERE  side IN ('BUY_YES', 'BUY_NO')
        ORDER  BY market_id ASC, ts ASC
        """
    ).fetchall()

    if not rows:
        return 0, 0

    # Group by market_id and pick first BUY card per (market_id, date)
    by_market_date: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        d = dict(r)
        try:
            ts_date = d["ts"][:10]  # YYYY-MM-DD
        except Exception:
            ts_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        key = (str(d["market_id"]), ts_date)
        by_market_date.setdefault(key, []).append(d)

    inserted = 0
    for (market_id, archive_date), card_list in by_market_date.items():
        card = _select_best_card(card_list)
        raw: dict = {}
        try:
            raw = json.loads(card["raw_json"])
        except Exception:
            raw = {}

        reason_codes = raw.get("reason_codes", [])
        if isinstance(reason_codes, list):
            reason_codes_str = ",".join(reason_codes)
        else:
            reason_codes_str = str(reason_codes)

        conn.execute(
            """
            INSERT INTO bet_journal
                (market_id, archive_date, phrase, side, p_literal,
                 yes_ask, no_ask, ev_yes, ev_no, first_seen_ts, reason_codes, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (market_id, archive_date) DO NOTHING
            """,
            (
                market_id,
                archive_date,
                str(card.get("phrase", "")),
                str(card["side"]),
                float(card["p_literal"]),
                float(card["yes_ask"]),
                float(card["no_ask"]),
                float(card.get("ev_yes", 0.0)),
                float(card.get("ev_no", 0.0)),
                str(card["ts"]),
                reason_codes_str,
                json.dumps(raw),
            ),
        )
        inserted += conn.execute("SELECT changes()").fetchone()[0]

    conn.commit()
    return len(by_market_date), inserted


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    conn = connect(DB_PATH)
    # Ensure bet_journal table exists (schema migration)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bet_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            market_id TEXT NOT NULL,
            archive_date TEXT NOT NULL,
            phrase TEXT NOT NULL DEFAULT '',
            side TEXT NOT NULL,
            p_literal REAL NOT NULL,
            yes_ask REAL NOT NULL,
            no_ask REAL NOT NULL DEFAULT 0.0,
            ev_yes REAL NOT NULL DEFAULT 0.0,
            ev_no REAL NOT NULL DEFAULT 0.0,
            first_seen_ts TEXT NOT NULL,
            reason_codes TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE (market_id, archive_date)
        );
        CREATE INDEX IF NOT EXISTS idx_bet_journal_market ON bet_journal (market_id);
        CREATE INDEX IF NOT EXISTS idx_bet_journal_date ON bet_journal (archive_date);
        """
    )
    try:
        attempted, inserted = archive(conn)
    finally:
        conn.close()

    print(f"Bet decisions processed: {attempted}")
    print(f"New entries written:     {inserted}")
    print(f"Already present (skipped): {attempted - inserted}")


if __name__ == "__main__":
    main()
