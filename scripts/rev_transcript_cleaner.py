#!/usr/bin/env python3
"""Rev transcript cleaner — extract a single speaker's words and save to corpus.

Pulls a Rev.com transcript page (or parses saved HTML), filters to the target
speaker's turns, cleans the text, and writes a ready-to-ingest corpus file.

Usage examples
--------------
# From a live URL:
python3 scripts/rev_transcript_cleaner.py \\
    "https://www.rev.com/transcripts/kennedy-center-luncheon" \\
    --corpus-speaker trump \\
    --speaker "Donald Trump"

# Supply the date explicitly if it can't be inferred:
python3 scripts/rev_transcript_cleaner.py \\
    "https://www.rev.com/transcripts/executive-orders-3-16-26" \\
    --corpus-speaker trump \\
    --speaker "Donald Trump" \\
    --date 2026-03-16

# From a locally-saved HTML file:
python3 scripts/rev_transcript_cleaner.py \\
    --html-file ./saved_page.html \\
    --corpus-speaker trump \\
    --speaker "Donald Trump" \\
    --date 2026-03-15 \\
    --event-type general

# Print to stdout instead of saving:
python3 scripts/rev_transcript_cleaner.py \\
    "https://www.rev.com/transcripts/some-speech" \\
    --corpus-speaker trump \\
    --stdout

# Batch mode — one URL per line in a file:
python3 scripts/rev_transcript_cleaner.py \\
    --urls-file urls.txt \\
    --corpus-speaker trump

Notes
-----
- Requires: beautifulsoup4, requests (both already in project deps)
- No browser automation needed; pure HTTP + HTML parsing.
- If Rev serves a JS-rendered page with no transcript content, the script will
  print a clear error and suggest using --html-file with a manually-saved copy.
- Files are written with O_EXCL (exclusive create) — will never overwrite.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# ── Optional imports ───────────────────────────────────────────────────────────
try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    sys.exit("ERROR: beautifulsoup4 not installed. Run: pip install beautifulsoup4")

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────
CORPUS_ROOT = Path("data/corpus")

# Default speaker labels recognised as Trump across Rev transcripts
_DEFAULT_TRUMP_SPEAKERS: tuple[str, ...] = (
    "donald trump",
    "president trump",
    "president donald trump",
    "trump",
)

# HTTP headers that look like a real browser to avoid 403s
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
}

# Timestamp patterns to strip: (00:00), 00:00, (1:23:45), 00:00:00
_TS_RE = re.compile(r"\(\s*\d{1,2}:\d{2}(?::\d{2})?\s*\)|\b\d{1,2}:\d{2}(?::\d{2})?\b")

# Bracket artifacts: [inaudible], [crosstalk], [applause], [laughter], [BLANK_AUDIO], etc.
_BRACKET_RE = re.compile(r"\[[^\]]{1,60}\]", re.IGNORECASE)

# Speaker-label patterns: "Donald Trump:" or "DONALD TRUMP:" at line start
_SPEAKER_LABEL_RE = re.compile(r"^[A-Z][A-Za-z .'-]{1,50}:\s*", re.MULTILINE)

# Rev event-type keywords → canonical event_type value
_EVENT_TYPE_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rally", re.I), "rally"),
    (re.compile(r"signing|executive.order", re.I), "signing"),
    (re.compile(r"press.brief|briefing", re.I), "briefing"),
    (re.compile(r"interview", re.I), "interview"),
    (re.compile(r"address|speech", re.I), "address"),
    (re.compile(r"news.conf|presser|press.conf", re.I), "presser"),
    (re.compile(r"town.?hall|townhall", re.I), "townhall"),
    (re.compile(r"summit|bilateral", re.I), "summit"),
    (re.compile(r"announcement|announce", re.I), "announcement"),
    (re.compile(r"visit|tour", re.I), "visit"),
]

# Date patterns in URL slugs: 2-15-26, 02-15-2026, 2026-02-15, february-15-2026
_DATE_SLUG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ISO: 2026-02-15
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), "iso"),
    # MM-DD-YY: 2-15-26
    (re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})-(\d{2})(?!\d)"), "mdy2"),
    # MM-DD-YYYY: 2-15-2026
    (re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})-(\d{4})(?!\d)"), "mdy4"),
]

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── Data types ─────────────────────────────────────────────────────────────────
class SpeakerTurn(NamedTuple):
    speaker: str
    text: str


class ParseResult(NamedTuple):
    turns: list[SpeakerTurn]
    all_speakers: list[str]        # every distinct speaker label found on page
    format_used: str               # for diagnostics


# ── HTML fetching ──────────────────────────────────────────────────────────────
def fetch_html(url: str, timeout: int = 20) -> str:
    if not _REQUESTS_AVAILABLE:
        sys.exit("ERROR: requests not installed. Run: pip install requests")
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            sys.exit(
                f"ERROR: 403 Forbidden from {url}\n"
                "Rev may be blocking direct fetches. Save the page manually\n"
                "(File > Save As > Webpage, Complete) then use:\n"
                f"  --html-file saved.html --date YYYY-MM-DD"
            )
        sys.exit(f"ERROR: HTTP {e.response.status_code if e.response else '?'} from {url}: {e}")
    except requests.exceptions.ConnectionError as e:
        sys.exit(f"ERROR: Could not connect to {url}: {e}")
    except requests.exceptions.Timeout:
        sys.exit(f"ERROR: Timed out fetching {url}")


# ── HTML parsing ───────────────────────────────────────────────────────────────
def parse_html(html: str) -> ParseResult:
    """Try Rev's known HTML formats in order of specificity.

    Supported formats
    -----------------
    1. Webflow richtext: <p>Speaker (<a>ts</a>): text</p> in .w-richtext divs
       (Rev's current /transcripts/ format as of 2025+)
    2. Structured segments: <div class="ts-segment" data-speaker="Name"> or
       children containing a speaker-label element + text element.
    3. Inline-strong paragraphs: <p><strong>Name: (ts)</strong> text</p>
    4. Two-block paragraphs: consecutive <p><strong>Name:</strong></p><p>text</p>
    5. Generic article extraction with post-hoc speaker splitting.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove script / style / nav / footer noise
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "noscript", "iframe"]):
        tag.decompose()

    result = _parse_webflow_richtext(soup)
    if result.turns:
        return result

    result = _parse_structured_segments(soup)
    if result.turns:
        return result

    result = _parse_inline_strong(soup)
    if result.turns:
        return result

    result = _parse_two_block(soup)
    if result.turns:
        return result

    result = _parse_generic_article(soup)
    return result


