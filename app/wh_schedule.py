"""White House schedule cache.

Loads data/wh_schedule.json (written by scripts/fetch_wh_schedule.py every 30 min)
and provides two key services to the scoring engine:

  1. event_type override — if WH schedule confirms an upcoming signing / remarks /
     briefing within `lookahead_hours`, override the scoring event_type instead of
     falling back to "general".

  2. topic keywords — titles of matching WH events often reveal what topics will
     come up, giving phrase-level boosting signals.

Usage:
    cache = WHScheduleCache.load()
    ev_type = cache.resolve_event_type("signing", lookahead_hours=24)
    keywords = cache.upcoming_keywords(hours=6)
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_PATH = Path("data/wh_schedule.json")
_MAX_AGE_SEC = 1800  # reload if file is >30 min old (matches fetch interval)

# event_types where we trust the WH confirmation — "general" is too vague to keep
_AUTHORITATIVE_TYPES = {"signing", "remarks", "briefing", "address", "interview", "rally", "summit", "visit", "announcement"}


@dataclass
class WHEvent:
    title: str
    url: str
    published_at: datetime | None
    event_type: str
    categories: list[str]
    keywords: list[str]
    source: str

    def age_hours(self, now: datetime) -> float:
        if self.published_at is None:
            return 999.0
        return (now - self.published_at).total_seconds() / 3600


@dataclass
class WHScheduleCache:
    events: list[WHEvent] = field(default_factory=list)
    generated_at: datetime | None = None
    _loaded_at: float = field(default_factory=time.monotonic, repr=False, compare=False)

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path = _DATA_PATH) -> "WHScheduleCache":
        if not path.exists():
            logger.debug("wh_schedule.json not found — WH schedule unavailable")
            return cls()
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("WHScheduleCache: failed to load %s: %s", path, exc)
            return cls()

        events = []
        for ev in raw.get("events", []):
            pub_dt = _parse_iso(ev.get("published_at", ""))
            events.append(WHEvent(
                title        = ev.get("title", ""),
                url          = ev.get("url", ""),
                published_at = pub_dt,
                event_type   = ev.get("event_type", "general"),
                categories   = ev.get("categories", []),
                keywords     = ev.get("keywords", []),
                source       = ev.get("source", ""),
            ))

        gen_dt = _parse_iso(raw.get("generated_at", ""))
        logger.debug("WHScheduleCache: loaded %d events from %s", len(events), path)
        return cls(events=events, generated_at=gen_dt)

    def is_stale(self) -> bool:
        return (time.monotonic() - self._loaded_at) > _MAX_AGE_SEC

    # ── Public API ────────────────────────────────────────────────────────────

    def resolve_event_type(
        self,
        current_event_type: str,
        *,
        lookahead_hours: float = 24.0,
        now: datetime | None = None,
    ) -> str:
        """Return WH-confirmed event_type if available, else `current_event_type`.

        Only overrides when the schedule has a high-confidence authoritative type
        (signing/remarks/briefing/etc.) published within the lookback window.
        Does NOT override if the caller already has a specific context.
        """
        if current_event_type in _AUTHORITATIVE_TYPES:
            # Caller already knows the context — trust it.
            return current_event_type

        best = self._most_recent_authoritative(lookahead_hours=lookahead_hours, now=now)
        if best is not None:
            logger.debug(
                "WHScheduleCache: overriding event_type '%s' → '%s' from '%s'",
                current_event_type, best.event_type, best.title[:50],
            )
            return best.event_type
        return current_event_type

    def upcoming_keywords(
        self,
        hours: float = 6.0,
        now: datetime | None = None,
    ) -> list[str]:
        """Keywords extracted from recent WH events (within last `hours`)."""
        _now = now or datetime.now(timezone.utc)
        cutoff = _now - timedelta(hours=hours)
        keywords: list[str] = []
        for ev in self.events:
            if ev.published_at and ev.published_at >= cutoff:
                keywords.extend(ev.keywords)
        return list(dict.fromkeys(keywords))  # deduplicated, order-preserving

    def recent_events(
        self,
        hours: float = 24.0,
        now: datetime | None = None,
    ) -> list[WHEvent]:
        """All events within the past `hours`."""
        _now = now or datetime.now(timezone.utc)
        cutoff = _now - timedelta(hours=hours)
        return [ev for ev in self.events if ev.published_at and ev.published_at >= cutoff]

    def most_recent_signing(self, hours: float = 24.0, now: datetime | None = None) -> WHEvent | None:
        """Return most recent signing event within `hours`, or None."""
        for ev in self.recent_events(hours=hours, now=now):
            if ev.event_type == "signing":
                return ev
        return None

    def has_recent_event(self, event_type: str, hours: float = 6.0, now: datetime | None = None) -> bool:
        """True if there's a recent WH event of the given type."""
        for ev in self.recent_events(hours=hours, now=now):
            if ev.event_type == event_type:
                return True
        return False

    def keyword_boost(self, phrase: str, hours: float = 6.0, now: datetime | None = None) -> float:
        """Return a p_literal boost for `phrase` if it appears in recent WH event keywords.

        Returns:
            +0.08  if the phrase appears in a signing/remarks event title keyword
            +0.04  if the phrase appears in any recent WH event keyword
            0.0    otherwise
        """
        phrase_lower = phrase.lower()
        for ev in self.recent_events(hours=hours, now=now):
            if phrase_lower in ev.keywords:
                boost = 0.08 if ev.event_type in ("signing", "remarks") else 0.04
                return boost
            # Also check the title directly for partial matches
            if phrase_lower in ev.title.lower():
                return 0.04
        return 0.0

    # ── Private ───────────────────────────────────────────────────────────────

    def _most_recent_authoritative(
        self,
        lookahead_hours: float,
        now: datetime | None,
    ) -> WHEvent | None:
        """Find the most recent event with an authoritative event_type."""
        _now = now or datetime.now(timezone.utc)
        cutoff = _now - timedelta(hours=lookahead_hours)
        for ev in self.events:  # already sorted newest-first
            if ev.published_at and ev.published_at >= cutoff:
                if ev.event_type in _AUTHORITATIVE_TYPES:
                    return ev
        return None


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except ValueError:
        return None
