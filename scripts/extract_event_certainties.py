#!/usr/bin/env python3
"""Event-title certainty injector — maps event agenda to near-certain p_overrides.

This is the highest-value pre-event signal.  Accounts making thousands on Kalshi
mention markets are NOT running complex ML models.  They are doing this:

  1. White House publishes schedule at ~8am: "President signs the SAVE AMERICA ACT"
  2. They see that KXTRUMPMENTION-26MAR30 has "save america act" at 35¢ (historical base rate)
  3. They immediately buy YES — it's 99% certain if the title IS the event
  4. Market corrects to 90¢ over next 2-3 hours.  They profit +55¢ per dollar.

This script automates step 3 by:
  - Parsing event titles for STRUCTURED certainties (bill names, person names, countries)
  - Matching extracted entities against active Kalshi market phrases for that event
  - Writing p_overrides (hard bypasses) or p_floors for near-certain matches

p_override values by certainty level:
  0.92  NAMED entity appears in event title (bill name, person being sworn in)
  0.85  EVENT FORMAT near-certainty (rally→political attacks; fundraiser→party line)
  0.78  INFERRED from title context (specific country meeting → country mentioned)

WHY p_override (not p_floor):
  p_floor raises the minimum but the formula still runs.  p_override completely
  bypasses the formula — the frequency model has ZERO relevance when you know
  the bill name being signed is literally the market phrase.

Usage:
    python3 scripts/extract_event_certainties.py
    python3 scripts/extract_event_certainties.py --dry-run
    python3 scripts/extract_event_certainties.py --event-id auto:trump:KXTRUMPMENTION-26MAR30
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT          = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.db import connect as _db_connect  # noqa: E402
DB_PATH       = ROOT / "data" / "edge.db"
MARKETS_PATH  = ROOT / "data" / "kalshi_markets.json"
SIGNALS_DIR   = ROOT / "data" / "event_signals"

# ── Certainty extraction rules ─────────────────────────────────────────────────

# Title patterns that signal WHAT the event IS about.
# Each tuple: (regex, p_override, label)
# We extract the named group "entity" from the match.
_TITLE_PATTERNS: list[tuple[re.Pattern, float, str]] = [
    # Bill/Act signing — extract the bill name in Title Case or ALL CAPS:
    # "Signing Ceremony for the SAVE AMERICA ACT" → "save america act"
    # "Signing Ceremony for the Save America Act" → "save america act"
    #
    # NO re.IGNORECASE: [A-Z] strictly matches uppercase only.  This means
    # the entity group stops when it hits a lowercase word ("for", "the"),
    # forcing the regex engine to advance past filler words until it finds
    # the real consecutive-capitals bill name.
    (re.compile(
        r"(?:[Ss]ign(?:ing|s|ed)?|[Ee]xecutive [Oo]rder)[^\"]*?"
        r"(?P<entity>[A-Z][A-Za-z\-]+(?:\s+[A-Z][A-Za-z\-]+){1,6})",
    ), 0.92, "BILL_SIGNING"),

    # Quoted name in any kind of quotation marks: "Save America Act" or "SAVE AMERICA ACT"
    (re.compile(
        r'["\u201c\u2018](?P<entity>[A-Za-z][A-Za-z\s\-\'\,\.]{3,})["\u201d\u2019]',
    ), 0.90, "QUOTED_NAME"),

    # Person name (First Last) immediately after a role title for swearing-in:
    # "Swearing-In for Secretary Kristi Noem" → "kristi noem"
    # Requires exactly TWO capitalized words (First Last) — prevents matching depts.
    (re.compile(
        r"(?i:swearing[\s\-]in|swears\s+in|inauguration|oath\s+of\s+office).*?"
        r"(?i:secretary|director|administrator|ambassador|chief|counsel|adviser|coordinator)\s+"
        r"(?!of\b)(?P<entity>[A-Z][a-z]+\s+[A-Z][a-z]+)",
    ), 0.92, "SWEARING_IN_PERSON"),

    # Swearing-in with department name (when person name absent):
    # "Swearing-In Ceremony for the Secretary of Homeland Security" → "homeland security"
    (re.compile(
        r"(?i:swearing[\s\-]in|swears\s+in).*?(?i:secretary|director|chief)\s+of\s+"
        r"(?P<entity>[A-Z][A-Za-z\s]+?)(?:\s*[,\.\?]|$)",
        re.IGNORECASE,
    ), 0.82, "SWEARING_IN_DEPT"),

    # Named bilateral / diplomatic meeting: "meeting with [ROLE] [Name]"
    (re.compile(
        r"(?:meeting|summit|talks|call|dinner|lunch|visit)\s+with\s+"
        r"(?:(?:president|prime\s+minister|pm|chancellor|premier|king|queen|prince|emir|sheikh)\s+)?"
        r"(?P<entity>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})",
        re.IGNORECASE,
    ), 0.88, "BILATERAL_MEETING"),

    # Named award/trophy
    (re.compile(
        r"(?:award|trophy|medal|honor|recognition|presentation)\s+(?:ceremony\s+)?(?:for\s+)?(?:the\s+)?"
        r"(?P<entity>[A-Z][A-Za-z\s\-]+(?:Trophy|Award|Medal|Cup|Shield|Prize))",
        re.IGNORECASE,
    ), 0.85, "AWARD_CEREMONY"),

    # Named summit / conference
    (re.compile(
        r"(?:at|attend(?:ing)?|opening\s+of|closing\s+of)\s+(?:the\s+)?"
        r"(?P<entity>[A-Z][A-Za-z\s]+(?:Summit|Conference|Forum|Assembly|Convention|Council))",
        re.IGNORECASE,
    ), 0.82, "NAMED_EVENT"),
]

# Event-format → implied phrase boosts.
# These are FORMAT-level certainties: a Republican fundraiser means Trump
# will DEFINITELY attack Democrats.  A diplomatic meeting means the country WILL be named.
_FORMAT_PHRASE_BOOSTS: list[tuple[re.Pattern, list[str], float, str]] = [
    # Republican fundraiser / NRCC / GOP dinner (allow words between party name and event type)
    (re.compile(r"(?:NRCC|RNCC|GOP|Republican|Conservative)\b.*?\b(?:dinner|gala|fundrais|retreat|caucus|conference)", re.IGNORECASE),
     ["radical left", "democrat", "republican", "election", "trump"],
     0.80, "GOP_FUNDRAISER"),

    # Diplomatic: country or its adjective form appearing in title
    (re.compile(r"(?:Japan(?:ese)?|China|Chinese|India(?:n)?|German(?:y)?|Franc(?:e|ais|ais)?|French|UK|United\s+Kingdom|British|Israel(?:i)?|Saudi|Ukraine(?:ian)?|Iran(?:ian)?|Canada|Canadian|Mexico|Mexican|South\s+Korea(?:n)?|Korean|Australia(?:n)?|Brazil(?:ian)?)", re.IGNORECASE),
     [],  # dynamically filled with country name
     0.85, "DIPLOMATIC_TOPIC"),

    # Press conference / briefing → specific topic mentions
    (re.compile(r"(?:press\s+conference|press\s+briefing|gaggle)\s+(?:on\s+)?(?P<topic>[A-Z][a-z]+(?:\s+[A-Z]?[a-z]+)?)", re.IGNORECASE),
     [],  # topic extracted from match
     0.82, "PRESS_ON_TOPIC"),

    # Rally / campaign-style speech
    (re.compile(r"(?:rally|campaign\s+event|Make\s+America\s+Great|MAGA|Save\s+America\s+Rally)", re.IGNORECASE),
     ["radical left", "election", "democrat", "fake news", "border"],
     0.72, "CAMPAIGN_RALLY"),
]

# Countries: when a country name appears in the title, these phrases in
# the market are very likely to be mentioned.
_COUNTRY_PHRASE_MAP: dict[str, list[str]] = {
    "japan":        ["japan", "toyota", "trade"],
    "japanese":     ["japan", "toyota", "trade"],
    "china":        ["china", "tariff", "trade"],
    "chinese":      ["china", "tariff", "trade"],
    "israel":       ["israel", "netanyahu", "bibi", "hostages", "hamas", "middle east"],
    "israeli":      ["israel", "netanyahu", "bibi", "hostages", "hamas", "middle east"],
    "iran":         ["iran", "nuclear", "deal"],
    "iranian":      ["iran", "nuclear", "deal"],
    "ukraine":      ["ukraine", "zelensky", "russia", "putin"],
    "ukrainian":    ["ukraine", "zelensky", "russia", "putin"],
    "canada":       ["canada", "tariff", "trade"],
    "canadian":     ["canada", "tariff", "trade"],
    "mexico":       ["mexico", "border", "tariff"],
    "mexican":      ["mexico", "border", "tariff"],
    "saudi":        ["saudi", "oil", "energy", "aramco"],
    "india":        ["india", "modi", "trade"],
    "indian":       ["india", "modi", "trade"],
    "south korea":  ["south korea", "korea", "samsung"],
    "korean":       ["south korea", "korea", "samsung"],
    "australia":    ["australia", "albanese"],
    "australian":   ["australia", "albanese"],
    "united kingdom": ["uk", "britain", "starmer", "trade deal"],
    "british":      ["uk", "britain", "starmer", "trade deal"],
    "germany":      ["germany", "merkel", "europe"],
    "german":       ["germany", "europe"],
    "france":       ["france", "macron", "europe"],
    "french":       ["france", "macron", "europe"],
}


def _clean_entity(raw: str) -> str:
    """Normalize extracted entity: strip articles, clean whitespace."""
    s = re.sub(r"^(?:the|a|an)\s+", "", raw.strip(), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def _extract_scotus_certainties(title: str, event_phrases: dict[str, str]) -> list[tuple[str, float, str]]:
    """For SCOTUS oral arguments, extract case-specific certainties.

    Kalshi only creates phrases that are SPECIFIC to the case being argued.
    A phrase like "Independent Contractor" appearing in the FLOWERS FOODS case
    market means Kalshi's researchers believe it's relevant to this case.
    ALL phrases in a SCOTUS oral argument market are case-specific — the market
    maker has already done the filtering work.

    Therefore: give every phrase in the market a minimum p_floor of 0.70.
    Core legal terms (FAA, Arbitration, Contractor, Exempt) get 0.82+.
    Party names extracted from the case title get p_override 0.90+.
    """
    if not re.search(r"(?i:oral\s+argument|supreme\s+court)", title):
        return []

    results: list[tuple[str, float, str]] = []

    # ── Extract party names from case title ──────────────────────────────────
    # "Flowers Foods, Inc. v. Brock" → "flowers foods", "brock"
    # "United States v. Skrmetti" → "skrmetti", "united states"
    case_match = re.search(
        r"(?:of\s+|in\s+)?(?P<p1>[A-Z][A-Za-z\s\.,]+?)\s+v\.?\s+(?P<p2>[A-Z][A-Za-z\s\.,]+?)(?:\s*[\(\[]|$)",
        title,
        re.IGNORECASE,
    )
    party_terms: list[str] = []
    if case_match:
        for grp in ("p1", "p2"):
            raw = _clean_entity(case_match.group(grp))
            # Strip corporate suffixes, keep the meaningful name
            cleaned = re.sub(r"\b(?:Inc|LLC|Corp|Ltd|Co|INC|LLC|CORP)\.?\b", "", raw).strip()
            words = [w.lower() for w in cleaned.split() if len(w) >= 3]
            party_terms.extend(words)

    # Legal boilerplate — phrases that appear in virtually every oral argument
    _LEGAL_BOILERPLATE: frozenset[str] = frozenset({
        "constitution", "constitutional", "statute", "statutory", "congress",
        "federal", "jurisdiction", "precedent", "amicus", "brief", "court",
        "justice", "opinion", "ruling", "appeal", "circuit",
    })

    for phrase_lower in event_phrases:
        # Skip the "Event does not qualify" escape hatch
        if "does not qualify" in phrase_lower:
            continue

        # Party name → strong certainty
        if any(pt in phrase_lower or phrase_lower in pt for pt in party_terms if len(pt) >= 4):
            results.append((phrase_lower, 0.90, "SCOTUS_PARTY"))
            continue

        # Legal boilerplate → moderate certainty
        if any(bl in phrase_lower for bl in _LEGAL_BOILERPLATE):
            results.append((phrase_lower, 0.72, "SCOTUS_BOILERPLATE"))
            continue

        # Everything else in the market: the market maker put it here for this case
        # → case-specific floor
        results.append((phrase_lower, 0.68, "SCOTUS_CASE_SPECIFIC"))

    return results


def _extract_hearing_certainties(title: str, event_phrases: dict[str, str]) -> list[tuple[str, float, str]]:
    """For congressional / senate / house committee hearings, extract certainties.

    Committee hearings are named after the SPECIFIC topic being discussed.
    A hearing titled "Protecting American Citizens from Criminal Illegal Aliens"
    means "immigrant", "ICE", "criminal", "taxpayer" are near-certain.

    Kalshi's phrase list for hearings is also case-specific (hand-curated).
    Strategy: blanket p_floor for all case phrases, boost topic-matching ones.
    """
    if not re.search(
        r"(?i:committee|subcommittee|senate|house\s+(?:oversight|judiciary|armed)|hearing|testimony)",
        title,
    ):
        return []

    results: list[tuple[str, float, str]] = []
    title_words = set(re.findall(r"[a-z]+", title.lower()))

    # Topic keywords in the hearing title that appear verbatim in phrase list
    _STOPWORDS: frozenset[str] = frozenset({
        "the", "and", "for", "from", "with", "that", "this", "are", "any",
        "have", "will", "what", "which", "their", "about", "into", "also",
        "been", "were", "they", "when", "before", "after", "committee",
        "subcommittee", "hearing", "senate", "house", "congress",
    })
    topic_words = title_words - _STOPWORDS

    for phrase_lower in event_phrases:
        if "does not qualify" in phrase_lower:
            continue
        phrase_words = set(re.findall(r"[a-z]+", phrase_lower))

        # Direct overlap between hearing title and phrase → high certainty
        overlap = phrase_words & topic_words
        if overlap and len(overlap) >= max(1, len(phrase_words) // 2):
            results.append((phrase_lower, 0.82, "HEARING_TOPIC_MATCH"))
        else:
            # All phrases in a hearing market are hearing-specific → moderate floor
            results.append((phrase_lower, 0.65, "HEARING_CASE_SPECIFIC"))

    return results


def _extract_fed_certainties(title: str, event_phrases: dict[str, str]) -> list[tuple[str, float, str]]:
    """For Fed/Powell press conferences, extract certainties based on rate decision.

    Powell press conferences follow a rigid script.  Several phrases appear in
    EVERY press conference regardless of the decision:
      - "data-dependent", "price stability", "inflation", "labor market" → ~95%+
      - "restrictive" (when rates are high) → ~90%
      - "uncertainty" (almost always, especially now) → ~90%

    Rate-decision-specific phrases:
      - "unchanged" → near-certain if Fed holds rates (check the actual decision)
      - "cut" / "lower" → near-certain if Fed cuts
      - Current context (2026): tariffs, trade war → "tariff", "uncertainty" → very likely

    Without the actual rate decision known, we apply context-based floors.
    """
    if not re.search(r"(?i:federal\s+reserve|fed\b|powell|fomc|press\s+conference)", title):
        return []

    results: list[tuple[str, float, str]] = []

    # Boilerplate phrases Powell says in EVERY press conference
    _FED_BOILERPLATE: dict[str, float] = {
        "inflation":        0.95,
        "price stability":  0.92,
        "labor market":     0.90,
        "data":             0.88,
        "employment":       0.88,
        "uncertainty":      0.90,  # especially high in 2026 tariff environment
        "tariff":           0.85,  # 2026 trade war context
        "trade":            0.82,
        "restrictive":      0.82,
        "unchanged":        0.85,  # most likely outcome — rates on hold
        "softening":        0.72,
        "slowdown":         0.72,
        "soft landing":     0.68,
        "stagflation":      0.65,
        "yield curve":      0.68,
        "shock":            0.62,
        "trade war":        0.78,
        "trump":            0.75,  # press always asks about Trump/tariffs
    }

    for phrase_lower in event_phrases:
        if "does not qualify" in phrase_lower:
            continue
        p = _FED_BOILERPLATE.get(phrase_lower)
        if p:
            results.append((phrase_lower, p, "FED_BOILERPLATE"))
        else:
            # Fed phrases are curated by Kalshi for that specific conference
            results.append((phrase_lower, 0.60, "FED_CONFERENCE"))

    return results


def _extract_pmq_certainties(title: str, event_phrases: dict[str, str]) -> list[tuple[str, float, str]]:
    """For UK Prime Minister's Questions (PMQs), apply PMQ-specific floors.

    PMQs is one of the most predictable political formats: 30 min weekly, UK
    Parliament, always covers the same structural topics.  The Kalshi phrase
    set maps directly to permanent PMQ staples.

    Rates derived from Hansard PMQ transcripts, March 2024–Feb 2026.
    These are FLOORS — the actual probability may be higher if current-week
    headlines align (which extract_ts_phrases / LLM handles on top).
    """
    if not re.search(
        r"(?i:prime\s*minister.s\s*question|pmq|minister.s\s*questions?\s*(?:\(|uk|parliament))",
        title,
    ):
        return []

    # Phrase-specific PMQ floors — apply whenever phrase appears in market list.
    # Current-affairs context (2026): Ukraine war, Trump trade disputes, UK defence
    # spending pledge, Gaza/ceasefire, Reform UK rise, net-zero energy debate.
    _PMQ_FLOORS: dict[str, float] = {
        "ukraine":    0.88,
        "trump":      0.78,
        "nato":       0.68,
        "defense":    0.65,
        "defence":    0.65,
        "nuclear":    0.55,
        "ceasefire":  0.52,
        "israel":     0.52,
        "reform":     0.50,
        "energy":     0.48,
        "iran":       0.40,
        "immigrant":  0.38,
        "immigration":0.40,
        "drone":      0.35,
        "oil":        0.28,
        "russia":     0.60,
        "china":      0.45,
        "tariff":     0.50,
        "trade":      0.52,
        "economy":    0.60,
        "nhs":        0.55,
        "health":     0.50,
    }

    results: list[tuple[str, float, str]] = []
    for phrase_lower in event_phrases:
        if "does not qualify" in phrase_lower:
            continue
        p = _PMQ_FLOORS.get(phrase_lower)
        if p:
            results.append((phrase_lower, p, "PMQ_FLOOR"))
        else:
            # Kalshi curates their Starmer phrase list for current affairs —
            # any phrase they include is likely topical; apply a moderate floor.
            results.append((phrase_lower, 0.40, "PMQ_GENERIC"))

    return results


def _extract_certainties_from_title(title: str) -> list[tuple[str, float, str]]:
    """Parse a market event title and return (entity_phrase, p_value, label) tuples."""
    results: list[tuple[str, float, str]] = []
    title_lower = title.lower()

    # Apply structural title patterns
    for pattern, p_val, label in _TITLE_PATTERNS:
        for m in pattern.finditer(title):
            entity = _clean_entity(m.group("entity"))
            # Guard: entity must start with an uppercase letter in the original title.
            # This prevents IGNORECASE patterns from matching lowercase filler words
            # like "ceremony for the" before the actual bill/person name.
            if not entity or len(entity) < 3:
                continue
            if not entity[0].isupper():
                continue
            results.append((entity.lower(), p_val, label))

    # Apply format-level boosts
    for fmt_pattern, implied_phrases, p_val, label in _FORMAT_PHRASE_BOOSTS:
        if not fmt_pattern.search(title):
            continue
        # Diplomatic: extract country name and look up associated phrases
        if label == "DIPLOMATIC_TOPIC":
            for country, related in _COUNTRY_PHRASE_MAP.items():
                if country in title_lower:
                    for phrase in related:
                        results.append((phrase, p_val, label + f":{country.upper()}"))
        else:
            for phrase in implied_phrases:
                results.append((phrase, p_val, label))
            # Also extract the named topic from press briefing
            m = fmt_pattern.search(title)
            if m and "topic" in m.groupdict() and m.group("topic"):
                topic = _clean_entity(m.group("topic")).lower()
                if len(topic) >= 4:
                    results.append((topic, p_val, label + ":TOPIC"))

    return results


def _load_event_phrases(event_ticker: str) -> dict[str, str]:
    """Return {phrase_lower: primary_phrase} for all phrases in this event's active markets."""
    if not MARKETS_PATH.exists():
        return {}
    data = json.loads(MARKETS_PATH.read_text(encoding="utf-8"))
    markets = data.get("markets", []) if isinstance(data, dict) else data

    phrase_map: dict[str, str] = {}
    for m in markets:
        if m.get("event_ticker") != event_ticker:
            continue
        if m.get("status") not in ("active", "open"):
            continue
        primary = m.get("primary_phrase", "")
        all_phrases = [primary] + m.get("phrase_variants", [])
        for phrase in all_phrases:
            if phrase:
                phrase_map[phrase.lower().strip()] = primary
    return phrase_map