def _normalise_speaker(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip().strip(":").strip()).lower()


# ── Webflow richtext parser (Rev /transcripts/ format, 2025+) ─────────────────
# Real paragraph structure (discovered via inspection 2026-03):
#   <p>Speaker Name ( 00:00 ):</p>          ← speaker label, own paragraph
#   <p>Speech text here.</p>                ← text (one or more paragraphs)
#   <p>( 01:19 ) More text continues.</p>   ← continuation with inline timestamp
#   <p>Next Speaker ( 02:00 ):</p>          ← next turn
_WF_SPEAKER_LABEL_RE = re.compile(
    r"^(?P<speaker>[A-Z][A-Za-z .'\-]{1,60}?)"
    r"\s*\(\s*[\d:]+\s*\)"
    r"\s*:?\s*$",
    re.IGNORECASE,
)
# Inline timestamp at start of a continuation paragraph
_WF_INLINE_TS_RE = re.compile(r"^\(\s*[\d:]+\s*\)\s*")


def _best_richtext_container(soup: BeautifulSoup) -> Tag | None:
    """Find the primary transcript container among all .w-richtext divs.

    Rev puts the copyright notice in a small .w-richtext div that appears
    BEFORE the main transcript in document order, so soup.find() returns
    the wrong element.  Strategy:
      1. id="main-content" (present on /transcripts/ pages)
      2. fs-toc-element="contents" attribute
      3. Largest .w-richtext div by text length
    """
    # 1. Explicit main-content ID
    el = soup.find(id="main-content")
    if el:
        return el
    # 2. Finsweet TOC marker (Webflow plugin attr used on the transcript body)
    el = soup.find(attrs={"fs-toc-element": "contents"})
    if el:
        return el
    # 3. Largest richtext div
    candidates = soup.find_all(class_=re.compile(r"w-richtext|richtext|blog-text-rich", re.I))
    if not candidates:
        return None
    return max(candidates, key=lambda c: len(c.get_text()))


