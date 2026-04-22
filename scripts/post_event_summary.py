#!/usr/bin/env python3
"""Automated post-event postmortem and drift detector.

Runs every 30 min via MaintenanceRunner.  For each event that resolved in the
last 4 hours, it:

  1. Computes per-event win rate, P&L, and per-reason-code breakdown
  2. Compares against the backtest baseline (72.4% win rate, +20.8% ROI)
  3. Flags drift when win rate drops >15pp below baseline or a reason_code
     is systematically losing (win rate <40% with n>=5)
  4. Appends a structured report to data/logs/postmortem_YYYYMMDD.log
  5. Prints a concise one-liner per event to stdout (captured by MaintenanceRunner)

Drift signals written to data/drift_alerts.json for dashboard consumption.
"""
from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import connect

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

DB_PATH       = Path("data/edge.db")
LOG_DIR       = Path("data/logs")
ALERTS_PATH   = Path("data/drift_alerts.json")

# ── Baseline from backtest (session 28) ──────────────────────────────────────
BASELINE_WIN_RATE = 0.724   # 72.4%
BASELINE_ROI      = 0.208   # +20.8% per bet

# Drift thresholds
DRIFT_WIN_RATE_THRESHOLD  = 0.15   # flag if win_rate < baseline - 15pp
DRIFT_ROI_THRESHOLD       = -0.10  # flag if ROI < -10%
REASON_CODE_MIN_N         = 5      # min bets before flagging a reason code
REASON_CODE_BAD_WIN_RATE  = 0.40   # flag reason code win rate below this
LOOKBACK_HOURS            = 4      # look for events resolved in last N hours


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_ratio(a: int, b: int) -> float:
    return 0.0 if b == 0 else a / b


