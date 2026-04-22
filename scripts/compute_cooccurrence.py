#!/usr/bin/env python3
"""Compute phrase co-occurrence lift scores from resolved Kalshi outcomes.

For each pair (phrase_A, phrase_B) within the same event, computes:
  lift = P(B=YES | A=YES) / P(B=YES)

If lift >> 1: phrase A being said strongly predicts phrase B will also be said.
If lift << 1: phrase A being said suppresses phrase B (topic shift).

Output: data/phrase_cooccurrence.json

The scorer reads this at startup and applies a conditional boost/suppression
to any phrase whose co-occurrence partner has already resolved YES in the
same event window.

Usage:
    python3 scripts/compute_cooccurrence.py
    python3 scripts/compute_cooccurrence.py --min-n 6 --min-lift 1.5
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

OUTCOMES_PATH  = Path("data/kalshi_outcomes.json")
OUTPUT_PATH    = Path("data/phrase_cooccurrence.json")

DEFAULT_MIN_N    = 6    # min joint observations for a pair
DEFAULT_MIN_LIFT = 1.4  # min lift to record as a positive signal
DEFAULT_MAX_LIFT = 0.6  # max lift to record as a negative signal (suppression)


def compute(min_n: int, min_lift: float, max_lift: float) -> dict:
    payload = json.loads(OUTCOMES_PATH.read_text(encoding="utf-8"))
    markets = [
        m for m in payload.get("markets", [])
        if m.get("result") in ("yes", "no") and m.get("primary_phrase")
    ]

    # Group by event_ticker
    events: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        et = str(m.get("event_ticker", "")).strip()
        if et:
            events[et].append(m)

    # Count joint outcomes for each phrase pair within the same event
    # key: (speaker, phrase_a_lower, phrase_b_lower)
    joint: dict[tuple, dict[str, int]] = defaultdict(
        lambda: {"aa_yes_bb_yes": 0, "aa_yes_bb_no": 0, "aa_no_bb_yes": 0, "aa_no_bb_no": 0}
    )

    for et, mlist in events.items():
        if len(mlist) < 2:
            continue
        speaker = str(mlist[0].get("speaker", "")).lower().strip()
        # Build (phrase → result) map for this event
        phrase_results: dict[str, str] = {}
        for m in mlist:
            p = str(m.get("primary_phrase", "")).lower().strip()
            if p:
                phrase_results[p] = m.get("result", "no")

        for pa, pb in itertools.permutations(phrase_results.keys(), 2):
            key = (speaker, pa, pb)
            ra = phrase_results[pa]
            rb = phrase_results[pb]
            slot = f"{'aa_yes' if ra == 'yes' else 'aa_no'}_{'bb_yes' if rb == 'yes' else 'bb_no'}"
            joint[key][slot] += 1

    # Compute lift for qualifying pairs
    results: list[dict] = []
    for (speaker, pa, pb), c in joint.items():
        n = sum(c.values())
        if n < min_n:
            continue

        a_yes_total = c["aa_yes_bb_yes"] + c["aa_yes_bb_no"]
        b_yes_total = c["aa_yes_bb_yes"] + c["aa_no_bb_yes"]

        if a_yes_total == 0 or n == 0:
            continue

        cond_p    = c["aa_yes_bb_yes"] / a_yes_total   # P(B=yes | A=yes)
        marginal_p = b_yes_total / n                     # P(B=yes)

        if marginal_p == 0:
            continue

        lift = cond_p / marginal_p

        if lift >= min_lift or lift <= max_lift:
            results.append(
                {
                    "speaker":    speaker,
                    "phrase_a":   pa,
                    "phrase_b":   pb,
                    "lift":       round(lift, 3),
                    "cond_p":     round(cond_p, 3),
                    "marginal_p": round(marginal_p, 3),
                    "n":          n,
                    "n_a_yes":    a_yes_total,
                }
            )

    # Sort by abs(lift - 1) descending
    results.sort(key=lambda x: -abs(x["lift"] - 1))

    return {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "min_n":        min_n,
        "min_lift":     min_lift,
        "max_lift":     max_lift,
        "total_pairs":  len(results),
        "pairs":        results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute phrase co-occurrence lift matrix")
    parser.add_argument("--min-n",    type=int,   default=DEFAULT_MIN_N)
    parser.add_argument("--min-lift", type=float, default=DEFAULT_MIN_LIFT)
    parser.add_argument("--max-lift", type=float, default=DEFAULT_MAX_LIFT)
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()

    if not OUTCOMES_PATH.exists():
        raise SystemExit(f"Outcomes not found: {OUTCOMES_PATH}")

    data = compute(args.min_n, args.min_lift, args.max_lift)

    print(f"Total qualifying phrase pairs: {data['total_pairs']}")
    print()
    print(f"{'Speaker':10s} {'Phrase A':28s} -> {'Phrase B':28s}  N    P(B)  P(B|A)  Lift")
    print("-" * 90)
    for p in data["pairs"][:30]:
        print(
            f"{p['speaker']:10s} {p['phrase_a'][:28]:28s} -> {p['phrase_b'][:28]:28s}"
            f"  {p['n']:4d}  {p['marginal_p']:5.1%}  {p['cond_p']:6.1%}  {p['lift']:5.2f}"
        )

    if args.dry_run:
        print("\n--dry-run: not writing output.")
        return

    OUTPUT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {OUTPUT_PATH}  ({data['total_pairs']} pairs)")


if __name__ == "__main__":
    main()
