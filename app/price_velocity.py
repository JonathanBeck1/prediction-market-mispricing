"""Price velocity signal — detect smart-money price movement.

Loads data/price_velocity.json (written every 5 min by compute_price_velocity.py)
and provides per-market signals for the scoring engine.

Signal types:
  SMART_MONEY_UP   — YES price rising unusually fast (informed buying)
  SMART_MONEY_DOWN — YES price falling unusually fast (informed selling / event cancelled)
  NEUTRAL          — price stable, no unusual movement

Scoring integration:
  SMART_MONEY_UP   → +p_boost  (configurable, default +0.04)
  SMART_MONEY_DOWN → -p_boost  (configurable, default -0.04)

The boost scales with signal_strength (0-1), so a strong signal gets a bigger
adjustment than a weak one.  Max boost is capped at MAX_P_BOOST to prevent the
velocity signal alone from flipping a recommendation.

Note: This signal improves significantly with continuous runner operation.
After 7+ days of uninterrupted running, 2h/6h deltas become reliable
indicators of same-session informed flow.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

VELOCITY_PATH = Path("data/price_velocity.json")

MAX_P_BOOST    = 0.05   # maximum adjustment in either direction
BASE_P_BOOST   = 0.025  # boost at signal_strength = 0.5
MAX_SIGNAL_AGE_MIN = 10  # ignore stale velocity data (> 10 min old)


@dataclass(frozen=True)
class VelocitySignal:
    market_id:       str
    signal:          str    # SMART_MONEY_UP / SMART_MONEY_DOWN / NEUTRAL
    signal_strength: float  # 0.0 – 1.0
    signal_window:   str    # "2h", "6h", "24h"
    current_price:   float
    delta_2h:        float | None
    delta_6h:        float | None
    delta_24h:       float | None

    def p_adjustment(self) -> float:
        """Return additive p_literal adjustment (+/-).

        Scales linearly with signal_strength, capped at MAX_P_BOOST.
        No adjustment for NEUTRAL signals.
        """
        if self.signal == "NEUTRAL" or self.signal_strength == 0.0:
            return 0.0
        raw = BASE_P_BOOST + (MAX_P_BOOST - BASE_P_BOOST) * self.signal_strength
        raw = min(MAX_P_BOOST, raw)
        if self.signal == "SMART_MONEY_DOWN":
            return -round(raw, 4)
        return round(raw, 4)

    @property
    def reason_tag(self) -> str:
        return self.signal  # "SMART_MONEY_UP" or "SMART_MONEY_DOWN"


class PriceVelocityCache:
    """In-memory store of per-market velocity signals."""

    def __init__(
        self,
        signals: dict[str, dict],
        generated_at: str = "",
    ) -> None:
        self._signals = signals
        self._generated_at = generated_at

    @classmethod
    def from_cache(cls, path: Path = VELOCITY_PATH) -> "PriceVelocityCache":
        if not path.exists():
            logger.debug("price_velocity.json not found — velocity signals disabled")
            return cls({})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            generated_at = data.get("generated_at", "")

            # Check freshness — stale velocity data is worse than no data
            if generated_at:
                try:
                    gen_dt = datetime.fromisoformat(generated_at)
                    age_min = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 60
                    if age_min > MAX_SIGNAL_AGE_MIN:
                        logger.debug(
                            "price_velocity.json is %.0f min old (max=%d) — skipping",
                            age_min, MAX_SIGNAL_AGE_MIN
                        )
                        return cls({}, generated_at)
                except ValueError:
                    pass

            markets = data.get("markets", {})
            count_up   = sum(1 for v in markets.values() if v.get("signal") == "SMART_MONEY_UP")
            count_down = sum(1 for v in markets.values() if v.get("signal") == "SMART_MONEY_DOWN")
            logger.info(
                "Loaded price velocity: %d markets  (%d UP, %d DOWN)",
                len(markets), count_up, count_down,
            )
            return cls(markets, generated_at)
        except Exception as exc:
            logger.warning("Could not load price velocity: %s", exc)
            return cls({})

    def get(self, market_id: str) -> VelocitySignal | None:
        """Return VelocitySignal for a market, or None if no signal."""
        entry = self._signals.get(market_id)
        if entry is None:
            return None
        signal = entry.get("signal", "NEUTRAL")
        if signal == "NEUTRAL":
            return None  # don't return neutral — no adjustment needed
        return VelocitySignal(
            market_id=market_id,
            signal=signal,
            signal_strength=float(entry.get("signal_strength", 0.0)),
            signal_window=str(entry.get("signal_window", "2h")),
            current_price=float(entry.get("current_yes_ask", 0.0)),
            delta_2h=entry.get("delta_2h"),
            delta_6h=entry.get("delta_6h"),
            delta_24h=entry.get("delta_24h"),
        )

    @property
    def signal_count(self) -> int:
        return sum(
            1 for v in self._signals.values()
            if v.get("signal") in ("SMART_MONEY_UP", "SMART_MONEY_DOWN")
        )
