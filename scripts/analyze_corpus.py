#!/usr/bin/env python3
"""Analyze the scraped corpus to calibrate base rates.

Runs phrase matching on all transcripts in data/corpus/ and outputs:
  - Per-speaker, per-event-type phrase hit rates
  - Suggested base_rates.yaml values
  - Phrases that never appear (potential traps)

Usage:
    python3 scripts/analyze_corpus.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.kalshi_api import LiveMarketCatalog
from app.phrase_matcher import PhraseMatcher

CORPUS_DIR = Path("data/corpus")


def collect_transcripts() -> list[dict]:
    """Scan corpus directory for transcript files."""
    transcripts = []
    for speaker_dir in sorted(CORPUS_DIR.iterdir()):
        if not speaker_dir.is_dir():
            continue
        speaker = speaker_dir.name
        for f in sorted(speaker_dir.glob("*.txt")):
            parts = f.stem.split("_")
            event_type = parts[0] if len(parts) >= 1 else "other"
            date = parts[1] if len(parts) >= 2 else "unknown"
            text = f.read_text(encoding="utf-8").strip()
            if text:
                transcripts.append({
                    "speaker": speaker,
                    "event_type": event_type,
                    "date": date,
                    "path": str(f),
                    "text": text,
                    "char_count": len(text),
                })
    return transcripts


def analyze_phrase_hits(transcripts: list[dict]) -> None:
    """Run phrase matching and compute hit rates."""
    catalog = LiveMarketCatalog.from_cache()
    if not catalog.markets:
        print("ERROR: No live markets cached. Run 'make fetch-markets' first.")
        return

    market_phrases = catalog.market_phrases_map()
    all_phrases = catalog.all_phrases()
    matcher = PhraseMatcher(all_phrases)

    # speaker -> event_type -> phrase -> [hit_count_per_transcript]
    hit_data: dict[str, dict[str, dict[str, list[int]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    # speaker -> event_type -> count
    doc_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # Which market phrases belong to which speaker
    speaker_tickers: dict[str, list[str]] = {}
    for m in catalog.markets:
        speaker_tickers.setdefault(m.speaker, []).append(m.ticker)

    print(f"Corpus: {len(transcripts)} transcripts")
    print(f"Live markets: {len(catalog.markets)}, Phrases: {len(all_phrases)}")
    print()

    for t in transcripts:
        speaker = t["speaker"]
        event_type = t["event_type"]
        doc_counts[speaker][event_type] += 1

        hits = matcher.find_hits(t["text"])
        hit_phrases = {h.phrase for h in hits}

        for ticker in speaker_tickers.get(speaker, []):
            phrases = market_phrases.get(ticker, [])
            for phrase in phrases:
                was_hit = 1 if phrase in hit_phrases else 0
                hit_data[speaker][event_type][phrase].append(was_hit)

    # Print results
    print("=" * 70)
    print("PHRASE HIT RATES (observed from corpus)")
    print("=" * 70)

    for speaker in sorted(hit_data.keys()):
        print(f"\n{speaker}:")
        for event_type in sorted(hit_data[speaker].keys()):
            n_docs = doc_counts[speaker][event_type]
            print(f"  {event_type} ({n_docs} transcripts):")
            for phrase in sorted(hit_data[speaker][event_type].keys()):
                hits_list = hit_data[speaker][event_type][phrase]
                hit_count = sum(hits_list)
                total = len(hits_list)
                rate = hit_count / total if total > 0 else 0
                bar = "#" * int(rate * 20)
                print(f"    {phrase:25s} {hit_count:3d}/{total:3d} = {rate:.2f}  {bar}")

    # Suggested YAML
    print()
    print("=" * 70)
    print("SUGGESTED base_rates.yaml (copy and adjust)")
    print("=" * 70)

    for speaker in sorted(hit_data.keys()):
        print(f"\n{speaker}:")
        for event_type in sorted(hit_data[speaker].keys()):
            if event_type == "_all":
                continue
            print(f"  {event_type}:")
            for phrase in sorted(hit_data[speaker][event_type].keys()):
                hits_list = hit_data[speaker][event_type][phrase]
                rate = sum(hits_list) / len(hits_list) if hits_list else 0.30
                print(f"    {phrase}: {rate:.2f}")

    # Phrases that never appeared
    print()
    print("=" * 70)
    print("NEVER-HIT PHRASES (potential traps or rare phrases)")
    print("=" * 70)
    for speaker in sorted(hit_data.keys()):
        for event_type in sorted(hit_data[speaker].keys()):
            for phrase in sorted(hit_data[speaker][event_type].keys()):
                hits_list = hit_data[speaker][event_type][phrase]
                if sum(hits_list) == 0 and len(hits_list) >= 3:
                    print(f"  {speaker}/{event_type}: \"{phrase}\" -- 0 hits in {len(hits_list)} transcripts")


def main():
    transcripts = collect_transcripts()
    if not transcripts:
        print("No transcripts found in data/corpus/")
        print("Run 'python3 scripts/scrape_corpus.py' first to build the corpus.")
        print()
        print("Or manually add .txt files to data/corpus/<speaker>/")
        print("Naming: <event_type>_<date>_<seq>.txt")
        print("Example: data/corpus/trump/rally_2026-02-20_01.txt")
        return

    # Summary
    from collections import Counter
    speakers = Counter(t["speaker"] for t in transcripts)
    types = Counter(f"{t['speaker']}/{t['event_type']}" for t in transcripts)
    total_chars = sum(t["char_count"] for t in transcripts)

    print("Corpus summary:")
    for s, c in speakers.most_common():
        print(f"  {s}: {c} transcripts")
    print(f"  Total: {len(transcripts)} transcripts, {total_chars:,} characters")
    print()

    analyze_phrase_hits(transcripts)


if __name__ == "__main__":
    main()