def _get_active_events(event_id_filter: str | None = None) -> list[dict]:
    """Return active events from edge.db, optionally filtered."""
    if not DB_PATH.exists():
        return []
    conn = _db_connect(DB_PATH)
    query = "SELECT event_id, speaker, event_type, speech_state, notes FROM events WHERE speech_state IN ('scheduled','live')"
    params: list = []
    if event_id_filter:
        query += " AND event_id = ?"
        params.append(event_id_filter)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [
        {"event_id": r[0], "speaker": r[1], "event_type": r[2], "speech_state": r[3], "notes": r[4]}
        for r in rows
    ]


def _get_event_title(event_id: str) -> str:
    """Try to find a human-readable title for this event from market data."""
    parts = event_id.split(":", 2)
    event_ticker = parts[2] if len(parts) == 3 else event_id

    # Check market snapshots in DB
    if DB_PATH.exists():
        try:
            conn = _db_connect(DB_PATH)
            row = conn.execute(
                "SELECT raw_json FROM market_snapshots WHERE market_id LIKE ? LIMIT 1",
                (f"{event_ticker}%",),
            ).fetchone()
            conn.close()
            if row:
                snap = json.loads(row[0])
                raw_api = snap.get("raw_api") or {}
                if isinstance(raw_api, str):
                    raw_api = json.loads(raw_api)
                title = raw_api.get("title", "")
                if title:
                    m = re.match(r"What will .+? say during (.+?)\??$", title)
                    return m.group(1) if m else title
        except Exception:
            pass

    # Check kalshi_markets.json
    if MARKETS_PATH.exists():
        try:
            data = json.loads(MARKETS_PATH.read_text(encoding="utf-8"))
            market_list = data.get("markets", []) if isinstance(data, dict) else data
            for mkt in market_list:
                if mkt.get("event_ticker") == event_ticker:
                    title = mkt.get("title", "") or mkt.get("event_title", "")
                    if title:
                        m = re.match(r"What will .+? say during (.+?)\??$", title)
                        return m.group(1) if m else title
        except Exception:
            pass

    return ""


