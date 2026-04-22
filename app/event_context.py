"""Event-topic context analysis.

Parses event titles to determine the topic, then scores each phrase's
relevance to that topic.  Phrases unrelated to the event topic get a
dampening multiplier so the model doesn't overweight generic historical
rates for topic-specific events.

Example: "Saving College Sports Roundtable" → topic "sports"
  - "tariff" relevance = 0.15  (unlikely at a sports event)
  - "NIL"    relevance = 1.40  (directly on-topic)
  - "iran"   relevance = 0.50  (Trump goes off-script sometimes)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

KALSHI_CACHE = Path("data/kalshi_markets.json")

TOPIC_KEYWORDS: dict[str, list[str]] = {
    "sports": [
        "sport", "college sport", "roundtable", "ncaa", "football", "basketball",
        "soccer", "baseball", "hockey", "golf", "olympic", "athlete", "nil",
        "transfer", "scholarship", "heisman", "playoff", "championship",
        "inter miami", "match", "game", "team",
    ],
    "economy": [
        "economic", "economy", "trade", "tariff", "market", "stock",
        "inflation", "jobs", "gdp", "treasury", "fiscal", "budget",
        "debt ceiling", "recession",
    ],
    "foreign_policy": [
        "nato", "ukraine", "russia", "china", "iran", "israel", "gaza",
        "north korea", "summit", "diplomatic", "treaty", "nuclear",
        "sanctions", "foreign",
    ],
    "immigration": [
        "border", "immigration", "migrant", "ice", "deportation",
        "asylum", "illegal", "wall",
    ],
    "tech": [
        "crypto", "bitcoin", "ai", "tech", "silicon valley",
        "social media", "tiktok",
    ],
    "rally": [
        "rally", "maga", "campaign", "supporter",
    ],
}

PHRASE_TOPIC_MAP: dict[str, list[str]] = {
    "sports": [
        "nil", "nfl", "nba", "heisman", "playoff", "kickoff", "soccer",
        "football", "golf", "golfing", "hockey", "olympic", "olympics",
        "transfer", "freshman", "scholarship", "graduate", "graduated",
        "graduating", "world cup", "ufc", "ballroom", "beckham", "ronaldo",
        "futbol", "pelé", "gianni", "infantino", "roll tide",
    ],
    "economy": [
        "tariff", "trillion", "stock market", "economy", "economic",
        "stimulus", "250", "credit", "budget", "deficit",
    ],
    "foreign_policy": [
        "iran", "israel", "ukraine", "china", "nuclear", "hormuz",
        "greenland", "moscow", "nine war", "make iran great again",
        "nato", "rocket man",
    ],
    "immigration": [
        "border", "ice", "illegal alien", "national guard", "terrorist",
        "narcoterrorist",
    ],
    "politics": [
        "biden", "democrat", "pelosi", "epstein", "fbi", "supreme court",
        "election", "rigged election", "stolen election", "congress",
        "congressional", "radical left", "shutdown", "shut down",
        "third term", "newscum", "tds", "trump derangement syndrome",
        "dei", "woke", "communist", "communism", "comrade kamala",
        "crazy bernie", "crying chuck", "cryin chuck", "pocahontas",
        "tampon tim", "fat slob", "piggy", "little communist",
        "low energy", "slopadopolous", "thug", "whack job", "wack job",
        "biden crime family", "autopen", "auto pen",
    ],
    "trump_catchphrases": [
        "hottest", "predict", "prediction", "drill baby drill",
        "who are you with", "where are you from", "cookie",
        "discombobulator", "mog", "mogged", "mogging", "golden dome",
        "space force", "gulf of america", "fat shot", "ozempic",
        "epstein island", "nobel", "marijuana", "weed", "cannabis",
        "ufo", "uap", "crypto", "bitcoin", "turning point",
        "autism", "transgender", "afford", "affordable", "affordability",
    ],
}

ON_TOPIC_BOOST = 1.20
OFF_TOPIC_DAMPEN = 0.20
WEAK_RELATION_FACTOR = 0.50
WILDCARD_FLOOR = 0.40

# ── Event format classifier ────────────────────────────────────────────────────
# Maps event format → human-readable label and LLM guidance note.
EVENT_FORMATS: dict[str, dict] = {
    "diplomatic": {
        "label": "Diplomatic Meeting / State Dinner",
        "note": (
            "Formal setting with foreign heads of state or senior officials. "
            "Trump tends to stay on-topic for bilateral issues. "
            "Partisan insults, domestic political attacks, and pop-culture references "
            "are unlikely. Trade, security alliances, and foreign policy dominate."
        ),
    },
    "rally": {
        "label": "Political Rally / Campaign Event",
        "note": (
            "Unscripted, high-energy campaign-style event. "
            "Trump goes off-script frequently. Partisan nicknames, domestic politics, "
            "immigration, and culture-war phrases have elevated probability. "
            "All phrase categories are plausible."
        ),
    },
    "presser": {
        "label": "Press Conference / Media Availability",
        "note": (
            "Reporter Q&A format. Topics driven by current news cycle. "
            "Trump responds to reporter questions, so off-script phrases are common. "
            "Breaking news topics have high probability."
        ),
    },
    "briefing": {
        "label": "White House Press Briefing",
        "note": (
            "Spokesperson-led briefing, not Trump speaking directly. "
            "Formal policy language dominates. Personal nicknames and catchphrases "
            "are suppressed. Policy/legislative topics are central."
        ),
    },
    "testimony": {
        "label": "Congressional Testimony / Senate Hearing",
        "note": (
            "Structured Q&A before Congress. Speaker is constrained by committee topic. "
            "Off-topic phrases are unlikely. Technical policy language dominates."
        ),
    },
    "earnings": {
        "label": "Earnings Call",
        "note": (
            "Corporate earnings call. Speaker is a company executive. "
            "Political phrases, Trump-related topics, and partisan language are extremely unlikely. "
            "Financial metrics, guidance, and industry-specific language dominate."
        ),
    },
    "signing": {
        "label": "Bill / Executive Order Signing",
        "note": (
            "Short ceremonial event focused on the specific legislation being signed. "
            "Remarks are scripted and on-topic. Tangential phrases have low probability."
        ),
    },
    "interview": {
        "label": "Media Interview",
        "note": (
            "One-on-one interview format. Topics driven by interviewer's questions. "
            "Trump goes off-script moderately. Hot news topics elevated."
        ),
    },
    "address": {
        "label": "Formal Address (SOTU / Rose Garden)",
        "note": (
            "Scripted formal address. Teleprompter likely. Off-script insults and "
            "catchphrases are suppressed relative to rallies. Policy language elevated."
        ),
    },
    "announcement": {
        "label": "Policy Announcement / Rose Garden Event",
        "note": (
            "Prepared remarks focused on a specific policy or nomination. "
            "Topics constrained to the announcement subject."
        ),
    },
    "general": {
        "label": "General / Unknown Format",
        "note": (
            "Format unclear. Apply standard historical base rates without "
            "strong format-specific adjustments."
        ),
    },
}

_FORMAT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("diplomatic", [
        "prime minister", "premier", "chancellor", "president of",
        "bilateral", "state dinner", "dinner with", "luncheon with",
        "meeting with the", "summit", "king", "queen", "prime min",
        "foreign minister", "head of state", "delegation",
    ]),
    ("rally", [
        "rally", "maga", "campaign",
        # "save america" / "make america" deliberately removed — these phrases appear
        # in market titles for Leavitt/WH briefings ("Will she say Save America Act")
        # and should NOT trigger rally classification. Rallies are identified only by
        # the event NAME containing "rally" or "maga".
        "town hall", "townhall",
    ]),
    ("testimony", [
        "hearing", "senate", "congress", "house committee", "senate committee",
        "confirmation hearing", "testimony", "subcommittee",
    ]),
    ("earnings", [
        "earnings", "quarterly results", "q1", "q2", "q3", "q4",
        "investor day", "analyst day",
    ]),
    ("signing", [
        "signing", "executive order", "sign into law",
    ]),
    ("presser", [
        "press conference", "press availability", "gaggle", "media",
    ]),
    ("briefing", [
        "briefing", "press briefing", "daily briefing",
    ]),
    ("interview", [
        "interview", "fox news", "cnn", "msnbc", "nbc", "abc news",
    ]),
    ("address", [
        "state of the union", "rose garden", "oval office address",
        "joint session", "inaugural",
    ]),
    ("announcement", [
        "announcement", "remarks on", "statement on", "unveiling",
    ]),
]


def classify_event_format(event_title: str, event_context: str = "") -> str:
    """Return the most likely event format string for the given title/context.

    Checks title and context against keyword lists, returns the best matching
    format key. Falls back to 'general' if nothing matches.
    """
    combined = (event_title + " " + event_context).lower()
    for fmt, keywords in _FORMAT_KEYWORDS:
        if any(kw in combined for kw in keywords):
            return fmt
    return "general"


def event_format_label(fmt: str) -> str:
    """Return human-readable label for an event format key."""
    return EVENT_FORMATS.get(fmt, EVENT_FORMATS["general"])["label"]


def event_format_note(fmt: str) -> str:
    """Return LLM guidance note for an event format key."""
    return EVENT_FORMATS.get(fmt, EVENT_FORMATS["general"])["note"]


def _detect_topic(event_title: str) -> str | None:
    """Return the primary topic for an event title, or None if generic."""
    title_lower = event_title.lower()

    best_topic: str | None = None
    best_score = 0

    for topic, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in title_lower)
        if score > best_score:
            best_score = score
            best_topic = topic

    if best_score >= 1:
        return best_topic
    return None


def _phrase_topics(phrase: str) -> list[str]:
    """Return list of topic categories a phrase belongs to."""
    phrase_lower = phrase.lower()
    topics = []
    for topic, phrases in PHRASE_TOPIC_MAP.items():
        if phrase_lower in phrases:
            topics.append(topic)
    return topics


def compute_topic_relevance(event_title: str, phrase: str) -> float:
    """Return a multiplier [OFF_TOPIC_DAMPEN .. ON_TOPIC_BOOST] for how
    relevant *phrase* is to the event described by *event_title*.

    Returns 1.0 for generic/unknown events (no adjustment).
    """
    topic = _detect_topic(event_title)
    if topic is None:
        return 1.0

    phrase_topics = _phrase_topics(phrase)
    if not phrase_topics:
        return WILDCARD_FLOOR

    if topic in phrase_topics:
        return ON_TOPIC_BOOST

    topic_families = {
        "sports": {"sports"},
        "economy": {"economy", "politics"},
        "foreign_policy": {"foreign_policy", "politics"},
        "immigration": {"immigration", "politics"},
        "tech": {"tech", "economy"},
        "rally": {"politics", "trump_catchphrases", "economy", "foreign_policy", "immigration"},
    }

    related = topic_families.get(topic, set())
    if any(pt in related for pt in phrase_topics):
        return WEAK_RELATION_FACTOR

    return OFF_TOPIC_DAMPEN


def load_event_titles() -> dict[str, str]:
    """Load event_ticker → event_title from the Kalshi market cache."""
    if not KALSHI_CACHE.exists():
        return {}
    data = json.loads(KALSHI_CACHE.read_text(encoding="utf-8"))
    titles: dict[str, str] = {}
    # Support both {"markets": [...]} dict format and legacy flat-list format.
    market_list: list = data.get("markets", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    for m in market_list:
        et = m.get("event_ticker", "")
        if et and et not in titles:
            title = m.get("title", "") or m.get("event_title", "")
            preamble_match = re.match(
                r"What will .+? say during (.+?)\?", title
            )
            if preamble_match:
                titles[et] = preamble_match.group(1)
            else:
                titles[et] = title
    return titles
