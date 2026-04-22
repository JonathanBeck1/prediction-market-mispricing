"""Window-market resolved state tracker.

For monthly and weekly window markets (KXTRUMPSAYMONTH, KXTRUMPSAYNICKNAME,
KXTRUMPSAY weekly, KXLEAVITTMENTION monthly, etc.), tracks which phrases have
ALREADY settled inside the current window so the scoring engine can:

  1. Clamp p_literal → 0.95 if the phrase already settled YES (stale cache case)
  2. Clamp p_literal → 0.04 if the phrase already settled NO (it ended without saying it)
  3. Provide a "window pace" signal — how many phrases/day are resolving so far, vs
     the historical pace — to slightly boost/dampen all remaining open phrases.

Data sources (in priority order):
  - data/live_settlements.json  (12-hour lookback, real-time)
  - data/kalshi_outcomes.json   (full history, refreshed daily)

The WindowState for a given event_ticker tells you:
  - yes_phrases: set of phrases already settled YES in this window
  - no_phrases:  set of phrases already settled NO in this window
  - pace:        phrases resolved per day so far in the window
  - days_remaining: calendar days left in the window

Usage:
    wsc = WindowStateCache.from_cache()
    state = wsc.get_state("KXTRUMPSAYMONTH-26APR01")
    if state:
        if state.is_yes("tds"):
            # already settled YES — clamp
        pace_boost = state.pace_signal()  # float in [-0.03, +0.03]
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

OUTCOMES_CACHE = Path("data/kalshi_outcomes.json")
SETTLEMENTS_CACHE = Path("data/live_settlements.json")

# Series that use rolling windows (monthly, weekly, nickname windows).
# These are the only ones where "already settled in this window" is meaningful.
WINDOW_SERIES: frozenset[str] = frozenset({
    "KXTRUMPSAYMONTH",
    "KXTRUMPSAYNICKNAME",
    "KXTRUMPSAY",        # weekly
    "KXTRUMPSAYEP",      # weekly EP
    "KXLEAVITTMENTION",  # monthly window
    "KXSECPRESSMENTION", # monthly window
    "KXMAMDANIMENTION",  # weekly window
    "KXTRUMPLATE",
    "KXLEAVITTLATE",
})

# p_literal clamp values when a phrase is already settled in the same window
P_WINDOW_SETTLED_YES = 0.95  # already resolved YES → nearly certain YES
P_WINDOW_SETTLED_NO  = 0.04  # already resolved NO  → nearly certain NO

# Max absolute boost/dampen from pace signal
_PACE_MAX = 0.03


@dataclass(frozen=True)
class WindowState:
    event_ticker: str
    series_ticker: str
    yes_phrases: frozenset[str]        # already settled YES (lowercase)
    no_phrases: frozenset[str]         # already settled NO  (lowercase)
    settled_count: int                 # total settled in this window
    days_elapsed: float                # days since window opened
    days_total: float                  # total window duration (days)
    historical_avg_rate: float = 0.45  # historical YES rate for this series

    @property
    def days_remaining(self) -> float:
        return max(0.0, self.days_total - self.days_elapsed)

    @property
    def fraction_elapsed(self) -> float:
        if self.days_total <= 0:
            return 1.0
        return min(1.0, self.days_elapsed / self.days_total)

    def is_yes(self, phrase: str) -> bool:
        return _norm(phrase) in self.yes_phrases

    def is_no(self, phrase: str) -> bool:
        return _norm(phrase) in self.no_phrases

    def is_settled(self, phrase: str) -> bool:
        return self.is_yes(phrase) or self.is_no(phrase)

    def pace_signal(self) -> float:
        """Return a small additive p_literal adjustment based on window pace.

        If phrases are resolving faster than historical average → small positive.
        If resolving slower → small negative.  Capped at ±_PACE_MAX.
        """
        if self.days_elapsed <= 0 or self.days_total <= 0:
            return 0.0
        # Expected settled count at this point if at historical pace
        expected = self.historical_avg_rate * self.fraction_elapsed * 30  # assume ~30 markets/window
        actual = self.settled_count
        if expected <= 0:
            return 0.0
        ratio = actual / expected
        # ratio > 1 → faster than expected → small boost
        signal = (ratio - 1.0) * 0.02  # 2% boost per doubling
        return max(-_PACE_MAX, min(_PACE_MAX, signal))


@dataclass
class WindowStateCache:
    """In-memory cache of window states for all active window-series events."""
    _states: dict[str, WindowState] = field(default_factory=dict, repr=False)
    _loaded_at: datetime | None = None

    @classmethod
    def from_cache(
        cls,
        outcomes_path: Path = OUTCOMES_CACHE,
        settlements_path: Path = SETTLEMENTS_CACHE,
    ) -> "WindowStateCache":
        obj = cls()
        obj._build(outcomes_path, settlements_path)
        obj._loaded_at = datetime.now(tz=timezone.utc)
        return obj

    def _build(self, outcomes_path: Path, settlements_path: Path) -> None:
        """Build window states from outcomes + live settlements."""
        now_utc = datetime.now(tz=timezone.utc)

        # ── 1. Load outcomes history ───────────────────────────────────────────
        # Collect (event_ticker, phrase, result, close_time) for window series
        by_event: dict[str, dict] = {}  # event_ticker → {yes: set, no: set, close_time: str, series: str}

        if outcomes_path.exists():
            try:
                data = json.loads(outcomes_path.read_text(encoding="utf-8"))
                for m in data.get("markets", []):
                    series = str(m.get("series_ticker", "")).upper().strip()
                    if series not in WINDOW_SERIES:
                        continue
                    result = str(m.get("result", "")).lower()
                    if result not in ("yes", "no"):
                        continue
                    et = str(m.get("event_ticker", "")).strip()
                    if not et:
                        continue
                    phrase = _norm(str(m.get("primary_phrase", "")))
                    variants = [_norm(v) for v in m.get("phrase_variants", []) if v]
                    close_time = str(m.get("close_time", ""))
                    if et not in by_event:
                        by_event[et] = {
                            "yes": set(), "no": set(),
                            "close_time": close_time,
                            "series": series,
                        }
                    bucket = by_event[et][result]
                    bucket.add(phrase)
                    bucket.update(variants)
                    if close_time > by_event[et]["close_time"]:
                        by_event[et]["close_time"] = close_time
            except Exception as exc:
                logger.warning("WindowStateCache: could not load outcomes: %s", exc)

        # ── 2. Overlay live settlements (higher recency, overrides outcomes) ───
        if settlements_path.exists():
            try:
                sdata = json.loads(settlements_path.read_text(encoding="utf-8"))
                for et, ev in sdata.get("events", {}).items():
                    series = _series_from_event_ticker(et)
                    if series not in WINDOW_SERIES:
                        continue
                    if et not in by_event:
                        by_event[et] = {
                            "yes": set(), "no": set(),
                            "close_time": ev.get("latest_settlement", ""),
                            "series": series,
                        }
                    for p in ev.get("yes", []):
                        by_event[et]["yes"].add(_norm(p))
                    for p in ev.get("no", []):
                        by_event[et]["no"].add(_norm(p))
            except Exception as exc:
                logger.warning("WindowStateCache: could not load live settlements: %s", exc)

        # ── 3. Build WindowState per event_ticker ─────────────────────────────
        for et, ev in by_event.items():
            series = ev["series"]
            window_days = _window_days_for_series(series)
            close_time_str = ev["close_time"]
            window_end = _parse_dt(close_time_str)

            if window_end is not None:
                window_start = window_end - _timedelta_days(window_days)
                days_elapsed = max(0.0, (now_utc - window_start).total_seconds() / 86400)
            else:
                days_elapsed = window_days / 2  # fallback

            yes_set = frozenset(ev["yes"])
            no_set = frozenset(ev["no"])
            state = WindowState(
                event_ticker=et,
                series_ticker=series,
                yes_phrases=yes_set,
                no_phrases=no_set,
                settled_count=len(yes_set) + len(no_set),
                days_elapsed=days_elapsed,
                days_total=float(window_days),
                historical_avg_rate=_historical_yes_rate(series),
            )
            self._states[et] = state

        logger.debug(
            "WindowStateCache: loaded %d window events (%d with YES settlements)",
            len(self._states),
            sum(1 for s in self._states.values() if s.yes_phrases),
        )

    def get_state(self, event_ticker: str) -> WindowState | None:
        """Return the WindowState for an event_ticker, or None if not a window market."""
        state = self._states.get(event_ticker)
        if state is not None:
            return state
        # Prefix match — e.g. event_ticker KXTRUMPSAY-26MAR16 may match
        # a settlement stored under KXTRUMPSAY-26MAR16 exactly
        for key, val in self._states.items():
            if key.startswith(event_ticker) or event_ticker.startswith(key):
                return val
        return None

    def check_phrase(
        self,
        event_ticker: str,
        phrase: str,
    ) -> tuple[bool, bool]:
        """Return (settled_yes, settled_no) for a phrase in the given event window.

        Both False means "no settlement data for this phrase in this window".
        """
        state = self.get_state(event_ticker)
        if state is None:
            return False, False
        return state.is_yes(phrase), state.is_no(phrase)

    def pace_signal(self, event_ticker: str) -> float:
        """Return a pace-based p_literal adjustment for remaining open phrases."""
        state = self.get_state(event_ticker)
        if state is None:
            return 0.0
        return state.pace_signal()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(phrase: str) -> str:
    return phrase.lower().strip()


def _series_from_event_ticker(et: str) -> str:
    """Extract series ticker from event_ticker. e.g. KXTRUMPSAYMONTH-26APR01 → KXTRUMPSAYMONTH."""
    return et.split("-")[0].upper()


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        clean = s[:19].replace("T", " ").replace("Z", "")
        return datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _timedelta_days(days: float):
    from datetime import timedelta
    return timedelta(days=days)


def _window_days_for_series(series: str) -> float:
    """Return the approximate window length in days for a series."""
    s = series.upper()
    if "MONTH" in s:
        return 31.0
    if "NICKNAME" in s:
        return 31.0
    if "LATE" in s:
        return 7.0
    # Weekly say/mention series
    return 7.0


def _historical_yes_rate(series: str) -> float:
    """Rough historical YES rate for pace baseline."""
    s = series.upper()
    if "MONTH" in s:
        return 0.58   # Trump monthly ~58% YES historically
    if "NICKNAME" in s:
        return 0.42
    return 0.50       # weekly default
