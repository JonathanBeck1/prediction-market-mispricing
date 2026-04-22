#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.story_hash import story_hash_for_rss_entry

MARKETS_PATH = Path("data/kalshi_markets.json")
SIGNALS_PATH = Path("data/signals.yaml")
RAW_NEWS_PATH = Path("data/news_posts.jsonl")

DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q=Trump+OR+White+House+OR+press+briefing&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "Accept": "application/rss+xml, application/xml, text/xml",
            "User-Agent": "kalshi-edge-news/1.0",
        },
    )
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _extract_entries(xml_text: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out

    # RSS
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            out.append({"title": title, "description": desc, "link": link, "published_at": pub})

    # Atom
    atom_ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//a:entry", atom_ns):
        title = (entry.findtext("a:title", default="", namespaces=atom_ns) or "").strip()
        summary = (entry.findtext("a:summary", default="", namespaces=atom_ns) or "").strip()
        link = ""
        link_node = entry.find("a:link", atom_ns)
        if link_node is not None:
            link = str(link_node.attrib.get("href", "")).strip()
        pub = (
            entry.findtext("a:updated", default="", namespaces=atom_ns)
            or entry.findtext("a:published", default="", namespaces=atom_ns)
            or ""
        ).strip()
        if title:
            out.append({"title": title, "description": summary, "link": link, "published_at": pub})

    return out


def _load_phrase_universe() -> list[str]:
    if not MARKETS_PATH.exists():
        return []
    try:
        data = json.loads(MARKETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    phrases: set[str] = set()
    for m in data.get("markets", []):
        p = str(m.get("primary_phrase", "")).strip().lower()
        if p:
            phrases.add(p)
        for v in m.get("phrase_variants", []):
            vv = str(v).strip().lower()
            if vv:
                phrases.add(vv)
    return sorted(phrases)


def _load_signals_map(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("signals", [])
    out: dict[str, dict] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        phrase = str(row.get("phrase", "")).strip().lower()
        if not phrase:
            continue
        # Preserve ALL fields (including llm_boost, llm_reasoning, etc.) so
        # LLM-written values survive subsequent signal-fetch runs.
        node = dict(row)
        node["phrase"] = phrase
        node.setdefault("news_pressure", 1.0)
        node.setdefault("x_buzz", 1.0)
        out[phrase] = node
    return out


def _save_signals_map(path: Path, signal_map: dict[str, dict]) -> None:
    rows = []
    for phrase in sorted(signal_map.keys()):
        node = dict(signal_map[phrase])
        # Ensure required scalar fields are correctly typed
        node["phrase"] = phrase
        node["news_pressure"] = float(node.get("news_pressure", 1.0))
        node["x_buzz"] = float(node.get("x_buzz", 1.0))
        node["updated_at"] = str(node.get("updated_at", "")).strip() or _now_iso()
        rows.append(node)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"signals": rows}, sort_keys=False), encoding="utf-8")


_MAX_NEWS_STORY_HASHES = 8


def _score_entries_with_hashes(
    entries: list[dict],
    phrases: list[str],
) -> tuple[dict[str, float], dict[str, set[str]]]:
    scores: dict[str, float] = {}
    phrase_hashes: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        h = story_hash_for_rss_entry(
            str(e.get("title", "") or ""),
            str(e.get("description", "") or ""),
        )
        text = f"{e.get('title', '')} {e.get('description', '')}".lower()
        for phrase in phrases:
            if len(phrase) < 3:
                continue
            pat = r"\b" + re.escape(phrase) + r"\b"
            if re.search(pat, text):
                scores[phrase] = scores.get(phrase, 0.0) + 1.0
                phrase_hashes[phrase].add(h)
    return scores, dict(phrase_hashes)


def _update_news_pressure(
    scores: dict[str, float],
    phrase_hashes: dict[str, set[str]],
) -> int:
    signal_map = _load_signals_map(SIGNALS_PATH)
    changed = 0
    for phrase, score in scores.items():
        node = signal_map.get(phrase)
        if node is None:
            node = {"phrase": phrase, "news_pressure": 1.0, "x_buzz": 1.0, "updated_at": None}
            signal_map[phrase] = node
        # free-feed signal: bounded lightweight pressure
        target = 1.0 + min(score, 20.0) * 0.02
        target = max(0.7, min(1.5, round(target, 2)))
        new_hashes = sorted(phrase_hashes.get(phrase, set()))[:_MAX_NEWS_STORY_HASHES]
        old_hashes = node.get("news_story_hashes")
        old_list = list(old_hashes) if isinstance(old_hashes, list) else []
        pressure_changed = float(node.get("news_pressure", 1.0)) != target
        hashes_changed = new_hashes != old_list
        if pressure_changed:
            node["news_pressure"] = target
            node["updated_at"] = _now_iso()
            changed += 1
        if hashes_changed:
            node["news_story_hashes"] = new_hashes
            if not pressure_changed:
                node["updated_at"] = _now_iso()
            changed += 1
    _save_signals_map(SIGNALS_PATH, signal_map)
    return changed


def main() -> None:
    feeds_raw = os.getenv("NEWS_FEED_URLS", "").strip()
    feeds = [u.strip() for u in feeds_raw.split(",") if u.strip()] if feeds_raw else list(DEFAULT_FEEDS)
    phrases = _load_phrase_universe()
    if not phrases:
        raise SystemExit("No phrase universe found. Run fetch_markets first.")

    entries: list[dict] = []
    for feed in feeds:
        try:
            xml_text = _fetch_text(feed)
            rows = _extract_entries(xml_text)
            for r in rows:
                r["source"] = feed
            entries.extend(rows)
        except (HTTPError, URLError, TimeoutError, OSError):
            continue

    if not entries:
        print("No news entries fetched; no signal updates.")
        return

    RAW_NEWS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RAW_NEWS_PATH.open("a", encoding="utf-8") as fh:
        ts = _now_iso()
        for row in entries:
            row_out = {
                "ts": ts,
                "source": row.get("source"),
                "title": row.get("title", ""),
                "description": row.get("description", ""),
                "link": row.get("link", ""),
                "published_at": row.get("published_at", ""),
            }
            fh.write(json.dumps(row_out, ensure_ascii=True) + "\n")

    score_map, phrase_hashes = _score_entries_with_hashes(entries, phrases)
    changed = _update_news_pressure(score_map, phrase_hashes)

    print(f"News feeds polled: {len(feeds)}")
    print(f"News entries fetched: {len(entries)}")
    print(f"Phrases with news matches: {len(score_map)}")
    print(f"signals.yaml news_pressure / news_story_hashes updates: {changed}")


if __name__ == "__main__":
    main()
