#!/usr/bin/env python3
"""Fetch real mention markets from Kalshi API and save to local cache.

Usage:
    python3 scripts/fetch_markets.py

Outputs:
    data/kalshi_markets.json   - full market data cache
    config/live_markets.yaml   - human-readable summary for review

The live markets replace the hardcoded mock catalog. The system reads from
data/kalshi_markets.json at startup.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MARKETS_CACHE_PATH = Path("data/kalshi_markets.json")
EVENTS_CACHE_PATH = Path("data/kalshi_events.json")
_FETCH_ERRORS: list[dict[str, str]] = []

# Seed series tickers for mention markets. Discovery functions will find
# additional series dynamically — no speaker restriction is applied.
# Series that we actively fetch and score.
# Only political / policy / speaker mention markets belong here.
MENTION_SERIES: dict[str, str] = {
    # ── Trump ──────────────────────────────────────────────────────────────
    "KXTRUMPSAY":           "trump",
    "KXTRUMPSAYEP":         "trump",
    "KXTRUMPMENTION":       "trump",
    "KXTRUMPMENTIONB":      "trump",
    "KXDJTRALLY":           "trump",
    "KXDJTINVESTMENT":      "trump",
    "KXDJTWOMENS":          "trump",
    "KXDJTCONF":            "trump",
    "KXTRUMPSAYMONTH":      "trump",
    "KXTRUMPSAYNICKNAME":   "trump",
    "KXTRUMPLATE":          "trump",
    "KXTRUMPMENTIONDURATION": "trump",
    # ── Leavitt / Press Secretary ──────────────────────────────────────────
    "KXLEAVITTMENTION":         "leavitt",
    "KXLEAVITTSMFMENTION":      "leavitt",
    "KXSECPRESSMENTION":        "leavitt",
    "KXLEAVITTLATE":            "leavitt",
    "KXLEAVITTMENTIONDURATION": "leavitt",
    # ── Mamdani ────────────────────────────────────────────────────────────
    "KXMAMDANIMENTION": "mamdani",
    "KXTRUMPSAYMAM":    "mamdani",
    # ── General presidential / WH ──────────────────────────────────────────
    "KXMENTION":        "auto",
    "KXPRESMENTION":    "trump",
    "KXWHPRESSBRIEFING": "whitehouse",
    # ── Federal Reserve / Powell ───────────────────────────────────────────
    "KXFEDMENTION":     "powell",
    # ── Other political / policy speakers ─────────────────────────────────
    "KXSTARMERMENTIONB":  "starmer",
    "KXHOMANMENTION":     "homan",
    "KXCARNEYMENTION":    "carney",
    "KXAOCMENTION":       "aoc",
    "KXHOCHULMENTION":    "hochul",
    "KXNEWSOMMENTION":    "newsom",
    "KXMELANIAMENTION":   "melania",
    "KXCONGRESSMENTION":  "auto",
    "KXSCOTUSMENTION":    "auto",
    "KXPERSONMENTION":    "auto",
    "KXPOLITICSMENTION":  "auto",
    "KXFOXNEWSMENTION":   "auto",
    "KXLASTWORDCOUNT":    "auto",
    "KXLASTWORDMENTION":  "auto",
    "KXTBPNMENTION":      "auto",
    # ── Sports broadcast mention markets ───────────────────────────────────
    "KXMLBMENTION":   "mlb",
    "KXNBAMENTION":   "nba",
    "KXNCAABMENTION": "ncaab",
    "KXFIGHTMENTION": "mma",
}

# Series we intentionally do NOT fetch — entertainment only.
# Sports broadcast markets (KXMLB, KXNBA, KXNCAAB, KXFIGHT) are now tracked
# with explicit priors in config/base_rates_priors.yaml (added session 46).
BLOCKED_SERIES: set[str] = {
    "KXWBCMENTION",           # World Baseball Classic (one-time event, not recurring)
    "KXSURVIVORMENTION",      # Survivor TV show
    "KXENTMENTION",           # Entertainment / celebrities
    "KXJENSENMENTION",        # Jensen Huang / NVIDIA earnings
    "KXEARNINGSMENTIONULTA",  # Ulta earnings call
    "KXEARNINGSMENTIONEA",    # EA earnings call
    "KXEARNINGSMENTIONADBE",  # Adobe earnings call
    # song / streaming chart markets
    "KXTOPSONGSPOTIFY",
    "KXTOPSONGSPOTIFYUSA",
    "KXTOPSONGRUNNERUPUSA",
    "KXTOPSONGTHIRDUSA",
    "KXTOPSONGTHIRD",
    "KXTOPSONGSPOTIFYRUNNERUP",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_fetch_error(url: str, error: str) -> None:
    _FETCH_ERRORS.append(
        {
            "ts": _now_iso(),
            "url": url,
            "error": error,
        }
    )


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _should_preserve_previous_cache(
    *,
    new_count: int,
    previous_count: int,
    has_fetch_errors: bool,
    min_ratio: float = 0.6,
    min_previous_for_guard: int = 40,
) -> bool:
    if previous_count < min_previous_for_guard:
        return False
    if new_count >= previous_count:
        return False
    ratio = (new_count / previous_count) if previous_count > 0 else 1.0
    # Only guard aggressively when fetch had API/network errors.
    return has_fetch_errors and ratio < min_ratio


def _normalize_scraped_event(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    et = str(raw.get("event_ticker") or raw.get("ticker") or "").strip()
    if not et:
        return None
    title = str(raw.get("title") or "").strip()
    speaker = str(raw.get("speaker") or "").strip().lower()
    if not speaker or speaker in ("unknown", "other", "auto"):
        speaker = _infer_speaker_for_market(raw) or _infer_speaker_for_series(et, title) or "auto"
    return {
        "event_ticker": et,
        "title": title,
        "speaker": speaker,
        "status": str(raw.get("status") or "open").strip().lower(),
        "close_time": str(raw.get("close_time") or "").strip(),
        "open_time": str(raw.get("open_time") or "").strip(),
        "open_markets": int(raw.get("open_markets") or 0),
        "source": "scrape_fallback",
    }


def fetch_open_events_from_scrape_fallback() -> list[dict]:
    """Optional deterministic fallback via external scraper command.

    Env:
      KALSHI_EVENT_SCRAPE_CMD='python3 scripts/some_scraper.py --json'

    Expected stdout JSON:
      {"events": [{event_ticker,title,speaker,...}, ...]}
      or just a list of event dicts.
    """
    cmd = os.getenv("KALSHI_EVENT_SCRAPE_CMD", "").strip()
    if not cmd:
        return []
    try:
        proc = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except Exception as exc:
        _record_fetch_error("scrape_fallback", f"command_failed: {exc}")
        return []
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        _record_fetch_error("scrape_fallback", f"nonzero_exit={proc.returncode} {err}")
        return []
    raw_out = (proc.stdout or "").strip()
    if not raw_out:
        return []
    try:
        parsed = json.loads(raw_out)
    except Exception as exc:
        _record_fetch_error("scrape_fallback", f"invalid_json: {exc}")
        return []
    rows = parsed.get("events", []) if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        ev = _normalize_scraped_event(row)
        if not ev:
            continue
        et = ev["event_ticker"]
        if et in seen:
            continue
        seen.add(et)
        out.append(ev)
    return out


def _is_mention_like_series(ticker: str, title: str) -> bool:
    t = (ticker or "").lower()
    name = (title or "").lower()
    if any(k in t for k in ("mention", "say", "late", "duration")):
        return True
    if "what will" in name and ("say" in name or "mention" in name):
        return True
    if "mention" in name:
        return True
    if "press briefing" in name:
        return True
    if any(k in name for k in ("press conference", "conference at", "remarks", "address")):
        if "trump" in name:
            return True
    return False


_SPEAKER_PATTERNS: list[tuple[str, list[str], list[str]]] = [
    # (canonical, ticker_keywords, title_keywords)
    ("leavitt", ["LEAVITT", "SECPRESS"], ["leavitt", "press secretary"]),
    ("mamdani", ["MAMDANI"], ["mamdani"]),
    ("sanders", ["BERN"], ["bernie sanders", "bernie"]),
    ("kelly", ["KELL"], ["mark kelly"]),
    ("conan", ["CONA"], ["conan o'brien", "conan"]),
    ("starmer", ["STARMER"], ["keir starmer", "starmer"]),
    ("fed", ["FEDMENTION"], ["fomc", "federal reserve", "fed chair", "powell"]),
    ("homan", ["HOMANMENTION"], ["tom homan", "homan", "border czar"]),
    ("carney", ["CARNEYMENTION"], ["mark carney", "carney", "prime minister carney"]),
    ("walz", ["WALZ"], ["tim walz", "walz"]),
    ("hegseth", ["HEGS"], ["pete hegseth", "hegseth"]),
    ("whitehouse", ["WHPRESSBRIEFING"], ["white house press briefing", "press briefing"]),
    ("witness", ["WITN", "WITNE"], ["witness"]),
    ("hakeemshah", ["HAKE"], ["hakeemshah", "hakeem"]),
    ("tisch", ["TISC"], ["tisch"]),
    ("albanese", ["ALBA"], ["albanese"]),
    ("nickmercs", ["NICK"], ["nickmercs"]),
    ("nba", ["NBAMENTION"], ["nba"]),
    ("mlb", ["MLBMENTION"], ["mlb", "baseball", "major league baseball"]),
    ("mma", ["FIGHTMENTION"], ["mma", "ufc", "fight", "boxing"]),
    ("survivor", ["SURVIVORMENTION"], ["survivor"]),
    ("wbc", ["WBCMENTION"], ["world baseball classic", "wbc"]),
    ("ncaab", ["NCAABMENTION"], ["ncaa basketball", "ncaab", "march madness"]),
    ("jensen", ["JENSENMENTION", "MEARNMU"], ["jensen huang", "nvidia"]),
    ("entertainment", ["ENTMENTION"], ["entertainment"]),
    ("earnings", ["EARNINGSMEN", "MENTIONEARN"], ["earnings call", "earnings mention", "earnings call"]),
    ("trump", ["TRUMP", "DJT", "PRESMENTION"], ["trump", "donald j. trump"]),
]


def _infer_speaker_for_series(ticker: str, title: str) -> str | None:
    """Infer speaker from series ticker/title. Returns None only when
    no pattern matches — callers should fall back to 'auto' rather than skip."""
    t = (ticker or "").upper()
    name = (title or "").lower()
    for speaker, tk_kw, name_kw in _SPEAKER_PATTERNS:
        if any(kw in t for kw in tk_kw):
            return speaker
        if any(kw in name for kw in name_kw):
            return speaker
    return None


def _extract_series_ticker(market: dict) -> str:
    series = str(market.get("series_ticker", "")).strip().upper()
    if series:
        return series
    ticker = str(market.get("ticker", "")).strip().upper()
    return ticker.split("-", 1)[0] if ticker else ""


def _extract_event_ticker(row: dict) -> str:
    et = str(row.get("event_ticker", "")).strip()
    if et:
        return et
    ticker = str(row.get("ticker", "")).strip()
    # Kalshi event tickers commonly look like SERIES-YYMONDD
    if ticker.count("-") >= 1:
        parts = ticker.split("-")
        if len(parts) >= 2:
            return "-".join(parts[:2])
    return ticker


def _infer_speaker_for_market(market: dict, fallback: str | None = None) -> str | None:
    title = str(market.get("title", "")).lower()
    rules = str(market.get("rules_primary", "")).lower()

    # Step 1: Extract subject from "What will <NAME> say/mention" — most reliable.
    # Only match the named subject, not the phrase being bet on.
    m = re.search(r"what will\s+(.+?)\s+(?:say|mention)", title)
    if m:
        subj = m.group(1).strip()
        for speaker, _, name_kw in _SPEAKER_PATTERNS:
            if any(kw in subj for kw in name_kw):
                return speaker

    # Step 2: Extract speaker from rules text using speaker-identifying patterns.
    # These patterns name the speaker (subject), not the phrase being bet on.
    _rules_patterns = [
        r"(?:said|says|stated) by\s+(.+?)[\.,]",   # "stated by Karoline Leavitt"
        r"if\s+(.+?)\s+says",                        # "If Leavitt says"
        r"([\w ]+?)\s+is\s+(?:the\s+)?(?:moderator|speaker|host|press secretary|briefer|panelist)",
    ]
    for pat in _rules_patterns:
        m = re.search(pat, rules, re.IGNORECASE)
        if m:
            subj = m.group(1).strip().lower()
            for speaker, _, name_kw in _SPEAKER_PATTERNS:
                if any(kw in subj for kw in name_kw):
                    return speaker

    # Step 3: Series-ticker-only check (NOT the full market ticker).
    # The full ticker includes a phrase-code suffix like "-TRUMP" or "-TRUM" which
    # would falsely match if the PHRASE being bet on is "trump" (not the speaker).
    # Example: KXHOMANMENTION-26MAR26B-TRUMP must NOT infer speaker=trump.
    series = _extract_series_ticker(market)
    event_ticker = str(market.get("event_ticker", "")).upper().split("-")[0]
    for speaker, tk_kw, _ in _SPEAKER_PATTERNS:
        if any(kw in series or kw in event_ticker for kw in tk_kw):
            return speaker

    # NOTE: No full title+rules keyword scan here. Such a scan causes false
    # positives when the PHRASE being bet on is a person's name — e.g.
    # "If Jack Black says 'Trump' at SNL" → rules contain "trump" → wrongly
    # infers speaker=trump. The series-level MENTION_SERIES dict + the structured
    # patterns above are sufficient; unknown speakers fall back to 'auto'.
    return fallback


def _is_mention_like_market(market: dict) -> bool:
    title = str(market.get("title", "")).lower()
    rules = str(market.get("rules_primary", "")).lower()
    sub = f"{market.get('yes_sub_title', '')} {market.get('no_sub_title', '')}".lower()
    text = f"{title} {rules} {sub}"
    if "what will" in text and (" say " in f" {text} " or "mention" in text):
        return True
    if "next press briefing" in text or "press briefing" in text:
        return True
    if "will " in text and " say " in text:
        return True
    return False


def _is_mention_like_event(row: dict) -> bool:
    text = " ".join(
        str(row.get(k, ""))
        for k in ("title", "subtitle", "description", "rules_primary")
    ).lower()
    if "what will" in text and (" say " in f" {text} " or "mention" in text):
        return True
    if "press briefing" in text:
        return True
    return False


def discover_additional_series(*, include_non_mention: bool = False, max_pages: int = 200) -> dict[str, str]:
    """Discover newly listed mention/say series from the Kalshi series feed."""
    discovered: dict[str, str] = {}
    cursor = ""
    pages = 0
    while True:
        url = f"{API_BASE}/series?limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        data = fetch_json(url)
        rows = data.get("series", [])
        if not rows:
            break
        for row in rows:
            ticker = str(row.get("ticker", "")).strip().upper()
            title = str(row.get("title", "")).strip()
            if not ticker:
                continue
            speaker = _infer_speaker_for_series(ticker, title) or "auto"
            if not include_non_mention and not _is_mention_like_series(ticker, title):
                continue
            discovered[ticker] = speaker
        cursor = data.get("cursor", "")
        pages += 1
        if not cursor or pages >= max_pages:
            break
        time.sleep(0.08)
    return discovered


def discover_series_from_open_markets(
    *,
    include_non_mention: bool = False,
    max_pages: int = 100,
) -> dict[str, str]:
    """Discover active series directly from open markets."""
    discovered: dict[str, str] = {}
    cursor = ""
    pages = 0
    while True:
        url = f"{API_BASE}/markets?status=open&limit=500"
        if cursor:
            url += f"&cursor={cursor}"
        data = fetch_json(url)
        rows = data.get("markets", [])
        if not rows:
            break
        for row in rows:
            series = _extract_series_ticker(row)
            if not series:
                continue
            speaker = _infer_speaker_for_market(
                row,
                fallback=_infer_speaker_for_series(series, str(row.get("title", ""))),
            ) or "auto"
            if not include_non_mention and not _is_mention_like_market(row):
                continue
            prev = discovered.get(series)
            if prev is None or prev == "unknown":
                discovered[series] = speaker
        cursor = data.get("cursor", "")
        pages += 1
        if not cursor or pages >= max_pages:
            break
        time.sleep(0.08)
    return discovered


def fetch_open_events(*, series_tickers: list[str] | None = None, max_pages: int = 100) -> list[dict]:
    """Fetch open events from Kalshi for visibility reconciliation.

    When series_tickers is provided, also performs targeted per-series lookups
    which often surface events the broad feed misses.
    """
    out: list[dict] = []
    seen_tickers: set[str] = set()

    cursor = ""
    pages = 0
    while True:
        url = f"{API_BASE}/events?status=open&limit=200&with_nested_markets=true"
        if cursor:
            url += f"&cursor={cursor}"
        data = fetch_json(url)
        rows = data.get("events", [])
        if not rows:
            break
        for r in rows:
            if not isinstance(r, dict):
                continue
            et = str(r.get("event_ticker", "")).strip()
            if et and et not in seen_tickers:
                out.append(r)
                seen_tickers.add(et)
        cursor = data.get("cursor", "")
        pages += 1
        if not cursor or pages >= max_pages:
            break
        time.sleep(0.08)

    if series_tickers:
        for st in series_tickers:
            targeted = _fetch_events_for_series(st)
            for r in targeted:
                et = str(r.get("event_ticker", "")).strip()
                if et and et not in seen_tickers:
                    out.append(r)
                    seen_tickers.add(et)
            time.sleep(0.1)

    return out


def _fetch_events_for_series(series_ticker: str) -> list[dict]:
    """Fetch open events for a specific series ticker."""
    out: list[dict] = []
    cursor = ""
    while True:
        url = f"{API_BASE}/events?series_ticker={series_ticker}&status=open&limit=200&with_nested_markets=true"
        if cursor:
            url += f"&cursor={cursor}"
        data = fetch_json(url)
        rows = data.get("events", [])
        if not rows:
            break
        out.extend([r for r in rows if isinstance(r, dict)])
        cursor = data.get("cursor", "")
        if not cursor:
            break
        time.sleep(0.08)
    return out


def fetch_event_by_ticker(event_ticker: str) -> dict | None:
    """Targeted lookup for a specific event by ticker."""
    url = f"{API_BASE}/events/{event_ticker}"
    data = fetch_json(url)
    if data and isinstance(data, dict) and data.get("event_ticker"):
        return data
    event = data.get("event") if isinstance(data, dict) else None
    if isinstance(event, dict) and event.get("event_ticker"):
        return event
    return None


def fetch_json(url: str, max_attempts: int = 6) -> dict:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "kalshi-edge-fetch-markets/1.0",
        },
    )
    for attempt in range(max_attempts):
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 429 and attempt < max_attempts - 1:
                wait = min(12.0, 0.7 * (attempt + 1))
                time.sleep(wait)
                continue
            msg = f"HTTPError code={e.code} url={url}"
            print(f"  ERROR fetching {url}: {e}")
            _record_fetch_error(url, msg)
            return {}
        except URLError as e:
            if attempt < max_attempts - 1:
                wait = min(8.0, 0.5 * (attempt + 1))
                time.sleep(wait)
                continue
            msg = f"URLError url={url} err={e}"
            print(f"  ERROR fetching {url}: {e}")
            _record_fetch_error(url, msg)
            return {}
    return {}


def fetch_markets_for_series(series_ticker: str) -> list[dict]:
    """Fetch all open markets for a series, handling pagination."""
    all_markets = []
    cursor = ""
    while True:
        url = f"{API_BASE}/markets?series_ticker={series_ticker}&status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        data = fetch_json(url)
        markets = data.get("markets", [])
        all_markets.extend(markets)
        cursor = data.get("cursor", "")
        if not cursor or not markets:
            break
        time.sleep(0.2)
    return all_markets


def extract_phrase_from_rules(rules: str) -> tuple[str, list[str]]:
    """Parse the resolution phrase(s) from Kalshi's rules_primary text.

    Returns (primary_phrase, list_of_all_phrase_variants).
    """
    # Pattern: 'says <PHRASE> as part of' or '<PHRASE>, or a plural..., is stated by'
    m = re.search(r'says\s+(.+?)\s+as part of', rules, re.IGNORECASE)
    if not m:
        m = re.search(r'If\s+(.+?),\s+or a plural', rules, re.IGNORECASE)
    if not m:
        m = re.search(r'says\s+"?(.+?)"?\s+at the next', rules, re.IGNORECASE)
    if not m:
        m = re.search(r'says\s+(.+?)\s+(?:before|after|during)', rules, re.IGNORECASE)

    if not m:
        return ("", [])

    raw = m.group(1).strip().strip('"').strip("'")

    # Split on " / " to get variants
    variants = [v.strip() for v in raw.split("/") if v.strip()]
    primary = variants[0] if variants else raw
    return (primary, variants)


def detect_event_context(rules: str) -> str:
    """Extract what kind of event the market is tied to."""
    rules_lower = rules.lower()
    if "nba" in rules_lower or "basketball" in rules_lower or "broadcast" in rules_lower:
        return "nba_broadcast"
    if "press briefing" in rules_lower:
        return "briefing"
    if "rally" in rules_lower:
        return "rally"
    if "earnings" in rules_lower:
        return "earnings"
    if "white house" in rules_lower and "youtube" in rules_lower:
        return "wh_live"
    if "announcement" in rules_lower:
        return "announcement"
    if "interview" in rules_lower:
        return "interview"
    if "debate" in rules_lower:
        return "debate"
    # UK Prime Minister's Questions — weekly parliamentary format
    if ("prime minister" in rules_lower and "question" in rules_lower) or "pmq" in rules_lower:
        return "pmq"
    if "bilateral" in rules_lower or ("joint" in rules_lower and "press" in rules_lower):
        return "bilateral_remarks"
    if "earnings call" in rules_lower or "quarterly" in rules_lower:
        return "earnings_call"
    return "general"


def process_markets() -> dict:
    """Fetch and process all mention markets."""
    _FETCH_ERRORS.clear()
    all_processed: list[dict] = []
    seen_tickers: set[str] = set()
    series_counts = {}
    series_map = dict(MENTION_SERIES)
    # "mention" keeps runtime lean; "speaker_full" can be enabled manually
    # when auditing broad speaker coverage.
    scope = os.getenv("FETCH_MARKETS_SCOPE", "mention").strip().lower()
    include_non_mention = scope in {"speaker_full", "full", "all"}
    print(f"Discovering series from /series endpoint...", flush=True)
    discovered_series = discover_additional_series(include_non_mention=include_non_mention)
    print(f"Discovering series from open markets...", flush=True)
    discovered_from_markets = discover_series_from_open_markets(
        include_non_mention=include_non_mention
    )
    discovered = {**discovered_series, **discovered_from_markets}
    all_series = sorted(set(series_map.keys()) | set(discovered.keys()))
    print(f"Fetching open events for {len(all_series)} series...", flush=True)
    open_events = fetch_open_events(series_tickers=all_series)
    scraped_events = fetch_open_events_from_scrape_fallback()
    if scraped_events:
        existing = {str(e.get("event_ticker", "")).strip() for e in open_events if isinstance(e, dict)}
        for ev in scraped_events:
            et = str(ev.get("event_ticker", "")).strip()
            if not et or et in existing:
                continue
            open_events.append(ev)
            existing.add(et)
    discovered_from_events: dict[str, str] = {}
    events_processed: list[dict] = []
    for ev in open_events:
        et = _extract_event_ticker(ev)
        title = str(ev.get("title", "")).strip()
        speaker = _infer_speaker_for_market(ev) or _infer_speaker_for_series(et, title) or "unknown"
        mention_like = _is_mention_like_event(ev)
        if mention_like and speaker in {"trump", "leavitt", "mamdani"}:
            events_processed.append(
                {
                    "event_ticker": et,
                    "title": title,
                    "speaker": speaker,
                    "status": str(ev.get("status", "")).strip().lower(),
                    "close_time": str(ev.get("close_time", "")).strip(),
                    "open_time": str(ev.get("open_time", "")).strip(),
                    "open_markets": (
                        len(ev.get("markets", []) if isinstance(ev.get("markets", []), list) else [])
                        if "markets" in ev
                        else int(ev.get("open_markets") or 0)
                    ),
                    "source": str(ev.get("source") or "events_endpoint"),
                }
            )

        nested = ev.get("markets", [])
        if not isinstance(nested, list):
            nested = []
        for m in nested:
            if not isinstance(m, dict):
                continue
            if not _is_mention_like_market(m) and not include_non_mention:
                continue
            ms = _extract_series_ticker(m)
            ms_speaker = _infer_speaker_for_market(m, fallback=speaker)
            if ms and ms_speaker in {"trump", "leavitt", "mamdani"}:
                discovered_from_events[ms] = ms_speaker

    series_map.update(discovered)
    series_map.update(discovered_from_events)

    if discovered or discovered_from_events:
        print(
            f"Discovered {len(discovered)} additional series "
            f"(/series={len(discovered_series)}, /markets={len(discovered_from_markets)}, "
            f"/events_nested={len(discovered_from_events)})."
        )
    print(f"Fetch scope: {scope}", flush=True)

    for series_ticker, speaker in series_map.items():
        print(f"Fetching {series_ticker} ({speaker})...", flush=True)
        markets = fetch_markets_for_series(series_ticker)
        series_counts[series_ticker] = len(markets)

        for m in markets:
            rules = m.get("rules_primary", "") or ""
            market_speaker = _infer_speaker_for_market(m, fallback=speaker) or speaker
            primary_phrase, variants = extract_phrase_from_rules(rules)
            if not variants:
                sub = (m.get("yes_sub_title") or m.get("no_sub_title") or "").strip()
                if sub:
                    parts = [p.strip() for p in sub.split("/") if p.strip()]
                    variants = parts
                    primary_phrase = parts[0] if parts else sub
            event_context = detect_event_context(rules)
            is_phrase_market = bool(primary_phrase or variants)

            processed = {
                "ticker": m["ticker"],
                "event_ticker": m.get("event_ticker", ""),
                "series_ticker": _extract_series_ticker(m) or series_ticker,
                "speaker": market_speaker,
                "title": m.get("title", ""),
                "is_phrase_market": is_phrase_market,
                "primary_phrase": primary_phrase,
                "phrase_variants": variants,
                "event_context": event_context,
                "rules_primary": rules,
                "rules_secondary": m.get("rules_secondary", ""),
                "status": m.get("status", ""),
                "yes_ask_dollars": m.get("yes_ask_dollars", ""),
                "yes_bid_dollars": m.get("yes_bid_dollars", ""),
                "no_ask_dollars": m.get("no_ask_dollars", ""),
                "no_bid_dollars": m.get("no_bid_dollars", ""),
                "volume_24h": m.get("volume_24h", 0),
                "open_interest": m.get("open_interest", 0),
                "close_time": m.get("close_time", ""),
                "open_time": m.get("open_time", ""),
                "last_price_dollars": m.get("last_price_dollars", ""),
            }
            tk = str(processed["ticker"])
            if tk and tk not in seen_tickers:
                all_processed.append(processed)
                seen_tickers.add(tk)

        time.sleep(0.3)

    # Reconcile via events endpoint nested markets: include mention-like contracts
    # that may not show up in series pulls yet. Only include active/open markets.
    _ACTIVE_STATUSES = {"open", "active", "initialized", ""}
    for ev in open_events:
        speaker_guess = _infer_speaker_for_market(ev) or "unknown"
        nested = ev.get("markets", [])
        if not isinstance(nested, list):
            continue
        for m in nested:
            if not isinstance(m, dict):
                continue
            mkt_status = str(m.get("status", "")).strip().lower()
            if mkt_status and mkt_status not in _ACTIVE_STATUSES:
                continue
            ticker = str(m.get("ticker", "")).strip()
            if not ticker or ticker in seen_tickers:
                continue
            if not include_non_mention and not _is_mention_like_market(m):
                continue
            rules = m.get("rules_primary", "") or ""
            primary_phrase, variants = extract_phrase_from_rules(rules)
            if not variants:
                sub = (m.get("yes_sub_title") or m.get("no_sub_title") or "").strip()
                if sub:
                    parts = [p.strip() for p in sub.split("/") if p.strip()]
                    variants = parts
                    primary_phrase = parts[0] if parts else sub
            market_speaker = _infer_speaker_for_market(m, fallback=speaker_guess) or speaker_guess
            processed = {
                "ticker": ticker,
                "event_ticker": m.get("event_ticker", ""),
                "series_ticker": _extract_series_ticker(m),
                "speaker": market_speaker,
                "title": m.get("title", ""),
                "is_phrase_market": bool(primary_phrase or variants),
                "primary_phrase": primary_phrase,
                "phrase_variants": variants,
                "event_context": detect_event_context(rules),
                "rules_primary": rules,
                "rules_secondary": m.get("rules_secondary", ""),
                "status": m.get("status", ""),
                "yes_ask_dollars": m.get("yes_ask_dollars", ""),
                "yes_bid_dollars": m.get("yes_bid_dollars", ""),
                "no_ask_dollars": m.get("no_ask_dollars", ""),
                "no_bid_dollars": m.get("no_bid_dollars", ""),
                "volume_24h": m.get("volume_24h", 0),
                "open_interest": m.get("open_interest", 0),
                "close_time": m.get("close_time", ""),
                "open_time": m.get("open_time", ""),
                "last_price_dollars": m.get("last_price_dollars", ""),
            }
            all_processed.append(processed)
            seen_tickers.add(ticker)
            st = processed["series_ticker"] or _extract_series_ticker(m)
            if st:
                series_counts[st] = series_counts.get(st, 0) + 1

    active_markets = []
    skipped_inactive = 0
    for m in all_processed:
        status = str(m.get("status", "")).strip().lower()
        if status in ("finalized", "closed", "settled"):
            skipped_inactive += 1
            continue
        active_markets.append(m)
    if skipped_inactive:
        print(f"Filtered out {skipped_inactive} non-active markets (finalized/closed/settled)")

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "series_counts": series_counts,
        "discovered_series": discovered,
        "discovered_series_events_nested": discovered_from_events,
        "scrape_fallback_events_count": len(scraped_events),
        "fetch_errors": list(_FETCH_ERRORS),
        "events": events_processed,
        "total_markets": len(active_markets),
        "markets": active_markets,
    }


def _merge_events_with_history(new_events: list[dict], previous_events: list[dict]) -> list[dict]:
    prev_by_et = {
        str(e.get("event_ticker", "")).strip(): e
        for e in previous_events
        if isinstance(e, dict) and str(e.get("event_ticker", "")).strip()
    }
    out: list[dict] = []
    seen: set[str] = set()
    now_iso = _now_iso()
    for e in new_events:
        if not isinstance(e, dict):
            continue
        et = str(e.get("event_ticker", "")).strip()
        if not et or et in seen:
            continue
        seen.add(et)
        prev = prev_by_et.get(et, {})
        merged = dict(e)
        merged["first_seen_at"] = str(prev.get("first_seen_at") or now_iso)
        merged["last_seen_at"] = now_iso
        out.append(merged)
    return out


def save_outputs(data: dict) -> None:
    """Save to JSON cache and human-readable YAML summary."""
    previous_market_payload = _read_json(MARKETS_CACHE_PATH) if MARKETS_CACHE_PATH.exists() else {}
    previous_events_payload = _read_json(EVENTS_CACHE_PATH) if EVENTS_CACHE_PATH.exists() else {}
    previous_market_count = int(previous_market_payload.get("total_markets", 0) or 0)
    new_market_count = int(data.get("total_markets", 0) or 0)
    has_fetch_errors = bool(data.get("fetch_errors"))

    quality_guard = {
        "previous_total_markets": previous_market_count,
        "new_total_markets": new_market_count,
        "preserved_previous_cache": False,
        "reason": "",
    }

    if _should_preserve_previous_cache(
        new_count=new_market_count,
        previous_count=previous_market_count,
        has_fetch_errors=has_fetch_errors,
    ):
        quality_guard["preserved_previous_cache"] = True
        quality_guard["reason"] = "market_count_collapse_with_fetch_errors"
        to_write = dict(previous_market_payload)
        to_write["last_refresh_attempt_at"] = data.get("fetched_at")
        to_write["last_refresh_fetch_errors"] = list(data.get("fetch_errors", []))
        to_write["quality_guard"] = quality_guard
        _atomic_write_json(MARKETS_CACHE_PATH, to_write)
        print(
            f"\nPreserved previous market cache ({previous_market_count} markets) "
            f"due to partial fetch and errors."
        )
    else:
        to_write = dict(data)
        to_write["quality_guard"] = quality_guard
        _atomic_write_json(MARKETS_CACHE_PATH, to_write)
        print(f"\nSaved {new_market_count} markets to {MARKETS_CACHE_PATH}")

    previous_events_count = int(previous_events_payload.get("total_events", 0) or 0)
    merged_events = _merge_events_with_history(
        data.get("events", []),
        previous_events_payload.get("events", []),
    )
    if _should_preserve_previous_cache(
        new_count=len(merged_events),
        previous_count=previous_events_count,
        has_fetch_errors=has_fetch_errors,
        min_ratio=0.5,
        min_previous_for_guard=2,
    ):
        merged_events = previous_events_payload.get("events", [])
    events_payload = {
        "fetched_at": data.get("fetched_at"),
        "scope": data.get("scope"),
        "fetch_errors": list(data.get("fetch_errors", [])),
        "events": merged_events,
    }
    events_payload["total_events"] = len(events_payload["events"])
    _atomic_write_json(EVENTS_CACHE_PATH, events_payload)
    print(f"Saved {events_payload['total_events']} events to {EVENTS_CACHE_PATH}")

    # Human-readable YAML-like summary
    yaml_path = Path("config/live_markets.yaml")
    lines = [
        f"# Kalshi mention markets - fetched {data['fetched_at']}",
        f"# Total: {data['total_markets']} open markets",
        "",
    ]

    by_speaker: dict[str, list[dict]] = {}
    for m in data["markets"]:
        by_speaker.setdefault(m["speaker"], []).append(m)

    for speaker in sorted(by_speaker.keys()):
        markets = by_speaker[speaker]
        lines.append(f"{speaker}:  # {len(markets)} markets")

        by_context: dict[str, list[dict]] = {}
        for m in markets:
            by_context.setdefault(m["event_context"], []).append(m)

        for ctx in sorted(by_context.keys()):
            ctx_markets = by_context[ctx]
            lines.append(f"  {ctx}:")
            for m in sorted(ctx_markets, key=lambda x: x["primary_phrase"].lower()):
                phrase = m["primary_phrase"]
                variants = " / ".join(m["phrase_variants"]) if len(m["phrase_variants"]) > 1 else ""
                ask = m["yes_ask_dollars"]
                vol = m["volume_24h"]
                lines.append(f"    - phrase: \"{phrase}\"")
                if variants:
                    lines.append(f"      variants: \"{variants}\"")
                lines.append(f"      ticker: {m['ticker']}")
                lines.append(f"      yes_ask: {ask}")
                lines.append(f"      volume_24h: {vol}")
                lines.append(f"      close: {m['close_time'][:10] if m['close_time'] else '?'}")
        lines.append("")

    yaml_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved summary to {yaml_path}")


def print_summary(data: dict) -> None:
    """Print quick summary to console."""
    print(f"\n{'='*60}")
    print(f"KALSHI MENTION MARKETS SUMMARY")
    print(f"{'='*60}")

    by_speaker: dict[str, list[dict]] = {}
    for m in data["markets"]:
        by_speaker.setdefault(m["speaker"], []).append(m)

    for speaker in sorted(by_speaker.keys()):
        markets = by_speaker[speaker]
        phrases = set()
        for m in markets:
            for v in m["phrase_variants"]:
                phrases.add(v.lower())
        print(f"\n{speaker.upper()}: {len(markets)} markets, {len(phrases)} unique phrases")
        for m in sorted(markets, key=lambda x: float(x["yes_ask_dollars"] or "0")):
            ask = m["yes_ask_dollars"]
            phrase = " / ".join(m["phrase_variants"]) if m["phrase_variants"] else "?"
            ctx = m["event_context"]
            print(f"  {ask}  {phrase:40s}  [{ctx}]  {m['ticker'][:40]}")


def main():
    print("Fetching mention markets from Kalshi API...", flush=True)
    print()
    data = process_markets()
    save_outputs(data)
    print_summary(data)
    print(f"\nDone. {data['total_markets']} markets across {len(data.get('series_counts', {}))} series.", flush=True)
    print("\nNext: review config/live_markets.yaml and run 'make scrape' for transcripts.")


if __name__ == "__main__":
    main()
