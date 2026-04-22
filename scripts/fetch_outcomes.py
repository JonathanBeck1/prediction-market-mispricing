#!/usr/bin/env python3
"""Fetch finalized mention-market outcomes from Kalshi API.

Usage:
    python3 scripts/fetch_outcomes.py

Outputs:
    data/kalshi_outcomes.json      - finalized market outcomes cache
    config/historical_outcomes.yaml - human-readable summary
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:  # pragma: no cover - runtime guard
    yaml = None

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Series tickers that contain mention markets for our tracked speakers.
MENTION_SERIES: dict[str, str] = {
    # Per-speech / per-week Trump mention markets (ground-truth phrase outcomes)
    "KXTRUMPSAY": "trump",
    "KXTRUMPSAYEP": "trump",
    "KXTRUMPMENTION": "trump",
    "KXTRUMPMENTIONB": "trump",
    # KXPRESMENTION: specific named-event markets (e.g. "Trump at Thermo Fisher")
    # These carry event_context in the title and are the richest per-speech signal.
    "KXPRESMENTION": "trump",
    # General presidential mention markets
    "KXMENTION": "auto",
    "KXDJTRALLY": "trump",
    "KXDJTINVESTMENT": "trump",
    "KXDJTWOMENS": "trump",
    "KXTRUMPSAYMONTH": "trump",
    "KXTRUMPSAYNICKNAME": "trump",
    "KXTRUMPLATE": "trump",
    "KXTRUMPMENTIONDURATION": "trump",
    "KXDJTCONF": "trump",
    "KXLEAVITTMENTION": "leavitt",
    "KXLEAVITTSMFMENTION": "leavitt",
    "KXSECPRESSMENTION": "leavitt",
    "KXLEAVITTLATE": "leavitt",
    "KXLEAVITTMENTIONDURATION": "leavitt",
    "KXMAMDANIMENTION": "mamdani",
    "KXTRUMPSAYMAM": "mamdani",
    # Additional series aligned with scripts/fetch_markets.py (speakers match
    # app/kalshi_watcher_live.py where they differ, e.g. KXFEDMENTION → powell).
    "KXWHPRESSBRIEFING": "whitehouse",
    "KXFEDMENTION": "powell",
    "KXSTARMERMENTIONB": "starmer",
    "KXHOMANMENTION": "homan",
    "KXCARNEYMENTION": "carney",
    "KXAOCMENTION": "aoc",
    "KXHOCHULMENTION": "hochul",
    "KXNEWSOMMENTION": "newsom",
    "KXMELANIAMENTION": "melania",
    "KXCONGRESSMENTION": "auto",
    "KXSCOTUSMENTION": "auto",
    "KXPERSONMENTION": "auto",
    "KXPOLITICSMENTION": "auto",
    "KXFOXNEWSMENTION": "auto",
    "KXLASTWORDCOUNT": "auto",
    "KXLASTWORDMENTION": "auto",
    "KXTBPNMENTION": "auto",
    # Sports broadcast mention markets
    "KXMLBMENTION": "mlb",
    "KXNBAMENTION": "nba",
    "KXNCAABMENTION": "ncaab",
    "KXFIGHTMENTION": "mma",
    # Earnings / company mention markets
    "KXMENTIONEARNNKE": "auto",
    "KXMENTIONEARNDAL": "auto",
    # Other speaker / event series
    "KXBARRMENTION": "auto",
    "KXFEDGOVMENTION": "auto",
    "KXMRBEASTMENTION": "auto",
    "KXSNLMENTION": "auto",
    "KXRUBIOMENTION": "auto",
    "KXVANCEMENTION": "auto",
}


def _fetch_json(url: str, _retries: int = 3) -> dict:
    req = Request(url, headers={"Accept": "application/json"})
    for attempt in range(_retries):
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as exc:
            if exc.code == 429:
                wait = 4 ** attempt  # 1s, 4s, 16s
                print(f"  Rate limited (429) on attempt {attempt+1}/{_retries}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            print(f"  ERROR fetching {url}: {exc}")
            return {}
        except (URLError, TimeoutError, OSError) as exc:
            print(f"  ERROR fetching {url}: {exc}")
            return {}
    print(f"  ERROR: exhausted retries for {url}")
    return {}


def _fetch_series_markets(series_ticker: str) -> list[dict]:
    """Fetch all markets for a series (both active + finalized)."""
    all_markets: list[dict] = []
    cursor = ""
    while True:
        # Note: status=finalized currently returns HTTP 400. Fetch all and filter.
        url = f"{API_BASE}/markets?series_ticker={series_ticker}&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        data = _fetch_json(url)
        markets = data.get("markets", [])
        all_markets.extend(markets)
        cursor = data.get("cursor", "")
        if not cursor or not markets:
            break
        time.sleep(0.2)
    return all_markets


def _extract_phrases(rules: str, title: str) -> tuple[str, list[str]]:
    """Parse phrase(s) from rules; fall back to title if needed."""
    m = re.search(r'says\s+(.+?)\s+as part of', rules, re.IGNORECASE)
    if not m:
        m = re.search(r'If\s+(.+?),\s+or a plural', rules, re.IGNORECASE)
    if not m:
        m = re.search(r'says\s+"?(.+?)"?\s+at the next', rules, re.IGNORECASE)
    if not m:
        m = re.search(r'says\s+(.+?)\s+(?:before|after|during)', rules, re.IGNORECASE)

    raw = ""
    if m:
        raw = m.group(1).strip().strip('"').strip("'")
    else:
        # Fallback for titles like: Will Trump say "NATO" before ...
        tm = re.search(r'Will\s+.+?\s+say\s+"(.+?)"', title, re.IGNORECASE)
        if tm:
            raw = tm.group(1).strip()

    if not raw:
        return ("", [])

    variants = [p.strip() for p in raw.split("/") if p.strip()]
    primary = variants[0] if variants else raw
    return (primary, variants)


def _detect_event_context(rules: str, event_title: str = "") -> str:
    """Classify the speech type from rules text and/or the event title.

    For KXPRESMENTION events the event_title carries the richest signal
    (e.g. "What will Trump say during his visit at Thermo Fisher Scientific?").
    """
    combined = (event_title + " " + rules).lower()

    # Press briefings / press conferences
    if any(kw in combined for kw in ("press briefing", "press conference", "news conference",
                                     "briefing", "presser")):
        return "briefing"
    # Rallies
    if "rally" in combined:
        return "rally"
    # Interviews
    if "interview" in combined:
        return "interview"
    # Debates
    if "debate" in combined:
        return "debate"
    # Town halls
    if "town hall" in combined or "townhall" in combined:
        return "townhall"
    # Joint sessions / addresses to Congress
    if any(kw in combined for kw in ("joint session", "state of the union", "sotu",
                                     "address to congress", "address to the nation")):
        return "address"
    # Signing ceremonies / executive order events
    if any(kw in combined for kw in ("signing", "executive order", "ceremony")):
        return "signing"
    # Summits / diplomatic / bilateral / foreign policy events
    if any(kw in combined for kw in ("summit", "bilateral", "meeting with", "visit to",
                                     "diplomacy", "foreign minister", "secretary of state")):
        return "summit"
    # Company / factory / facility visits
    if any(kw in combined for kw in ("visit at", "visit to", "factory", "plant",
                                     "facility", "scientific", "manufacturing",
                                     "roundtable", "business")):
        return "visit"
    # Remarks in a city/state (domestic travel speech)
    if re.search(r"remarks in [a-z]", combined):
        return "remarks"
    # Announcements
    if "announcement" in combined:
        return "announcement"
    return "general"


def _fetch_event_titles(series_ticker: str) -> dict[str, str]:
    """Return {event_ticker: event_title} for a series.

    Used to enrich event_context for specific-event series like KXPRESMENTION
    where the event title carries the speech type (e.g. "visit at Thermo Fisher").
    """
    titles: dict[str, str] = {}
    cursor = ""
    pages = 0
    while pages < 20:
        url = f"{API_BASE}/events?series_ticker={series_ticker}&limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        data = _fetch_json(url)
        for ev in data.get("events", []):
            et = str(ev.get("event_ticker", ""))
            title = str(ev.get("title", ""))
            if et and title:
                titles[et] = title
        cursor = data.get("cursor", "")
        pages += 1
        if not cursor:
            break
        time.sleep(0.2)
    return titles


# Series where we fetch event-level titles to enrich event_context
_SERIES_WITH_NAMED_EVENTS = {"KXPRESMENTION", "KXTRUMPMENTION", "KXTRUMPMENTIONB"}


def build_outcomes() -> dict:
    outcomes: list[dict] = []
    per_series: dict[str, dict[str, int]] = {}

    for series_ticker, speaker in MENTION_SERIES.items():
        print(f"Fetching outcomes for {series_ticker} ({speaker})...")
        markets = _fetch_series_markets(series_ticker)
        finalized = [m for m in markets if str(m.get("status", "")).lower() == "finalized"]
        valid = [m for m in finalized if str(m.get("result", "")).lower() in {"yes", "no"}]

        yes_count = sum(1 for m in valid if str(m.get("result", "")).lower() == "yes")
        no_count = sum(1 for m in valid if str(m.get("result", "")).lower() == "no")
        per_series[series_ticker] = {
            "total": len(markets),
            "finalized": len(valid),
            "yes": yes_count,
            "no": no_count,
        }

        # For named-event series, fetch event titles so we can classify context properly.
        event_titles: dict[str, str] = {}
        if series_ticker in _SERIES_WITH_NAMED_EVENTS and valid:
            print(f"  Fetching event titles for {series_ticker}...")
            event_titles = _fetch_event_titles(series_ticker)
            print(f"  Got {len(event_titles)} event titles")

        for m in valid:
            rules = m.get("rules_primary", "") or ""
            title = m.get("title", "") or ""
            event_ticker = str(m.get("event_ticker", ""))
            # Use the event-level title (more descriptive) when available
            event_title = event_titles.get(event_ticker, "")
            primary_phrase, phrase_variants = _extract_phrases(rules, title)
            event_context = _detect_event_context(rules, event_title=event_title or title)
            result = str(m.get("result", "")).lower()

            outcomes.append(
                {
                    "ticker": m.get("ticker", ""),
                    "event_ticker": event_ticker,
                    "series_ticker": series_ticker,
                    "speaker": speaker,
                    "title": title,
                    "event_title": event_title,
                    "primary_phrase": primary_phrase,
                    "phrase_variants": phrase_variants,
                    "event_context": event_context,
                    "status": m.get("status", ""),
                    "result": result,
                    "settlement_value": m.get("settlement_value"),
                    "settlement_value_dollars": m.get("settlement_value_dollars"),
                    "settlement_ts": m.get("settlement_ts"),
                    "close_time": m.get("close_time"),
                    "rules_primary": rules,
                }
            )

        time.sleep(0.3)

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_outcomes": len(outcomes),
        "series_counts": per_series,
        "markets": outcomes,
    }


def save_outputs(data: dict) -> None:
    json_path = Path("data/kalshi_outcomes.json")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    print(f"Saved {data['total_outcomes']} finalized outcomes to {json_path}")

    summary_path = Path("config/historical_outcomes.yaml")
    if yaml is None:
        print("PyYAML not installed; skipping config/historical_outcomes.yaml summary")
        return

    by_speaker: dict[str, dict[str, int]] = {}
    for m in data["markets"]:
        speaker = m.get("speaker", "unknown")
        block = by_speaker.setdefault(
            speaker,
            {"total": 0, "yes": 0, "no": 0},
        )
        block["total"] += 1
        if m.get("result") == "yes":
            block["yes"] += 1
        elif m.get("result") == "no":
            block["no"] += 1

    summary = {
        "fetched_at": data.get("fetched_at"),
        "total_outcomes": data.get("total_outcomes", 0),
        "by_speaker": by_speaker,
        "series_counts": data.get("series_counts", {}),
    }
    summary_path.write_text(yaml.safe_dump(summary, sort_keys=False), encoding="utf-8")
    print(f"Saved summary to {summary_path}")


def main() -> None:
    print("Fetching finalized mention-market outcomes from Kalshi API...")
    data = build_outcomes()
    save_outputs(data)
    print("Done.")


if __name__ == "__main__":
    main()
