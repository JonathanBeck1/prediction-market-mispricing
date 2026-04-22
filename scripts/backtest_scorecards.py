#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import connect

DB_PATH = Path("data/edge.db")


def _rate(wins: int, total: int) -> float:
    return 0.0 if total == 0 else (wins / total) * 100


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest scorecards from settled outcome_reviews")
    parser.add_argument("--days", type=int, default=90, help="Lookback in days")
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"DB missing: {DB_PATH}")

    conn = connect(DB_PATH)
    rows: list = []
    try:
        rows = conn.execute(
            """
            SELECT
                side,
                speaker,
                event_ticker,
                tags,
                raw_json,
                p_literal,
                yes_ask,
                no_ask,
                ev_yes,
                ev_no,
                outcome,
                realized_pnl
            FROM outcome_reviews
            WHERE resolved_ts >= datetime('now', ?)
            """,
            (f"-{args.days} days",),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No settled rows. Run: make record-outcomes")
        return

    total = len(rows)
    wins = 0
    pnl = 0.0
    for r in rows:
        won = (r["side"] == "BUY_YES" and r["outcome"] == "yes") or (
            r["side"] == "BUY_NO" and r["outcome"] == "no"
        )
        if won:
            wins += 1
        pnl += float(r["realized_pnl"])

    _print_header(f"Overall ({args.days}d)")
    print(f"rows={total} wins={wins} win_rate={_rate(wins, total):.1f}% pnl={pnl:+.4f}")

    _print_header("By side")
    for side in ("BUY_YES", "BUY_NO"):
        srows = [r for r in rows if r["side"] == side]
        swins = sum(
            1
            for r in srows
            if (r["side"] == "BUY_YES" and r["outcome"] == "yes")
            or (r["side"] == "BUY_NO" and r["outcome"] == "no")
        )
        spnl = sum(float(r["realized_pnl"]) for r in srows)
        print(f"{side:8s} rows={len(srows):4d} wins={swins:4d} wr={_rate(swins, len(srows)):6.1f}% pnl={spnl:+.4f}")

    _print_header("By confidence bucket (side-aware)")
    buckets = [
        ("0.00-0.20", 0.00, 0.20),
        ("0.20-0.40", 0.20, 0.40),
        ("0.40-0.60", 0.40, 0.60),
        ("0.60-0.80", 0.60, 0.80),
        ("0.80-1.00", 0.80, 1.01),
    ]

    def _chosen_confidence(row) -> float:
        p = float(row["p_literal"])
        return p if row["side"] == "BUY_YES" else (1.0 - p)

    for label, lo, hi in buckets:
        brows = [r for r in rows if lo <= _chosen_confidence(r) < hi]
        if not brows:
            continue
        bwins = sum(
            1
            for r in brows
            if (r["side"] == "BUY_YES" and r["outcome"] == "yes")
            or (r["side"] == "BUY_NO" and r["outcome"] == "no")
        )
        bpnl = sum(float(r["realized_pnl"]) for r in brows)
        print(f"{label:10s} rows={len(brows):4d} wins={bwins:4d} wr={_rate(bwins, len(brows)):6.1f}% pnl={bpnl:+.4f}")

    _print_header("By score confidence bucket")
    conf_buckets = [
        ("0.00-0.40", 0.00, 0.40),
        ("0.40-0.60", 0.40, 0.60),
        ("0.60-0.80", 0.60, 0.80),
        ("0.80-1.00", 0.80, 1.01),
    ]

    def _score_conf(row) -> float:
        raw = str(row["raw_json"] or "")
        if not raw:
            return 0.0
        try:
            payload = json.loads(raw)
        except Exception:
            return 0.0
        return float(payload.get("score_confidence", 0.0) or 0.0)

    for label, lo, hi in conf_buckets:
        crows = [r for r in rows if lo <= _score_conf(r) < hi]
        if not crows:
            continue
        cwins = sum(
            1
            for r in crows
            if (r["side"] == "BUY_YES" and r["outcome"] == "yes")
            or (r["side"] == "BUY_NO" and r["outcome"] == "no")
        )
        cpnl = sum(float(r["realized_pnl"]) for r in crows)
        print(f"{label:10s} rows={len(crows):4d} wins={cwins:4d} wr={_rate(cwins, len(crows)):6.1f}% pnl={cpnl:+.4f}")

    _print_header("By regime tag")
    tags = [
        "ON_TOPIC",
        "OFF_TOPIC",
        "MARKET_ANCHOR",
        "POLY_DIVERGE",
        "POLY_HIGHER",
        "POLY_LOWER",
        "SOURCE_AGREE_UP",
        "SOURCE_AGREE_DOWN",
        "SOURCE_CONFLICT",
    ]
    for tag in tags:
        trows = [r for r in rows if tag in str(r["tags"]).split(",")]
        if not trows:
            continue
        twins = sum(
            1
            for r in trows
            if (r["side"] == "BUY_YES" and r["outcome"] == "yes")
            or (r["side"] == "BUY_NO" and r["outcome"] == "no")
        )
        tpnl = sum(float(r["realized_pnl"]) for r in trows)
        print(f"{tag:15s} rows={len(trows):4d} wins={twins:4d} wr={_rate(twins, len(trows)):6.1f}% pnl={tpnl:+.4f}")

    # ── Retroactive simulation of new gates ──────────────────────────────────
    # Show what the current scoring rules would have decided vs what was
    # actually placed.  Does NOT require re-running the model — uses the
    # stored p_literal, yes_ask, and reason_codes to replicate gate logic.
    _print_header("Retroactive gate simulation (what new rules would block)")
    blocked_no_floor = []   # BUY_NO p_literal >= 0.40
    blocked_bearish  = []   # BUY_YES yes_ask < 0.12
    blocked_veto_no  = []   # BUY_NO market_divergence >= 0.10  (market veto)
    blocked_veto_yes = []   # BUY_YES market_divergence <= -0.10

    for r in rows:
        p   = float(r["p_literal"])
        ya  = float(r["yes_ask"] or 0)
        div = ya - p

        if r["side"] == "BUY_NO":
            if p >= 0.40:
                blocked_no_floor.append(r)
            elif div >= 0.10:
                blocked_veto_no.append(r)
        elif r["side"] == "BUY_YES":
            if ya < 0.12:
                blocked_bearish.append(r)
            elif -div >= 0.10:
                blocked_veto_yes.append(r)

    all_blocked_ids = set(
        id(r) for group in [blocked_no_floor, blocked_bearish, blocked_veto_no, blocked_veto_yes]
        for r in group
    )
    survivors = [r for r in rows if id(r) not in all_blocked_ids]

    def _gate_stats(label: str, group: list) -> None:
        if not group:
            return
        wins = sum(
            1 for r in group
            if (r["side"] == "BUY_YES" and r["outcome"] == "yes")
            or (r["side"] == "BUY_NO" and r["outcome"] == "no")
        )
        pnl = sum(float(r["realized_pnl"]) for r in group)
        print(f"  {label:30s} blocked={len(group):3d} wins_inside={wins:2d} ({_rate(wins, len(group)):.0f}%)  pnl_inside={pnl:+.2f}")

    print("  (gates applied in order; each row counted once)")
    _gate_stats("BUY_NO conviction floor (p>=0.40)", blocked_no_floor)
    _gate_stats("BUY_YES market-bearish (y_ask<0.12)", blocked_bearish)
    _gate_stats("Market veto BUY_NO (div>=0.10)",    blocked_veto_no)
    _gate_stats("Market veto BUY_YES (div<=-0.10)",  blocked_veto_yes)

    if survivors:
        swins = sum(
            1 for r in survivors
            if (r["side"] == "BUY_YES" and r["outcome"] == "yes")
            or (r["side"] == "BUY_NO" and r["outcome"] == "no")
        )
        spnl = sum(float(r["realized_pnl"]) for r in survivors)
        sroi = spnl / (len(survivors) * 10) * 100  # assume $10/bet
        print(f"\n  SURVIVING BETS after all gates:")
        print(f"    rows={len(survivors)} wins={swins} wr={_rate(swins, len(survivors)):.1f}% pnl={spnl:+.2f} roi={sroi:+.1f}%")


if __name__ == "__main__":
    main()
