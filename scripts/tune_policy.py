#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import connect

DB_PATH = Path("data/edge.db")
OUTPUT_PATH = Path("config/safe_mode.env")


def _won(side: str, outcome: str) -> bool:
    return (side == "BUY_YES" and outcome == "yes") or (side == "BUY_NO" and outcome == "no")


def _simulate(
    rows,
    ev_threshold: float,
    pre_event_yes_threshold: float,
    block_off_topic_yes: bool,
    penny_threshold: float,
):
    pnl = 0.0
    taken = 0
    wins = 0

    for r in rows:
        side = str(r["side"])
        yes_ask = float(r["yes_ask"])
        no_ask = float(r["no_ask"])
        ev_yes = float(r["ev_yes"])
        ev_no = float(r["ev_no"])
        tags = str(r["tags"]).split(",") if r["tags"] else []
        outcome = str(r["outcome"])
        raw_json = str(r["raw_json"] or "")
        reason_codes: list[str] = []
        speech_state = ""
        risk_adjusted_ev_yes = ev_yes
        risk_adjusted_ev_no = ev_no
        adaptive_floor = ev_threshold
        if raw_json:
            try:
                payload = json.loads(raw_json)
                reason_codes = [str(x) for x in payload.get("reason_codes", [])]
                ev = payload.get("event", {})
                if isinstance(ev, dict):
                    speech_state = str(ev.get("speech_state", ""))
                risk_adjusted_ev_yes = float(payload.get("risk_adjusted_ev_yes", ev_yes) or ev_yes)
                risk_adjusted_ev_no = float(payload.get("risk_adjusted_ev_no", ev_no) or ev_no)
                adaptive_floor = float(payload.get("effective_ev_threshold", ev_threshold) or ev_threshold)
            except Exception:
                reason_codes = []

        hit_today = "PHRASE_HIT" in reason_codes

        # Re-decide side under candidate thresholds.
        cand = "WATCH"
        eff_thr = max(ev_threshold, adaptive_floor)
        if risk_adjusted_ev_yes >= eff_thr and risk_adjusted_ev_yes >= risk_adjusted_ev_no:
            cand = "BUY_YES"
        elif risk_adjusted_ev_no >= eff_thr:
            cand = "BUY_NO"

        # Guardrails.
        if cand == "BUY_YES" and speech_state == "scheduled" and ev_yes < pre_event_yes_threshold:
            cand = "WATCH"
        if (
            cand == "BUY_YES"
            and speech_state == "scheduled"
            and block_off_topic_yes
            and ("OFF_TOPIC" in tags)
            and not hit_today
        ):
            cand = "WATCH"
        if cand in {"BUY_YES", "BUY_NO"} and not hit_today:
            if cand == "BUY_YES" and yes_ask <= penny_threshold:
                if not (("ON_TOPIC" in tags) or ("POLY_HIGHER" in tags)):
                    cand = "WATCH"
            if cand == "BUY_NO" and no_ask <= penny_threshold:
                if not (("OFF_TOPIC" in tags) or ("POLY_LOWER" in tags)):
                    cand = "WATCH"

        if cand == "WATCH":
            continue
        taken += 1
        if cand == "BUY_YES":
            trade_pnl = (1.0 - yes_ask) if outcome == "yes" else -yes_ask
        else:
            trade_pnl = (1.0 - no_ask) if outcome == "no" else -no_ask
        pnl += trade_pnl
        if _won(cand, outcome):
            wins += 1

    wr = (wins / taken) if taken else 0.0
    return {"pnl": pnl, "taken": taken, "wins": wins, "win_rate": wr}


def main() -> None:
    parser = argparse.ArgumentParser(description="Tune score policy thresholds from settled outcomes")
    parser.add_argument("--days", type=int, default=90, help="Lookback window")
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"DB not found: {DB_PATH}")

    conn = connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT side, yes_ask, no_ask, ev_yes, ev_no, tags, outcome, raw_json
            FROM outcome_reviews
            WHERE resolved_ts >= datetime('now', ?)
            """,
            (f"-{args.days} days",),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        raise SystemExit("No settled outcomes found. Run make record-outcomes first.")

    best = None
    for ev_threshold in (0.03, 0.04, 0.05, 0.06, 0.08):
        for yes_threshold in (0.04, 0.05, 0.06, 0.08, 0.10):
            for off_topic_block in (False, True):
                for penny_threshold in (0.0, 0.01, 0.02):
                    stats = _simulate(rows, ev_threshold, yes_threshold, off_topic_block, penny_threshold)
                    key = (stats["pnl"], stats["win_rate"], -stats["taken"])
                    if best is None or key > best["key"]:
                        best = {
                            "key": key,
                            "ev_threshold": ev_threshold,
                            "yes_threshold": yes_threshold,
                            "off_topic_block": off_topic_block,
                            "penny_threshold": penny_threshold,
                            "stats": stats,
                        }

    assert best is not None
    out = [
        "# Auto-generated by scripts/tune_policy.py",
        f"EV_THRESHOLD={best['ev_threshold']:.2f}",
        f"PRE_EVENT_YES_THRESHOLD={best['yes_threshold']:.2f}",
        f"BLOCK_OFF_TOPIC_YES={'1' if best['off_topic_block'] else '0'}",
        f"PENNY_PRICE_THRESHOLD={best['penny_threshold']:.2f}",
    ]
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")

    s = best["stats"]
    print("Best policy found:")
    print(
        f"EV={best['ev_threshold']:.2f} YES_PRE={best['yes_threshold']:.2f} "
        f"OFF_TOPIC_BLOCK={int(best['off_topic_block'])} PENNY={best['penny_threshold']:.2f}"
    )
    print(f"taken={s['taken']} wins={s['wins']} win_rate={s['win_rate']*100:.1f}% pnl={s['pnl']:+.4f}")
    print(f"Wrote preset: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