def _parse_webflow_richtext(soup: BeautifulSoup) -> ParseResult:
    """Rev /transcripts/ Webflow format (2025+).

    Actual paragraph structure:
      <p>Speaker Name ( 00:00 ):</p>          ← speaker label, no body text
      <p>Speech text here.</p>                ← one or more text paragraphs
      <p>( 01:19 ) Continuation text.</p>     ← same speaker, inline timestamp
      <p>Next Speaker ( 02:00 ):</p>          ← new turn
    """
    container = _best_richtext_container(soup)
    if not container:
        return ParseResult([], [], "webflow_richtext")

    # Unwrap all <a> tags — keep link text, discard href wrappers
    for a in container.find_all("a"):
        a.unwrap()

    raw_turns: list[tuple[str, list[str]]] = []  # (speaker, [text_paragraphs])
    current_speaker: str | None = None

    for p in container.find_all("p"):
        text = p.get_text(separator=" ").strip()
        if not text:
            continue

        m = _WF_SPEAKER_LABEL_RE.match(text)
        if m:
            # New speaker turn starts
            current_speaker = m.group("speaker").strip()
            raw_turns.append((current_speaker, []))
        elif current_speaker and raw_turns:
            # Text paragraph — strip any leading inline timestamp
            body = _WF_INLINE_TS_RE.sub("", text).strip()
            if body:
                raw_turns[-1][1].append(body)

    if not raw_turns:
        return ParseResult([], [], "webflow_richtext")

    # Flatten: join each speaker's paragraphs into a single string
    flat_turns: list[tuple[str, str]] = [
        (spk, "\n\n".join(paras))
        for spk, paras in raw_turns
        if paras  # skip turns with no text (label-only with no body)
    ]
    if not flat_turns:
        return ParseResult([], [], "webflow_richtext")

    result = _collect_turns(flat_turns)
    return ParseResult(result.turns, result.all_speakers, "webflow_richtext")


def _collect_turns(raw_turns: list[tuple[str, str]]) -> ParseResult:
    all_speakers = []
    seen: set[str] = set()
    turns: list[SpeakerTurn] = []
    for spk, text in raw_turns:
        norm = _normalise_speaker(spk)
        if norm not in seen:
            seen.add(norm)
            all_speakers.append(norm)
        turns.append(SpeakerTurn(speaker=norm, text=text.strip()))
    return ParseResult(turns=turns, all_speakers=all_speakers, format_used="?")


def _parse_structured_segments(soup: BeautifulSoup) -> ParseResult:
    """Rev newer format: <div class="ts-segment" data-speaker="...">"""
    segments = soup.find_all(attrs={"data-speaker": True})
    if not segments:
        # Also look for ts-segment divs with a child speaker-label element
        segments = soup.find_all("div", class_=re.compile(r"ts-segment|segment", re.I))
    if not segments:
        return ParseResult([], [], "structured_segments")

    raw_turns: list[tuple[str, str]] = []
    for seg in segments:
        # Speaker name: from data-speaker attribute or from .ts-speaker-label child
        spk = seg.get("data-speaker", "").strip()
        if not spk:
            lbl = seg.find(class_=re.compile(r"speaker.?label|speaker.?name", re.I))
            if lbl:
                spk = lbl.get_text(separator=" ").strip()
        if not spk:
            continue
        # Text: everything except timestamp and speaker-label children
        for noise in seg.find_all(class_=re.compile(r"timestamp|speaker.?label|speaker.?name", re.I)):
            noise.decompose()
        text = seg.get_text(separator=" ").strip()
        if text:
            raw_turns.append((spk, text))

    if not raw_turns:
        return ParseResult([], [], "structured_segments")
    result = _collect_turns(raw_turns)
    return ParseResult(result.turns, result.all_speakers, "structured_segments")


def _parse_inline_strong(soup: BeautifulSoup) -> ParseResult:
    """Classic Rev format: <p><strong>Speaker: (00:00)</strong> text</p>"""
    # Pattern: a <strong> whose text ends with ": (timestamp)" or just ":"
    _spk_strong_re = re.compile(r"^(.{1,60}):\s*(?:\(\s*[\d:]+\s*\))?\s*$")
    raw_turns: list[tuple[str, str]] = []

    for p in soup.find_all("p"):
        strong = p.find("strong")
        if not strong:
            continue
        strong_text = strong.get_text(separator=" ").strip()
        m = _spk_strong_re.match(strong_text)
        if not m:
            # Try matching just "Name:" with timestamp in the text
            m = re.match(r"^(.{1,60}):", strong_text)
        if not m:
            continue
        spk = m.group(1).strip()
        # Get text after the strong tag
        strong.extract()
        tail_text = p.get_text(separator=" ").strip()
        if tail_text or strong_text:
            raw_turns.append((spk, tail_text or ""))

    if not raw_turns:
        return ParseResult([], [], "inline_strong")
    result = _collect_turns(raw_turns)
    return ParseResult(result.turns, result.all_speakers, "inline_strong")


