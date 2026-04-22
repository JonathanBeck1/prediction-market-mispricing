#!/usr/bin/env python3
"""Discover new Rev.com transcript URLs for a given speaker.

Tries three backends in priority order:
  1. Brave Search API  (if BRAVE_API_KEY is set in env — best precision)
  2. Rev WordPress RSS feed for the transcripts category
  3. Rev blog HTML search page (plain HTTP, no JS required)

Output: one canonical Rev URL per line, printed to stdout.
Already-ingested URLs (tracked in data/corpus/.rev_ingested.json) are filtered
out automatically; pass --all to see every discovered URL.

Usage
-----
  # Print new URLs for Trump (default):
  python3 scripts/discover_rev_transcripts.py

  # Print all discovered URLs including already-ingested ones:
  python3 scripts/discover_rev_transcripts.py --all

  # Override speaker keyword:
  python3 scripts/discover_rev_transcripts.py --keyword "donald trump"

  # Limit results:
  python3 scripts/discover_rev_transcripts.py --max 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode, urlparse

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests not installed. Run: pip install requests")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("ERROR: beautifulsoup4 not installed. Run: pip install beautifulsoup4")

# ── Load runtime.env (same pattern as app/config.py and analyze_signals.py) ────
def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


_load_env_file(Path(os.getenv("RUNTIME_ENV_FILE", "config/runtime.env")))

# ── Constants ──────────────────────────────────────────────────────────────────
LEDGER_PATH = Path("data/corpus/.rev_ingested.json")

# Rev URL patterns we accept as transcript pages
_REV_TRANSCRIPT_RE = re.compile(
    r"https?://(?:www\.)?rev\.com/"
    r"(?:blog/transcripts|transcripts)/[a-z0-9\-/]+",
    re.IGNORECASE,
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_BRAVE_BASE = "https://api.search.brave.com/res/v1/web/search"
# Rev moved to Webflow — the WordPress RSS feed is gone.
# Rev now uses two URL schemes: /blog/transcripts/ (old) and /transcripts/ (new).
# Fallback: scrape the Trump category page and blog search.
_REV_TRUMP_CATEGORY = "https://www.rev.com/category/donald-trump"
_REV_TRANSCRIPTS_INDEX = "https://www.rev.com/transcripts"
_REV_SEARCH_URL = "https://www.rev.com/blog/"

_DEFAULT_KEYWORD = "trump"


# ── Ledger helpers ─────────────────────────────────────────────────────────────
def load_ledger() -> dict[str, dict]:
    """Load the ingestion ledger. Returns {} if the file doesn't exist yet."""
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def is_ingested(url: str, ledger: dict) -> bool:
    entry = ledger.get(url, {})
    return entry.get("status") in ("ok", "skipped")


# ── URL normalisation ──────────────────────────────────────────────────────────
def normalise_url(raw: str) -> str | None:
    """Return a clean https URL if it matches Rev transcript patterns, else None."""
    raw = raw.strip().rstrip("/")
    if not _REV_TRANSCRIPT_RE.search(raw):
        return None
    parsed = urlparse(raw)
    return f"https://www.rev.com{parsed.path}"


