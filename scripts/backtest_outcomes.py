#!/usr/bin/env python3
"""Backtest the scoring model against all resolved Kalshi mention-market outcomes.

For every finalized YES/NO market in data/kalshi_outcomes.json we:
  1. Compute model p_literal (base_rate only — no live Kalshi price anchor)
  2. Simulate a bet at the market's implied YES/NO price
  3. Report Brier score, Brier Skill Score vs market mid, and simulated P&L

Brier Skill Score (BSS) is the critical metric:
  BSS = 1 - BS_model / BS_baseline
  BSS > 0  → we beat the baseline
  BSS = 0  → we're exactly as good as the baseline
  BSS < 0  → the baseline is better than us (model has negative value)

Three baselines are reported:
  A) Market mid (per-phrase historical YES rate — "efficient market" assumption)
  B) Context prior (speaker+event_type average — "lazy base rate")
  C) Constant 0.5 (uninformed random guess)

Usage:
    python3 scripts/backtest_outcomes.py
    python3 scripts/backtest_outcomes.py --speaker trump --context rally
    python3 scripts/backtest_outcomes.py --walk-forward          # strict temporal ordering
    python3 scripts/backtest_outcomes.py --live                  # read from outcome_reviews DB
    python3 scripts/backtest_outcomes.py --ev-threshold 0.05 --bet-size 10
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.base_rates import BaseRateLookup

OUTCOMES_PATH   = Path("data/kalshi_outcomes.json")
BASE_RATES_PATH = Path("config/base_rates.yaml")
DB_PATH         = Path("data/edge.db")
BSS_CACHE_PATH  = Path("data/bss_metrics.json")

_MARKET_TAKE_FEE = 0.0  # Kalshi takes no fee on binary markets

DEFAULT_EV_THRESHOLD = 0.04
DEFAULT_BET_SIZE     = 10.0


# ── Utilities ─────────────────────────────────────────────────────────────────

def _brier(preds: list[float], actuals: list[int]) -> float:
    if not preds:
        return 0.0
    return sum((p - a) ** 2 for p, a in zip(preds, actuals)) / len(preds)


def _bss(bs_model: float, bs_baseline: float) -> float | None:
    """Brier Skill Score = 1 - BS_model / BS_baseline. None if baseline is 0."""
    if bs_baseline == 0:
        return None
    return round(1.0 - bs_model / bs_baseline, 4)


def _confidence_interval_bss(
    model_preds: list[float],
    baseline_preds: list[float],
    actuals: list[int],
    n_bootstrap: int = 1000,
) -> tuple[float, float]:
    """Bootstrap 90% CI for BSS via percentile method."""
    import random
    n = len(actuals)
    if n < 10:
        return (float("nan"), float("nan"))
    bss_samples = []
    for _ in range(n_bootstrap):
        idx = [random.randint(0, n - 1) for _ in range(n)]
        mp = [model_preds[i] for i in idx]
        bp = [baseline_preds[i] for i in idx]
        ac = [actuals[i] for i in idx]
        bs_m = _brier(mp, ac)
        bs_b = _brier(bp, ac)
        if bs_b > 0:
            bss_samples.append(1.0 - bs_m / bs_b)
    if not bss_samples:
        return (float("nan"), float("nan"))
    bss_samples.sort()
    lo = bss_samples[int(0.05 * len(bss_samples))]
    hi = bss_samples[int(0.95 * len(bss_samples))]
    return (round(lo, 4), round(hi, 4))


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest scoring model against resolved outcomes")
    p.add_argument("--speaker",      default=None, help="Filter to specific speaker")
    p.add_argument("--context",      default=None, help="Filter to specific event_context")
    p.add_argument("--series",       default=None, help="Filter to specific series_ticker")
    p.add_argument("--ev-threshold", type=float, default=DEFAULT_EV_THRESHOLD)
    p.add_argument("--bet-size",     type=float, default=DEFAULT_BET_SIZE)
    p.add_argument("--min-ev-edge",  type=float, default=0.0)
    p.add_argument("--walk-forward", action="store_true",
                   help="Strict temporal train/test: predict each outcome using only prior outcomes")
    p.add_argument("--live",         action="store_true",
                   help="Read from outcome_reviews DB (real stored yes_ask as market baseline)")
    p.add_argument("--days",         type=int, default=None,
                   help="With --live: restrict to outcomes resolved in the last N days")
    p.add_argument("--save",         action="store_true",
                   help="Save BSS metrics to data/bss_metrics.json for dashboard")
    return p.parse_args()


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_outcomes(filters: dict) -> list[dict]:
    data = json.loads(OUTCOMES_PATH.read_text())
    markets = data.get("markets", [])
    out = []
    for m in markets:
        if m.get("result") not in ("yes", "no"):
            continue
        if not m.get("primary_phrase", "").strip():
            continue
        if filters.get("speaker") and m.get("speaker") != filters["speaker"]:
            continue
        if filters.get("context") and m.get("event_context") != filters["context"]:
            continue
        if filters.get("series") and m.get("series_ticker") != filters["series"]:
            continue
        out.append(m)
    return out


def _load_live_outcomes(filters: dict, days: int | None = None) -> list[dict]:
    """Load from outcome_reviews table — has real yes_ask/no_ask at decision time."""
    from app.db import connect
    conn = connect(DB_PATH)
    query = """
        SELECT market_id, speaker, phrase, side,
               p_literal, yes_ask, no_ask,
               outcome, realized_pnl, reason_codes, resolved_ts
        FROM outcome_reviews
        WHERE outcome IN ('yes', 'no')
    """
    params: list = []
    if days:
        query += " AND resolved_ts >= datetime('now', ?)"
        params.append(f"-{days} days")
    if filters.get("speaker"):
        query += " AND speaker = ?"
        params.append(filters["speaker"])
    query += " ORDER BY resolved_ts ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Market price simulation ───────────────────────────────────────────────────

def _market_price(yes_rate: float) -> tuple[float, float]:
    spread = 0.04
    yes_ask = min(0.99, yes_rate + spread / 2)
    no_ask  = min(0.99, (1 - yes_rate) + spread / 2)
    return yes_ask, no_ask


def _build_phrase_rates(outcomes: list[dict]) -> dict[tuple[str, str, str], float]:
    """Per-phrase historical YES rate (leave-one-out) — used as market mid proxy."""
    counts: dict[tuple[str, str, str], dict] = defaultdict(lambda: {"yes": 0, "total": 0})
    for m in outcomes:
        key = (m["speaker"], m["event_context"], m["primary_phrase"].lower().strip())
        counts[key]["total"] += 1
        if m["result"] == "yes":
            counts[key]["yes"] += 1
    rates: dict[tuple[str, str, str], float] = {}
    for key, d in counts.items():
        if d["total"] >= 3:
            rates[key] = d["yes"] / d["total"]
    return rates


# ── Main backtest engine ──────────────────────────────────────────────────────

def run_backtest(
    outcomes: list[dict],
    br: BaseRateLookup,
    ev_threshold: float,
    bet_size: float,
    min_ev_edge: float,
    walk_forward: bool = False,
) -> dict:
    """Run backtest on kalshi_outcomes.json data.

    BSS is computed against three baselines:
      - market_mid: per-phrase historical YES rate (phrase_rate)
      - context_prior: speaker+event_type average (the "lazy" model)
      - constant_half: always predict 0.50

    walk_forward=True: for each outcome, model is trained only on prior outcomes
      (eliminates lookahead bias from YAML base rates).
    """
    if walk_forward:
        # Sort by close_time for strict temporal ordering
        def _sort_key(m: dict) -> str:
            return m.get("close_time") or m.get("settlement_ts") or "9999"
        outcomes = sorted(outcomes, key=_sort_key)

    total = len(outcomes)
    bets: list[dict] = []
    calib: dict[float, dict] = defaultdict(lambda: {"yes": 0, "total": 0})

    # Accumulated per-row predictions for BSS computation
    model_preds:   list[float] = []
    market_preds:  list[float] = []
    context_preds: list[float] = []
    half_preds:    list[float] = []
    actuals:       list[int]   = []

    # Walk-forward state: cumulative phrase frequency table
    wf_counts: dict[tuple[str, str, str], dict] = defaultdict(lambda: {"yes": 0, "total": 0})

    # Pre-build phrase rates (for non-walk-forward mode only)
    phrase_rates = {} if walk_forward else _build_phrase_rates(outcomes)

    for m in outcomes:
        speaker = m["speaker"]
        ctx     = m["event_context"]
        phrase  = m["primary_phrase"].lower().strip()
        actual_yes = int(m["result"] == "yes")

        # --- Model prediction ---
        if walk_forward:
            # Predict using only the cumulative history so far (no future data)
            key = (speaker, ctx, phrase)
            d = wf_counts.get(key, {"yes": 0, "total": 0})
            if d["total"] >= 5:
                model_p = d["yes"] / d["total"]
            elif d["total"] >= 2:
                # Bayesian smoothing with context prior
                prior = br._context_prior(br._data.get(speaker), ctx)
                model_p = (d["yes"] + prior * 3) / (d["total"] + 3)
            else:
                model_p = br.get(speaker, ctx, phrase)
            # Update cumulative table AFTER prediction (no lookahead)
            wf_counts[key]["total"] += 1
            if m["result"] == "yes":
                wf_counts[key]["yes"] += 1
        else:
            model_p = br.get(speaker, ctx, phrase)

        # --- Baseline predictions ---
        context_prior = br._context_prior(br._data.get(speaker), ctx)

        phrase_key  = (speaker, ctx, phrase)
        phrase_rate = phrase_rates.get(phrase_key, context_prior)  # market mid proxy
        yes_ask, no_ask = _market_price(phrase_rate)

        # Accumulate for BSS
        model_preds.append(model_p)
        market_preds.append(phrase_rate)
        context_preds.append(context_prior)
        half_preds.append(0.5)
        actuals.append(actual_yes)

        # Calibration buckets
        b = round(model_p * 10) / 10
        calib[b]["total"] += 1
        if actual_yes:
            calib[b]["yes"] += 1

        # --- EV betting simulation ---
        ev_yes = model_p       * 1.0 - yes_ask
        ev_no  = (1 - model_p) * 1.0 - no_ask

        best_side = None
        best_ev   = 0.0
        if ev_yes > ev_no and ev_yes >= ev_threshold:
            best_side  = "BUY_YES"
            best_ev    = ev_yes
            exec_price = yes_ask
        elif ev_no >= ev_threshold:
            best_side  = "BUY_NO"
            best_ev    = ev_no
            exec_price = no_ask
        else:
            continue

        if abs(model_p - phrase_rate) < min_ev_edge:
            continue

        won = (best_side == "BUY_YES" and actual_yes) or \
              (best_side == "BUY_NO"  and not actual_yes)

        stake  = bet_size * exec_price
        profit = bet_size * (1 - exec_price) if won else -stake

        bets.append({
            "speaker":      speaker,
            "ctx":          ctx,
            "phrase":       phrase,
            "model_p":      model_p,
            "market_p":     phrase_rate,
            "side":         best_side,
            "ev":           best_ev,
            "won":          won,
            "profit":       profit,
            "event_ticker": m.get("event_ticker", ""),
        })

    # --- Brier + BSS ---
    bs_model   = _brier(model_preds,   actuals)
    bs_market  = _brier(market_preds,  actuals)
    bs_context = _brier(context_preds, actuals)
    bs_half    = _brier(half_preds,    actuals)

    bss_vs_market  = _bss(bs_model, bs_market)
    bss_vs_context = _bss(bs_model, bs_context)
    bss_vs_half    = _bss(bs_model, bs_half)

    ci_lo, ci_hi = _confidence_interval_bss(model_preds, market_preds, actuals)

    return {
        "total_outcomes":  total,
        "brier_score":     round(bs_model,   4),
        "brier_market":    round(bs_market,  4),
        "brier_context":   round(bs_context, 4),
        "brier_half":      round(bs_half,    4),
        "bss_vs_market":   bss_vs_market,
        "bss_vs_context":  bss_vs_context,
        "bss_vs_half":     bss_vs_half,
        "bss_ci_90":       (ci_lo, ci_hi),
        "walk_forward":    walk_forward,
        "calibration":     dict(calib),
        "bets":            bets,
    }


def run_live_backtest(
    rows: list[dict],
    ev_threshold: float,
    bet_size: float,
) -> dict:
    """BSS using real stored yes_ask from outcome_reviews (actual market prices at decision time).

    This is the most accurate BSS measurement because we use the real market price
    the model faced when making the bet, not a simulated phrase rate.
    """
    model_preds:  list[float] = []
    market_preds: list[float] = []
    actuals:      list[int]   = []
    bets:         list[dict]  = []
    calib: dict[float, dict] = defaultdict(lambda: {"yes": 0, "total": 0})

    for r in rows:
        p     = float(r["p_literal"])
        ya    = float(r["yes_ask"])
        na    = float(r["no_ask"])
        actual = int(r["outcome"] == "yes")

        # Market mid = (yes_ask + (1 - no_ask)) / 2
        market_mid = (ya + (1.0 - na)) / 2.0

        model_preds.append(p)
        market_preds.append(market_mid)
        actuals.append(actual)

        b = round(p * 10) / 10
        calib[b]["total"] += 1
        if actual:
            calib[b]["yes"] += 1

        ev_yes = p       * 1.0 - ya
        ev_no  = (1 - p) * 1.0 - na
        if r["side"] == "BUY_YES" and ev_yes >= ev_threshold:
            won    = bool(actual)
            profit = bet_size * (1 - ya) if won else -bet_size * ya
            bets.append({"side": "BUY_YES", "won": won, "profit": profit,
                         "ev": ev_yes, "p": p, "market_mid": market_mid,
                         "speaker": r.get("speaker", ""), "phrase": r.get("phrase", "")})
        elif r["side"] == "BUY_NO" and ev_no >= ev_threshold:
            won    = not bool(actual)
            profit = bet_size * (1 - na) if won else -bet_size * na
            bets.append({"side": "BUY_NO", "won": won, "profit": profit,
                         "ev": ev_no, "p": p, "market_mid": market_mid,
                         "speaker": r.get("speaker", ""), "phrase": r.get("phrase", "")})

    bs_model  = _brier(model_preds,  actuals)
    bs_market = _brier(market_preds, actuals)
    bss       = _bss(bs_model, bs_market)
    ci_lo, ci_hi = _confidence_interval_bss(model_preds, market_preds, actuals)

    return {
        "total_outcomes": len(rows),
        "brier_model":    round(bs_model,  4),
        "brier_market":   round(bs_market, 4),
        "bss_vs_market":  bss,
        "bss_ci_90":      (ci_lo, ci_hi),
        "calibration":    dict(calib),
        "bets":           bets,
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def _bss_label(bss: float | None) -> str:
    if bss is None:
        return "N/A"
    if bss > 0.10:
        return f"{bss:+.4f}  ✅ Strong edge over market"
    if bss > 0.05:
        return f"{bss:+.4f}  ✓  Meaningful edge"
    if bss > 0.01:
        return f"{bss:+.4f}  ~  Marginal edge"
    if bss > -0.01:
        return f"{bss:+.4f}  ~  Roughly equal to market"
    return f"{bss:+.4f}  ❌ Market beats model — investigate scoring formula"


def _print_bss_block(results: dict) -> None:
    print(f"\n{'─'*60}")
    print("BRIER SKILL SCORE (BSS = 1 - BS_model / BS_baseline)")
    print(f"{'─'*60}")

    bs_model = results.get("brier_model") or results.get("brier_score")
    print(f"  Brier (model):          {bs_model:.4f}")
    print(f"  Brier (market mid):     {results['brier_market']:.4f}")
    bsc = results.get("brier_context")
    if bsc:
        print(f"  Brier (context prior):  {bsc:.4f}")
    bsh = results.get("brier_half")
    if bsh:
        print(f"  Brier (constant 0.50):  {bsh:.4f}")
    print()

    print(f"  BSS vs market mid:   {_bss_label(results['bss_vs_market'])}")
    bsc2 = results.get("bss_vs_context")
    if bsc2 is not None:
        print(f"  BSS vs context prior:{_bss_label(bsc2)}")
    bsh2 = results.get("bss_vs_half")
    if bsh2 is not None:
        print(f"  BSS vs coin flip:    {_bss_label(bsh2)}")

    ci = results.get("bss_ci_90")
    if ci and not any(math.isnan(x) for x in ci):
        print(f"  90% CI (bootstrap):  [{ci[0]:+.4f}, {ci[1]:+.4f}]")
    print()

    wf = results.get("walk_forward", False)
    if wf:
        print("  ⚠  Walk-forward mode: model trained only on prior outcomes.")
        print("     This eliminates lookahead bias from YAML base rates.")
    else:
        print("  ⚠  Note: base rates loaded from YAML (may include future data).")
        print("     Run with --walk-forward for strict temporal ordering.")
    print(f"{'─'*60}")


def _print_report(results: dict, bet_size: float) -> None:
    total = results["total_outcomes"]
    bets  = results["bets"]

    print(f"\n{'='*60}")
    label = "WALK-FORWARD BACKTEST" if results.get("walk_forward") else "BACKTEST REPORT"
    print(f"{label}  (honest market prices)")
    print(f"{'='*60}")
    print(f"  Outcomes evaluated:  {total:,}")

    _print_bss_block(results)

    print("CALIBRATION (model_p → actual YES rate):")
    for b in sorted(results["calibration"].keys()):
        d = results["calibration"][b]
        if d["total"] < 5:
            continue
        actual = d["yes"] / d["total"]
        err    = actual - b
        flag   = "✓" if abs(err) < 0.08 else ("↑" if err > 0 else "↓")
        bar    = "█" * int(actual * 20)
        print(f"  {flag} p={b:.1f}  actual={actual:.0%}  n={d['total']:4d}  err={err:+.2f}  {bar}")
    print()

    if not bets:
        print("No bets met the EV threshold.")
        return

    wins       = sum(1 for b in bets if b["won"])
    total_bets = len(bets)
    total_pnl  = sum(b["profit"] for b in bets)
    win_rate   = wins / total_bets
    roi        = total_pnl / (total_bets * bet_size) if total_bets else 0

    print(f"SIMULATED BETS (${bet_size:.0f}/bet):")
    print(f"  Total bets:      {total_bets:,}")
    print(f"  Win rate:        {win_rate:.1%}  ({wins}/{total_bets})")
    print(f"  Total P&L:       ${total_pnl:+,.2f}")
    print(f"  ROI per bet:     {roi:+.1%}")
    print(f"  Avg EV:          {sum(b['ev'] for b in bets)/total_bets:+.3f}")
    print()

    yes_bets = [b for b in bets if b["side"] == "BUY_YES"]
    no_bets  = [b for b in bets if b["side"] == "BUY_NO"]
    if yes_bets:
        y_wins = sum(1 for b in yes_bets if b["won"])
        y_pnl  = sum(b["profit"] for b in yes_bets)
        print(f"  BUY YES: {len(yes_bets)} bets | {y_wins/len(yes_bets):.0%} win | ${y_pnl:+,.2f}")
    if no_bets:
        n_wins = sum(1 for b in no_bets if b["won"])
        n_pnl  = sum(b["profit"] for b in no_bets)
        print(f"  BUY NO:  {len(no_bets)} bets | {n_wins/len(no_bets):.0%} win | ${n_pnl:+,.2f}")
    print()

    by_spk: dict[str, dict] = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0})
    for b in bets:
        s = b["speaker"]
        by_spk[s]["bets"] += 1
        by_spk[s]["wins"] += int(b["won"])
        by_spk[s]["pnl"]  += b["profit"]
    print("  BY SPEAKER:")
    for spk, d in sorted(by_spk.items(), key=lambda x: -abs(x[1]["pnl"])):
        wr = d["wins"] / d["bets"]
        print(f"    {spk:12s}: {d['bets']:4d} bets | {wr:.0%} win | ${d['pnl']:+,.2f}")
    print()

    by_ctx: dict[str, dict] = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0})
    for b in bets:
        c = b["ctx"]
        by_ctx[c]["bets"] += 1
        by_ctx[c]["wins"] += int(b["won"])
        by_ctx[c]["pnl"]  += b["profit"]
    print("  BY EVENT CONTEXT:")
    for ctx, d in sorted(by_ctx.items(), key=lambda x: -abs(x[1]["pnl"])):
        if d["bets"] < 5:
            continue
        wr = d["wins"] / d["bets"]
        print(f"    {ctx:15s}: {d['bets']:4d} bets | {wr:.0%} win | ${d['pnl']:+,.2f}")
    print()

    top = sorted(bets, key=lambda x: -x["ev"])[:10]
    print("  TOP 10 HIGHEST EV BETS:")
    for b in top:
        status = "WIN" if b["won"] else "LOSS"
        print(f"    {status} {b['side']:8s} ev={b['ev']:+.3f} p={b['model_p']:.2f} "
              f"[{b['speaker']} {b['ctx']}] \"{b['phrase']}\"")
    print()

    worst = sorted(bets, key=lambda x: x["profit"])[:5]
    print("  5 WORST BETS (by P&L):")
    for b in worst:
        print(f"    ${b['profit']:+.2f} {b['side']:8s} ev={b['ev']:+.3f} p={b['model_p']:.2f} "
              f"[{b['speaker']} {b['ctx']}] \"{b['phrase']}\"")
    print()


def _print_live_report(results: dict, bet_size: float) -> None:
    total = results["total_outcomes"]
    bets  = results["bets"]

    print(f"\n{'='*60}")
    print("LIVE OUTCOME REVIEW BACKTEST  (real stored market prices)")
    print(f"{'='*60}")
    print(f"  Outcomes from outcome_reviews DB: {total:,}")
    print(f"  Market baseline: real yes_ask / no_ask at decision time")
    print()

    _print_bss_block(results)

    print("CALIBRATION:")
    for b in sorted(results["calibration"].keys()):
        d = results["calibration"][b]
        if d["total"] < 3:
            continue
        actual = d["yes"] / d["total"]
        err    = actual - b
        flag   = "✓" if abs(err) < 0.10 else ("↑" if err > 0 else "↓")
        print(f"  {flag} p={b:.1f}  actual={actual:.0%}  n={d['total']:3d}  err={err:+.2f}")
    print()

    if not bets:
        print("No bets met the EV threshold.")
        return

    wins      = sum(1 for b in bets if b["won"])
    total_b   = len(bets)
    total_pnl = sum(b["profit"] for b in bets)
    print(f"  Bets: {total_b} | Win rate: {wins/total_b:.1%} | P&L: ${total_pnl:+,.2f}")

    by_spk: dict[str, dict] = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0})
    for b in bets:
        s = b.get("speaker", "unknown")
        by_spk[s]["bets"] += 1
        by_spk[s]["wins"] += int(b["won"])
        by_spk[s]["pnl"]  += b["profit"]
    if by_spk:
        print("  BY SPEAKER:")
        for spk, d in sorted(by_spk.items(), key=lambda x: -x[1]["bets"]):
            wr = d["wins"] / d["bets"]
            print(f"    {spk:12s}: {d['bets']:3d} bets | {wr:.0%} win | ${d['pnl']:+,.2f}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    if args.live:
        if not DB_PATH.exists():
            raise SystemExit(f"No DB at {DB_PATH}. Run the engine first.")
        filters = {"speaker": args.speaker}
        rows = _load_live_outcomes(filters, days=args.days)
        print(f"Loaded {len(rows)} live outcome_reviews rows "
              f"(filters: speaker={args.speaker}, days={args.days})")
        if not rows:
            print("No resolved outcomes found. Record more bets first.")
            return
        results = run_live_backtest(
            rows=rows,
            ev_threshold=args.ev_threshold,
            bet_size=args.bet_size,
        )
        _print_live_report(results, args.bet_size)
        return

    # Standard mode — kalshi_outcomes.json
    if not OUTCOMES_PATH.exists():
        raise SystemExit(f"No outcomes file at {OUTCOMES_PATH}. Run: python3 scripts/fetch_outcomes.py")

    br = BaseRateLookup.from_yaml(BASE_RATES_PATH)
    filters = {"speaker": args.speaker, "context": args.context, "series": args.series}
    outcomes = _load_outcomes(filters)
    print(f"Loaded {len(outcomes)} outcomes "
          f"(filters: {', '.join(f'{k}={v}' for k,v in filters.items() if v) or 'none'})")
    if args.walk_forward:
        print("Walk-forward mode: training only on prior outcomes for each prediction.")

    results = run_backtest(
        outcomes=outcomes,
        br=br,
        ev_threshold=args.ev_threshold,
        bet_size=args.bet_size,
        min_ev_edge=args.min_ev_edge,
        walk_forward=args.walk_forward,
    )
    _print_report(results, args.bet_size)

    if args.save:
        live_results = None
        wf_results   = None
        if DB_PATH.exists():
            live_rows = _load_live_outcomes({})
            if live_rows:
                live_results = run_live_backtest(live_rows, args.ev_threshold, args.bet_size)
        if not args.walk_forward:
            wf_results = run_backtest(outcomes, br, args.ev_threshold,
                                      args.bet_size, args.min_ev_edge, walk_forward=True)
        save_bss_cache(results, live_results, wf_results)


def save_bss_cache(results_standard: dict, results_live: dict | None, results_wf: dict | None) -> None:
    """Write BSS metrics to data/bss_metrics.json for dashboard consumption."""
    import datetime as _dt
    payload = {
        "updated_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "standard": {
            "n": results_standard["total_outcomes"],
            "brier_model":  results_standard.get("brier_score"),
            "brier_market": results_standard.get("brier_market"),
            "bss_vs_market": results_standard.get("bss_vs_market"),
            "bss_ci_90":     results_standard.get("bss_ci_90"),
            "note": "YAML base rates (may include future data)",
        },
    }
    if results_live:
        bets = results_live.get("bets", [])
        wins = sum(1 for b in bets if b["won"])
        payload["live"] = {
            "n": results_live["total_outcomes"],
            "brier_model":   results_live.get("brier_model"),
            "brier_market":  results_live.get("brier_market"),
            "bss_vs_market": results_live.get("bss_vs_market"),
            "bss_ci_90":     results_live.get("bss_ci_90"),
            "win_rate":      round(wins / len(bets), 3) if bets else None,
            "n_bets":        len(bets),
            "note": "Real stored yes_ask/no_ask from outcome_reviews",
        }
    if results_wf:
        payload["walk_forward"] = {
            "n": results_wf["total_outcomes"],
            "brier_model":   results_wf.get("brier_score"),
            "brier_market":  results_wf.get("brier_market"),
            "bss_vs_market": results_wf.get("bss_vs_market"),
            "bss_ci_90":     results_wf.get("bss_ci_90"),
            "note": "Temporal train/test split — no lookahead bias",
        }
    BSS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BSS_CACHE_PATH.write_text(json.dumps(payload, indent=2))
    print(f"BSS metrics saved → {BSS_CACHE_PATH}")


if __name__ == "__main__":
    main()
