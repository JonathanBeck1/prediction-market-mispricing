"""Phrase vocabulary trend cache for VOCAB_TRENDING_UP/DOWN scoring signals.

Loads data/phrase_trends.json (written by scripts/compute_phrase_trends.py)
and provides a fast per-(speaker, phrase) multiplier to the ScoringEngine.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

_TRENDS_PATH = Path(__file__).resolve().parent.parent / "data" / "phrase_trends.json"

# Scoring multipliers
_TREND_UP_MULT   = 1.10  # 30d rate >> 90d rate  — phrase gaining relevance
_TREND_DOWN_MULT = 0.90  # 30d rate << 90d rate  — phrase fading from use

# How often to reload from disk (seconds)
_RELOAD_INTERVAL = 300


class TrendSignal(NamedTuple):
    flag:       str    # "VOCAB_TRENDING_UP" | "VOCAB_TRENDING_DOWN" | ""
    multiplier: float  # 1.10 / 0.90 / 1.0
    delta:      float  # 30d_rate - 90d_rate
    rate_30d:   float | None
    rate_90d:   float | None


_NEUTRAL = TrendSignal("", 1.0, 0.0, None, None)


class PhraseTrendCache:
    """Thread-safe read-only cache of phrase trend signals."""

    def __init__(self) -> None:
        self._trends: dict[str, dict] = {}
        self._loaded_at: float = 0.0

    def _maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._loaded_at < _RELOAD_INTERVAL:
            return
        if not _TRENDS_PATH.exists():
            return
        try:
            data = json.loads(_TRENDS_PATH.read_text(encoding="utf-8"))
            self._trends = data.get("trends", {})
            self._loaded_at = now
            up   = data.get("trending_up", 0)
            down = data.get("trending_down", 0)
            logger.debug(
                "PhraseTrendCache reloaded: %d phrases, %d↑ %d↓",
                len(self._trends), up, down,
            )
        except Exception as exc:
            logger.warning("Failed to reload phrase_trends.json: %s", exc)

    def get_signal(self, speaker: str, phrase: str) -> TrendSignal:
        """Return trend signal for (speaker, phrase). Falls back to NEUTRAL."""
        self._maybe_reload()
        key = f"{(speaker or '').lower().strip()}:{(phrase or '').lower().strip()}"
        entry = self._trends.get(key)
        if not entry:
            return _NEUTRAL
        flag  = entry.get("trend_flag") or ""
        delta = entry.get("trend_delta") or 0.0
        rates = entry.get("rates", {})
        r30   = rates.get("30d")
        r90   = rates.get("90d")
        if flag == "VOCAB_TRENDING_UP":
            return TrendSignal(flag, _TREND_UP_MULT, delta, r30, r90)
        if flag == "VOCAB_TRENDING_DOWN":
            return TrendSignal(flag, _TREND_DOWN_MULT, delta, r30, r90)
        return _NEUTRAL

    def all_trending(self) -> list[dict]:
        """Return all phrases with active trend flags (for dashboard display)."""
        self._maybe_reload()
        return [
            v for v in self._trends.values()
            if v.get("trend_flag")
        ]
