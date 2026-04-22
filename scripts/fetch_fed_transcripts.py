#!/usr/bin/env python3
"""Fetch Fed press conference transcripts from federalreserve.gov.

The Federal Reserve publishes PDF transcripts for FOMC press conferences at:
  https://www.federalreserve.gov/mediacenter/files/FOMCpresconf{YYYYMMDD}.pdf

This script:
  1. Discovers available press conference dates from the Fed FOMC calendar page
  2. Downloads each PDF and extracts Powell's (or the chair's) text
  3. Writes to data/corpus/powell/press_conference_{date}_{seq}.txt

Usage:
    python3 scripts/fetch_fed_transcripts.py
    python3 scripts/fetch_fed_transcripts.py --max 10
    python3 scripts/fetch_fed_transcripts.py --since 2022-01-01
    python3 scripts/fetch_fed_transcripts.py --dry-run
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: requests and beautifulsoup4 required.")
    sys.exit(1)

try:
    from pdfminer.high_level import extract_text_to_fp
    from pdfminer.layout import LAParams
    _PDF_OK = True
except ImportError:
    _PDF_OK = False

CORPUS_DIR   = Path("data/corpus/powell")
FOMC_CAL     = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
PDF_URL_TMPL = "https://www.federalreserve.gov/mediacenter/files/FOMCpresconf{date}.pdf"

CHAIR_LABELS = frozenset([
    "chair powell", "mr. powell", "mr powell", "powell",
    "chairman powell", "jerome powell",
])

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (research-bot/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/pdf",
}


def _get(url: str, timeout: int = 30, stream: bool = False) -> requests.Response:
    resp = requests.get(url, headers=_HEADERS, timeout=timeout, stream=stream)
    resp.raise_for_status()
    return resp


def discover_fomc_dates(since: date | None = None) -> list[str]:
    """Return list of YYYYMMDD strings for available FOMC press conferences."""
    try:
        resp = _get(FOMC_CAL)
    except Exception as exc:
        print(f"WARNING: Could not fetch FOMC calendar: {exc}")
        return []
    soup  = BeautifulSoup(resp.text, "html.parser")
    dates = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"fomcpresconf(\d{8})\.htm", a["href"])
        if m:
            datestr = m.group(1)
            if since is not None:
                try:
                    d = datetime.strptime(datestr, "%Y%m%d").date()
                    if d < since:
                        continue
                except ValueError:
                    pass
            if datestr not in dates:
                dates.append(datestr)
    dates.sort(reverse=True)
    return dates


def _pdf_to_text(pdf_bytes: bytes) -> str | None:
    if not _PDF_OK:
        return None
    try:
        buf = io.StringIO()
        extract_text_to_fp(io.BytesIO(pdf_bytes), buf, laparams=LAParams())
        return buf.getvalue()
    except Exception as exc:
        print(f"  WARNING: pdfminer error: {exc}")
        return None


def extract_chair_text_from_pdf(pdf_text: str) -> str:
    """Extract paragraphs attributed to the Fed chair from raw PDF text."""
    lines = [l.strip() for l in pdf_text.splitlines() if l.strip()]
    chunks: list[str] = []
    current_is_chair = False

    for line in lines:
        line_lower = line.lower()
        # Speaker attribution pattern: "CHAIR POWELL. " or "MR. POWELL. "
        is_speaker = any(line_lower.startswith(label) for label in CHAIR_LABELS)
        if is_speaker:
            current_is_chair = True
            # Strip the speaker label prefix
            m = re.match(r"^[A-Za-z .]+\.\s*", line)
            content = line[m.end():].strip() if m else line
            if content:
                chunks.append(content)
        elif re.match(r"^[A-Z][A-Z\s\.]{3,}\.\s", line):
            # Another speaker → stop capturing
            current_is_chair = False
        elif current_is_chair and len(line) > 20:
            chunks.append(line)

    return "\n\n".join(chunks)


def _next_seq(outdir: Path, prefix: str) -> int:
    return len(list(outdir.glob(f"{prefix}_*.txt"))) + 1


def fetch_and_save(datestr: str, dry_run: bool = False) -> bool:
    url = PDF_URL_TMPL.format(date=datestr)
    try:
        resp = _get(url, stream=True)
        pdf_bytes = resp.content
    except Exception as exc:
        print(f"  SKIP {datestr}: fetch failed — {exc}")
        return False

    raw_text = _pdf_to_text(pdf_bytes)
    if raw_text is None:
        print(f"  SKIP {datestr}: pdfminer not available — install pdfminer.six")
        return False

    text = extract_chair_text_from_pdf(raw_text)

    if not text or len(text) < 500:
        # Fall back to full PDF text (still useful for phrase matching)
        text = "\n\n".join(
            l.strip() for l in raw_text.splitlines()
            if len(l.strip()) > 30
        )
        if not text or len(text) < 500:
            print(f"  SKIP {datestr}: extracted text too short ({len(text)} chars)")
            return False

    try:
        d = datetime.strptime(datestr, "%Y%m%d")
        date_fmt = d.strftime("%Y-%m-%d")
    except ValueError:
        date_fmt = datestr

    prefix   = f"press_conference_{date_fmt}"
    seq      = _next_seq(CORPUS_DIR, prefix)
    out_path = CORPUS_DIR / f"{prefix}_{seq:02d}.txt"

    if dry_run:
        words = len(text.split())
        print(f"  [dry-run] Would write {out_path.name}  ({len(text)} chars / {words} words)")
        return True

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    words = len(text.split())
    print(f"  Wrote {out_path.name}  ({len(text)} chars / {words} words)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Fed FOMC press conference transcripts")
    parser.add_argument("--max",      type=int,   default=20)
    parser.add_argument("--since",    type=str,   default="2022-01-01")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--delay",    type=float, default=1.5)
    args = parser.parse_args()

    since_date: date | None = None
    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: invalid --since date: {args.since}")
            sys.exit(1)

    if not _PDF_OK:
        print("WARNING: pdfminer.six not installed. Run: pip3 install pdfminer.six")
        print("Continuing — will skip PDFs that cannot be parsed.")

    existing_dates: set[str] = set()
    if CORPUS_DIR.exists():
        for f in CORPUS_DIR.glob("press_conference_*.txt"):
            m = re.search(r"press_conference_(\d{4}-\d{2}-\d{2})", f.name)
            if m:
                existing_dates.add(m.group(1).replace("-", ""))

    print("Discovering FOMC press conference dates...")
    dates = discover_fomc_dates(since=since_date)
    if not dates:
        print("No dates found from FOMC calendar.")
        sys.exit(1)

    print(f"Found {len(dates)} dates. Already have {len(existing_dates)} in corpus.")
    new_dates   = [d for d in dates if d not in existing_dates]
    fetch_dates = new_dates[: args.max]
    print(f"Fetching {len(fetch_dates)} new transcripts (PDF)...")

    ok = 0
    for datestr in fetch_dates:
        if fetch_and_save(datestr, dry_run=args.dry_run):
            ok += 1
        if not args.dry_run:
            time.sleep(args.delay)

    print(f"\nDone: {ok}/{len(fetch_dates)} transcripts saved to {CORPUS_DIR}/")

    if ok > 0 and not args.dry_run:
        print("\nRunning ingest_corpus to update DB...")
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/ingest_corpus.py"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(result.stdout.strip())
        else:
            print(f"WARNING: ingest_corpus failed: {result.stderr[:200]}")


if __name__ == "__main__":
    main()
