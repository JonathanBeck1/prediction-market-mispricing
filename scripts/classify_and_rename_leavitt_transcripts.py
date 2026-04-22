#!/usr/bin/env python3
"""Classify Leavitt corpus transcripts by content, remove duplicates, and rename.

1. Removes duplicate files (same normalized content); keeps one per content hash.
2. Classifies each remaining file by content and renames to {event_type}_{date}_{seq}.txt.

Only touches data/corpus/leavitt/. Uses same event-type logic as Trump script.

Usage:
    python3 scripts/classify_and_rename_leavitt_transcripts.py [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

CORPUS_DIR = Path("data/corpus/leavitt")
VALID_TYPES = (
    "rally", "briefing", "interview", "townhall", "general",
    "address", "announcement", "presser", "visit", "remarks", "summit", "signing", "other",
)


def _normalize_content(text: str) -> str:
    """Normalize for duplicate detection: strip and collapse whitespace."""
    return re.sub(r"\s+", " ", text.strip())


def _classify_from_content(text: str) -> str:
    """Infer event_type from transcript text. Briefing first (Leavitt = Press Sec)."""
    sample = (text[:6000] + " " + text[-800:] if len(text) > 6800 else text).lower()

    # Press briefing / press conference first (Leavitt's main format)
    if any(
        kw in sample
        for kw in (
            "press briefing",
            "press conference",
            "white house press",
            "briefing room",
            "welcome to the briefing",
            "news conference",
        )
    ):
        return "briefing"
    if "briefing" in sample and ("take your question" in sample or "take questions" in sample or "kick us off" in sample or "kick it off" in sample):
        return "briefing"

    # Summit / diplomatic (only if not already clearly a briefing)
    if any(
        kw in sample
        for kw in (
            "board of peace",
            "institute of peace",
            "world leaders",
            "prime minister",
            "dignitaries",
            "bilateral",
            "united nations",
            "inaugural meeting",
        )
    ):
        return "summit"
    if "secretary of state" in sample and "press" not in sample[:2000] and "briefing" not in sample[:2000]:
        return "summit"

    # Rally
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

    # Formal address
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

    # Signing
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

    # Visit
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

    # Remarks
    if re.search(r"remarks in [a-z]", sample) or re.search(r"remarks at [a-z]", sample):
        return "remarks"
    if "commonwealth of pennsylvania" in sample and "investments" in sample:
        return "remarks"
    if "here in pittsburgh" in sample or "here in pennsylvania" in sample:
        return "remarks"

    # Announcement
    if "pleased to announce" in sample or "we're announcing" in sample or "announcing " in sample:
        return "announcement"

    # Rally fallback
    if "thank you very much" in sample and ("crowd" in sample or "people" in sample) and "rally" in sample:
        return "rally"

    return "general"


def _dedupe(corpus_dir: Path, dry_run: bool) -> int:
    """Group files by content hash; remove duplicates (keep first by sorted path). Returns count removed."""
    by_hash: dict[str, list[Path]] = {}
    for path in sorted(corpus_dir.glob("*.txt")):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        key = hashlib.sha256(_normalize_content(text).encode("utf-8")).hexdigest()
        by_hash.setdefault(key, []).append(path)

    removed = 0
    for key, paths in by_hash.items():
        if len(paths) <= 1:
            continue
        # Keep the first by path name; remove the rest
        keep, *dupes = sorted(paths, key=lambda p: p.name)
        for dup in dupes:
            if dry_run:
                print(f"  [dedupe] would remove {dup.name} (duplicate of {keep.name})")
            else:
                dup.unlink()
                print(f"  [dedupe] removed {dup.name} (duplicate of {keep.name})")
            removed += 1
    return removed


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Dedupe and classify/rename Leavitt transcripts by content"
    )
    ap.add_argument("--dry-run", action="store_true", help="Print only; do not delete or rename")
    args = ap.parse_args()

    if not CORPUS_DIR.exists():
        raise SystemExit(f"Corpus dir not found: {CORPUS_DIR}")

    print("Deduplicating by content...")
    removed = _dedupe(CORPUS_DIR, args.dry_run)
    print(f"  Duplicates removed: {removed}\n")

    print("Classifying and renaming...")
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
