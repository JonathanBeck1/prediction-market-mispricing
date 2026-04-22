"""EventSignalStore: loads and caches per-event LLM phrase multipliers,
probability floors, and hard overrides.

Per-event signal files are written by scripts/analyze_event.py and stored in
data/event_signals/{safe_event_id}.json. This store loads them lazily and
hot-reloads whenever the file changes on disk.

Three tiers of event-specific adjustment (in precedence order):
  1. p_override: Hard bypass — discard the frequency formula entirely and use
     this probability.  For causally certain phrases, e.g. the name of the
     person being sworn in at a swearing-in ceremony.  Set by either
     events.yaml (human-authored) or manually editing the signal file.
  2. p_floor: Soft minimum — the frequency formula still runs, but the result
     is clamped to at least this value.  For domain-elevated phrases, e.g.
     "deport" at a DHS ceremony.  Output by the LLM in analyze_event.py or
     set manually.
  3. multiplier: Existing behaviour — multiplicative adjustment to base rate.

Usage in scoring.py:
    # 1. Check hard override first
    p_ov = engine.event_signals.get_override(event.event_id, phrase)
    if p_ov is not None:
        return p_ov  # bypass frequency formula

    # 2. Compute frequency-based p as before
    event_llm, reason = engine.event_signals.get(event.event_id, phrase)
    p = base * topic_rel * decay * news * buzz * global_llm * event_llm

    # 3. Apply floor
    p_fl = engine.event_signals.get_floor(event.event_id, phrase)
    if p_fl is not None:
        p = max(p, p_fl)
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

EVENT_SIGNALS_DIR = Path("data/event_signals")

# Asymmetric clamp applied at read time.
# Trust suppress signals strongly (100% WR in live data);
# cap boost lower than the generation-time ceiling (0.05-3.0)
# to prevent a single LLM call from dominating the p_literal formula.
EVENT_LLM_MIN = 0.10  # strong suppress allowed (format-based suppression is accurate)
EVENT_LLM_MAX = 1.80  # cap boost (boost signals are less reliable than suppress)


@dataclass
class EventSignalStore:
    """Hot-reloading store for per-event LLM phrase multipliers."""

    _dir: Path = field(default_factory=lambda: EVENT_SIGNALS_DIR)
    # cache: event_id → (adjustments_dict, p_floors_dict, p_overrides_dict, mtime)
    _cache: dict[str, tuple[dict[str, dict], dict[str, float], dict[str, float], float]] = field(
        default_factory=dict, repr=False
    )

    @classmethod
    def from_dir(cls, directory: Path | str | None = None) -> "EventSignalStore":
        d = Path(directory) if directory else EVENT_SIGNALS_DIR
        store = cls(_dir=d)
        return store

    @staticmethod
    def _safe_filename(event_id: str) -> str:
        return re.sub(r"[^\w\-]", "_", event_id) + ".json"

    def _load(self, event_id: str) -> tuple[dict[str, dict], dict[str, float], dict[str, float]]:
        """Load (or hot-reload) signals for event_id.

        Returns (adjustments, p_floors, p_overrides). All dicts use
        lower-stripped phrase keys.  Returns ({}, {}, {}) on miss.
        """
        path = self._dir / self._safe_filename(event_id)
        if not path.exists():
            return {}, {}, {}

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {}, {}, {}

        cached = self._cache.get(event_id)
        if cached and mtime <= cached[3]:
            return cached[0], cached[1], cached[2]

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load event signals %s: %s", path.name, exc)
            if cached:
                return cached[0], cached[1], cached[2]
            return {}, {}, {}

        # ── Adjustments (multipliers) ────────────────────────────────────────
        raw_adj = data.get("adjustments", {})
        if isinstance(raw_adj, list):
            raw_adj = {
                a["phrase"]: {"multiplier": a.get("multiplier", 1.0), "reason": a.get("reason", "")}
                for a in raw_adj
                if isinstance(a, dict) and a.get("phrase")
            }
        adj: dict[str, dict] = {}
        for phrase, info in raw_adj.items():
            if isinstance(info, dict):
                mult = float(info.get("multiplier", 1.0))
                reason = str(info.get("reason", ""))
                adj[phrase.lower().strip()] = {"multiplier": mult, "reason": reason}

        # ── p_floors: minimum probability regardless of base rate ────────────
        # Format: {"phrase": 0.65} or {"phrase": {"p_floor": 0.65, "reason": "..."}}
        p_floors: dict[str, float] = {}
        raw_floors = data.get("p_floors", {})
        for phrase, val in raw_floors.items():
            key = phrase.lower().strip()
            try:
                if isinstance(val, dict):
                    p_floors[key] = float(val["p_floor"])
                else:
                    p_floors[key] = float(val)
            except (KeyError, TypeError, ValueError):
                pass

        # ── p_overrides: bypass frequency formula entirely ───────────────────
        # Format: {"phrase": 0.95} or {"phrase": {"p_override": 0.95, "reason": "..."}}
        p_overrides: dict[str, float] = {}
        raw_overrides = data.get("p_overrides", {})
        for phrase, val in raw_overrides.items():
            key = phrase.lower().strip()
            try:
                if isinstance(val, dict):
                    p_overrides[key] = float(val["p_override"])
                else:
                    p_overrides[key] = float(val)
            except (KeyError, TypeError, ValueError):
                pass

        self._cache[event_id] = (adj, p_floors, p_overrides, mtime)
        logger.debug(
            "Loaded event signals for %s: %d adjustments, %d floors, %d overrides (mtime %.0f)",
            event_id, len(adj), len(p_floors), len(p_overrides), mtime,
        )
        return adj, p_floors, p_overrides

    def get(self, event_id: str, phrase: str) -> tuple[float, str]:
        """Return (multiplier, reason) for phrase at this event. Defaults to (1.0, '').

        Applies asymmetric clamp: suppress allowed down to EVENT_LLM_MIN (0.10),
        boost capped at EVENT_LLM_MAX (1.80).  Raw values from analyze_event.py
        can reach 0.05–3.0; clamping here prevents a single LLM call from
        dominating p_literal, while still allowing strong format-based suppression.
        """
        if not event_id or not phrase:
            return 1.0, ""
        adj, _, _ = self._load(event_id)
        info = adj.get(phrase.lower().strip())
        if info is None:
            return 1.0, ""
        raw_mult = float(info.get("multiplier", 1.0))
        clamped  = max(EVENT_LLM_MIN, min(EVENT_LLM_MAX, raw_mult))
        return clamped, str(info.get("reason", ""))

    def get_floor(self, event_id: str, phrase: str) -> float | None:
        """Return the p_floor for phrase at this event, or None if not set.

        A p_floor is a minimum probability: regardless of what the frequency
        formula computes, the result will be at least this value.  Used for
        domain-elevated phrases where the event context structurally increases
        probability above the historical base rate (e.g. "deport" at a DHS
        swearing-in ceremony).
        """
        if not event_id or not phrase:
            return None
        _, p_floors, _ = self._load(event_id)
        return p_floors.get(phrase.lower().strip())

    def get_override(self, event_id: str, phrase: str) -> float | None:
        """Return a hard p_override for phrase at this event, or None if not set.

        A p_override completely bypasses the frequency formula.  Use for
        causally certain phrases: e.g. the name of the person being sworn in
        at a swearing-in ceremony, or the title of a bill at a signing ceremony.
        The probability returned should be very high (0.85–0.97) but not 1.0,
        leaving room for the edge case where the speaker is unexpectedly absent
        or the event is extremely brief.
        """
        if not event_id or not phrase:
            return None
        _, _, p_overrides = self._load(event_id)
        return p_overrides.get(phrase.lower().strip())

    def has_file(self, event_id: str) -> bool:
        """Return True if an event signal file exists for this event."""
        return (self._dir / self._safe_filename(event_id)).exists()

    def clear_cache(self) -> None:
        self._cache.clear()
