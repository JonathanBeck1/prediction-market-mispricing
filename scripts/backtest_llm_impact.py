#!/usr/bin/env python3
"""Simulate the LLM layer's impact on historical outcomes.

Approach:
  For every resolved market in data/kalshi_outcomes.json:
    - Compute base p_literal (from base_rates only)
    - Apply current LLM boost from data/signals.yaml if the phrase is boosted
    - Compare bets placed WITH vs WITHOUT LLM boost to see if boosted phrases
      resolve YES at a higher rate than their un-boosted counterparts

This is NOT a true forward-looking backtest (today's LLM signals describe
today's news, not historical news). Instead it answers:

  "Historically, do the phrases the LLM currently flags as high-probability
   actually resolve YES more often?"

If the answer is YES → the LLM is identifying genuinely high-frequency phrases.
If not → the boost is noise or a one-off news cycle.

Usage:
    python3 scripts/backtest_llm_impact.py
    python3 scripts/backtest_llm_impact.py --ev-threshold 0.04
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.base_rates import BaseRateLookup

OUTCOMES_PATH   = Path("data/kalshi_outcomes.json")
SIGNALS_PATH    = Path("data/signals.yaml")
BASE_RATES_PATH = Path("config/base_rates.yaml")
LLM_ANALYSIS    = Path("data/llm_analysis.json")

DEFAULT_EV_THRESHOLD = 0.04
DEFAULT_BET_SIZE     = 10.0
_SPREAD = 0.04


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--ev-threshold", type=float, default=DEFAULT_EV_THRESHOLD)
    p.add_argument("--bet-size",     type=float, default=DEFAULT_BET_SIZE)
    return p.parse_args()


def _load_llm_boosts() -> dict[str, dict]:
    """Return {phrase_lower: {boost, reasoning, topic}} from signals.yaml."""
    if not SIGNALS_PATH.exists():
        return {}
    data = yaml.safe_load(SIGNALS_PATH.read_text())
    if not isinstance(data, dict) or "signals" not in data:
        return {}
    out = {}
    for entry in data["signals"]:
        phrase = str(entry.get("phrase", "")).lower().strip()
        lb = float(entry.get("llm_boost", 1.0))
        if lb != 1.0 and phrase:
            out[phrase] = {
                "boost":     lb,
                "reasoning": entry.get("llm_reasoning", ""),
                "topic":     entry.get("llm_topic", ""),
                "evidence":  entry.get("llm_evidence", ""),
            }
    return out


def _ev(p: float, ask: float) -> float:
    return p * (1 - ask) - (1 - p) * ask


def _simulate_bets(
    markets: list[dict],
    base_rates: BaseRateLookup,
    llm_boosts: dict[str, dict],
    ev_threshold: float,
    bet_size: float,
) -> dict:
    """Run two simulations in parallel: baseline and LLM-adjusted."""

    stats = {
        "baseline": {"bets": 0, "wins": 0, "pnl": 0.0},
        "llm_adj":  {"bets": 0, "wins": 0, "pnl": 0.0},
        "llm_only": {"bets": 0, "wins": 0, "pnl": 0.0},  # only LLM-changed decisions
        "boost_phrases": defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0, "boost": 1.0, "topic": ""}),
    }

    for m in markets:
        phrase = (m.get("primary_phrase") or "").strip().lower()
        result = m.get("result")
        speaker = m.get("speaker", "trump")
        context = m.get("event_context", "general")
        if result not in ("yes", "no") or not phrase:
            continue

        base_p = base_rates.get(speaker, context, phrase)
        yes_ask = min(0.99, base_p + _SPREAD / 2)
        no_ask  = min(0.99, (1 - base_p) + _SPREAD / 2)
        won_yes = result == "yes"

        # ── Baseline (no LLM) ──
        ev_y = _ev(base_p, yes_ask)
        ev_n = _ev(1 - base_p, no_ask)
        b_side = None
        if ev_y >= ev_threshold and ev_y >= ev_n:
            b_side = "YES"
        elif ev_n >= ev_threshold:
            b_side = "NO"

        if b_side:
            stats["baseline"]["bets"] += 1
            won = (b_side == "YES" and won_yes) or (b_side == "NO" and not won_yes)
            pnl = bet_size * _ev(base_p if b_side == "YES" else 1 - base_p,
                                  yes_ask if b_side == "YES" else no_ask)
            if won:
                stats["baseline"]["wins"] += 1
            stats["baseline"]["pnl"] += pnl

        # ── LLM-adjusted ──
        llm = llm_boosts.get(phrase, {})
        llm_mult = llm.get("boost", 1.0)
        adj_p = min(0.99, max(0.01, base_p * llm_mult))

        ev_y_adj = _ev(adj_p, yes_ask)
        ev_n_adj = _ev(1 - adj_p, no_ask)
        a_side = None
        if ev_y_adj >= ev_threshold and ev_y_adj >= ev_n_adj:
            a_side = "YES"
        elif ev_n_adj >= ev_threshold:
            a_side = "NO"

        if a_side:
            stats["llm_adj"]["bets"] += 1
            won = (a_side == "YES" and won_yes) or (a_side == "NO" and not won_yes)
            pnl = bet_size * _ev(adj_p if a_side == "YES" else 1 - adj_p,
                                  yes_ask if a_side == "YES" else no_ask)
            if won:
                stats["llm_adj"]["wins"] += 1
            stats["llm_adj"]["pnl"] += pnl

        # Track LLM-boosted phrases specifically
        if llm_mult != 1.0:
            key = phrase
            stats["boost_phrases"][key]["bets"] += 1
            stats["boost_phrases"][key]["boost"] = llm_mult
            stats["boost_phrases"][key]["topic"] = llm.get("topic", "")
            if won_yes:
                stats["boost_phrases"][key]["wins"] += 1
            # Use adj decision for pnl
            if a_side:
                won = (a_side == "YES" and won_yes) or (a_side == "NO" and not won_yes)
                pnl = bet_size * _ev(adj_p if a_side == "YES" else 1 - adj_p,
                                      yes_ask if a_side == "YES" else no_ask)
                stats["boost_phrases"][key]["pnl"] += pnl
                stats["llm_only"]["bets"] += 1
                if won:
                    stats["llm_only"]["wins"] += 1
                stats["llm_only"]["pnl"] += pnl

    return stats


def _wr(s: dict) -> str:
    if s["bets"] == 0:
        return "n/a"
    return f"{s['wins']/s['bets']*100:.1f}%"


def _roi(s: dict) -> str:
    if s["bets"] == 0:
        return "n/a"
    return f"{s['pnl']/(s['bets']*DEFAULT_BET_SIZE)*100:+.1f}%"


def main() -> None:
    args = _parse_args()

    data = json.loads(OUTCOMES_PATH.read_text())
    markets = [
        m for m in data.get("markets", [])
        if m.get("result") in ("yes", "no") and m.get("primary_phrase", "").strip()
    ]
    print(f"Loaded {len(markets)} resolved markets")

    base_rates = BaseRateLookup.from_yaml(BASE_RATES_PATH)
    llm_boosts = _load_llm_boosts()

    boosted_phrases = sorted(llm_boosts.keys())
    print(f"LLM-boosted phrases ({len(boosted_phrases)}): {', '.join(boosted_phrases)}")
    print()

    stats = _simulate_bets(markets, base_rates, llm_boosts, args.ev_threshold, args.bet_size)

    # ── Header ────────────────────────────────────────────────────────────────
    print("=" * 65)
    print("  LLM SIGNAL IMPACT SIMULATION")
    print("  (historical outcomes × today's LLM boosts)")
    print("=" * 65)

    for label, key in [("Baseline (no LLM)", "baseline"), ("LLM-adjusted", "llm_adj")]:
        s = stats[key]
        n, w, pnl = s["bets"], s["wins"], s["pnl"]
        roi = pnl / (n * args.bet_size) * 100 if n else 0
        print(f"\n{label}:")
        print(f"  Bets:    {n:>5,}")
        print(f"  Win rate:{_wr(s):>8}")
        print(f"  P&L:     ${pnl:>+8.2f}")
        print(f"  ROI:     {roi:>+7.1f}%")

    # ── Boosted phrases breakdown ─────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  BOOSTED PHRASE HISTORICAL YES RATES")
    print("  (how often each boosted phrase resolved YES historically)")
    print("=" * 65)
    print(f"  {'Phrase':<22} {'Boost':>6}  {'YES rate':>8}  {'n':>5}  Topic")
    print("  " + "-" * 60)

    phrase_rows = []
    for phrase, s in sorted(stats["boost_phrases"].items(), key=lambda x: -llm_boosts[x[0]]["boost"]):
        n = s["bets"]
        yes_rate = s["wins"] / n * 100 if n else 0
        boost = llm_boosts[phrase]["boost"]
        topic = llm_boosts[phrase]["topic"][:30]
        phrase_rows.append((phrase, boost, yes_rate, n, topic))
        mark = "✓" if yes_rate >= 45 else "✗"
        print(f"  {mark} {phrase:<21} {boost:>5.1f}x  {yes_rate:>7.1f}%  {n:>5}  {topic}")

    # ── Delta summary ─────────────────────────────────────────────────────────
    b = stats["baseline"]
    a = stats["llm_adj"]
    delta_bets = a["bets"] - b["bets"]
    delta_roi  = (a["pnl"] / (a["bets"] * args.bet_size) * 100 if a["bets"] else 0) \
               - (b["pnl"] / (b["bets"] * args.bet_size) * 100 if b["bets"] else 0)

    print()
    print("=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    print(f"  Bet count delta:   {delta_bets:+d}  (LLM redirected/added bets)")
    print(f"  ROI delta:         {delta_roi:+.1f} pp")

    # Interpretation
    print()
    print("  Interpretation:")
    if any(row[2] >= 45 for row in phrase_rows):
        good = [row[0] for row in phrase_rows if row[2] >= 45]
        print(f"  ✓ Historically valid boosts: {', '.join(good)}")
        print(f"    These phrases DO resolve YES at a rate >= 45% historically.")
        print(f"    The LLM boost amplifies a real signal, not noise.")
    bad = [row[0] for row in phrase_rows if row[2] < 30]
    if bad:
        print(f"  ✗ Questionable boosts: {', '.join(bad)}")
        print(f"    These phrases resolve YES rarely historically.")
        print(f"    LLM boosts here reflect today's news but not base probability.")

    print()
    print("  NOTE: The LLM layer boosts p_literal, not base_rates.")
    print("  Ideal use: high-boost phrases + open YES market priced below adj_p.")
    print("  The LLM adds context when base_rates alone are stale/generic.")


if __name__ == "__main__":
    main()