# ── Backend 1: Brave Search API ────────────────────────────────────────────────
def _brave_search(keyword: str, max_results: int, api_key: str) -> list[str]:
    # Note: site:rev.com/blog/transcripts (subdirectory) returns 0 on Brave.
    # site:rev.com with keyword is correct — we filter URLs to Rev transcript
    # patterns in normalise_url after.
    params = {
        "q": f"{keyword} transcript site:rev.com",
        "count": min(max_results, 20),
        "search_lang": "en",
    }
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    try:
        r = requests.get(_BRAVE_BASE, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        results = data.get("web", {}).get("results", [])
        urls = []
        for item in results:
            url = normalise_url(item.get("url", ""))
            if url:
                urls.append(url)
        return urls
    except Exception as exc:
        print(f"[discover] BraveAPI error: {exc}", file=sys.stderr)
        return []


# ── Backend 2: Rev transcripts index page scrape ──────────────────────────────
def _rev_index_scrape(keyword: str, max_results: int) -> list[str]:
    """Scrape Rev's /blog/transcripts index page for links matching keyword."""
    kw_lower = keyword.lower()
    urls: list[str] = []
    seen: set[str] = set()
    # Trump category page + transcripts index + blog search
    pages = [
        _REV_TRUMP_CATEGORY,
        _REV_TRANSCRIPTS_INDEX,
        f"{_REV_SEARCH_URL}?s={keyword.replace(' ', '+')}+transcript",
    ]
    for page_url in pages:
        try:
            r = requests.get(page_url, headers=_HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as exc:
            print(f"[discover] Rev index scrape error ({page_url}): {exc}", file=sys.stderr)
            continue

        for a in soup.find_all("a", href=True):
            url = normalise_url(a["href"])
            if not url or url in seen:
                continue
            text = a.get_text(" ").lower()
            if kw_lower not in url.lower() and kw_lower not in text:
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) >= max_results:
                return urls
    return urls


# ── Backend 3: Rev blog HTML search ───────────────────────────────────────────
def _rev_html_search(keyword: str, max_results: int) -> list[str]:
    """Scrape Rev's blog search page for transcript links matching keyword."""
    params = {"s": f"{keyword} transcript"}
    try:
        r = requests.get(_REV_SEARCH_URL, params=params, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        print(f"[discover] Rev HTML scrape error: {exc}", file=sys.stderr)
        return []

    kw_lower = keyword.lower()
    urls = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        url = normalise_url(a["href"])
        if not url or url in seen:
            continue
        # Require keyword to appear in URL or link text
        text = a.get_text(" ").lower()
        if kw_lower not in url.lower() and kw_lower not in text:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= max_results:
            break
    return urls


# ── Main discovery function (all backends) ────────────────────────────────────
def discover(
    keyword: str = _DEFAULT_KEYWORD,
    max_results: int = 10,
    show_all: bool = False,
    verbose: bool = False,
) -> list[str]:
    """Return a deduplicated list of new Rev transcript URLs.

    If show_all=False (default), URLs already in the ingestion ledger are removed.
    """
    ledger = load_ledger()
    api_key = os.getenv("BRAVE_API_KEY", "").strip()

    candidates: list[str] = []
    used_backend = ""

    if api_key:
        if verbose:
            print("[discover] Using Brave Search API", file=sys.stderr)
        candidates = _brave_search(keyword, max_results * 2, api_key)
        used_backend = "brave"

    if not candidates:
        if verbose:
            print("[discover] Trying Rev transcripts index page", file=sys.stderr)
        candidates = _rev_index_scrape(keyword, max_results * 2)
        used_backend = "rev_index"

    if not candidates:
        if verbose:
            print("[discover] Trying Rev blog HTML search", file=sys.stderr)
        candidates = _rev_html_search(keyword, max_results * 2)
        used_backend = "rev_html"

    if verbose and candidates:
        print(f"[discover] Backend '{used_backend}' returned {len(candidates)} candidates", file=sys.stderr)

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            unique.append(url)

    if not show_all:
        unique = [u for u in unique if not is_ingested(u, ledger)]

    return unique[:max_results]


# ── CLI ────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Discover new Rev transcript URLs for a speaker.",
    )
    p.add_argument(
        "--keyword",
        default=_DEFAULT_KEYWORD,
        help="Search keyword / speaker name (default: 'trump')",
    )
    p.add_argument(
        "--max",
        type=int,
        default=10,
        metavar="N",
        help="Maximum URLs to return (default: 10)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Include already-ingested URLs",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print backend diagnostics to stderr",
    )
    args = p.parse_args(argv)

    urls = discover(
        keyword=args.keyword,
        max_results=args.max,
        show_all=args.show_all,
        verbose=args.verbose,
    )

    for url in urls:
        print(url)

    if not urls:
        if args.verbose:
            print("[discover] No new URLs found.", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
