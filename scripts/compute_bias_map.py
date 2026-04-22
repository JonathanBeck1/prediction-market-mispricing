#!/usr/bin/env python3
"""Compute market-maker bias map from resolved outcomes.

Identifies phrases where the market systematically OVERprices YES (and
we should buy NO) or UNDERprices YES (and we should buy YES).

Methodology:
  - For each (series, phrase), compute the empirical YES rate with Bayesian smoothing
  - Compare to the current Kalshi market YES ask price
  - Flag as OVERPRICED if empirical rate << market price  → buy NO edge
  - Flag as UNDERPRICED if empirical rate >> market price → buy YES edge (cross-check)

Output: data/bias_map.json  — loaded by the scorer as a structural signal.
        The bias_map provides a probability FLOOR for highly biased phrases
        (overpriced: p_override = empirical rate; underpriced: similar).

Usage:
    python3 scripts/compute_bias_map.py
    python3 scripts/compute_bias_map.py --min-n 12 --overpriced-threshold 0.20
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

OUTCOMES_PATH = Path("data/kalshi_outcomes.json")
MARKETS_PATH  = Path("data/kalshi_markets.json")
OUTPUT_PATH   = Path("data/bias_map.json")

GLOBAL_PRIOR       = 0.45
MIN_PSEUDO_SAMPLES = 8.0
DEFAULT_MIN_N      = 10    # min resolutions before trusting the rate
OVERPRICED_THRESH  = 0.18  # market YES ask > empirical rate + this → OVERPRICED
UNDERPRICED_THRESH = 0.18  # empirical rate > market YES ask + this → UNDERPRICED

# Series aliases: outcomes from legacy series are reused to price-match against
# the current live series.  Kalshi periodically renames/restructures series
# (e.g. KXTRUMPMENTIONB → KXTRUMPMENTION) while the phrase distributions
# stay the same.  When we have outcomes for the old series but live prices only
# for the new one, we bridge them here.
# Key = outcomes series, Value = live-pricing series to use for comparison.
SERIES_PRICE_ALIASES: dict[str, str] = {
    "KXTRUMPMENTIONB": "KXTRUMPMENTION",
    "KXPRESMENTION":   "KXTRUMPMENTION",  # KXPRESMENTION outcomes → KXTRUMPMENTION live prices
}


def _bayesian_rate(yes: int, total: int,
                   prior: float = GLOBAL_PRIOR,
                   pseudo: float = MIN_PSEUDO_SAMPLES) -> float:
    return (yes + pseudo * prior) / (total + pseudo)


def load_empirical_rates(min_n: int) -> dict[tuple[str, str], tuple[float, int]]:
    """Return {(series, phrase_lower): (empirical_rate, n)}."""
    payload = json.loads(OUTCOMES_PATH.read_text(encoding="utf-8"))
    markets = payload.get("markets", [])

    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"yes": 0, "no": 0})
    for m in markets:
        result = str(m.get("result", "")).strip().lower()
        if result not in ("yes", "no"):
            continue
        phrase = str(m.get("primary_phrase", "")).strip().lower()
        if not phrase or phrase in ("?", ""):
            continue
        series = str(m.get("series_ticker", "")).strip()
        if not series:
            continue
        counts[(series, phrase)][result] += 1

    out = {}
    for (series, phrase), c in counts.items():
        n = c["yes"] + c["no"]
        if n < min_n:
            continue
        rate = _bayesian_rate(c["yes"], n)
        out[(series, phrase)] = (round(rate, 4), n)
    return out


def load_live_prices() -> dict[tuple[str, str], float]:
    """Return {(series, phrase_lower): yes_ask} from live market cache."""
    if not MARKETS_PATH.exists():
        return {}
    payload = json.loads(MARKETS_PATH.read_text(encoding="utf-8"))
    markets = payload.get("markets", [])
    out: dict[tuple[str, str], float] = {}
    for m in markets:
        if m.get("status") not in ("open", "active"):
            continue
        series = str(m.get("series_ticker", "")).strip()
        phrase = str(m.get("primary_phrase", "")).strip().lower()
        ask = m.get("yes_ask_dollars") or m.get("yes_ask") or None
        if series and phrase and ask is not None:
            try:
                ask_f = float(ask)
                if 0.01 <= ask_f <= 0.99:
                    out[(series, phrase)] = round(ask_f, 3)
            except (TypeError, ValueError):
                pass
    return out


def compute(min_n: int, overpriced_thresh: float, underpriced_thresh: float) -> dict:
    empirical = load_empirical_rates(min_n)
    live_prices = load_live_prices()

    overpriced: list[dict] = []    # market YES ask >> empirical → BUY_NO edge
    underpriced: list[dict] = []   # empirical >> market YES ask → BUY_YES edge
    all_biases: list[dict] = []

    for (series, phrase), (emp_rate, n) in empirical.items():
        # Try direct price lookup first; fall back to aliased live series.
        live_ask = live_prices.get((series, phrase))
        live_series = series  # the series used for the live-price label
        if live_ask is None and series in SERIES_PRICE_ALIASES:
            alias = SERIES_PRICE_ALIASES[series]
            live_ask = live_prices.get((alias, phrase))
            if live_ask is not None:
                live_series = alias

        # Use the live-market series as the canonical key so the scorer can
        # look up by live market series_ticker (e.g. KXTRUMPMENTION, not B).
        canonical_series = live_series if (live_ask is not None and live_series != series) else series
        entry: dict = {
            "series": canonical_series,
            "source_series": series if canonical_series != series else None,
            "phrase": phrase,
            "empirical_rate": round(emp_rate, 4),
            "n": n,
            "live_ask": live_ask,
            "bias": "neutral",
            "bias_magnitude": 0.0,
            "action": None,
        }

        if live_ask is not None:
            gap = live_ask - emp_rate
            if gap >= overpriced_thresh:
                entry["bias"] = "overpriced"
                entry["bias_magnitude"] = round(gap, 4)
                entry["action"] = "BUY_NO"
                overpriced.append(entry)
            elif -gap >= underpriced_thresh:
                entry["bias"] = "underpriced"
                entry["bias_magnitude"] = round(-gap, 4)
                entry["action"] = "BUY_YES"
                underpriced.append(entry)
        else:
            # No live market right now — still useful for base rate reference
            if emp_rate <= 0.12:
                entry["bias"] = "historically_rare"
                entry["action"] = "BUY_NO_PREFERRED"

        all_biases.append(entry)

    overpriced.sort(key=lambda x: -x["bias_magnitude"])
    underpriced.sort(key=lambda x: -x["bias_magnitude"])
    all_biases.sort(key=lambda x: -abs(x["bias_magnitude"]))

    return {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "min_n": min_n,
        "overpriced_threshold": overpriced_thresh,
        "underpriced_threshold": underpriced_thresh,
        "total_phrases_analyzed": len(empirical),
        "overpriced_count": len(overpriced),
        "underpriced_count": len(underpriced),
        "overpriced": overpriced,
        "underpriced": underpriced,
        "all": all_biases,
    }


def _merge_with_previous(data: dict, previous_path: Path) -> dict:
    """Merge new computed entries with previously persisted entries.

    Strategy: new data (with live prices) always wins.  Old entries that lack
    a live price match in the new run are preserved — they still represent
    valid historical bias signals.  This prevents a maintenance run with a
    small outcomes file from wiping a large curated dataset.
    """
    if not previous_path.exists():
        return data

    try:
        prev = json.loads(previous_path.read_text(encoding="utf-8"))
    except Exception:
        return data

    # Index the new "all" entries by (series, phrase) so we can quickly check
    # whether the current run has an updated entry for a given pair.
    new_keys: set[tuple[str, str]] = {
        (e["series"], e["phrase"]) for e in data.get("all", [])
    }

    # Collect previous entries that are not covered by this run's results and
    # have meaningful bias data (overpriced / underpriced with live_ask).
    preserved: list[dict] = []
    for e in prev.get("all", []) + prev.get("overpriced", []) + prev.get("underpriced", []):
        key = (e.get("series", ""), e.get("phrase", ""))
        if key in new_keys:
            continue  # new data supersedes this entry
        if not e.get("live_ask"):
            continue  # no price anchor — not reliable enough to preserve
        if e.get("bias") not in ("overpriced", "underpriced"):
            continue  # only preserve actionable bias entries
        preserved.append(e)

    if not preserved:
        return data

    # Merge: new entries first, then preserved old entries
    merged_all = data.get("all", []) + preserved
    merged_all.sort(key=lambda x: -abs(x.get("bias_magnitude", 0)))

    merged_overpriced = [e for e in merged_all if e.get("bias") == "overpriced"]
    merged_underpriced = [e for e in merged_all if e.get("bias") == "underpriced"]

    # Deduplicate (keep first occurrence, which is the most recently computed)
    seen: set[tuple[str, str]] = set()
    deduped_all, deduped_op, deduped_up = [], [], []
    for e in merged_all:
        k = (e["series"], e["phrase"])
        if k not in seen:
            seen.add(k)
            deduped_all.append(e)
            if e.get("bias") == "overpriced":
                deduped_op.append(e)
            elif e.get("bias") == "underpriced":
                deduped_up.append(e)

    return {
        **data,
        "total_phrases_analyzed": len(deduped_all),
        "overpriced_count": len(deduped_op),
        "underpriced_count": len(deduped_up),
        "overpriced": deduped_op,
        "underpriced": deduped_up,
        "all": deduped_all,
        "preserved_from_previous": len(preserved),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute market-maker bias map")
    parser.add_argument("--min-n",               type=int,   default=DEFAULT_MIN_N)
    parser.add_argument("--overpriced-threshold", type=float, default=OVERPRICED_THRESH)
    parser.add_argument("--underpriced-threshold",type=float, default=UNDERPRICED_THRESH)
    parser.add_argument("--dry-run",             action="store_true")
    args = parser.parse_args()

    data = compute(args.min_n, args.overpriced_threshold, args.underpriced_threshold)
    data = _merge_with_previous(data, OUTPUT_PATH)

    print(f"Phrases analyzed (N >= {args.min_n}): {data['total_phrases_analyzed']}")
    print(f"Overpriced (BUY_NO edge):             {data['overpriced_count']}")
    print(f"Underpriced (BUY_YES edge):            {data['underpriced_count']}")

    if data["overpriced"]:
        print(f"\nTop OVERPRICED phrases (market YES ask >> empirical rate by >= {args.overpriced_threshold:.0%}):")
        print(f"{'Series':30s} {'Phrase':30s} {'N':5s} {'Empirical':9s} {'Market':7s} {'Gap':6s}")
        print("-" * 85)
        for entry in data["overpriced"][:25]:
            ask_str = f"{entry['live_ask']:.3f}" if entry['live_ask'] else "  n/a"
            print(
                f"{entry['series']:30s} {entry['phrase'][:30]:30s} {entry['n']:5d}"
                f" {entry['empirical_rate']:9.3f} {ask_str:7s} +{entry['bias_magnitude']:.3f}"
            )

    if data["underpriced"]:
        print(f"\nTop UNDERPRICED phrases (empirical rate >> market YES ask by >= {args.underpriced_threshold:.0%}):")
        print(f"{'Series':30s} {'Phrase':30s} {'N':5s} {'Empirical':9s} {'Market':7s} {'Gap':6s}")
        print("-" * 85)
        for entry in data["underpriced"][:15]:
            ask_str = f"{entry['live_ask']:.3f}" if entry['live_ask'] else "  n/a"
            print(
                f"{entry['series']:30s} {entry['phrase'][:30]:30s} {entry['n']:5d}"
                f" {entry['empirical_rate']:9.3f} {ask_str:7s} +{entry['bias_magnitude']:.3f}"
            )

    if args.dry_run:
        print("\n--dry-run: not writing output file.")
        return

    OUTPUT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
