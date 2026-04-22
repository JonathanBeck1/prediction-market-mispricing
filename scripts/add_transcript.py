#!/usr/bin/env python3
"""Manually add a transcript to the corpus from a file or stdin.

Usage:
    # From a file
    python3 scripts/add_transcript.py --speaker trump --type rally --date 2026-02-20 < transcript.txt

    # From clipboard (macOS)
    pbpaste | python3 scripts/add_transcript.py --speaker leavitt --type briefing --date 2026-02-28

    # From a local file
    python3 scripts/add_transcript.py --speaker trump --type rally --date 2026-02-20 --file /path/to/text.txt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


CORPUS_DIR = Path("data/corpus")


def main():
    parser = argparse.ArgumentParser(description="Add a transcript to the corpus")
    parser.add_argument("--speaker", required=True, choices=[
        "trump", "leavitt", "mamdani", "powell",
        "carney", "starmer", "homan",
    ])
    parser.add_argument("--type", required=True, help="Event type: rally, briefing, interview, townhall, etc.")
    parser.add_argument("--date", required=True, help="Date: YYYY-MM-DD")
    parser.add_argument("--file", help="Read from file instead of stdin")
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print("Paste transcript text, then Ctrl-D when done:")
        text = sys.stdin.read()

    text = text.strip()
    if not text:
        print("ERROR: empty transcript")
        sys.exit(1)

    out_dir = CORPUS_DIR / args.speaker
    out_dir.mkdir(parents=True, exist_ok=True)

    # Find next sequence number
    existing = list(out_dir.glob(f"{args.type}_{args.date}_*.txt"))
    seq = len(existing) + 1

    out_path = out_dir / f"{args.type}_{args.date}_{seq:02d}.txt"
    out_path.write_text(text, encoding="utf-8")

    print(f"Saved: {out_path} ({len(text):,} characters)")
    print(f"Run 'python3 scripts/analyze_corpus.py' to see phrase hit rates.")


if __name__ == "__main__":
    main()
