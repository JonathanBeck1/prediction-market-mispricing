#!/usr/bin/env python3
"""Fast, lightweight fetcher for same-day specific-event markets.

Kalshi creates KXPRESMENTION / KXTRUMPMENTION events only hours (sometimes
minutes) before the speech starts.  Running fetch_markets.py every 60 minutes
is too slow to catch them in time.  This script only pulls the "hot" series —
runs in seconds rather than minutes — and merges any newly discovered markets
into the existing kalshi_markets.json cache.

Usage:
    python3 scripts/fetch_hot_events.py

Designed to run every 5 minutes via MaintenanceRunner.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.fetch_markets import (
    MENTION_SERIES,
    _infer_speaker_for_market,
    _infer_speaker_for_series,
)

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MARKETS_CACHE = Path("data/kalshi_markets.json")

# Series that get new markets same-day — check every 5 minutes.
HOT_SERIES = [
    "KXPRESMENTION",    # Named single-event markets: "Trump at Thermo Fisher"
    "KXTRUMPMENTION",   # Named single-event markets: "Trump at Shield of Americas"
    "KXTRUMPMENTIONB",  # Named single-event markets (batch B)
    "KXLEAVITTMENTION", # Same-day Leavitt briefing markets
    "KXSECPRESSMENTION",# Monthly Leavitt window markets (still useful to catch new ones)
]


def _fetch(url: str) -> dict:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-edge/1.0"})
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"  WARN {url}: {exc}")
        return {}


def _load_cache() -> tuple[dict, list[dict]]:
    """Return (payload_dict, markets_list) from the cache file.

    Handles both the dict format ({"markets": [...]}) written by fetch_markets.py
    and gracefully degrades if the file is missing or corrupt.
    """
    if not MARKETS_CACHE.exists():
        return {}, []
    try:
        data = json.loads(MARKETS_CACHE.read_text())
        if isinstance(data, dict):
            return data, list(data.get("markets", []))
        if isinstance(data, list):
            # Legacy / unexpected flat-list format — wrap it
            return {"markets": data}, data
    except Exception:
        pass
    return {}, []


def _save_cache(payload: dict, markets: list[dict]) -> None:
    updated = dict(payload)
    updated["markets"] = markets
    updated["total_markets"] = len(markets)
    tmp = MARKETS_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(updated, indent=2))
    tmp.replace(MARKETS_CACHE)


def fetch_hot() -> int:
    """Fetch open markets for HOT_SERIES and merge into cache. Returns # new markets added."""
    payload, existing = _load_cache()
    existing_ids = {m.get("ticker", "") for m in existing if isinstance(m, dict)}

    new_markets: list[dict] = []

    for series in HOT_SERIES:
        speaker_default = MENTION_SERIES.get(series, "auto")
        d = _fetch(f"{API_BASE}/markets?series_ticker={series}&status=open&limit=200")
        markets = d.get("markets", [])
        for m in markets:
            ticker = m.get("ticker", "")
            if not ticker or ticker in existing_ids:
                continue
            # Infer speaker
            speaker = _infer_speaker_for_market(m) or speaker_default
            # Only keep phrase markets (has a yes_sub_title phrase)
            if not m.get("yes_sub_title", "").strip():
                continue
            new_markets.append({
                "ticker": ticker,
                "event_ticker": m.get("event_ticker", ""),
                "series_ticker": series,
                "title": m.get("title", ""),
                "yes_sub_title": m.get("yes_sub_title", ""),
                "no_sub_title": m.get("no_sub_title", ""),
                "speaker": speaker,
                "status": m.get("status", "open"),
                "close_time": m.get("close_time", ""),
                "is_phrase_market": True,
                "primary_phrase": m.get("yes_sub_title", ""),
            })
        time.sleep(0.15)

    updated = existing + new_markets
    # Always save to ensure the file stays in the correct dict format.
    _save_cache(payload, updated)
    if new_markets:
        print(f"fetch_hot_events: added {len(new_markets)} new markets to cache "
              f"({[m['ticker'] for m in new_markets[:5]]}...)")
    else:
        print(f"fetch_hot_events: no new hot markets found ({len(updated)} cached)")

    return len(new_markets)


if __name__ == "__main__":
    n = fetch_hot()
    sys.exit(0)