def _parse_two_block(soup: BeautifulSoup) -> ParseResult:
    """Some Rev pages: <p><strong>Name:</strong></p> followed by <p>text</p>"""
    _name_only_re = re.compile(r"^(.{1,60}):\s*$")
    raw_turns: list[tuple[str, str]] = []
    paras = soup.find_all("p")
    i = 0
    while i < len(paras):
        p = paras[i]
        strong = p.find("strong")
        if strong and not p.get_text().replace(strong.get_text(), "").strip():
            m = _name_only_re.match(strong.get_text().strip())
            if m and i + 1 < len(paras):
                spk = m.group(1)
                text = paras[i + 1].get_text(separator=" ").strip()
                raw_turns.append((spk, text))
                i += 2
                continue
        i += 1

    if not raw_turns:
        return ParseResult([], [], "two_block")
    result = _collect_turns(raw_turns)
    return ParseResult(result.turns, result.all_speakers, "two_block")


def _parse_generic_article(soup: BeautifulSoup) -> ParseResult:
    """Fallback: extract body text then split on 'Speaker: text' patterns."""
    containers = (
        soup.find(class_=re.compile(r"fl-callout-text|callout|transcript|article-body|entry-content|post-content", re.I))
        or soup.find("article")
        or soup.find("main")
        or soup.body
    )
    if not containers:
        return ParseResult([], [], "generic_article")

    full_text = containers.get_text(separator="\n")
    # Split on "Speaker Name: text" at start of line
    _line_spk_re = re.compile(
        r"^(?P<speaker>[A-Z][A-Za-z .'\-]{1,50}):\s*(?P<text>.+)$",
        re.MULTILINE,
    )
    raw_turns: list[tuple[str, str]] = []
    for m in _line_spk_re.finditer(full_text):
        raw_turns.append((m.group("speaker"), m.group("text")))

    if not raw_turns:
        return ParseResult([], [], "generic_article")
    result = _collect_turns(raw_turns)
    return ParseResult(result.turns, result.all_speakers, "generic_article")


# ── Text cleaning ──────────────────────────────────────────────────────────────
def clean_text(raw: str) -> str:
    """Remove timestamps, bracket artifacts, extra whitespace; preserve paragraphs."""
    text = _TS_RE.sub("", raw)
    text = _BRACKET_RE.sub("", text)
    # Remove leading speaker-label remnants (e.g. "Donald Trump: " at line start)
    text = _SPEAKER_LABEL_RE.sub("", text)
    # Collapse runs of spaces (but not newlines)
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse 3+ blank lines to 2 (preserve paragraph breaks)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text


def filter_and_join(
    turns: list[SpeakerTurn],
    target_speakers_norm: set[str],
) -> str:
    """Keep only target-speaker turns; join with paragraph breaks."""
    paragraphs: list[str] = []
    for turn in turns:
        if turn.speaker in target_speakers_norm:
            cleaned = clean_text(turn.text)
            if cleaned:
                paragraphs.append(cleaned)
    return "\n\n".join(paragraphs)


# ── Date / event_type inference ────────────────────────────────────────────────
def infer_date_from_slug(slug: str) -> str | None:
    """Try to extract YYYY-MM-DD from a URL slug. Returns None if uncertain."""
    for pat, fmt in _DATE_SLUG_PATTERNS:
        m = pat.search(slug)
        if m:
            try:
                if fmt == "iso":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif fmt == "mdy2":
                    mo, d, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    y = 2000 + y2
                elif fmt == "mdy4":
                    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    continue
                if 2020 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
            except ValueError:
                continue
    return None


