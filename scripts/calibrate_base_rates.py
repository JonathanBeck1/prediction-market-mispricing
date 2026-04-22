#!/usr/bin/env python3
"""Generate config/base_rates.yaml from local corpus transcripts.

This script calibrates per-phrase base rates with simple safeguards:
  - minimum transcript count per speaker/event_type before writing that block
  - minimum transcript count per speaker before writing speaker _default block
  - Bayesian smoothing toward global default for stability on small samples
  - optional blend with finalized Kalshi outcomes (if outcomes cache exists)
  - recency weighting: recent outcomes count more (exponential decay)
  - stores ALL outcome phrases (not just active catalog) in the general bucket

Usage:
    python3 scripts/calibrate_base_rates.py
    python3 scripts/calibrate_base_rates.py --recency-halflife-days 60
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - runtime guard
    raise SystemExit("PyYAML is required. Install with: python3 -m pip install pyyaml") from exc

# Allow running as: python3 scripts/calibrate_base_rates.py
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.kalshi_api import LiveMarketCatalog
from app.phrase_matcher import PhraseMatcher

CORPUS_DIR = Path("data/corpus")
OUTPUT_PATH = Path("config/base_rates.yaml")
OUTCOMES_PATH = Path("data/kalshi_outcomes.json")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate base rates from transcript corpus")
    parser.add_argument(
        "--min-event-docs",
        type=int,
        default=3,
        help="Minimum docs for a speaker/event_type block (default: 3)",
    )
    parser.add_argument(
        "--min-speaker-docs",
        type=int,
        default=5,
        help="Minimum docs for speaker _default block (default: 5)",
    )
    parser.add_argument(
        "--global-default",
        type=float,
        default=0.30,
        help="Global fallback probability (default: 0.30)",
    )
    parser.add_argument(
        "--prior-strength",
        type=float,
        default=2.0,
        help="Smoothing strength toward global default (default: 2.0)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="Output YAML path (default: config/base_rates.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print YAML to stdout instead of writing file",
    )
    parser.add_argument(
        "--outcomes-path",
        type=Path,
        default=OUTCOMES_PATH,
        help="Finalized outcomes cache path (default: data/kalshi_outcomes.json)",
    )
    parser.add_argument(
        "--outcome-weight",
        type=float,
        default=1.0,
        help="Weight multiplier for finalized outcomes in blended rates (default: 1.0)",
    )
    parser.add_argument(
        "--no-outcomes",
        action="store_true",
        help="Disable outcome blending even if outcomes cache exists",
    )
    parser.add_argument(
        "--recency-halflife-days",
        type=float,
        default=90.0,
        help=(
            "Recency half-life for outcome weighting in days (default: 90). "
            "Outcomes 90 days old count ~0.5x, 180 days old ~0.25x. "
            "Set to 0 to disable recency weighting (treat all equally)."
        ),
    )
    return parser.parse_args()


def _collect_transcripts(corpus_dir: Path) -> list[dict[str, str]]:
    transcripts: list[dict[str, str]] = []
    if not corpus_dir.exists():
        return transcripts

    for speaker_dir in sorted(p for p in corpus_dir.iterdir() if p.is_dir()):
        speaker = speaker_dir.name
        for file_path in sorted(speaker_dir.glob("*.txt")):
            parts = file_path.stem.split("_")
            event_type = parts[0] if parts else "other"
            text = file_path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            transcripts.append(
                {
                    "speaker": speaker,
                    "event_type": event_type,
                    "text": text,
                }
            )
    return transcripts


def _collect_outcomes(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for m in raw.get("markets", []):
        speaker = str(m.get("speaker", "")).strip().lower()
        event_type = str(m.get("event_context", "general")).strip().lower() or "general"
        result = str(m.get("result", "")).strip().lower()
        if result not in {"yes", "no"}:
            continue

        variants = [
            str(p).strip().lower()
            for p in m.get("phrase_variants", [])
            if str(p).strip()
        ]
        if not variants:
            primary = str(m.get("primary_phrase", "")).strip().lower()
            if primary:
                variants = [primary]
        if not variants:
            continue

        rows.append(
            {
                "speaker": speaker,
                "event_type": event_type,
                "result": result,
                "phrases": variants,
                # close_time used for recency weighting
                "close_time": str(m.get("close_time", "") or ""),
            }
        )
    return rows


def _recency_weight(close_time_str: str, halflife_days: float, now: datetime) -> float:
    """Return an exponential decay weight for an outcome based on its close_time.

    Recent outcomes count more: weight = 2^(-days_old / halflife).
    An outcome from today counts 1.0; one from halflife_days ago counts 0.5.
    If halflife_days == 0 or the date can't be parsed, returns 1.0.
    """
    if halflife_days <= 0 or not close_time_str:
        return 1.0
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(close_time_str[:19].rstrip("Z"), fmt.rstrip("Z"))
            break
        except ValueError:
            continue
    else:
        return 1.0
    days_old = max(0.0, (now - dt).total_seconds() / 86400.0)
    return math.pow(2.0, -days_old / halflife_days)


def _smoothed_rate(
    hit_count: float,
    doc_count: float,
    global_default: float,
    prior_strength: float,
) -> float:
    if doc_count <= 0:
        return global_default
    numerator = hit_count + (prior_strength * global_default)
    denominator = doc_count + prior_strength
    rate = numerator / denominator
    # Keep stable numeric bounds for YAML output.
    rate = max(0.0, min(1.0, rate))
    return round(rate, 2)


def main() -> None:
    args = _parse_args()

    if args.min_event_docs < 1 or args.min_speaker_docs < 1:
        raise SystemExit("min docs thresholds must be >= 1")
    if args.prior_strength < 0:
        raise SystemExit("prior-strength must be >= 0")
    if args.outcome_weight < 0:
        raise SystemExit("outcome-weight must be >= 0")
    if not (0.0 <= args.global_default <= 1.0):
        raise SystemExit("global-default must be in [0, 1]")

    catalog = LiveMarketCatalog.from_cache()
    if not catalog.markets:
        raise SystemExit("No live market cache found. Run: make fetch-markets")

    transcripts = _collect_transcripts(CORPUS_DIR)
    outcomes = [] if args.no_outcomes else _collect_outcomes(args.outcomes_path)
    if not transcripts and not outcomes:
        raise SystemExit("No transcripts in data/corpus and no outcomes cache found")

    speaker_phrases: dict[str, list[str]] = {
        speaker: catalog.phrases_for_speaker(speaker) for speaker in catalog.speakers
    }
    all_phrases = catalog.all_phrases()
    matcher = PhraseMatcher(all_phrases)

    # Recency: reference time for decay calculation
    now_utc = datetime.utcnow()

    # speaker -> event_type -> phrase -> {"docs": float, "hits": float}  (weighted)
    event_stats: dict[str, dict[str, dict[str, dict[str, float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: {"docs": 0.0, "hits": 0.0}))
    )
    # speaker -> phrase -> {"docs": float, "hits": float}
    speaker_stats: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"docs": 0.0, "hits": 0.0})
    )
    # speaker -> event_type -> phrase -> {"docs": float, "hits": float}  (outcome-weighted)
    outcome_event_stats: dict[str, dict[str, dict[str, dict[str, float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: {"docs": 0.0, "hits": 0.0}))
    )
    # speaker -> phrase -> {"docs": float, "hits": float}
    outcome_speaker_stats: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"docs": 0.0, "hits": 0.0})
    )
    # ALL-outcomes phrase bucket: every outcome phrase, not just active catalog.
    # speaker -> phrase -> {"docs": float, "hits": float}
    all_outcome_general: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"docs": 0.0, "hits": 0.0})
    )

    for t in transcripts:
        speaker = t["speaker"]
        event_type = t["event_type"]
        phrases = speaker_phrases.get(speaker, [])
        if not phrases:
            continue

        hit_set = {h.phrase for h in matcher.find_hits(t["text"])}
        for phrase in phrases:
            event_stats[speaker][event_type][phrase]["docs"] += 1.0
            speaker_stats[speaker][phrase]["docs"] += 1.0
            if phrase in hit_set:
                event_stats[speaker][event_type][phrase]["hits"] += 1.0
                speaker_stats[speaker][phrase]["hits"] += 1.0

    # Outcome stats are phrase-by-market outcomes (finalized yes/no).
    # Two separate passes:
    #   1. Catalog-restricted: only phrases in the active live market catalog,
    #      blended with transcript stats for event-type and _default blocks.
    #   2. All-outcomes: every phrase from any outcome, stored in a dedicated
    #      'general' bucket so future markets for those phrases get real rates.
    allowed_phrase_sets: dict[str, set[str]] = {
        s: set(ps) for s, ps in speaker_phrases.items()
    }
    for o in outcomes:
        speaker = o["speaker"]
        event_type = o["event_type"]
        recency_w = _recency_weight(o["close_time"], args.recency_halflife_days, now_utc)
        outcome_w = args.outcome_weight * recency_w

        # Pass 1: catalog-restricted (for event_type and _default blocks)
        phrase_set = allowed_phrase_sets.get(speaker, set())
        for phrase in o["phrases"]:
            if phrase in phrase_set:
                outcome_event_stats[speaker][event_type][phrase]["docs"] += outcome_w
                outcome_speaker_stats[speaker][phrase]["docs"] += outcome_w
                if o["result"] == "yes":
                    outcome_event_stats[speaker][event_type][phrase]["hits"] += outcome_w
                    outcome_speaker_stats[speaker][phrase]["hits"] += outcome_w

        # Pass 2: ALL outcome phrases → general bucket (no catalog restriction)
        for phrase in o["phrases"]:
            if not phrase.strip():
                continue
            all_outcome_general[speaker][phrase]["docs"] += outcome_w
            if o["result"] == "yes":
                all_outcome_general[speaker][phrase]["hits"] += outcome_w

    output: dict[str, Any] = {}

    speakers_written = 0
    speakers = sorted(
        set(event_stats.keys())
        | set(speaker_stats.keys())
        | set(outcome_event_stats.keys())
        | set(outcome_speaker_stats.keys())
        | set(all_outcome_general.keys())
    )

    for speaker in speakers:
        speaker_block: dict[str, Any] = {}

        # Event type blocks (transcript + outcome blended, catalog-restricted)
        event_types = sorted(
            set(event_stats[speaker].keys()) | set(outcome_event_stats[speaker].keys())
        )
        for event_type in event_types:
            phrase_keys = sorted(
                set(event_stats[speaker][event_type].keys())
                | set(outcome_event_stats[speaker][event_type].keys())
            )
            phrase_block: dict[str, float] = {}
            max_docs = 0.0
            for phrase in phrase_keys:
                stats = event_stats[speaker][event_type][phrase]
                o_stats = outcome_event_stats[speaker][event_type][phrase]
                docs = stats["docs"] + o_stats["docs"]
                hits = stats["hits"] + o_stats["hits"]
                max_docs = max(max_docs, docs)
                phrase_block[phrase] = _smoothed_rate(
                    hit_count=hits,
                    doc_count=docs,
                    global_default=args.global_default,
                    prior_strength=args.prior_strength,
                )

            if max_docs >= float(args.min_event_docs) and phrase_block:
                speaker_block[event_type] = phrase_block

        # Speaker-level _default block (transcript + outcome blended, catalog-restricted)
        default_phrase_keys = sorted(
            set(speaker_stats[speaker].keys()) | set(outcome_speaker_stats[speaker].keys())
        )
        max_speaker_docs = 0.0
        if default_phrase_keys:
            default_block: dict[str, float] = {}
            for phrase in default_phrase_keys:
                stats = speaker_stats[speaker][phrase]
                o_stats = outcome_speaker_stats[speaker][phrase]
                docs = stats["docs"] + o_stats["docs"]
                hits = stats["hits"] + o_stats["hits"]
                max_speaker_docs = max(max_speaker_docs, docs)
                default_block[phrase] = _smoothed_rate(
                    hit_count=hits,
                    doc_count=docs,
                    global_default=args.global_default,
                    prior_strength=args.prior_strength,
                )
            if max_speaker_docs >= float(args.min_speaker_docs) and default_block:
                speaker_block["_default"] = default_block

        # 'general' bucket: ALL outcome phrases (not restricted to active catalog).
        # This ensures future markets for retired/new phrases get real rates, not
        # the stale context prior.  Only requires >=2 observations to write a rate.
        general_block: dict[str, float] = {}
        for phrase, stats in all_outcome_general[speaker].items():
            if stats["docs"] >= 2.0:
                general_block[phrase] = _smoothed_rate(
                    hit_count=stats["hits"],
                    doc_count=stats["docs"],
                    global_default=args.global_default,
                    prior_strength=args.prior_strength,
                )
        if general_block:
            speaker_block["general"] = general_block

        # Per-context empirical base rate block (_context_base).
        # This tells the lookup: "for a phrase we've never seen before,
        # trump at a rally is typically 57% YES, not 30%."
        # Computed directly from outcome YES/NO counts, no transcript blending.
        context_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"yes": 0, "total": 0})
        for o in outcomes:
            if o["speaker"] != speaker:
                continue
            ctx = o["event_type"]
            context_counts[ctx]["total"] += 1
            if o["result"] == "yes":
                context_counts[ctx]["yes"] += 1

        context_base: dict[str, float] = {}
        for ctx, cnts in context_counts.items():
            if cnts["total"] >= 20:
                # Light Bayesian smoothing toward global default
                rate = _smoothed_rate(
                    hit_count=float(cnts["yes"]),
                    doc_count=float(cnts["total"]),
                    global_default=args.global_default,
                    prior_strength=args.prior_strength,
                )
                context_base[ctx] = rate

        # Also compute overall speaker prior ("general" catch-all)
        all_total = sum(c["total"] for c in context_counts.values())
        all_yes = sum(c["yes"] for c in context_counts.values())
        if all_total >= 20 and "general" not in context_base:
            context_base["general"] = _smoothed_rate(
                hit_count=float(all_yes),
                doc_count=float(all_total),
                global_default=args.global_default,
                prior_strength=args.prior_strength,
            )

        if context_base:
            speaker_block["_context_base"] = context_base

        if speaker_block:
            output[speaker] = speaker_block
            speakers_written += 1

    # Global default: use weighted average YES rate from all outcomes instead
    # of the hardcoded 0.30, so unknown speakers get a realistic prior.
    if outcomes:
        all_out_yes = sum(1 for o in outcomes if o["result"] == "yes")
        all_out_total = len(outcomes)
        empirical_global = _smoothed_rate(
            float(all_out_yes), float(all_out_total),
            args.global_default, args.prior_strength * 2,
        )
        output["_global_default"] = round(empirical_global, 2)
    else:
        output["_global_default"] = round(float(args.global_default), 2)

    # Phrase co-occurrence for the scorer is ONLY written by
    # scripts/compute_cooccurrence.py (pairs[] schema, recency-weighted).
    # This script used to call _build_cooccurrence() which emitted a legacy
    # dict shape and overwrote that file — do not duplicate here.
    # Run: python3 scripts/compute_cooccurrence.py (also daily in MaintenanceRunner).

    if args.dry_run:
        print(yaml.safe_dump(output, sort_keys=False))
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            yaml.safe_dump(output, sort_keys=False),
            encoding="utf-8",
        )
        # Summary stats
        total_general_phrases = sum(
            len(output.get(s, {}).get("general", {}))
            for s in speakers
        )
        print(f"Wrote {args.output} (speakers={speakers_written}, transcripts={len(transcripts)})")
        print(f"Outcomes blended: {len(outcomes)} rows (weight={args.outcome_weight}, "
              f"recency_halflife={args.recency_halflife_days}d)")
        print(f"General-bucket phrases written: {total_general_phrases} "
              f"(includes uncatalogued outcome phrases)")
        print(
            "Thresholds: "
            f"min_event_docs={args.min_event_docs}, "
            f"min_speaker_docs={args.min_speaker_docs}, "
            f"prior_strength={args.prior_strength}"
        )


if __name__ == "__main__":
    main()
