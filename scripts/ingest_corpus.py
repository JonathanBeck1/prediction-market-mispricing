#!/usr/bin/env python3
"""Bulk-load corpus transcripts from data/corpus/ into the SQLite database.

This connects the on-disk transcript files to the phrase_hits pipeline so the
scoring engine can compute real probabilities based on actual speech data.

Usage:
    python3 scripts/ingest_corpus.py          # load all corpus files
    python3 scripts/ingest_corpus.py --fresh   # wipe existing transcripts first
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import init_db
from app.kalshi_api import LiveMarketCatalog
from app.market_catalog import MARKET_PHRASES, all_market_phrases
from app.phrase_matcher import PhraseMatcher, store_phrase_hits

DB_PATH = Path("data/edge.db")
CORPUS_DIR = Path("data/corpus")


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _build_matcher() -> PhraseMatcher:
    try:
        catalog = LiveMarketCatalog.from_cache()
        if catalog.markets:
            phrases = catalog.all_phrases()
            print(f"Using {len(phrases)} phrases from live market catalog")
            return PhraseMatcher(phrases)
    except Exception:
        pass

    phrases = all_market_phrases()
    print(f"Using {len(phrases)} phrases from mock market catalog")
    return PhraseMatcher(phrases)


def _parse_date_from_filename(filename: str) -> str:
    parts = filename.replace(".txt", "").split("_")
    for part in parts:
        if len(part) == 10 and part[4] == "-" and part[7] == "-":
            return part
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def ingest_corpus(conn: sqlite3.Connection, matcher: PhraseMatcher, fresh: bool = False) -> None:
    if fresh:
        print("Wiping existing transcripts and phrase_hits...")
        conn.execute("DELETE FROM phrase_hits")
        conn.execute("DELETE FROM transcripts")
        conn.commit()

    if not CORPUS_DIR.exists():
        print(f"Corpus directory not found: {CORPUS_DIR}")
        return

    total_files = 0
    total_inserted = 0
    total_hits = 0
    total_skipped = 0

    speaker_matchers: dict[str, PhraseMatcher] = {}
    fallback_matcher = matcher
    try:
        catalog = LiveMarketCatalog.from_cache()
    except Exception:
        catalog = LiveMarketCatalog()

    for speaker_dir in sorted(p for p in CORPUS_DIR.iterdir() if p.is_dir()):
        speaker = speaker_dir.name
        if catalog.markets:
            sp_phrases = catalog.phrases_for_speaker(speaker)
            if sp_phrases:
                speaker_matchers[speaker] = PhraseMatcher(sp_phrases)
            else:
                speaker_matchers[speaker] = fallback_matcher
        else:
            speaker_matchers[speaker] = fallback_matcher

        for file_path in sorted(speaker_dir.glob("*.txt")):
            total_files += 1
            text = file_path.read_text(encoding="utf-8").strip()
            if not text:
                continue

            h = _text_hash(text)
            source_ref = f"corpus:{speaker}/{file_path.name}"

            existing = conn.execute(
                "SELECT 1 FROM transcripts WHERE source_ref = ? AND text_hash = ? LIMIT 1",
                (source_ref, h),
            ).fetchone()
            if existing:
                total_skipped += 1
                continue

            file_date = _parse_date_from_filename(file_path.name)
            ts = f"{file_date}T12:00:00+00:00"

            cursor = conn.execute(
                "INSERT INTO transcripts (ts, source, source_ref, text, text_hash) VALUES (?, ?, ?, ?, ?)",
                (ts, "corpus", source_ref, text, h),
            )
            transcript_id = int(cursor.lastrowid)

            active_matcher = speaker_matchers.get(speaker, fallback_matcher)
            hits = active_matcher.find_hits(text)
            hit_count = store_phrase_hits(conn, transcript_id=transcript_id, hits=hits, ts=ts)

            total_inserted += 1
            total_hits += hit_count

            if hit_count > 0:
                hit_phrases = sorted(set(hit.phrase for hit in hits))
                print(f"  {source_ref}: {hit_count} hits -> {', '.join(hit_phrases[:5])}")

    conn.commit()

    print(f"\nCorpus ingestion complete:")
    print(f"  Files scanned:  {total_files}")
    print(f"  Inserted:       {total_inserted}")
    print(f"  Skipped (dupe): {total_skipped}")
    print(f"  Phrase hits:    {total_hits}")

    top_phrases = conn.execute("""
        SELECT phrase, COUNT(*) as c FROM phrase_hits
        GROUP BY phrase ORDER BY c DESC LIMIT 15
    """).fetchall()
    if top_phrases:
        print(f"\nTop phrase hits in DB:")
        for row in top_phrases:
            print(f"  {row[1]:4d}x  {row[0]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest corpus transcripts into SQLite")
    parser.add_argument("--fresh", action="store_true", help="Wipe existing transcripts first")
    args = parser.parse_args()

    conn = init_db(DB_PATH)
    matcher = _build_matcher()
    ingest_corpus(conn, matcher, fresh=args.fresh)
    conn.close()


if __name__ == "__main__":
    main()