def infer_date_from_page(soup: BeautifulSoup) -> str | None:
    """Try to find a date in JSON-LD, og:description, <title>, or visible date elements."""
    import json as _json

    # 1. JSON-LD schema.org: datePublished / dateModified (most reliable)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(script.string or "")
            # Handle single object or @graph array
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                for key in ("datePublished", "dateModified", "date"):
                    val = node.get(key, "")
                    if val and re.match(r"^\d{4}-\d{2}-\d{2}", val):
                        return val[:10]
                # Also check nested 'about' article
                about = node.get("about", {})
                if isinstance(about, dict):
                    for key in ("datePublished", "dateModified"):
                        val = about.get(key, "")
                        if val and re.match(r"^\d{4}-\d{2}-\d{2}", val):
                            return val[:10]
        except Exception:
            pass

    candidates: list[str] = []

    title = soup.find("title")
    if title:
        candidates.append(title.get_text())
    og_title = soup.find("meta", property="og:title")
    if og_title:
        candidates.append(og_title.get("content", ""))
    og_desc = soup.find("meta", property="og:description")
    if og_desc:
        candidates.append(og_desc.get("content", ""))
    # Visible date-like elements
    for cls in ["date", "pub-date", "article-date", "entry-date", "post-date"]:
        el = soup.find(class_=re.compile(cls, re.I))
        if el:
            candidates.append(el.get_text())

    # Pattern: "Month DD, YYYY" or "Month DD YYYY"
    _long_date_re = re.compile(
        r"(?P<month>january|february|march|april|may|june|july|august|"
        r"september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
        r"\s+(?P<day>\d{1,2})[,\s]+(?P<year>20\d{2})",
        re.IGNORECASE,
    )
    for text in candidates:
        m = _long_date_re.search(text)
        if m:
            mo_name = m.group("month").lower()
            mo = _MONTH_NAMES.get(mo_name)
            if mo:
                d = int(m.group("day"))
                y = int(m.group("year"))
                if 2020 <= y <= 2035 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def infer_event_type(slug: str, title: str = "") -> str:
    """Map URL slug keywords to canonical event_type. Falls back to 'general'."""
    combined = (slug + " " + title).lower()
    for pat, etype in _EVENT_TYPE_MAP:
        if pat.search(combined):
            return etype
    return "general"


# ── File naming ────────────────────────────────────────────────────────────────
def choose_output_path(
    corpus_speaker: str,
    event_type: str,
    date: str,
) -> Path:
    """Return a new path, auto-incrementing seq to avoid collisions."""
    out_dir = CORPUS_ROOT / corpus_speaker
    out_dir.mkdir(parents=True, exist_ok=True)
    seq = 1
    while True:
        p = out_dir / f"{event_type}_{date}_{seq:02d}.txt"
        if not p.exists():
            return p
        seq += 1


def write_exclusive(path: Path, text: str) -> None:
    """Write text to path, refusing to overwrite (O_EXCL / X flag)."""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        sys.exit(f"ERROR: File already exists: {path}")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


# ── Per-URL processing ─────────────────────────────────────────────────────────
def process_source(
    *,
    html: str,
    url: str | None,
    target_speakers: list[str],
    corpus_speaker: str,
    event_type_override: str | None,
    date_override: str | None,
    stdout_mode: bool,
) -> bool:
    """Parse HTML, filter, clean, write. Returns True on success."""
    soup = BeautifulSoup(html, "html.parser")

    # ── Date ──────────────────────────────────────────────────────────────────
    date = date_override
    if not date and url:
        date = infer_date_from_slug(url)
    if not date:
        date = infer_date_from_page(soup)
    if not date:
        print(
            "ERROR: Could not infer date from URL or page content.\n"
            "Supply --date YYYY-MM-DD explicitly.",
            file=sys.stderr,
        )
        return False

    # ── Event type ────────────────────────────────────────────────────────────
    slug = url or ""
    page_title = (soup.find("title") or soup.find("h1") or "")
    page_title_str = page_title.get_text() if hasattr(page_title, "get_text") else str(page_title)
    event_type = event_type_override or infer_event_type(slug, page_title_str)

    # ── Parse + filter ────────────────────────────────────────────────────────
    parse_result = parse_html(html)
    target_norm = {s.lower().strip() for s in target_speakers}
    output_text = filter_and_join(parse_result.turns, target_norm)

    if not output_text:
        detected = parse_result.all_speakers
        print(
            f"ERROR: No text found for speaker(s): {target_speakers}\n"
            f"       Detected speakers on page: {detected or ['(none — transcript may be JS-rendered)']}\n"
            f"       Format tried: {parse_result.format_used}\n"
            f"       If the page requires JavaScript, save it manually and use --html-file.",
            file=sys.stderr,
        )
        return False

    # ── Output ────────────────────────────────────────────────────────────────
    line_count = output_text.count("\n") + 1

    if stdout_mode:
        print(output_text)
    else:
        out_path = choose_output_path(corpus_speaker, event_type, date)
        write_exclusive(out_path, output_text)
        _print_summary(
            url=url,
            date=date,
            event_type=event_type,
            all_speakers=parse_result.all_speakers,
            format_used=parse_result.format_used,
            char_count=len(output_text),
            line_count=line_count,
            out_path=out_path,
        )

    return True


