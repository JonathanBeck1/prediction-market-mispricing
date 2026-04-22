"""Series-specific phrase bias map cache.

Loads data/bias_map.json and provides per-(series, phrase) empirical rates.
Used by ScoringEngine as the most precise base rate source — more accurate
than speaker+context bucket rates because it's series-specific.

Priority chain in scoring:
  1. bias_map  (series + phrase specific, empirical from resolutions)   ← this module
  2. base_rates.yaml + base_rates_auto.yaml  (speaker + context)
  3. global default (0.45)
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

_DATA_PATH  = Path("data/bias_map.json")
_RELOAD_SEC = 300.0    # reload every 5 minutes so compute_bias_map updates propagate quickly


class BiasEntry(NamedTuple):
    series:         str
    phrase:         str
    empirical_rate: float   # Bayesian-smoothed YES rate
    n:              int     # number of resolutions
    live_ask:       float | None
    bias:           str     # "overpriced" | "underpriced" | "historically_rare" | "neutral"
    bias_magnitude: float
    action:         str | None  # "BUY_NO" | "BUY_YES" | "BUY_NO_PREFERRED" | None


_NEUTRAL_ENTRY = None


class BiasMapCache:
    """Read-only cache for series-specific phrase bias entries."""

    def __init__(self, path: Path = _DATA_PATH) -> None:
        self._path        = path
        self._index: dict[tuple[str, str], BiasEntry] = {}
        self._loaded_at   = 0.0
        self._maybe_reload()

    def _maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._loaded_at < _RELOAD_SEC and self._index:
            return
        if not self._path.exists():
            return
        try:
            data  = json.loads(self._path.read_text(encoding="utf-8"))
            index: dict[tuple[str, str], BiasEntry] = {}
            # Support both flat "all" list and split "overpriced"/"underpriced" lists.
            all_entries = (
                data.get("all")
                or (data.get("overpriced", []) + data.get("underpriced", []))
            )
            for entry in all_entries:
                series = str(entry.get("series", "")).strip()
                phrase = str(entry.get("phrase", "")).strip().lower()
                if not series or not phrase:
                    continue
                index[(series, phrase)] = BiasEntry(
                    series         = series,
                    phrase         = phrase,
                    empirical_rate = float(entry.get("empirical_rate", 0.45)),
                    n              = int(entry.get("n", 0)),
                    live_ask       = entry.get("live_ask"),
                    bias           = str(entry.get("bias", "neutral")),
                    bias_magnitude = float(entry.get("bias_magnitude", 0.0)),
                    action         = entry.get("action"),
                )
            self._index     = index
            self._loaded_at = now
            logger.debug("BiasMapCache: loaded %d entries", len(index))
        except Exception as exc:
            logger.warning("BiasMapCache: failed to load %s: %s", self._path, exc)

    def get(self, series: str, phrase: str) -> BiasEntry | None:
        """Return the BiasEntry for (series, phrase), or None if not found."""
        self._maybe_reload()
        return self._index.get((series.strip(), phrase.strip().lower()))

    def get_empirical_rate(self, series: str, phrase: str) -> float | None:
        """Return the empirical base rate for (series, phrase), or None."""
        entry = self.get(series, phrase)
        return entry.empirical_rate if entry is not None else None

    def is_overpriced(self, series: str, phrase: str,
                      threshold: float = 0.18) -> bool:
        """True if the market systematically overprices YES for this phrase."""
        entry = self.get(series, phrase)
        return entry is not None and entry.bias == "overpriced" and entry.bias_magnitude >= threshold

    def is_underpriced(self, series: str, phrase: str,
                       threshold: float = 0.30, min_n: int = 15) -> bool:
        """True when the market systematically underprices YES with strong evidence.

        Requires:
          - bias_magnitude >= threshold  (empirical >> market by at least 30¢)
          - n >= min_n                   (at least 15 historical resolutions)

        Used to bypass YES_PRICE_FLOOR_BLOCK / SETTLED_MARKET_BLOCK in the scorer
        for phrases whose low market price reflects chronic market-maker bias, not
        genuine rareness (e.g. KXTRUMPSAY "rigged election": emp=71%, mkt=22¢).
        """
        entry = self.get(series, phrase)
        if entry is None:
            return False
        return (
            entry.bias == "underpriced"
            and entry.bias_magnitude >= threshold
            and entry.n >= min_n
        )

    def is_historically_rare(self, series: str, phrase: str,
                              max_rate: float = 0.15) -> bool:
        """True if the phrase historically resolves YES < max_rate of the time."""
        entry = self.get(series, phrase)
        return entry is not None and entry.empirical_rate <= max_rate
