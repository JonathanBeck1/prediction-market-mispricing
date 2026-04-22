#!/usr/bin/env python3
"""Grid search on historical outcome data to find optimal EV/Kelly/confidence thresholds.

Sweeps combinations of:
  - EV minimum threshold (ev_threshold)
  - Kelly minimum fraction (kelly_min)
  - Score confidence threshold for HIGH (above this = CONF_HIGH)
  - NO_EV_PREMIUM multiplier

Evaluates each combination on historical outcome_reviews using the
actual ev_yes/ev_no values stored at decision time.

Usage:
    python3 scripts/optimize_thresholds.py
    python3 scripts/optimize_thresholds.py --days 60 --output config/adaptive_thresholds.yaml
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
from app.db import connect as _db_connect  # noqa: E402
DB_PATH   = REPO_ROOT / "data" / "edge.db"
OUTPUT    = REPO_ROOT / "data" / "threshold_optimization.json"

# Grid search ranges
EV_THRESHOLDS     = [0.08, 0.10, 0.12, 0.15, 0.18]
KELLY_MINS        = [0.03, 0.05, 0.08, 0.10]
CONF_THRESHOLDS   = [0.65, 0.70, 0.75, 0.80]  # SCORE_CONF_HIGH cutoff
NO_EV_PREMIUMS    = [1.0, 1.25, 1.5, 1.75, 2.0]


def simulate_bets(
    outcomes: list[dict],
    ev_threshold: float,
    kelly_min: float,
    conf_threshold_high: float,
    no_ev_premium: float,
) -> dict:
    """Simulate bet selection with given thresholds."""
    bets: list[dict] = []
    for o in outcomes:
        ev_yes = o["ev_yes"]
        ev_no  = o["ev_no"]
        score_conf = o.get("score_conf", 0.5)
        side   = o["side"]
        outcome = o["outcome"]
        pnl    = o["realized_pnl"]
        kelly  = o.get("kelly_fraction", 0.0)

        # Apply threshold filters
        if kelly < kelly_min:
            continue
        if score_conf < conf_threshold_high and side == "BUY_NO":
            continue  # require CONF_HIGH for NO

        # EV thresholds
        if side == "BUY_YES" and ev_yes < ev_threshold:
            continue
        if side == "BUY_NO" and ev_no < ev_threshold * no_ev_premium:
            continue

        bets.append({
            "side": side, "outcome": outcome, "pnl": pnl,
        })

    if not bets:
        return {"n": 0, "win_pct": 0.0, "pnl": 0.0, "roi": 0.0}

    wins = sum(
        1 for b in bets
        if (b["side"] == "BUY_NO" and b["outcome"] == "no")
        or (b["side"] == "BUY_YES" and b["outcome"] == "yes")
    )
    total_pnl = sum(b["pnl"] for b in bets)
    return {
        "n": len(bets),
        "win_pct": round(100.0 * wins / len(bets), 1),
        "pnl": round(total_pnl, 2),
        "roi": round(total_pnl / (len(bets) * 10.0) * 100, 1),  # assuming $10/bet
    }


def run(days: int = 60) -> None:
    conn = _db_connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT side, outcome, realized_pnl, ev_yes, ev_no, p_literal,
                  reason_codes, raw_json
           FROM outcome_reviews
           WHERE resolved_ts > datetime('now', ?)
             AND side IN ('BUY_NO','BUY_YES')
        """,
        (f"-{days} days",),
    ).fetchall()
    conn.close()

    if not rows:
        print("No outcome data found")
        return

    # Parse outcomes with score_conf and kelly from raw_json
    outcomes: list[dict] = []
    for r in rows:
        try:
            data = json.loads(r["raw_json"] or "{}")
            scores = data.get("scores", {})
            score_conf = scores.get("score_confidence", 0.5)
            kelly = scores.get("kelly_fraction", 0.0)
        except Exception:
            score_conf = 0.5
            kelly = 0.05

        outcomes.append({
            "side": r["side"],
            "outcome": r["outcome"],
            "realized_pnl": r["realized_pnl"],
            "ev_yes": r["ev_yes"],
            "ev_no": r["ev_no"],
            "score_conf": score_conf,
            "kelly_fraction": kelly,
        })

    print(f"Optimizing over {len(outcomes)} outcomes from last {days} days...")
    print(f"Grid: {len(EV_THRESHOLDS)}×{len(KELLY_MINS)}×{len(CONF_THRESHOLDS)}×{len(NO_EV_PREMIUMS)} "
          f"= {len(EV_THRESHOLDS)*len(KELLY_MINS)*len(CONF_THRESHOLDS)*len(NO_EV_PREMIUMS)} combinations")
    print()

    results: list[dict] = []
    best_pnl  = -999.0
    best_roi  = -999.0
    best_params: dict | None = None

    for ev_t in EV_THRESHOLDS:
        for kelly_m in KELLY_MINS:
            for conf_t in CONF_THRESHOLDS:
                for no_prem in NO_EV_PREMIUMS:
                    stats = simulate_bets(outcomes, ev_t, kelly_m, conf_t, no_prem)
                    if stats["n"] < 20:
                        continue  # too few bets to trust

                    combo = {
                        "ev_threshold": ev_t,
                        "kelly_min": kelly_m,
                        "conf_high_threshold": conf_t,
                        "no_ev_premium": no_prem,
                        **stats,
                    }
                    results.append(combo)

                    # Pareto frontier: maximize P&L if win rate > 55%
                    if stats["win_pct"] >= 55 and stats["pnl"] > best_pnl:
                        best_pnl = stats["pnl"]
                        best_params = combo

    # Sort by P&L descending, show top results
    results.sort(key=lambda x: x["pnl"], reverse=True)

    print("Top 10 threshold combinations:")
    print(f"{'EV':>6} {'Kelly':>7} {'ConfH':>7} {'NoPrem':>8} {'N':>5} {'WR%':>6} {'P&L':>8} {'ROI%':>7}")
    print("-" * 65)
    for r in results[:10]:
        print(
            f"{r['ev_threshold']:>6.2f} {r['kelly_min']:>7.2f} "
            f"{r['conf_high_threshold']:>7.2f} {r['no_ev_premium']:>8.2f} "
            f"{r['n']:>5} {r['win_pct']:>5.1f}% {r['pnl']:>8.2f} {r['roi']:>6.1f}%"
        )

    if best_params:
        print()
        print("=== OPTIMAL PARAMETERS (P&L ≥ max, WR ≥ 55%) ===")
        for k, v in best_params.items():
            print(f"  {k}: {v}")

    output = {
        "optimized_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "days_analyzed": days,
        "n_outcomes": len(outcomes),
        "best_params": best_params,
        "top_results": results[:20],
    }
    OUTPUT.write_text(json.dumps(output, indent=2))
    print(f"\nFull results written to {OUTPUT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()
    run(days=args.days)
