#!/usr/bin/env python3
"""Compute empirical base rates from kalshi_outcomes.json and write to
config/base_rates_auto.yaml.

The auto file is loaded by BaseRateLookup AFTER the manual base_rates.yaml,
so empirical rates override manual guesses when enough data exists.

Bayesian smoothing: blends empirical rate with a global prior (0.45) using
a pseudo-count equal to MIN_PSEUDO_SAMPLES.  This prevents extreme rates
from very small samples while still honoring strong empirical signals.

Usage:
    python3 scripts/compute_base_rates.py
    python3 scripts/compute_base_rates.py --min-n 8   # minimum resolutions
    python3 scripts/compute_base_rates.py --dry-run   # print diff only
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML is required: pip install pyyaml")

OUTCOMES_PATH = Path("data/kalshi_outcomes.json")
OUTPUT_PATH   = Path("config/base_rates_auto.yaml")

GLOBAL_PRIOR      = 0.45   # prior mean for Bayesian smoothing
MIN_PSEUDO_SAMPLES = 8.0   # pseudo-count for prior  (equivalent observations)
DEFAULT_MIN_N      = 8     # minimum real observations before we emit a rate


def _bayesian_rate(yes: int, total: int, prior: float = GLOBAL_PRIOR,
                   pseudo: float = MIN_PSEUDO_SAMPLES) -> float:
    """Blend empirical rate with prior using pseudo-count smoothing."""
    return round((yes + pseudo * prior) / (total + pseudo), 4)


def compute_rates(outcomes_path: Path, min_n: int) -> dict[str, dict]:
    """Return nested dict: speaker → context → phrase → rate."""
    if not outcomes_path.exists():
        raise SystemExit(f"Outcomes file not found: {outcomes_path}")

    payload = json.loads(outcomes_path.read_text(encoding="utf-8"))
    markets = payload.get("markets", [])

    counts: dict[tuple, dict] = defaultdict(lambda: {"yes": 0, "no": 0})
    for m in markets:
        result = str(m.get("result", "")).strip().lower()
        if result not in ("yes", "no"):
            continue
        phrase = str(m.get("primary_phrase", "")).strip().lower()
        if not phrase or phrase in ("?", ""):
            continue
        speaker  = str(m.get("speaker", "")).strip().lower() or "auto"
        ctx      = str(m.get("event_context", "general")).strip().lower() or "general"
        counts[(speaker, ctx, phrase)][result] += 1

    # Build nested output
    out: dict[str, dict] = {}
    skipped_low_n = 0
    total_written = 0
    for (speaker, ctx, phrase), c in counts.items():
        n = c["yes"] + c["no"]
        if n < min_n:
            skipped_low_n += 1
            continue
        rate = _bayesian_rate(c["yes"], n)
        out.setdefault(speaker, {}).setdefault(ctx, {})[phrase] = rate
        total_written += 1

    print(f"Outcomes processed: {len(markets):,}")
    print(f"Groups with N >= {min_n}: {total_written}  (skipped {skipped_low_n} low-N)")
    return out


def diff_vs_manual(auto: dict, manual_path: Path) -> list[tuple]:
    """Return list of (speaker, ctx, phrase, manual_rate, auto_rate, delta) where |delta|>0.10."""
    if not manual_path.exists():
        return []
    manual: dict = yaml.safe_load(manual_path.read_text(encoding="utf-8")) or {}

    diffs = []
    for speaker, ctxs in auto.items():
        for ctx, phrases in ctxs.items():
            manual_ctxs = manual.get(speaker, {})
            for phrase, auto_rate in phrases.items():
                manual_rate = manual_ctxs.get(ctx, {}).get(phrase) if isinstance(manual_ctxs.get(ctx), dict) else None
                if manual_rate is None:
                    continue
                delta = auto_rate - float(manual_rate)
                if abs(delta) >= 0.10:
                    diffs.append((speaker, ctx, phrase, float(manual_rate), auto_rate, delta))
    diffs.sort(key=lambda x: -abs(x[5]))
    return diffs


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute empirical base rates from resolved outcomes")
    parser.add_argument("--min-n", type=int, default=DEFAULT_MIN_N,
                        help=f"Minimum resolutions required (default: {DEFAULT_MIN_N})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print diff vs manual rates; do not write output file")
    args = parser.parse_args()

    rates = compute_rates(OUTCOMES_PATH, args.min_n)

    manual_path = Path("config/base_rates.yaml")
    diffs = diff_vs_manual(rates, manual_path)
    if diffs:
        print(f"\nSignificant changes vs manual base_rates.yaml (|delta| >= 0.10):")
        print(f"{'Speaker':10s} {'Ctx':12s} {'Phrase':30s} {'Manual':7s}   {'Auto':6s}  Delta")
        print("-" * 75)
        for speaker, ctx, phrase, manual_r, auto_r, delta in diffs[:40]:
            sign = "+" if delta >= 0 else "-"
            print(f"{speaker:10s} {ctx:12s} {phrase[:30]:30s} {manual_r:.3f}  -> {auto_r:.3f}  {sign}{abs(delta):.3f}")
        if len(diffs) > 40:
            print(f"  ... and {len(diffs) - 40} more")

    if args.dry_run:
        print("\n--dry-run: not writing output file.")
        return

    # Add metadata header as a comment block via YAML dump
    header = (
        "# AUTO-GENERATED by scripts/compute_base_rates.py\n"
        "# DO NOT EDIT MANUALLY — re-run script to regenerate.\n"
        "# Loaded by BaseRateLookup AFTER base_rates.yaml so these override manual values.\n"
        f"# min_n={args.min_n}  prior={GLOBAL_PRIOR}  pseudo_count={MIN_PSEUDO_SAMPLES}\n"
        "#\n"
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    yaml_body = yaml.dump(rates, default_flow_style=False, sort_keys=True, allow_unicode=True)
    OUTPUT_PATH.write_text(header + yaml_body, encoding="utf-8")
    print(f"\nWrote {OUTPUT_PATH}")

    total_phrases = sum(len(phrases) for ctxs in rates.values() for phrases in ctxs.values())
    print(f"Total phrase entries written: {total_phrases}")


if __name__ == "__main__":
    main()
