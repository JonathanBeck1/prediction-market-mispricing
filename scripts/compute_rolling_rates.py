#!/usr/bin/env python3
"""Compute rolling N-speech hit rates from resolved Kalshi outcomes.

For each (speaker, phrase) pair, calculates YES rates over the last 3, 5, and
10 single-event speeches (excluding monthly/nickname window markets, which span
the whole month and distort per-speech rates).

Why this matters:
  Historical base rates are recency-weighted (90-day half-life), but that's
  slow to react to sharp regime changes. Examples where rolling rates matter:
    - "golden dome": was 38% historically, now 0% (Trump stopped saying it)
    - "tariff": was 45%, now 85% (he's been saying it every speech)
    - "drill baby drill": was 60%, now 20% (faded from rhetoric)

  The 5-speech rolling rate catches these breaks 4-6 weeks faster than the
  decay-weighted historical rate.

Output: data/rolling_hit_rates.json
  {
    "generated_at": "2026-03-16T00:00:00Z",
    "rates": {
      "trump": {
        "tariff": {
          "n3": 1.0,   "n3_obs": 3,
          "n5": 0.8,   "n5_obs": 5,
          "n10": 0.7,  "n10_obs": 10,
          "last_seen": "2026-03-13"
        },
        ...
      }
    }
  }

Run via MaintenanceRunner (daily) or manually:
  python3 scripts/compute_rolling_rates.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

OUTCOMES_PATH = Path("data/kalshi_outcomes.json")
OUTPUT_PATH   = Path("data/rolling_hit_rates.json")

# Only these series count as "per-speech" events for rolling rates.
# Monthly / nickname window markets are excluded because a single market
# can remain open across many speeches.
SINGLE_EVENT_SERIES: frozenset[str] = frozenset({
    "KXTRUMPSAY",
    "KXTRUMPSAYEP",
    "KXTRUMPMENTION",
    "KXTRUMPMENTIONB",
    "KXPRESMENTION",
    "KXLEAVITTMENTION",
    "KXSECPRESSMENTION",
    "KXMAMDANIMENTION",
    "KXMENTION",
    "KXFEDMENTION",
    "KXDJTRALLY",
    "KXDJTINVESTMENT",
    "KXWHPRESSBRIEFING",
    "KXSTARMERMENTIONB",
})

# Rolling windows to compute
WINDOWS = (3, 5, 10)

# Minimum observations before we trust a rolling rate enough to use it
MIN_OBS = 3


def _norm(phrase: str) -> str:
    return phrase.strip().lower()


def load_outcomes(path: Path) -> list[dict]:
    if not path.exists():
        print(f"WARN: {path} not found")
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("markets", [])


def build_rolling_rates(outcomes: list[dict]) -> dict[str, dict[str, dict]]:
    """Build {speaker: {phrase: {n3, n5, n10, ...}}} from resolved outcomes.

    Only single-event series are used.  For each phrase, observations are
    sorted chronologically (most-recent last) and we look back N speeches.
    """
    # Collect (speaker, phrase, event_ticker, result, close_time)
    # De-duplicate: one row per (event_ticker, phrase) — if multiple variants
    # resolve, take the canonical primary_phrase result.
    seen_key: set[tuple[str, str, str]] = set()
    obs: list[dict] = []  # {speaker, phrase, et, result, close}

    for m in outcomes:
        series = str(m.get("series_ticker", "")).upper().strip()
        if series not in SINGLE_EVENT_SERIES:
            continue
        result = str(m.get("result", "")).lower()
        if result not in ("yes", "no"):
            continue
        et      = str(m.get("event_ticker", "")).strip()
        speaker = str(m.get("speaker", "")).strip().lower()
        phrase  = _norm(str(m.get("primary_phrase", "")))
        close   = str(m.get("close_time", ""))

        if not (et and speaker and phrase):
            continue

        key = (speaker, phrase, et)
        if key in seen_key:
            continue
        seen_key.add(key)

        obs.append({
            "speaker": speaker,
            "phrase":  phrase,
            "et":      et,
            "result":  result,
            "close":   close,
        })

    # Sort all observations chronologically (oldest first)
    obs.sort(key=lambda x: x["close"])

    # Group by (speaker, phrase) maintaining time order
    by_phrase: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for o in obs:
        by_phrase[(o["speaker"], o["phrase"])].append(o)

    # Compute rolling rates
    rates: dict[str, dict[str, dict]] = defaultdict(dict)

    for (speaker, phrase), history in by_phrase.items():
        # history is already sorted oldest→newest
        entry: dict = {}
        for n in WINDOWS:
            recent = history[-n:]  # last N observations
            if len(recent) < MIN_OBS:
                # Not enough data for this window — skip this N
                continue
            yes_count = sum(1 for r in recent if r["result"] == "yes")
            rate = yes_count / len(recent)
            entry[f"n{n}"]      = round(rate, 4)
            entry[f"n{n}_obs"]  = len(recent)

        if not entry:
            continue  # no window had enough data

        # Last-seen date (most recent observation, regardless of result)
        entry["last_seen"] = history[-1]["close"][:10]
        # Number of total observations
        entry["total_obs"] = len(history)

        rates[speaker][phrase] = entry

    return dict(rates)


def print_summary(rates: dict[str, dict[str, dict]]) -> None:
    print(f"\nRolling hit rates summary:")
    for speaker, phrases in sorted(rates.items()):
        n3  = sum(1 for p in phrases.values() if "n3"  in p)
        n5  = sum(1 for p in phrases.values() if "n5"  in p)
        n10 = sum(1 for p in phrases.values() if "n10" in p)
        print(f"  {speaker}: {len(phrases)} phrases  "
              f"(n3={n3}, n5={n5}, n10={n10})")

    # Show most interesting phrases: big gap between n5 and historical
    print("\nTop phrases by n5 rate (most recent trend):")
    all_phrases = [
        (spk, phrase, entry)
        for spk, phrases in rates.items()
        for phrase, entry in phrases.items()
        if "n5" in entry
    ]
    all_phrases.sort(key=lambda x: -x[2]["n5"])
    for spk, phrase, entry in all_phrases[:10]:
        print(f"  {spk:<10} {phrase:<30} n5={entry['n5']:.2f}  "
              f"n10={entry.get('n10', '?')!r}  obs={entry['total_obs']}")


def main() -> None:
    print(f"Loading outcomes from {OUTCOMES_PATH}...")
    outcomes = load_outcomes(OUTCOMES_PATH)
    print(f"  {len(outcomes)} total resolved markets")

    rates = build_rolling_rates(outcomes)

    total_phrases = sum(len(p) for p in rates.values())
    print(f"  {total_phrases} phrases with rolling rates "
          f"({sum(len(p) for p in rates.values())} total)")

    print_summary(rates)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_obs": MIN_OBS,
        "windows": list(WINDOWS),
        "rates": rates,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    tmp.replace(OUTPUT_PATH)
    print(f"\nWrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
