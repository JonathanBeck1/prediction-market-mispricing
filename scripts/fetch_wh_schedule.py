#!/usr/bin/env python3
"""Fetch White House schedule and recent activity.

Sources:
  1. whitehouse.gov/news/feed/ — official WH RSS (Executive Orders, Remarks, Briefings)
  2. Google News RSS — surface upcoming signing/remarks/briefing announcements

Output: data/wh_schedule.json
  {
    "generated_at": "...",
    "events": [
      {
        "title":        "Trump Signs Executive Order on Energy",
        "url":          "https://...",
        "published_at": "2026-03-16T20:50:00+00:00",
        "event_type":   "signing",
        "categories":   ["Presidential Actions", "Executive Orders"],
        "keywords":     ["energy", "executive order", "oil"],
        "source":       "whitehouse"
      }, ...
    ]
  }

Runs every 30 min via MaintenanceRunner.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

OUT_PATH = Path("data/wh_schedule.json")
LOOKBACK_HOURS = 72   # keep events from the past 3 days + upcoming

# ─── Feed URLs ────────────────────────────────────────────────────────────────
WH_FEED = "https://www.whitehouse.gov/news/feed/"

GOOGLE_NEWS_FEEDS = [
    # Catches live / upcoming signing ceremonies and remarks
    (
        "https://news.google.com/rss/search?q=%22White+House%22+"
        "%22signing+ceremony%22+OR+%22will+sign%22+OR+%22press+briefing%22"
        "+OR+%22delivers+remarks%22&hl=en-US&gl=US&ceid=US:en",
        "google_news",
    ),
    # Catches schedule-preview headlines ("Trump to sign X", "Trump holds Y")
    (
        "https://news.google.com/rss/search?q=Trump+"
        "%22to+sign%22+OR+%22holds+remarks%22+OR+%22briefing+today%22"
        "+OR+%22signing+ceremony%22&hl=en-US&gl=US&ceid=US:en",
        "google_news",
    ),
]

# ─── Event-type inference ─────────────────────────────────────────────────────
# WH feed categories → event_type
CATEGORY_EVENT_TYPE: dict[str, str] = {
    "executive orders":          "signing",
    "presidential actions":      "signing",
    "speeches & remarks":        "remarks",
    "speeches and remarks":      "remarks",
    "press briefings":           "briefing",
    "briefings & statements":    "announcement",
    "briefings and statements":  "announcement",
    "statements & releases":     "announcement",
    "statements and releases":   "announcement",
    "fact sheets":               "signing",  # fact sheets accompany signings
    "proclamations":             "announcement",
    "memoranda":                 "signing",
}

# Title / description patterns → event_type  (first match wins)
_TITLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(signs|signed|signing)\b.*\b(executive order|bill|act|law)\b", re.I), "signing"),
    (re.compile(r"\b(executive order|signing ceremony)\b", re.I), "signing"),
    (re.compile(r"\b(delivers|hold[s]?)\b.*\bremarks\b", re.I), "remarks"),
    (re.compile(r"\bremarks\b.*\b(by|from)\b.*\b(president|trump)\b", re.I), "remarks"),
    (re.compile(r"\b(holds?|hold)\b.*\b(press (conference|briefing)|briefing)\b", re.I), "briefing"),
    (re.compile(r"\bpress (conference|briefing)\b", re.I), "briefing"),
    (re.compile(r"\baddress(es)?\b.{0,30}\bnation\b", re.I), "address"),
    (re.compile(r"\b(joint address|state of the union|address to congress)\b", re.I), "address"),
    (re.compile(r"\binterview(s|ed)?\b", re.I), "interview"),
    (re.compile(r"\b(maga )?rally\b", re.I), "rally"),
    (re.compile(r"\b(summit|bilateral meeting|official visit)\b", re.I), "summit"),
    (re.compile(r"\b(visit[s]?|travels? to)\b", re.I), "visit"),
    (re.compile(r"\b(will sign|to sign)\b", re.I), "signing"),
    (re.compile(r"\b(announcement|announce[sd]?)\b", re.I), "announcement"),
    (re.compile(r"\bremarks\b", re.I), "remarks"),
]

# Known phrases to extract as keywords from event titles
KNOWN_PHRASES: list[str] = [
    "energy", "oil", "gas", "tariff", "iran", "israel", "china", "russia",
    "ukraine", "border", "immigration", "military", "nuclear", "executive order",
    "fraud", "election", "fentanyl", "drug", "healthcare", "education",
    "trillion", "economy", "inflation", "ai", "crypto", "bitcoin",
    "epstein", "democrat", "woke", "dei", "transgender", "college",
    "greenland", "nato", "investment", "america first",
]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fetch(url: str) -> str | None:
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; kalshi-edge/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        })
        with urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="ignore")
    except (URLError, OSError) as exc:
        logger.warning("Fetch failed %s: %s", url[:80], exc)
        return None


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc)
    except Exception:
        pass
    # ISO 8601 fallback
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(raw.strip(), fmt).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def _infer_event_type(title: str, categories: list[str]) -> str:
    # 1. Category-based (most reliable for official WH feed)
    for cat in categories:
        ev = CATEGORY_EVENT_TYPE.get(cat.lower().strip())
        if ev:
            return ev
    # 2. Title-pattern based
    for pattern, ev_type in _TITLE_PATTERNS:
        if pattern.search(title):
            return ev_type
    return "general"


def _extract_keywords(title: str, description: str = "") -> list[str]:
    text = (title + " " + description).lower()
    found = []
    for phrase in KNOWN_PHRASES:
        if phrase in text:
            found.append(phrase)
    return found


def _parse_rss(xml_text: str, source: str, cutoff: datetime) -> list[dict]:
    events: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("RSS parse error (%s): %s", source, exc)
        return events

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        desc  = (item.findtext("description") or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()
        cats  = [c.text.strip() for c in item.findall("category") if c.text]

        if not title:
            continue

        pub_dt = _parse_date(pub)
        if pub_dt and pub_dt < cutoff:
            continue  # too old

        ev_type  = _infer_event_type(title, cats)
        keywords = _extract_keywords(title, desc)
        pub_iso  = pub_dt.isoformat() if pub_dt else ""

        events.append({
            "title":        title,
            "url":          link,
            "published_at": pub_iso,
            "event_type":   ev_type,
            "categories":   cats,
            "keywords":     keywords,
            "source":       source,
        })

    return events


def fetch_wh_schedule() -> list[dict]:
    now = _now_utc()
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    all_events: list[dict] = []
    seen_urls: set[str] = set()

    # 1. Official WH feed
    xml = _fetch(WH_FEED)
    if xml:
        evs = _parse_rss(xml, "whitehouse", cutoff)
        logger.info("WH feed: %d items (after cutoff)", len(evs))
        for ev in evs:
            url = ev["url"]
            if url not in seen_urls:
                seen_urls.add(url)
                all_events.append(ev)

    # 2. Google News feeds
    for url, src in GOOGLE_NEWS_FEEDS:
        xml = _fetch(url)
        if not xml:
            continue
        evs = _parse_rss(xml, src, cutoff)
        logger.info("Google News feed: %d items (after cutoff)", len(evs))
        added = 0
        for ev in evs:
            # Deduplicate by URL, and skip events that don't mention Trump directly
            ev_url = ev["url"]
            if ev_url in seen_urls:
                continue
            if not re.search(r"\btrump\b|\bwhite house\b|\bpresident\b", ev["title"], re.I):
                continue
            seen_urls.add(ev_url)
            all_events.append(ev)
            added += 1
        logger.info("  → %d new after dedup/filter", added)

    # Sort newest first
    all_events.sort(key=lambda e: e.get("published_at", ""), reverse=True)
    return all_events


def main() -> None:
    logger.info("Fetching WH schedule …")
    events = fetch_wh_schedule()

    payload = {
        "generated_at": _now_utc().isoformat(),
        "events": events,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    logger.info("Wrote %d events → %s", len(events), OUT_PATH)

    # Quick summary of event types found
    from collections import Counter
    by_type = Counter(e["event_type"] for e in events)
    logger.info("By type: %s", dict(by_type.most_common()))


if __name__ == "__main__":
    main()
