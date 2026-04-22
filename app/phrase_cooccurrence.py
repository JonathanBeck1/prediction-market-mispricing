"""Phrase co-occurrence signal cache.

Loads data/phrase_cooccurrence.json and provides:
  get_lift(speaker, phrase_a, phrase_b) -> float

When phrase_a has already resolved YES in the current event window, the
scorer uses this lift to boost/suppress phrase_b's base probability.

Multiplier applied to base probability:
  lift > 1  -> boost  (phrase_a said => phrase_b more likely)
  lift < 1  -> suppress (phrase_a said => phrase_b less likely)

Capped to BOOST_CAP / SUPPRESS_CAP to avoid overconfidence.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

_DATA_PATH  = Path("data/phrase_cooccurrence.json")
_RELOAD_SEC = 3600.0  # reload file hourly

BOOST_CAP    = 2.0   # max lift applied as multiplier
SUPPRESS_CAP = 0.5   # min lift applied as multiplier (floor)


class CooccurrenceCache:
    """Thread-safe read-only cache for phrase co-occurrence lift values."""

    def __init__(self, path: Path = _DATA_PATH) -> None:
        self._path         = path
        self._index: dict[tuple[str, str, str], float] = {}
        self._loaded_at: float = 0.0
        self._maybe_reload()

    def _maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._loaded_at < _RELOAD_SEC and self._index:
            return
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            pairs = data.get("pairs", [])
            index: dict[tuple[str, str, str], float] = {}
            for p in pairs:
                speaker = str(p.get("speaker", "")).lower().strip()
                pa      = str(p.get("phrase_a", "")).lower().strip()
                pb      = str(p.get("phrase_b", "")).lower().strip()
                lift    = float(p.get("lift", 1.0))
                if speaker and pa and pb:
                    index[(speaker, pa, pb)] = lift
            self._index     = index
            self._loaded_at = now
            logger.debug("CooccurrenceCache: loaded %d pairs", len(index))
        except Exception as exc:
            logger.warning("CooccurrenceCache: failed to load %s: %s", self._path, exc)

    def get_lift(self, speaker: str, phrase_a: str, phrase_b: str) -> float:
        """Return the lift multiplier for P(B=yes | A=yes).

        Returns 1.0 (neutral) if no data available.
        """
        self._maybe_reload()
        key = (
            speaker.lower().strip(),
            phrase_a.lower().strip(),
            phrase_b.lower().strip(),
        )
        raw_lift = self._index.get(key, 1.0)
        # Cap to avoid overconfidence
        return max(SUPPRESS_CAP, min(BOOST_CAP, raw_lift))

    def get_boost_multiplier(self, speaker: str, phrase_a: str, phrase_b: str) -> float:
        """Return a probability multiplier in (0.5, 2.0].

        Suitable for multiplying base probability before Platt calibration.
        A multiplier of 1.4 means: 'phrase_a being said boosts phrase_b by 40%'.
        """
        return self.get_lift(speaker, phrase_a, phrase_b)

    def get_confirmed_phrases_for_event(
        self, speaker: str, confirmed_yes_phrases: list[str], target_phrase: str
    ) -> tuple[float, list[str]]:
        """Compute the combined co-occurrence multiplier for target_phrase.

        Given a list of phrases already confirmed YES in this event window,
        returns (combined_multiplier, list_of_triggering_phrases).

        Only the STRONGEST single signal is used (no double-counting).
        """
        self._maybe_reload()
        best_lift = 1.0
        best_triggers: list[str] = []

        for confirmed in confirmed_yes_phrases:
            lift = self.get_lift(speaker, confirmed, target_phrase)
            if abs(lift - 1.0) > abs(best_lift - 1.0):
                best_lift = lift
                best_triggers = [confirmed]

        return best_lift, best_triggers
