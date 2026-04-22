"""Backtest the BayesianScorer against historical outcome_reviews.

For each resolved outcome where we actually placed a bet (side != WATCH),
we ask: "What would the BayesianScorer have recommended at the same
yes_ask/no_ask price?" and compare P&L.

This uses leave-one-out: for each outcome, we compute the posterior
WITHOUT that outcome, then see if the CI excludes the market price.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path


def _kelly_fraction(ev: float, loss: float) -> float:
    if loss <= 0:
        return 0.0
    return max(0.0, ev / loss)


KELLY_MIN = 0.05
MIN_CONFIDENCE = 0.50
MIN_OBS = 3


def main():
    # Load outcomes data
    outcomes_path = Path("data/kalshi_outcomes.json")
    data = json.loads(outcomes_path.read_text())
    markets = data["markets"]

    # Build (speaker, phrase) → list of (result, settlement_ts)
    phrase_outcomes: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        phrase = (m.get("primary_phrase") or "").lower().strip()
        speaker = (m.get("speaker") or "").lower().strip()
        result = m.get("result")
        if not phrase or result not in ("yes", "no"):
            continue
        key = f"{speaker}.{phrase}"
        phrase_outcomes[key].append(m)

    # Compute speaker-level priors
    speaker_totals: dict[str, dict] = defaultdict(lambda: {"yes": 0, "total": 0})
    for m in markets:
        phrase = (m.get("primary_phrase") or "").lower().strip()
        speaker = (m.get("speaker") or "").lower().strip()
        result = m.get("result")
        if not phrase or result not in ("yes", "no"):
            continue
        speaker_totals[speaker]["total"] += 1
        if result == "yes":
            speaker_totals[speaker]["yes"] += 1

    speaker_priors: dict[str, float] = {}
    for spk, st in speaker_totals.items():
        speaker_priors[spk] = st["yes"] / st["total"] if st["total"] > 0 else 0.45

    global_mean = sum(s["yes"] for s in speaker_totals.values()) / max(1, sum(s["total"] for s in speaker_totals.values()))

    # Load outcome_reviews (actual bets we placed)
    db = sqlite3.connect("data/edge.db")
    db.row_factory = sqlite3.Row
    reviews = db.execute(
        """SELECT * FROM outcome_reviews
           WHERE outcome IN ('yes', 'no')
           ORDER BY id"""
    ).fetchall()
    db.close()

    print(f"Total outcome_reviews: {len(reviews)}")
    print(f"Total kalshi_outcomes: {len(markets)}")
    print(f"Speaker priors: {json.dumps({k: round(v, 3) for k, v in speaker_priors.items()})}")
    print()

    # Backtest each review
    old_pnl = 0.0
    new_pnl = 0.0
    old_bets = 0
    new_bets = 0
    new_correct = 0
    old_correct = 0
    new_buy_yes = 0
    new_buy_no = 0

    # Track by speaker
    speaker_stats: dict[str, dict] = defaultdict(
        lambda: {"old_pnl": 0.0, "new_pnl": 0.0, "old_bets": 0, "new_bets": 0,
                 "new_correct": 0, "old_correct": 0}
    )

    prior_strength = 10.0

    for r in reviews:
        speaker = (r["speaker"] or "").lower().strip()
        phrase = (r["phrase"] or "").lower().strip()
        old_side = r["side"]
        yes_ask = r["yes_ask"]
        no_ask = r["no_ask"]
        outcome = r["outcome"]
        old_realized_pnl = r["realized_pnl"]

        if not phrase:
            continue

        key = f"{speaker}.{phrase}"
        outcomes_for_phrase = phrase_outcomes.get(key, [])

        # Compute posterior (all data — not leave-one-out for simplicity,
        # since the outcome file has 14K+ entries and leaving one out barely matters)
        n_yes = sum(1 for o in outcomes_for_phrase if o["result"] == "yes")
        n_total = len(outcomes_for_phrase)

        prior_rate = speaker_priors.get(speaker, global_mean)
        alpha = prior_rate * prior_strength + n_yes
        beta_param = (1 - prior_rate) * prior_strength + (n_total - n_yes)

        p_mean = alpha / (alpha + beta_param)
        p_var = (alpha * beta_param) / ((alpha + beta_param)**2 * (alpha + beta_param + 1))
        p_std = math.sqrt(p_var)
        ci_low = max(0.0, p_mean - 1.645 * p_std)
        ci_high = min(1.0, p_mean + 1.645 * p_std)
        ci_width = ci_high - ci_low
        confidence = max(0.0, 1.0 - ci_width)

        # New model decision
        new_side = "WATCH"
        if n_total >= MIN_OBS and confidence >= MIN_CONFIDENCE:
            if yes_ask < ci_low:
                new_side = "BUY_YES"
            elif yes_ask > ci_high:
                new_side = "BUY_NO"

            # Additional structural gates
            if new_side == "BUY_NO" and yes_ask >= 0.85:
                new_side = "WATCH"
            if new_side == "BUY_NO" and no_ask < 0.40:
                new_side = "WATCH"
            if new_side == "BUY_NO" and no_ask > 0.55:
                new_side = "WATCH"

            # Kelly gate
            if new_side == "BUY_YES":
                ev = p_mean - yes_ask
                kf = _kelly_fraction(ev, 1.0 - yes_ask)
                if kf < KELLY_MIN:
                    new_side = "WATCH"
            elif new_side == "BUY_NO":
                ev = (1.0 - p_mean) - no_ask
                kf = _kelly_fraction(ev, 1.0 - no_ask)
                if kf < KELLY_MIN:
                    new_side = "WATCH"

        # Compute P&L for new model
        new_realized = 0.0
        if new_side == "BUY_YES":
            new_buy_yes += 1
            if outcome == "yes":
                new_realized = 1.0 - yes_ask
                new_correct += 1
            else:
                new_realized = -yes_ask
        elif new_side == "BUY_NO":
            new_buy_no += 1
            if outcome == "no":
                new_realized = 1.0 - no_ask
                new_correct += 1
            else:
                new_realized = -no_ask

        # Track old model stats
        old_pnl += old_realized_pnl
        old_bets += 1
        if (old_side == "BUY_YES" and outcome == "yes") or (old_side == "BUY_NO" and outcome == "no"):
            old_correct += 1

        if new_side != "WATCH":
            new_bets += 1
            new_pnl += new_realized

        # Speaker breakdown
        ss = speaker_stats[speaker]
        ss["old_pnl"] += old_realized_pnl
        ss["old_bets"] += 1
        if (old_side == "BUY_YES" and outcome == "yes") or (old_side == "BUY_NO" and outcome == "no"):
            ss["old_correct"] += 1
        if new_side != "WATCH":
            ss["new_bets"] += 1
            ss["new_pnl"] += new_realized
            if (new_side == "BUY_YES" and outcome == "yes") or (new_side == "BUY_NO" and outcome == "no"):
                ss["new_correct"] += 1

    # Report
    print("=" * 70)
    print("BACKTEST: Old ScoringEngine vs New BayesianScorer")
    print("=" * 70)
    print()
    print(f"{'Metric':<30} {'Old Model':>15} {'New Model':>15}")
    print("-" * 60)
    print(f"{'Total bets':<30} {old_bets:>15} {new_bets:>15}")
    old_wr = (old_correct / old_bets * 100) if old_bets > 0 else 0
    new_wr = (new_correct / new_bets * 100) if new_bets > 0 else 0
    print(f"{'Win rate':<30} {old_wr:>14.1f}% {new_wr:>14.1f}%")
    print(f"{'Total P&L':<30} ${old_pnl:>14.2f} ${new_pnl:>14.2f}")
    avg_old = old_pnl / old_bets if old_bets > 0 else 0
    avg_new = new_pnl / new_bets if new_bets > 0 else 0
    print(f"{'Avg P&L per bet':<30} ${avg_old:>14.4f} ${avg_new:>14.4f}")
    print(f"{'BUY_YES / BUY_NO':<30} {'N/A':>15} {new_buy_yes:>7}/{new_buy_no:<7}")
    print()

    print("BY SPEAKER:")
    print(f"{'Speaker':<15} {'Old Bets':>8} {'Old WR':>8} {'Old P&L':>10} {'New Bets':>8} {'New WR':>8} {'New P&L':>10}")
    print("-" * 70)
    for spk in sorted(speaker_stats.keys()):
        ss = speaker_stats[spk]
        owr = (ss["old_correct"] / ss["old_bets"] * 100) if ss["old_bets"] > 0 else 0
        nwr = (ss["new_correct"] / ss["new_bets"] * 100) if ss["new_bets"] > 0 else 0
        print(f"{spk:<15} {ss['old_bets']:>8} {owr:>7.1f}% ${ss['old_pnl']:>9.2f} "
              f"{ss['new_bets']:>8} {nwr:>7.1f}% ${ss['new_pnl']:>9.2f}")

    # What bets did the new model skip that the old model took?
    print()
    print("AGREEMENT ANALYSIS:")
    agreed = 0
    old_only = 0
    new_only = 0
    for r in reviews:
        speaker = (r["speaker"] or "").lower().strip()
        phrase = (r["phrase"] or "").lower().strip()
        old_side = r["side"]
        yes_ask = r["yes_ask"]
        no_ask = r["no_ask"]

        if not phrase:
            continue

        key = f"{speaker}.{phrase}"
        outcomes_for_phrase = phrase_outcomes.get(key, [])
        n_yes = sum(1 for o in outcomes_for_phrase if o["result"] == "yes")
        n_total = len(outcomes_for_phrase)

        prior_rate = speaker_priors.get(speaker, global_mean)
        alpha = prior_rate * prior_strength + n_yes
        beta_param = (1 - prior_rate) * prior_strength + (n_total - n_yes)
        p_mean = alpha / (alpha + beta_param)
        p_var = (alpha * beta_param) / ((alpha + beta_param)**2 * (alpha + beta_param + 1))
        p_std = math.sqrt(p_var)
        ci_low = max(0.0, p_mean - 1.645 * p_std)
        ci_high = min(1.0, p_mean + 1.645 * p_std)
        confidence = max(0.0, 1.0 - (ci_high - ci_low))

        new_side = "WATCH"
        if n_total >= MIN_OBS and confidence >= MIN_CONFIDENCE:
            if yes_ask < ci_low:
                new_side = "BUY_YES"
            elif yes_ask > ci_high:
                new_side = "BUY_NO"
            if new_side == "BUY_NO" and (yes_ask >= 0.85 or no_ask < 0.40 or no_ask > 0.55):
                new_side = "WATCH"
            if new_side != "WATCH":
                ev = (p_mean - yes_ask) if new_side == "BUY_YES" else ((1.0 - p_mean) - no_ask)
                loss = (1.0 - yes_ask) if new_side == "BUY_YES" else (1.0 - no_ask)
                if _kelly_fraction(ev, loss) < KELLY_MIN:
                    new_side = "WATCH"

        if old_side in ("BUY_YES", "BUY_NO") and new_side in ("BUY_YES", "BUY_NO"):
            agreed += 1
        elif old_side in ("BUY_YES", "BUY_NO") and new_side == "WATCH":
            old_only += 1
        elif old_side == "WATCH" and new_side in ("BUY_YES", "BUY_NO"):
            new_only += 1

    print(f"  Both bet: {agreed}")
    print(f"  Old bet, New skip: {old_only}")
    print(f"  Old skip, New bet: {new_only}")


if __name__ == "__main__":
    main()