def _print_summary(
    *,
    url: str | None,
    date: str,
    event_type: str,
    all_speakers: list[str],
    format_used: str,
    char_count: int,
    line_count: int,
    out_path: Path,
) -> None:
    print()
    print("━" * 60)
    print(f"  URL         : {url or '(local file)'}")
    print(f"  Date        : {date}")
    print(f"  Event type  : {event_type}")
    print(f"  HTML format : {format_used}")
    print(f"  Speakers    : {', '.join(all_speakers) or '(none detected)'}")
    print(f"  Output      : {char_count:,} chars  {line_count} lines")
    print(f"  Saved to    : {out_path}")
    print("━" * 60)
    print()
    print("Next step: run ingest_corpus.py to load into the DB and re-run phrase matching.")
    print("  python3 scripts/ingest_corpus.py")
    print()


# ── CLI ────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract a speaker's words from a Rev transcript and save to corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              # Live URL
              python3 scripts/rev_transcript_cleaner.py \\
                  "https://www.rev.com/transcripts/trump-kennedy-center-3-24-26" \\
                  --corpus-speaker trump

              # Local saved HTML
              python3 scripts/rev_transcript_cleaner.py \\
                  --html-file saved.html --corpus-speaker trump --date 2026-03-24

              # Batch
              python3 scripts/rev_transcript_cleaner.py \\
                  --urls-file urls.txt --corpus-speaker trump
        """),
    )
    p.add_argument(
        "url",
        nargs="?",
        help="Rev transcript URL to fetch and parse.",
    )
    p.add_argument(
        "--html-file",
        metavar="PATH",
        help="Parse a locally-saved HTML file instead of fetching a URL.",
    )
    p.add_argument(
        "--urls-file",
        metavar="PATH",
        help=(
            "Text file with one Rev URL per line. "
            "Blank lines and lines starting with # are ignored."
        ),
    )
    p.add_argument(
        "--corpus-speaker",
        required=True,
        metavar="SLUG",
        help="Corpus sub-folder, e.g. trump (must exist or be created under data/corpus/).",
    )
    p.add_argument(
        "--speaker",
        action="append",
        dest="speakers",
        metavar="NAME",
        help=(
            "Speaker label(s) to extract, case-insensitive. "
            "Repeatable. Default: Trump label set."
        ),
    )
    p.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Override date (required if it cannot be inferred from URL/page).",
    )
    p.add_argument(
        "--event-type",
        metavar="TYPE",
        help=(
            "Override event type (rally, briefing, signing, interview, address, "
            "presser, townhall, summit, announcement, visit, general). "
            "Auto-inferred from URL slug if omitted."
        ),
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="Print cleaned text to stdout instead of writing a file.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=20,
        metavar="SEC",
        help="HTTP request timeout in seconds (default: 20).",
    )
    return p


def _load_urls_file(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def _resolve_target_speakers(raw: list[str] | None) -> list[str]:
    if raw:
        return raw
    # Default to the known Trump label set
    return list(_DEFAULT_TRUMP_SPEAKERS)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    target_speakers = _resolve_target_speakers(args.speakers)

    # ── Collect sources ────────────────────────────────────────────────────────
    sources: list[tuple[str | None, str | None]] = []  # (url, html_file_path)

    if args.urls_file:
        urls = _load_urls_file(args.urls_file)
        print(f"Batch mode: {len(urls)} URLs from {args.urls_file}")
        for u in urls:
            sources.append((u, None))

    if args.url:
        sources.append((args.url, None))

    if args.html_file:
        sources.append((None, args.html_file))

    if not sources:
        parser.error("Provide a URL, --html-file, or --urls-file.")

    # ── Process each source ────────────────────────────────────────────────────
    success_count = 0
    fail_count = 0

    for url, html_file in sources:
        if url:
            print(f"Fetching: {url}")
            html = fetch_html(url, timeout=args.timeout)
        else:
            assert html_file
            print(f"Parsing: {html_file}")
            html = Path(html_file).read_text(encoding="utf-8")

        ok = process_source(
            html=html,
            url=url,
            target_speakers=target_speakers,
            corpus_speaker=args.corpus_speaker,
            event_type_override=args.event_type,
            date_override=args.date,
            stdout_mode=args.stdout,
        )
        if ok:
            success_count += 1
        else:
            fail_count += 1

    if len(sources) > 1:
        print(f"\nBatch complete: {success_count} succeeded, {fail_count} failed.")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