def _signal_file_for_event(event_id: str) -> Path | None:
    """Find the event signal file for this event_id."""
    if not SIGNALS_DIR.exists():
        return None
    for f in SIGNALS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("event_id") == event_id:
                return f
        except Exception:
            continue
    return None


def _inject_certainties(
    signal_file: Path,
    certainties: list[tuple[str, float, str]],
    dry_run: bool,
) -> list[dict]:
    """Write p_overrides/p_floors into a signal file.

    High certainty (>= 0.90): write as p_override (bypasses formula entirely).
    Medium certainty (0.75-0.89): write as p_floor (soft minimum).
    Returns list of injections made.
    """
    try:
        data: dict = json.loads(signal_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s", signal_file, exc)
        return []

    p_overrides: dict[str, float] = data.get("p_overrides") or {}
    p_floors: dict[str, float] = data.get("p_floors") or {}
    injected = []

    for phrase, p_val, label in certainties:
        if p_val >= 0.90:
            existing = p_overrides.get(phrase)
            if existing is not None and existing >= p_val:
                continue
            p_overrides[phrase] = p_val
            injected.append({"phrase": phrase, "type": "p_override", "value": p_val, "label": label, "was": existing})
        else:
            existing = p_floors.get(phrase)
            if existing is not None and existing >= p_val:
                continue
            p_floors[phrase] = p_val
            injected.append({"phrase": phrase, "type": "p_floor", "value": p_val, "label": label, "was": existing})

    if injected:
        data["p_overrides"] = p_overrides
        data["p_floors"]    = p_floors
        data["event_certainty_injection"] = {
            "injected_at": datetime.now(tz=timezone.utc).isoformat(),
            "injections": len(injected),
        }
        if not dry_run:
            signal_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        verb = "[DRY RUN]" if dry_run else "INJECTED"
        logger.info("%s %d certainties → %s", verb, len(injected), signal_file.name)
        for rec in injected:
            was = f"(was {rec['was']:.2f})" if rec["was"] is not None else "(new)"
            logger.info(
                "  %s %-30s %s=%.2f  [%s] %s",
                rec["type"].upper().ljust(11), rec["phrase"],
                "override" if rec["type"] == "p_override" else "floor   ",
                rec["value"], rec["label"], was,
            )

    return injected


def run(event_id_filter: str | None = None, dry_run: bool = False) -> None:
    events = _get_active_events(event_id_filter)
    if not events:
        logger.info("No active/scheduled events found.")
        return

    logger.info("Processing %d active events...", len(events))
    total_injections = 0

    for event in events:
        event_id = event["event_id"]
        parts = event_id.split(":", 2)
        event_ticker = parts[2] if len(parts) == 3 else event_id

        title = _get_event_title(event_id)
        if not title:
            logger.debug("No title found for %s — skipping certainty extraction", event_id)
            continue

        # Skip per-phrase question titles (rolling window markets like KXTRUMPSAY).
        # These are of the form "Will Trump say X before [date]?" — not event descriptions.
        # We only want EVENT descriptions like "Remarks at the NRCC Annual Dinner".
        # The "What will X say during Y" pattern gets cleaned to just "Y" by _get_event_title.
        # If after cleaning the title still starts with "Will " or "Does " or is shorter
        # than 15 characters, it's a per-phrase question — skip it.
        if re.match(r"(?i:Will|Does|Did|Has|Is|Are|Was|Were|Would|Could|Should)\s", title):
            logger.debug("Skipping per-phrase question title: %r", title[:60])
            continue
        if len(title) < 12:
            logger.debug("Skipping short/uninformative title: %r", title)
            continue

        logger.debug("Event: %s | title: %s", event_id, title)

        # Load phrases for this specific event's markets (needed by all paths below)
        event_phrases = _load_event_phrases(event_ticker)
        if not event_phrases:
            logger.debug("No active phrases found for event_ticker=%s", event_ticker)
            continue

        matched: list[tuple[str, float, str]] = []

        # ── Path A: Structured event types (SCOTUS, hearings, Fed) ──────────
        # These use ALL phrases in the market with blanket floors + boosts,
        # since Kalshi curates phrases specifically for each event.
        scotus_certs = _extract_scotus_certainties(title, event_phrases)
        if scotus_certs:
            matched = scotus_certs

        elif re.search(r"(?i:committee|subcommittee|hearing|testimony\s+before)", title):
            hearing_certs = _extract_hearing_certainties(title, event_phrases)
            if hearing_certs:
                matched = hearing_certs

        elif re.search(r"(?i:federal\s+reserve|powell|fomc\b)", title):
            fed_certs = _extract_fed_certainties(title, event_phrases)
            if fed_certs:
                matched = fed_certs

        elif re.search(
            r"(?i:prime\s*minister.s\s*question|pmq|minister.s\s*questions?\s*(?:\(|uk|parliament))",
            title,
        ):
            pmq_certs = _extract_pmq_certainties(title, event_phrases)
            if pmq_certs:
                matched = pmq_certs

        # ── Path B: Title-entity matching (political speeches, signings) ─────
        if not matched:
            raw_certainties = _extract_certainties_from_title(title)
            if not raw_certainties:
                logger.debug("No certainties extracted from: %r", title)
                continue

            for entity, p_val, label in raw_certainties:
                for phrase_lower in event_phrases:
                    if (entity == phrase_lower
                            or entity in phrase_lower
                            or phrase_lower in entity):
                        matched.append((phrase_lower, p_val, label))
                        break

        if not matched:
            logger.debug("No phrase matches for event=%s title=%r", event_id, title[:60])
            continue

        logger.info("Event: %s", event_id)
        logger.info("  Title: %s", title[:80])
        logger.info("  Certainties matched: %d", len(matched))
        for phrase, p_val, label in matched:
            logger.info("    %-30s  p=%.2f  [%s]", phrase, p_val, label)

        # Find or skip signal file
        sf = _signal_file_for_event(event_id)
        if sf is None:
            logger.warning("  No signal file for %s — cannot inject (run analyze_event.py first)", event_id)
            continue

        recs = _inject_certainties(sf, matched, dry_run)
        total_injections += len(recs)

    action = "[DRY RUN] Would have injected" if dry_run else "Total injections:"
    logger.info("%s %d certainties across %d events.", action, total_injections, len(events))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--event-id", help="Process only this event_id")
    args = ap.parse_args()
    run(event_id_filter=args.event_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
