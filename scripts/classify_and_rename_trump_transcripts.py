#!/usr/bin/env python3
"""Classify Trump corpus transcripts by content and rename to correct event_type.

Reads each file in data/corpus/trump/, infers event_type from content (keyword-based),
and renames to {event_type}_{date}_{seq}.txt when the current filename prefix is wrong.

Usage:
    python3 scripts/classify_and_rename_trump_transcripts.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

CORPUS_DIR = Path("data/corpus/trump")
VALID_TYPES = (
    "rally", "briefing", "interview", "townhall", "general",
    "address", "announcement", "presser", "visit", "remarks", "summit", "signing", "other",
)


def _classify_from_content(text: str) -> str:
    """Infer event_type from transcript text. Returns one of VALID_TYPES."""
    sample = (text[:6000] + " " + text[-800:] if len(text) > 6800 else text).lower()

    # Summit / diplomatic (world leaders, Board of Peace, etc.)
    if any(
        kw in sample
        for kw in (
            "board of peace",
            "institute of peace",
            "world leaders",
            "prime minister",
            "dignitaries",
            "bilateral",
            "secretary of state",
            "united nations",
            "inaugural meeting",
        )
    ):
        if "press" not in sample[:1500] and "briefing" not in sample[:1500]:
            return "summit"

    # Press briefing / press conference
    if any(
        kw in sample
        for kw in (
            "press briefing",
            "press conference",
            "white house press",
            "briefing",
            "news conference",
        )
    ):
        return "briefing"

    # Rally (campaign rally, "at that rally", MAGA)
    if any(kw in sample for kw in ("campaign rally", "at that rally", " at a rally ")):
        return "rally"
    if "rally" in sample and ("rally in" in sample or "rally at" in sample):
        return "rally"

    # Interview
    if any(
        kw in sample
        for kw in (
            "exclusive interview",
            "sat down with",
            "interview with",
            "doing an interview",
            "podcast",
        )
    ):
        return "interview"
    if "interview" in sample and ("terry" in sample or "anchor" in sample or "correspondent" in sample):
        return "interview"

    # Town hall
    if "town hall" in sample or "townhall" in sample:
        return "townhall"

    # Formal address (joint session, SOTU, Independence Day, national ceremony)
    if any(
        kw in sample
        for kw in (
            "joint session",
            "state of the union",
            "address to congress",
            "independence day",
            "4th of july",
            "fourth of july",
            "national anthem",
            "marine band",
            "god bless the usa",
        )
    ):
        return "address"

    # Signing ceremony
    if any(
        kw in sample
        for kw in (
            "signing ceremony",
            "sign the bill",
            "signed the bill",
            "executive order",
            "signing the",
        )
    ):
        if "independence day" in sample or "4th of july" in sample:
            return "address"
        return "signing"

    # Company / facility visit (roundtable, factory, plant, Thermo Fisher)
    if any(
        kw in sample
        for kw in (
            "visit at",
            "visit to",
            "thermo fisher",
            "roundtable",
            "factory",
            "manufacturing",
            "facility",
            "here at ",
            "investments in pennsylvania",
            "data center",
            "power plant",
        )
    ):
        if "commonwealth of pennsylvania" in sample or "here in pittsburgh" in sample:
            return "visit"
        if "drill" in sample and "pennsylvania" in sample:
            return "visit"
        return "visit"

    # Remarks (domestic travel, "remarks in X")
    if re.search(r"remarks in [a-z]", sample) or re.search(r"remarks at [a-z]", sample):
        return "remarks"
    if "commonwealth of pennsylvania" in sample and "investments" in sample:
        return "remarks"
    if "here in pittsburgh" in sample or "here in pennsylvania" in sample:
        return "remarks"

    # Announcement
    if "pleased to announce" in sample or "we're announcing" in sample or "announcing " in sample:
        return "announcement"

    # Rally fallback for crowd-heavy political speech
    if "thank you very much" in sample and ("crowd" in sample or "people" in sample) and "rally" in sample:
        return "rally"

    return "general"


def main() -> None:
    ap = argparse.ArgumentParser(description="Classify and rename Trump transcripts by content")
    ap.add_argument("--dry-run", action="store_true", help="Print renames only, do not rename")
    args = ap.parse_args()

    if not CORPUS_DIR.exists():
        raise SystemExit(f"Corpus dir not found: {CORPUS_DIR}")

    renamed = 0
    skipped = 0
    for path in sorted(CORPUS_DIR.glob("*.txt")):
        stem = path.stem
        parts = stem.split("_")
        if len(parts) < 3:
            skipped += 1
            continue
        current_type = parts[0].lower()
        date_part = parts[1]
        seq_part = parts[2]

        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  skip {path.name}: {e}")
            skipped += 1
            continue

        inferred = _classify_from_content(text)
        if inferred not in VALID_TYPES:
            inferred = "general"

        if current_type == inferred:
            skipped += 1
            continue

        new_name = f"{inferred}_{date_part}_{seq_part}.txt"
        new_path = path.parent / new_name
        if new_path.exists() and new_path != path:
            print(f"  skip {path.name} -> {new_name} (target exists)")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  {path.name} -> {new_name}")
        else:
            path.rename(new_path)
            print(f"  {path.name} -> {new_name}")
        renamed += 1

    print(f"\nRenamed: {renamed}, Unchanged/skipped: {skipped}")


if __name__ == "__main__":
    main()
