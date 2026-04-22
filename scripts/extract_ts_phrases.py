#!/usr/bin/env python3
"""D1b — Truth Social direct-phrase p_floor injector.

Reads data/truth_social_posts.json and injects p_floor values into active event
signal files when a Kalshi market phrase appears verbatim in a recent Trump post.

WHY THIS WORKS:
  If Trump posted "SAVE AMERICA ACT" 12 hours ago, the probability he says it at
  his next speech is ~85%+, regardless of the 14% historical base rate.  This is
  CAUSAL evidence, not statistical.  The base rate assumes no prior knowledge;
  Truth Social posts are prior knowledge.

p_floor values by post age:
  < 24h  → 0.88   (near-certain talking point)
  24-48h → 0.78   (very likely to repeat)
  48-72h → 0.68   (elevated, fading fast)

  Any older than 72h: no injection (stale context, market already priced).

SPEAKER FILTER:
  Only injected into events for speakers where Trump's posts are directly
  predictive: trump, leavitt, hegseth.  Not for earnings, auto, sports,
  mamdani, starmer, or fed (different principals, different talking points).

SAFETY GUARDS:
  * If the event signal file already has a p_floor >= our value for a phrase,
    we leave it alone (don't downgrade a human-written signal).
  * If the event is "ended" or not in scheduled/live state, skip it.
  * Blocklist of common English words that appear in any political text but
    carry no predictive signal (single-word stop terms).
  * Minimum phrase length: 4 characters; single words must pass blocklist check.

Output:
  Modifies data/event_signals/auto_<speaker>_<ticker>.json in-place.
  Prints a summary of all injections made.

Usage:
    python3 scripts/extract_ts_phrases.py
    python3 scripts/extract_ts_phrases.py --dry-run
    python3 scripts/extract_ts_phrases.py --max-age-hours 48
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect as _db_connect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
TRUTH_SOCIAL_PATH = Path("data/truth_social_posts.json")
MARKETS_PATH      = Path("data/kalshi_markets.json")
SIGNALS_DIR       = Path("data/event_signals")
DB_PATH           = Path("data/edge.db")

# ── Config ─────────────────────────────────────────────────────────────────────
DEFAULT_MAX_AGE_H = 72

# Only speakers where Trump's posts directly predict what will be said.
# Leavitt and Hegseth echo White House talking points; mamdani/starmer do not.
_TS_PREDICTIVE_SPEAKERS: frozenset[str] = frozenset({"trump", "leavitt", "hegseth"})

# p_floor by age bucket
_FLOOR_BY_AGE: list[tuple[float, float]] = [
    (24.0,  0.88),   # < 24h
    (48.0,  0.78),   # 24-48h
    (72.0,  0.68),   # 48-72h
]

# Single-word phrases that are so common in political speech that their presence
# in a Trump post carries zero marginal signal.  Multi-word phrases are exempt.
_COMMON_WORD_BLOCKLIST: frozenset[str] = frozenset({
    "good", "great", "best", "big", "new", "old", "safe", "real", "free",
    "deal", "time", "year", "day", "job", "win", "get", "back", "make",
    "true", "fact", "law", "said", "said", "only", "just", "also", "even",
    "ever", "many", "much", "very", "more", "less", "than", "that", "this",
    "with", "from", "been", "have", "will", "well", "come", "know", "need",
    "help", "part", "keep", "look", "move", "work", "take", "call", "hold",
    "turn", "show", "talk", "stop", "change", "start", "tell", "never",
    "want", "over", "down", "here", "next", "each", "what", "when", "how",
    "people", "place", "world", "going", "right", "wrong", "doing", "bring",
    "again", "still", "every", "after", "power", "money", "house", "state",
    "thing", "ready", "under", "about", "would", "could", "should", "might",
    "high", "low", "long", "hard", "fast", "soon", "away", "stay", "last",
    "president",   # appears in every Trump post — not a differentiator
    "america",     # same — too broad
    "country",     # too broad
    "said",        # too broad
    "congress",    # too broad for a single-word floor
    "democrat",    # use "radical left democrats" or "sleepy joe" variants
    "republican",  # too broad
    "billion",     # too broad when a single word
    "million",     # too broad
    "dollars",     # too broad
    "federal",     # too broad
    "national",    # too broad
    "american",    # too broad
    "united",      # too broad
    "states",      # too broad
    "press",       # too broad
    "report",      # too broad
    "news",        # too broad
    "media",       # too broad
})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_posts(max_age_hours: float) -> list[dict]:
    """Load Truth Social posts newer than max_age_hours, with age annotation."""
    if not TRUTH_SOCIAL_PATH.exists():
        logger.warning("Truth Social posts file not found: %s", TRUTH_SOCIAL_PATH)
        return []
    data = json.loads(TRUTH_SOCIAL_PATH.read_text(encoding="utf-8"))
    posts = data.get("posts", []) if isinstance(data, dict) else data
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=max_age_hours)
    result = []
    for p in posts:
        ts_str = p.get("posted_at") or p.get("created_at") or ""
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if dt < cutoff:
            continue
        age_h = (datetime.now(tz=timezone.utc) - dt).total_seconds() / 3600
        result.append({**p, "_age_h": age_h})
    result.sort(key=lambda x: x["_age_h"])
    logger.info("Loaded %d Truth Social posts within last %.0fh", len(result), max_age_hours)
    return result


def _p_floor_for_age(age_h: float) -> float | None:
    """Return the p_floor value for a given post age, or None if too old."""
    for threshold, floor in _FLOOR_BY_AGE:
        if age_h < threshold:
            return floor
    return None


def _load_active_markets() -> dict[str, dict[str, Any]]:
    """Return {phrase_lower: {speaker, tickers: [...]}} for active markets."""
    if not MARKETS_PATH.exists():
        logger.warning("kalshi_markets.json not found")
        return {}
    data = json.loads(MARKETS_PATH.read_text(encoding="utf-8"))
    markets = data.get("markets", []) if isinstance(data, dict) else data

    phrase_map: dict[str, dict[str, Any]] = {}
    for m in markets:
        if m.get("status") not in ("active", "open"):
            continue
        speaker = (m.get("speaker") or "").lower()
        if speaker not in _TS_PREDICTIVE_SPEAKERS:
            continue
        event_ticker = m.get("event_ticker", "")
        ticker = m.get("ticker", "")
        all_phrases = [m.get("primary_phrase", "")] + m.get("phrase_variants", [])
        for phrase in all_phrases:
            p = phrase.lower().strip()
            if not p or len(p) < 4:
                continue
            # Block single common words
            if " " not in p and p in _COMMON_WORD_BLOCKLIST:
                continue
            entry = phrase_map.setdefault(p, {"speakers": set(), "tickers": [], "event_tickers": []})
            entry["speakers"].add(speaker)
            if ticker not in entry["tickers"]:
                entry["tickers"].append(ticker)
            if event_ticker not in entry["event_tickers"]:
                entry["event_tickers"].append(event_ticker)
    logger.info("Active TS-eligible phrases: %d", len(phrase_map))
    return phrase_map


def _get_active_signal_files(speakers: set[str]) -> list[Path]:
    """Return event signal files for active (scheduled/live) events for these speakers."""
    if not DB_PATH.exists():
        logger.warning("edge.db not found, will scan all signal files for speakers")
        return [
            f for f in SIGNALS_DIR.glob("*.json")
            if any(f.name.startswith(f"auto_{spk}_") for spk in speakers)
        ]
    conn = _db_connect(DB_PATH)
    placeholders = ",".join("?" * len(speakers))
    rows = conn.execute(
        f"SELECT event_id FROM events WHERE speech_state IN ('scheduled','live') "
        f"AND speaker IN ({placeholders})",
        list(speakers),
    ).fetchall()
    conn.close()
    active_event_ids = {r[0] for r in rows}

    files = []
    for f in SIGNALS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("event_id") in active_event_ids:
            files.append(f)
    logger.info("Found %d active signal files for speakers %s", len(files), speakers)
    return files


def _find_phrase_matches(
    posts: list[dict],
    phrase_map: dict[str, dict[str, Any]],
) -> dict[str, tuple[float, float]]:
    """Scan posts for verbatim phrase appearances.

    Returns {phrase: (best_p_floor, age_h_of_best_match)}.
    When a phrase appears in multiple posts, we use the most recent (lowest age_h).
    """
    matches: dict[str, tuple[float, float]] = {}
    for post in posts:
        content = str(post.get("content", "")).lower()
        age_h = post["_age_h"]
        p_floor = _p_floor_for_age(age_h)
        if p_floor is None:
            continue
        for phrase in phrase_map:
            # Multi-word: require verbatim substring
            # Single-word: require word-boundary match to avoid partial matches
            if " " in phrase:
                found = phrase in content
            else:
                found = bool(re.search(r"\b" + re.escape(phrase) + r"\b", content))
            if found:
                existing = matches.get(phrase)
                if existing is None or p_floor > existing[0]:
                    matches[phrase] = (p_floor, age_h)
    return matches


def _inject_floors(
    signal_file: Path,
    phrase_matches: dict[str, tuple[float, float]],
    phrase_map: dict[str, dict[str, Any]],
    dry_run: bool,
) -> list[dict]:
    """Inject p_floors into a single signal file.

    Returns list of injection records (for logging).
    """
    try:
        data: dict = json.loads(signal_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s", signal_file, exc)
        return []

    event_speaker = (data.get("speaker") or "").lower()
    if event_speaker not in _TS_PREDICTIVE_SPEAKERS:
        return []

    p_floors: dict[str, float] = data.get("p_floors") or {}
    injected = []

    for phrase, (new_floor, age_h) in phrase_matches.items():
        info = phrase_map.get(phrase, {})
        # Only inject if this phrase's speaker matches the event speaker
        phrase_speakers = info.get("speakers", set())
        if event_speaker not in phrase_speakers:
            continue
        existing = p_floors.get(phrase)
        if existing is not None and existing >= new_floor:
            # Don't downgrade a higher (human or prior) floor
            continue
        p_floors[phrase] = new_floor
        injected.append({
            "phrase": phrase,
            "p_floor": new_floor,
            "age_h": round(age_h, 1),
            "was": existing,
        })

    if injected:
        data["p_floors"] = p_floors
        data["ts_phrase_injection"] = {
            "injected_at": datetime.now(tz=timezone.utc).isoformat(),
            "phrases_injected": len(injected),
        }
        if not dry_run:
            signal_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        verb = "[DRY RUN] Would inject" if dry_run else "Injected"
        logger.info("%s %d p_floors into %s", verb, len(injected), signal_file.name)
        for rec in injected:
            was_str = f"(was {rec['was']:.2f})" if rec["was"] is not None else "(new)"
            logger.info(
                "  %-30s p_floor=%.2f  age=%.1fh %s",
                rec["phrase"], rec["p_floor"], rec["age_h"], was_str,
            )

    return injected


# ── Main ───────────────────────────────────────────────────────────────────────

def run(max_age_hours: float = DEFAULT_MAX_AGE_H, dry_run: bool = False) -> None:
    posts        = _load_posts(max_age_hours)
    phrase_map   = _load_active_markets()

    if not posts or not phrase_map:
        logger.info("Nothing to do (no posts or no active phrases).")
        return

    phrase_matches = _find_phrase_matches(posts, phrase_map)
    if not phrase_matches:
        logger.info("No verbatim phrase matches found in Truth Social posts.")
        return

    logger.info("Phrase matches found: %d", len(phrase_matches))
    for phrase, (floor, age_h) in sorted(phrase_matches.items(), key=lambda x: -x[1][0]):
        logger.info("  %-30s p_floor=%.2f  age=%.1fh", phrase, floor, age_h)

    all_speakers: set[str] = set()
    for info in phrase_map.values():
        all_speakers |= info.get("speakers", set())
    all_speakers &= _TS_PREDICTIVE_SPEAKERS

    signal_files = _get_active_signal_files(all_speakers)
    if not signal_files:
        logger.warning("No active signal files found for speakers %s", all_speakers)
        return

    total_injections = 0
    for sf in signal_files:
        recs = _inject_floors(sf, phrase_matches, phrase_map, dry_run)
        total_injections += len(recs)

    action = "[DRY RUN] Would have injected" if dry_run else "Total injections:"
    logger.info("%s %d p_floors across %d signal files.", action, total_injections, len(signal_files))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Print planned injections without writing")
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_H, help="Max post age in hours (default: 72)")
    args = ap.parse_args()
    run(max_age_hours=args.max_age_hours, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
