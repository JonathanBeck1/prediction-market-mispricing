#!/usr/bin/env python3
"""Detect systematically losing signal patterns in recent outcome data.

Scans outcome_reviews for (reason_code, side) combinations that have
consistently poor win rates over recent bets. Writes regime_alerts.json
which the dashboard and scorer read to flag or suppress bad patterns.

Usage:
    python3 scripts/detect_regimes.py
    python3 scripts/detect_regimes.py --days 30 --min-bets 10 --max-wr 40
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect as _db_connect  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
DB_PATH   = REPO_ROOT / "data" / "edge.db"
OUTPUT    = REPO_ROOT / "data" / "regime_alerts.json"

# Candidate reason codes to scan for regime patterns
_CANDIDATE_CODES = [
    # Confidence/quality signals
    "SCORE_CONF_MED", "SCORE_CONF_LOW", "ADAPTIVE_THRESHOLD_RAISED",
    "LOW_DEPTH", "WIDE_SPREAD",
    # Signal-quality flags  
    "WALLET_LOW_CONF", "POLY_LOW_CONF", "ROLLING_N5", "ROLLING_N3",
    "POLY_DIVERGE", "POLY_HIGHER", "POLY_POOL",
    # Event-state
    "EVENT_ENDING_SOON", "WINDOW_ACTIVE_PACE",
    # LLM signals
    "LLM_SUPPRESS", "LLM_SUPPRESS_HIGH", "LLM_BOOST", "EVENT_LLM_SUPPRESS",
    # Newly added gates
    "HAZARD_MODEL", "SIGNAL_CORRELATION_CAPPED", "PRE_EVENT_EV_FILTER",
    # Bias map
    "BIAS_MAP_RATE", "BIAS_MAP_OVERPRICED",
]

# Combination pairs to evaluate (order matters for naming)
_COMBO_PAIRS = [
    ("SCORE_CONF_MED", "ADAPTIVE_THRESHOLD_RAISED"),
    ("SCORE_CONF_MED", "LOW_DEPTH"),
    ("SCORE_CONF_MED", "WIDE_SPREAD"),
    ("SCORE_CONF_HIGH", "LOW_DEPTH"),
    ("WALLET_LOW_CONF", "SCORE_CONF_HIGH"),
    ("BIAS_MAP_RATE", "SCORE_CONF_MED"),
    ("BIAS_MAP_OVERPRICED", "SCORE_CONF_MED"),
    ("ROLLING_N5", "POLY_DIVERGE"),
    ("EVENT_ENDING_SOON", "SCORE_CONF_MED"),
    ("LLM_SUPPRESS_HIGH", "SCORE_CONF_MED"),
]


def analyze_single_codes(conn: sqlite3.Connection, days: int, min_bets: int, max_wr: float) -> list[dict]:
    """Find individual reason codes with bad performance."""
    alerts: list[dict] = []

    for side in ("BUY_NO", "BUY_YES"):
        win_outcome = "no" if side == "BUY_NO" else "yes"
        for code in _CANDIDATE_CODES:
            rows = conn.execute(
                f"""SELECT COUNT(*) as n,
                    ROUND(100.0*SUM(CASE WHEN outcome=? THEN 1.0 ELSE 0.0 END)/COUNT(*),1) as wr,
                    ROUND(SUM(realized_pnl),2) as pnl,
                    ROUND(AVG(realized_pnl),3) as avg_pnl
                FROM outcome_reviews
                WHERE side=? AND reason_codes LIKE ?
                  AND resolved_ts > datetime('now', ?)
                """,
                (win_outcome, side, f"%{code}%", f"-{days} days"),
            ).fetchone()
            if rows and rows[0] >= min_bets and rows[1] is not None and rows[1] <= max_wr:
                alerts.append({
                    "type": "single_code",
                    "code": code,
                    "side": side,
                    "n_bets": rows[0],
                    "win_pct": rows[1],
                    "total_pnl": rows[2],
                    "avg_pnl": rows[3],
                    "days": days,
                    "severity": "high" if rows[1] <= 25 else "medium",
                })
    return alerts


def analyze_combo_codes(conn: sqlite3.Connection, days: int, min_bets: int, max_wr: float) -> list[dict]:
    """Find two-code combinations with bad performance."""
    alerts: list[dict] = []

    for side in ("BUY_NO", "BUY_YES"):
        win_outcome = "no" if side == "BUY_NO" else "yes"
        for code_a, code_b in _COMBO_PAIRS:
            rows = conn.execute(
                f"""SELECT COUNT(*) as n,
                    ROUND(100.0*SUM(CASE WHEN outcome=? THEN 1.0 ELSE 0.0 END)/COUNT(*),1) as wr,
                    ROUND(SUM(realized_pnl),2) as pnl,
                    ROUND(AVG(realized_pnl),3) as avg_pnl
                FROM outcome_reviews
                WHERE side=? AND reason_codes LIKE ? AND reason_codes LIKE ?
                  AND resolved_ts > datetime('now', ?)
                """,
                (win_outcome, side, f"%{code_a}%", f"%{code_b}%", f"-{days} days"),
            ).fetchone()
            if rows and rows[0] >= min_bets and rows[1] is not None and rows[1] <= max_wr:
                alerts.append({
                    "type": "combo",
                    "codes": [code_a, code_b],
                    "side": side,
                    "n_bets": rows[0],
                    "win_pct": rows[1],
                    "total_pnl": rows[2],
                    "avg_pnl": rows[3],
                    "days": days,
                    "severity": "high" if rows[1] <= 25 else "medium",
                })
    return alerts


def analyze_speaker_regimes(conn: sqlite3.Connection, days: int, min_bets: int, max_wr: float) -> list[dict]:
    """Find speaker-specific losing regimes."""
    alerts: list[dict] = []

    rows = conn.execute(
        """SELECT speaker, side,
            COUNT(*) as n,
            ROUND(100.0*SUM(CASE WHEN
              (side='BUY_NO' AND outcome='no') OR
              (side='BUY_YES' AND outcome='yes')
            THEN 1.0 ELSE 0.0 END)/COUNT(*),1) as wr,
            ROUND(SUM(realized_pnl),2) as pnl,
            ROUND(AVG(realized_pnl),3) as avg_pnl
        FROM outcome_reviews
        WHERE resolved_ts > datetime('now', ?)
          AND side IN ('BUY_NO','BUY_YES')
        GROUP BY speaker, side
        HAVING COUNT(*) >= ?
        ORDER BY pnl""",
        (f"-{days} days", min_bets),
    ).fetchall()

    for speaker, side, n, wr, pnl, avg_pnl in rows:
        if wr is not None and wr <= max_wr:
            alerts.append({
                "type": "speaker",
                "speaker": speaker,
                "side": side,
                "n_bets": n,
                "win_pct": wr,
                "total_pnl": pnl,
                "avg_pnl": avg_pnl,
                "days": days,
                "severity": "high" if wr <= 25 else "medium",
            })
    return alerts


def run(days: int = 30, min_bets: int = 10, max_wr: float = 45.0) -> None:
    conn = _db_connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    single = analyze_single_codes(conn, days, min_bets, max_wr)
    combos = analyze_combo_codes(conn, days, min_bets, max_wr)
    speakers = analyze_speaker_regimes(conn, days, min_bets, max_wr)
    conn.close()

    all_alerts = single + combos + speakers
    all_alerts.sort(key=lambda x: x.get("total_pnl", 0))  # worst first

    output = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "params": {"days": days, "min_bets": min_bets, "max_wr": max_wr},
        "total_alerts": len(all_alerts),
        "high_severity": sum(1 for a in all_alerts if a["severity"] == "high"),
        "alerts": all_alerts,
    }

    OUTPUT.write_text(json.dumps(output, indent=2))

    print(f"Regime detection: {len(all_alerts)} patterns found")
    print(f"High severity: {output['high_severity']}")
    print()

    if all_alerts:
        print(f"{'Pattern':<40} {'Side':<8} {'N':>4} {'WR%':>6} {'P&L':>8}")
        print("-" * 75)
        for a in all_alerts[:20]:
            if a["type"] == "single_code":
                pattern = a["code"]
            elif a["type"] == "combo":
                pattern = "+".join(a["codes"])
            else:
                pattern = f"speaker:{a.get('speaker','?')}"
            sev = " ⚠️" if a["severity"] == "high" else ""
            print(
                f"{pattern:<40} {a['side']:<8} {a['n_bets']:>4} "
                f"{a['win_pct']:>5.1f}% {a['total_pnl']:>8.2f}{sev}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--min-bets", type=int, default=10)
    parser.add_argument("--max-wr", type=float, default=45.0)
    args = parser.parse_args()
    run(days=args.days, min_bets=args.min_bets, max_wr=args.max_wr)
