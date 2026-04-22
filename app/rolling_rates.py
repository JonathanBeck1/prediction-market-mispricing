"""Rolling N-speech hit rate signal.

Provides per-(speaker, phrase) YES rates computed over the last 3, 5, and 10
single-event speeches from data/rolling_hit_rates.json.

Why this is useful over the 90-day recency-weighted historical rate:
  * Historical rates (even recency-weighted) react slowly to sharp breaks.
  * "tariff" might show 58% historically but 100% in last 5 speeches.
  * "golden dome" might show 38% historically but 0% in last 5 speeches.
  * Rolling rates catch these regime changes 4-6 weeks faster.

Blend formula (in scoring.py):
  p_blended = (1 - ROLLING_WEIGHT) * p_historical + ROLLING_WEIGHT * p_rolling

  where ROLLING_WEIGHT is determined by observation count:
    n3_obs >= 3  → weight 0.20  (mild adjustment)
    n5_obs >= 5  → weight 0.30  (moderate adjustment, preferred window)
    n10_obs >= 8 → weight 0.25  (smoother, less reactive)

  We prefer n5 (5-speech window) as the primary signal — reactive enough to
  catch breaks but stable enough to avoid single-speech noise.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ROLLING_RATES_PATH = Path("data/rolling_hit_rates.json")

# Minimum observations before we apply a rolling rate blend
MIN_OBS_N3  = 3
MIN_OBS_N5  = 5
MIN_OBS_N10 = 8

# Blend weights: how much to trust rolling rate vs historical
# These are deliberately conservative — rolling is a nudge, not a replacement.
WEIGHT_N5  = 0.30   # preferred window
WEIGHT_N3  = 0.20   # few observations — lighter touch
WEIGHT_N10 = 0.25   # smoother long window

# Delta threshold: only blend if rolling rate differs from historical by this much
# Prevents jitter when rolling ≈ historical
MIN_DELTA = 0.05


@dataclass(frozen=True)
class RollingRate:
    """Rolling rates for a single (speaker, phrase) pair."""
    speaker:    str
    phrase:     str
    n3:         float | None   # last-3-speech YES rate
    n3_obs:     int
    n5:         float | None   # last-5-speech YES rate (primary signal)
    n5_obs:     int
    n10:        float | None   # last-10-speech YES rate
    n10_obs:    int
    last_seen:  str            # YYYY-MM-DD of most recent observation
    total_obs:  int

    def best_rate(self) -> tuple[float, float, str] | None:
        """Return (rate, weight, window_tag) for the most reliable window.

        Prefers n5 (5-speech window). Falls back to n3 or n10.
        Returns None if no window has enough observations.
        """
        if self.n5 is not None and self.n5_obs >= MIN_OBS_N5:
            return self.n5, WEIGHT_N5, "ROLLING_N5"
        if self.n10 is not None and self.n10_obs >= MIN_OBS_N10:
            return self.n10, WEIGHT_N10, "ROLLING_N10"
        if self.n3 is not None and self.n3_obs >= MIN_OBS_N3:
            return self.n3, WEIGHT_N3, "ROLLING_N3"
        return None

    def is_stale(self, max_age_days: int = 21) -> bool:
        """True if the most recent observation is older than max_age_days.

        Stale rolling rates capture outdated speech patterns and can degrade
        model accuracy. When stale, fall back to historical base rates.
        """
        if not self.last_seen:
            return True
        try:
            from datetime import date
            last_date = date.fromisoformat(self.last_seen)
            age_days = (date.today() - last_date).days
            return age_days > max_age_days
        except Exception:
            return True

    def blend(self, p_historical: float, max_age_days: int = 21) -> tuple[float, str] | None:
        """Blend historical rate with rolling rate.

        Returns (blended_p, reason_tag) or None if rolling rate has no signal,
        the delta is too small to matter, or the data is stale.
        """
        # Staleness guard: don't blend with outdated rolling data
        if self.is_stale(max_age_days):
            return None  # Fall back to historical base rate

        result = self.best_rate()
        if result is None:
            return None
        rolling_rate, weight, tag = result
        delta = abs(rolling_rate - p_historical)
        if delta < MIN_DELTA:
            return None   # rolling ≈ historical — no meaningful adjustment
        blended = (1.0 - weight) * p_historical + weight * rolling_rate
        return round(blended, 4), tag


class RollingRatesCache:
    """In-memory lookup for rolling hit rates, loaded from JSON."""

    def __init__(self, rates: dict[str, dict[str, dict]]) -> None:
        self._rates = rates  # {speaker: {phrase: {...}}}

    @classmethod
    def from_cache(cls, path: Path = ROLLING_RATES_PATH) -> "RollingRatesCache":
        if not path.exists():
            logger.debug("rolling_hit_rates.json not found — rolling rates disabled")
            return cls({})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rates = data.get("rates", {})
            count = sum(len(v) for v in rates.values())
            logger.info("Loaded rolling rates: %d phrases across %d speakers", count, len(rates))
            return cls(rates)
        except Exception as exc:
            logger.warning("Could not load rolling rates: %s", exc)
            return cls({})

    def get(self, speaker: str, phrase: str) -> RollingRate | None:
        """Return RollingRate for (speaker, phrase), or None if not found."""
        speaker_data = self._rates.get(speaker.lower(), {})
        entry = speaker_data.get(phrase.lower().strip())
        if entry is None:
            return None
        return RollingRate(
            speaker=speaker,
            phrase=phrase,
            n3=entry.get("n3"),
            n3_obs=entry.get("n3_obs", 0),
            n5=entry.get("n5"),
            n5_obs=entry.get("n5_obs", 0),
            n10=entry.get("n10"),
            n10_obs=entry.get("n10_obs", 0),
            last_seen=entry.get("last_seen", ""),
            total_obs=entry.get("total_obs", 0),
        )

    def blend(
        self,
        speaker: str,
        phrase: str,
        p_historical: float,
    ) -> tuple[float, str] | None:
        """Convenience: get rolling blend for (speaker, phrase).

        Returns (blended_p, reason_tag) or None if no rolling signal.
        """
        rr = self.get(speaker, phrase)
        if rr is None:
            return None
        return rr.blend(p_historical)

    @property
    def phrase_count(self) -> int:
        return sum(len(v) for v in self._rates.values())
