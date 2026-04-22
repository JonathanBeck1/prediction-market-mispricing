from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# LLM suppress has 100% WR in live data; LLM boost is an anti-signal for BUY_YES.
# Live data (394 outcomes):
#   LLM_BOOST + BUY_YES: 31.3% WR, -$1.86 — boost makes YES predictions WORSE
#   LLM_SUPPRESS + BUY_YES: 45% WR, +$2.38 — suppress works
#   NO_LLM + BUY_NO: 58.1% WR, +$5.01 — no signal is the best signal
#
# Fix: neutralize all boost caps to 1.00 so LLM boost has NO upward effect on
# p_literal.  Suppress caps are unchanged (MODIFIER_MIN = 0.3 still applies).
# This means LLM entries with llm_boost > 1.0 are silently clamped to 1.0
# — the model ignores the LLM's "buy" signal while preserving its "avoid" signal.
MODIFIER_MIN = 0.3     # trust LLM suppress signals fully (100% WR)
MODIFIER_MAX = 1.0     # LLM boost capped at 1.0 — neutralized (was 1.30)
_BOOST_CAP_MEDIUM = 1.0    # medium confidence: neutral (was 1.08)
_BOOST_CAP_LOW    = 1.0    # low confidence: neutral (was 1.01)


@dataclass(frozen=True)
class SignalModifiers:
    news_pressure: float = 1.0
    x_buzz: float = 1.0
    llm_boost: float = 1.0
    llm_reasoning: str = ""   # 2-3 sentence explanation from LLM
    llm_evidence: str = ""    # direct quote / data point that triggered it
    llm_topic: str = ""       # which key topic this phrase connects to
    # Provenance: 16-hex ids from fetch_signals + optional LLM echo in evidence (Sprint C).
    source_story_hashes: tuple[str, ...] = ()
    # Deterministic ids from fetch_news_signals (aligned with app.story_hash).
    news_story_hashes: tuple[str, ...] = ()


@dataclass
class SignalStore:
    """Loads data/signals.yaml and provides per-phrase modifiers.

    Supports hot-reload: call get() and the store will transparently reload
    signals.yaml whenever the file changes on disk (checked via mtime).
    """

    _path: Path = field(default_factory=Path)
    _lookup: dict[str, SignalModifiers] = field(default_factory=dict)
    _mtime: float = 0.0

    @classmethod
    def from_yaml(cls, path: Path) -> SignalStore:
        store = cls(_path=path)
        store._reload()
        return store

    def _reload(self) -> None:
        """Reload the YAML file if it has changed on disk."""
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed; signals default to 1.0")
            return

        if not self._path.exists():
            return

        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            return

        if mtime <= self._mtime:
            return  # file unchanged

        try:
            with self._path.open("r") as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", self._path, exc)
            return

        if not isinstance(data, dict) or "signals" not in data:
            return

        new_lookup: dict[str, SignalModifiers] = {}
        for entry in data["signals"]:
            phrase = str(entry.get("phrase", "")).lower()
            if not phrase:
                continue
            np = _clamp(float(entry.get("news_pressure", 1.0)))
            xb = _clamp(float(entry.get("x_buzz", 1.0)))
            # Confidence-aware boost cap: low-conf entries barely move p_literal.
            # Suppresses are unaffected (MODIFIER_MIN = 0.3 still applies).
            conf = str(entry.get("llm_confidence", "")).lower().strip()
            if conf == "expired":
                lb = 1.0  # stale signal — force neutral
            else:
                boost_cap = (
                    _BOOST_CAP_LOW    if conf == "low"
                    else _BOOST_CAP_MEDIUM if conf == "medium"
                    else MODIFIER_MAX  # high or unlabelled
                )
                raw_lb = float(entry.get("llm_boost", 1.0))
                lb = max(MODIFIER_MIN, min(boost_cap, raw_lb))
            raw_hashes = entry.get("source_story_hashes")
            if isinstance(raw_hashes, list):
                story_hs = tuple(
                    str(h).lower()[:16]
                    for h in raw_hashes
                    if h and str(h).strip()
                )
            else:
                story_hs = ()

            raw_news = entry.get("news_story_hashes")
            if isinstance(raw_news, list):
                news_hs = tuple(
                    str(h).lower()[:16]
                    for h in raw_news
                    if h and str(h).strip()
                )
            else:
                news_hs = ()

            new_lookup[phrase] = SignalModifiers(
                news_pressure=np,
                x_buzz=xb,
                llm_boost=lb,
                llm_reasoning=str(entry.get("llm_reasoning", "")),
                llm_evidence=str(entry.get("llm_evidence", "")),
                llm_topic=str(entry.get("llm_topic", "")),
                source_story_hashes=story_hs,
                news_story_hashes=news_hs,
            )

        self._lookup = new_lookup
        self._mtime = mtime
        logger.info(
            "SignalStore reloaded %d entries from %s (mtime %.0f)",
            len(new_lookup), self._path, mtime,
        )

    def get(self, phrase: str) -> SignalModifiers:
        self._reload()  # no-op if file is unchanged
        return self._lookup.get(phrase.lower(), SignalModifiers())


def _clamp(value: float) -> float:
    return max(MODIFIER_MIN, min(MODIFIER_MAX, value))