def _load_recent_reviews(conn, hours: float) -> list[dict]:
    cutoff = f"-{hours} hours"
    rows = conn.execute(
        """
        SELECT market_id, event_ticker, speaker, phrase, side,
               p_literal, yes_ask, no_ask, ev_yes, ev_no,
               outcome, realized_pnl, reason_codes, raw_json,
               resolved_ts, prediction_ts
        FROM outcome_reviews
        WHERE resolved_ts >= datetime('now', ?)
        ORDER BY event_ticker, resolved_ts
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def _extract_event_type(raw_json_str: str) -> str:
    try:
        raw = json.loads(raw_json_str)
        return raw.get("event", {}).get("event_type", "general")
    except Exception:
        return "general"


def _analyze_event(event_ticker: str, rows: list[dict]) -> dict:
    """Compute full postmortem metrics for a single event."""
    total = len(rows)
    wins  = sum(
        1 for r in rows
        if (r["side"] == "BUY_YES" and r["outcome"] == "yes")
        or (r["side"] == "BUY_NO"  and r["outcome"] == "no")
    )
    pnl   = sum(r["realized_pnl"] or 0.0 for r in rows)
    win_rate = _safe_ratio(wins, total)
    roi_per_bet = _safe_ratio(pnl, total)

    # Extract event_type from first row with raw_json
    event_type = "general"
    for r in rows:
        if r.get("raw_json"):
            event_type = _extract_event_type(r["raw_json"])
            break

    # Per-reason-code breakdown
    reason_stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for r in rows:
        codes = [c.strip() for c in (r.get("reason_codes") or "").split(",") if c.strip()]
        is_win = (r["side"] == "BUY_YES" and r["outcome"] == "yes") or \
                 (r["side"] == "BUY_NO"  and r["outcome"] == "no")
        for code in codes:
            reason_stats[code]["n"]    += 1
            reason_stats[code]["wins"] += int(is_win)
            reason_stats[code]["pnl"]  += r["realized_pnl"] or 0.0

    # Drift detection
    drift_flags: list[str] = []
    drift_score = 0.0

    if total >= 5:
        wr_gap = BASELINE_WIN_RATE - win_rate
        if wr_gap > DRIFT_WIN_RATE_THRESHOLD:
            drift_flags.append(
                f"WIN_RATE_BELOW_BASELINE ({win_rate:.0%} vs {BASELINE_WIN_RATE:.0%} baseline, gap={wr_gap:.0%})"
            )
            drift_score += wr_gap

        if roi_per_bet < DRIFT_ROI_THRESHOLD:
            drift_flags.append(f"NEGATIVE_ROI ({roi_per_bet:+.1%} per bet)")
            drift_score += abs(roi_per_bet)

    # Flag systematically bad reason codes
    bad_codes: list[str] = []
    for code, s in sorted(reason_stats.items(), key=lambda x: -x[1]["n"]):
        if s["n"] >= REASON_CODE_MIN_N:
            wr = _safe_ratio(s["wins"], s["n"])
            if wr < REASON_CODE_BAD_WIN_RATE:
                bad_codes.append(f"{code}({wr:.0%} wr, n={s['n']})")
    if bad_codes:
        drift_flags.append(f"BAD_REASON_CODES: {', '.join(bad_codes)}")
        drift_score += len(bad_codes) * 0.05

    # BSS vs market mid using real stored yes_ask/no_ask
    bss_vs_market = None
    try:
        model_preds  = [float(r["p_literal"]) for r in rows]
        market_preds = [(float(r["yes_ask"]) + (1.0 - float(r["no_ask"]))) / 2.0 for r in rows]
        actuals      = [int(r["outcome"] == "yes") for r in rows]
        bs_model  = sum((p - a) ** 2 for p, a in zip(model_preds, actuals)) / total
        bs_market = sum((p - a) ** 2 for p, a in zip(market_preds, actuals)) / total
        if bs_market > 0:
            bss_vs_market = round(1.0 - bs_model / bs_market, 4)
    except Exception:
        pass

    if bss_vs_market is not None and total >= 5:
        if bss_vs_market < -0.10:
            drift_flags.append(
                f"BSS_NEGATIVE ({bss_vs_market:+.3f} — market mid beats model)"
            )
            drift_score += abs(bss_vs_market) * 0.5

    # Best / worst individual bets
    sorted_bets = sorted(rows, key=lambda r: r["realized_pnl"] or 0.0)
    worst = sorted_bets[:3]
    best  = sorted_bets[-3:][::-1]

    return {
        "event_ticker":  event_ticker,
        "event_type":    event_type,
        "total_bets":    total,
        "wins":          wins,
        "win_rate":      round(win_rate, 4),
        "pnl":           round(pnl, 4),
        "roi_per_bet":   round(roi_per_bet, 4),
        "bss_vs_market": bss_vs_market,
        "drift_flags":   drift_flags,
        "drift_score":   round(drift_score, 3),
        "reason_stats": {k: {
            "n": v["n"],
            "win_rate": round(_safe_ratio(v["wins"], v["n"]), 3),
            "pnl": round(v["pnl"], 3),
        } for k, v in reason_stats.items()},
        "worst_bets":   [_summarize_bet(r) for r in worst],
        "best_bets":    [_summarize_bet(r) for r in best],
        "resolved_at":  max(r["resolved_ts"] for r in rows),
    }


def _summarize_bet(r: dict) -> dict:
    return {
        "market_id": r["market_id"],
        "phrase":    r["phrase"],
        "side":      r["side"],
        "p_literal": r["p_literal"],
        "yes_ask":   r["yes_ask"],
        "outcome":   r["outcome"],
        "pnl":       round(r["realized_pnl"] or 0.0, 3),
    }


def _format_report(summary: dict) -> str:
    lines = [
        f"{'='*65}",
        f"POST-EVENT POSTMORTEM: {summary['event_ticker']}",
        f"  context:    {summary['event_type']}",
        f"  resolved:   {summary['resolved_at']}",
        f"  bets:       {summary['total_bets']}  wins={summary['wins']}  "
        f"wr={summary['win_rate']:.1%}  pnl={summary['pnl']:+.2f}  "
        f"roi={summary['roi_per_bet']:+.1%}/bet",
        f"  baseline:   wr={BASELINE_WIN_RATE:.1%}  roi={BASELINE_ROI:+.1%}/bet",
        f"  BSS vs mkt: {summary['bss_vs_market']:+.4f}" if summary.get('bss_vs_market') is not None
        else "  BSS vs mkt: N/A (< 5 bets)",
    ]

    if summary["drift_flags"]:
        lines.append(f"\n  ⚠  DRIFT DETECTED:")
        for flag in summary["drift_flags"]:
            lines.append(f"     • {flag}")

    lines.append("\n  Reason-code breakdown:")
    rs = summary["reason_stats"]
    for code, s in sorted(rs.items(), key=lambda x: -x[1]["n"])[:10]:
        bar = "█" * int(s["win_rate"] * 10)
        lines.append(
            f"    {code:<30} n={s['n']:3d}  wr={s['win_rate']:.0%}  {bar}"
            f"  pnl={s['pnl']:+.2f}"
        )

    if summary["worst_bets"]:
        lines.append("\n  Worst bets:")
        for b in summary["worst_bets"]:
            lines.append(
                f"    {b['pnl']:+.2f}  {b['side']:<9} p={b['p_literal']:.2f}  "
                f"ask={b['yes_ask']:.2f}  [{b['phrase']}]  → {b['outcome']}"
            )

    if summary["best_bets"]:
        lines.append("\n  Best bets:")
        for b in summary["best_bets"]:
            lines.append(
                f"    {b['pnl']:+.2f}  {b['side']:<9} p={b['p_literal']:.2f}  "
                f"ask={b['yes_ask']:.2f}  [{b['phrase']}]  → {b['outcome']}"
            )

    lines.append("")
    return "\n".join(lines)


def _write_log(report_text: str, now: datetime) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"postmortem_{now.strftime('%Y%m%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n[{now.isoformat()}]\n")
        f.write(report_text)
    return log_path


def _update_drift_alerts(summaries: list[dict], now: datetime) -> None:
    """Write/update drift_alerts.json with any flagged events."""
    existing: dict = {}
    if ALERTS_PATH.exists():
        try:
            existing = json.loads(ALERTS_PATH.read_text())
        except Exception:
            pass

    alerts = existing.get("alerts", [])

    # Add new alerts for flagged events
    for s in summaries:
        if s["drift_flags"]:
            alerts.append({
                "event_ticker": s["event_ticker"],
                "event_type":   s["event_type"],
                "detected_at":  now.isoformat(),
                "win_rate":     s["win_rate"],
                "roi_per_bet":  s["roi_per_bet"],
                "total_bets":   s["total_bets"],
                "drift_score":  s["drift_score"],
                "flags":        s["drift_flags"],
            })

    # Keep only last 30 alerts
    alerts = alerts[-30:]

    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERTS_PATH.write_text(json.dumps({
        "generated_at": now.isoformat(),
        "baseline_win_rate": BASELINE_WIN_RATE,
        "baseline_roi": BASELINE_ROI,
        "alerts": alerts,
    }, indent=2))


def main() -> None:
    if not DB_PATH.exists():
        logger.warning("DB not found — skipping postmortem")
        return

    now = _now_utc()
    conn = connect(DB_PATH)

    try:
        rows = _load_recent_reviews(conn, hours=LOOKBACK_HOURS)
    finally:
        conn.close()

    if not rows:
        logger.info("No outcome_reviews resolved in last %dh — nothing to report", LOOKBACK_HOURS)
        return

    # Group by event_ticker
    by_event: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_event[r["event_ticker"] or "(unknown)"].append(r)

    summaries: list[dict] = []
    for event_ticker, event_rows in sorted(by_event.items()):
        summary = _analyze_event(event_ticker, event_rows)
        summaries.append(summary)

        report = _format_report(summary)
        log_path = _write_log(report, now)

        # Concise stdout line for MaintenanceRunner log
        drift_marker = "  ⚠ DRIFT" if summary["drift_flags"] else ""
        logger.info(
            "Postmortem %s: %d bets  wr=%.0f%%  pnl=%+.2f  roi=%+.1f%%%s",
            event_ticker,
            summary["total_bets"],
            summary["win_rate"] * 100,
            summary["pnl"],
            summary["roi_per_bet"] * 100,
            drift_marker,
        )
        if summary["drift_flags"]:
            for flag in summary["drift_flags"]:
                logger.warning("  DRIFT FLAG: %s", flag)

    _update_drift_alerts(summaries, now)
    logger.info("Postmortem complete — %d events, log → %s", len(summaries), log_path)


if __name__ == "__main__":
    main()
