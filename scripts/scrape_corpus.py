#!/usr/bin/env python3
"""Scrape historical transcripts into the local corpus using OpenClaw Browser Relay.

Usage:
    python3 scripts/scrape_corpus.py

Reads transcript URLs from config/corpus_urls.yaml and saves each transcript
to data/corpus/{speaker}/{event_type}_{date}_{seq}.txt.

After scraping, run the analysis:
    python3 scripts/analyze_corpus.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

PROFILE = os.getenv("OPENCLAW_BROWSER_PROFILE", "openclaw")
TIMEOUT = int(os.getenv("OPENCLAW_TIMEOUT_SEC", "30"))
CORPUS_DIR = Path("data/corpus")
EXTRACT_JS = "() => document.body.innerText"


def load_urls() -> list[dict]:
    """Load transcript URLs from config/corpus_urls.yaml."""
    try:
        import yaml
    except ImportError:
        print("ERROR: pyyaml required. Run: pip install pyyaml")
        sys.exit(1)

    path = Path("config/corpus_urls.yaml")
    if not path.exists():
        print(f"ERROR: {path} not found. Create it first -- see template below.")
        print_template()
        sys.exit(1)

    with path.open() as f:
        data = yaml.safe_load(f)

    return data.get("transcripts", [])


def scrape_url(url: str) -> str | None:
    """Use OpenClaw Browser Relay to extract page text."""
    # Start browser session
    if not _run(["openclaw", "browser", "--browser-profile", PROFILE, "start"]):
        return None

    # Navigate to URL
    if not _run(["openclaw", "browser", "--browser-profile", PROFILE, "open", url]):
        return None

    # Wait for page to load
    time.sleep(2)

    # Extract text
    result = _run_output(
        ["openclaw", "browser", "--browser-profile", PROFILE,
         "evaluate", "--fn", EXTRACT_JS, "--json"]
    )
    if not result:
        return None

    return _parse_result(result)


def save_transcript(speaker: str, event_type: str, date: str, seq: int, text: str) -> Path:
    out_dir = CORPUS_DIR / speaker
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{event_type}_{date}_{seq:02d}.txt"
    path = out_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


def _run(args: list[str]) -> bool:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT)
        if result.returncode != 0:
            print(f"  WARN: {' '.join(args[:4])} failed: {result.stderr.strip()[:100]}")
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  ERROR: {e}")
        return False


def _run_output(args: list[str]) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT)
        if result.returncode != 0:
            print(f"  WARN: {' '.join(args[:4])} failed: {result.stderr.strip()[:100]}")
            return None
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  ERROR: {e}")
        return None


def _parse_result(raw: str) -> str:
    try:
        data = json.loads(raw)
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, dict):
            for key in ("result", "value", "data", "text"):
                if key in data and isinstance(data[key], str):
                    return data[key].strip()
    except json.JSONDecodeError:
        pass
    return raw.strip()


def print_template():
    print("""
Create config/corpus_urls.yaml with this format:

transcripts:
  # Trump rallies
  - speaker: trump
    event_type: rally
    date: "2026-02-20"
    url: "https://rev.com/blog/transcripts/trump-rally-transcript-feb-20-2026"
  - speaker: trump
    event_type: rally
    date: "2026-02-15"
    url: "https://rev.com/blog/transcripts/trump-rally-transcript-feb-15-2026"

  # Leavitt briefings
  - speaker: leavitt
    event_type: briefing
    date: "2026-02-28"
    url: "https://www.whitehouse.gov/briefing-room/press-briefings/2026/02/28/"
  - speaker: leavitt
    event_type: briefing
    date: "2026-02-27"
    url: "https://www.whitehouse.gov/briefing-room/press-briefings/2026/02/27/"

  # Mamdani
  - speaker: mamdani
    event_type: townhall
    date: "2026-02-22"
    url: "https://example.com/mamdani-townhall-transcript"

Good transcript sources:
  - rev.com/blog/transcripts (Trump rallies, interviews)
  - whitehouse.gov/briefing-room/press-briefings (Leavitt)
  - factba.se/transcript (Trump, various)
  - c-span.org/video transcripts
  - YouTube auto-captions (via OpenClaw on the video page)
""")


def main():
    entries = load_urls()
    if not entries:
        print("No transcript URLs found in config/corpus_urls.yaml")
        print_template()
        return

    print(f"Found {len(entries)} transcript URLs to scrape")
    print()

    seq_counters: dict[str, int] = {}
    success = 0
    failed = 0

    for entry in entries:
        speaker = entry.get("speaker", "unknown")
        event_type = entry.get("event_type", "other")
        date = entry.get("date", "unknown")
        url = entry.get("url", "")

        if not url:
            print(f"  SKIP: no URL for {speaker}/{event_type}/{date}")
            failed += 1
            continue

        key = f"{speaker}:{event_type}:{date}"
        seq = seq_counters.get(key, 0) + 1
        seq_counters[key] = seq

        # Check if already scraped
        out_path = CORPUS_DIR / speaker / f"{event_type}_{date}_{seq:02d}.txt"
        if out_path.exists() and out_path.stat().st_size > 100:
            print(f"  SKIP (exists): {out_path}")
            success += 1
            continue

        print(f"  Scraping: {speaker}/{event_type}/{date} from {url[:60]}...")
        text = scrape_url(url)

        if not text or len(text) < 50:
            print(f"  FAILED: got {len(text) if text else 0} chars")
            failed += 1
            continue

        path = save_transcript(speaker, event_type, date, seq, text)
        print(f"  OK: {len(text)} chars -> {path}")
        success += 1

        # Small delay between scrapes to be polite
        time.sleep(1)

    print()
    print(f"Done: {success} scraped, {failed} failed, {len(entries)} total")


if __name__ == "__main__":
    main()
