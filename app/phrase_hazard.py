"""Phrase-specific empirical hazard rates for live event scoring.

Replaces the static exponential time-decay formula with empirical hazard
functions derived from corpus transcript analysis. Each phrase has its own
h(t) = probability density of being said at time t given it hasn't been said yet.

This accounts for phrases that are typically mentioned early vs late in speeches.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_PATH = Path("data/phrase_hazard_rates.json")
_RELOAD_SEC = 3600.0  # Reload every hour (hazard rates change slowly)

# Fallback hazard rates (uniform distribution, similar to current static decay)
_FALLBACK_HAZARD = [0.12] * 10


class PhraseHazardCache:
    """Provides empirical hazard rates for phrase timing in live events."""
    
    def __init__(self, path: Path = _DATA_PATH) -> None:
        self._path = path
        self._hazard_data: dict[str, dict] = {}
        self._loaded_at = 0.0
        self._maybe_reload()
    
    def _maybe_reload(self) -> None:
        """Reload hazard data if stale or empty."""
        now = time.monotonic()
        if now - self._loaded_at < _RELOAD_SEC and self._hazard_data:
            return
            
        if not self._path.exists():
            logger.warning("Hazard rates file not found: %s", self._path)
            return
            
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._hazard_data = data.get("phrases", {})
            self._loaded_at = now
            logger.info("Loaded hazard rates: %d phrases", len(self._hazard_data))
        except Exception as exc:
            logger.warning("Failed to load hazard rates: %s", exc)
    
    def get_hazard_rates(self, speaker: str, event_type: str, phrase: str) -> list[float]:
        """Return 10-bucket hazard rates for this phrase, or fallback if not found."""
        self._maybe_reload()
        
        # Try exact match first
        key = f"{speaker}.{event_type}.{phrase.lower()}"
        if key in self._hazard_data:
            return self._hazard_data[key].get("buckets", _FALLBACK_HAZARD)
        
        # Try general fallback for speaker
        general_key = f"{speaker}.general.{phrase.lower()}"
        if general_key in self._hazard_data:
            return self._hazard_data[general_key].get("buckets", _FALLBACK_HAZARD)
        
        # Use fallback uniform hazard
        return _FALLBACK_HAZARD
    
    def compute_survival_probability(self, speaker: str, event_type: str, phrase: str, 
                                   time_fraction: float) -> float:
        """Compute P(phrase not said by time_fraction) using hazard function.
        
        Args:
            time_fraction: 0.0 to 1.0 (fraction of event elapsed)
            
        Returns:
            Probability that phrase has NOT been said by this time
        """
        if time_fraction <= 0:
            return 1.0
        if time_fraction >= 1:
            return 0.05  # Min decay floor like current system
            
        hazard_rates = self.get_hazard_rates(speaker, event_type, phrase)
        bucket_size = 1.0 / len(hazard_rates)
        
        # Compute cumulative survival probability
        survival = 1.0
        for i in range(len(hazard_rates)):
            bucket_start = i * bucket_size
            bucket_end = (i + 1) * bucket_size
            
            if time_fraction <= bucket_start:
                break
                
            # Fraction of this bucket that has elapsed
            if time_fraction >= bucket_end:
                bucket_frac = 1.0  # Entire bucket elapsed
            else:
                bucket_frac = (time_fraction - bucket_start) / bucket_size
            
            # Apply hazard for this bucket fraction
            # survival *= exp(-hazard * bucket_frac * bucket_size)
            # Approximation: survival *= (1 - hazard * bucket_frac * bucket_size)
            hazard_effect = hazard_rates[i] * bucket_frac * bucket_size
            survival *= max(0.01, 1.0 - hazard_effect)
        
        # Survival probability = 1 - cumulative_mention_prob
        # We want decay factor (analog to current time_decay), which is survival prob
        return max(0.05, survival)  # Same floor as current MIN_DECAY
    
    def get_phrase_count(self) -> int:
        """Return number of phrases with hazard rates."""
        self._maybe_reload()
        return len(self._hazard_data)