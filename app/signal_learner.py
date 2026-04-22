"""Adaptive Signal Weight Learner — the meta-learning layer.

Tracks win rates per (speaker, signal_source) combination from live outcome
data and produces dynamic signal weights that the scoring engine uses instead
of fixed multipliers.

Core insight: different signals have different predictive value for different
speakers/markets. The LLM layer might be net-negative for Trump but valuable
for Leavitt. News pressure might be strong for political speakers but noise
for sports. Instead of manually discovering this and hardcoding bypasses,
this module learns it from outcomes automatically.

Architecture:
  1. Periodically scans outcome_reviews for resolved bets
  2. For each bet, extracts which signals were active (from reason_codes)
  3. Groups by (speaker, signal, side) and computes rolling win rates
  4. Produces a weight map: signal_weights[speaker][signal] → [0.0, 1.5]
  5. The scoring engine reads these weights and scales signals accordingly

A signal that consistently loses money gets its weight driven toward 0.
A signal that consistently wins gets its weight driven toward 1.0-1.5.
A signal with insufficient data stays at 1.0 (neutral / trust default).

Refresh: every 30 minutes from outcome_reviews (same cadence as calibrator).
Output: data/signal_weights.json (hot-reloaded by scorer).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_PATH = Path("data/signal_weights.json")
_DB_PATH = Path("data/edge.db")
_RELOAD_SEC = 300.0  # hot-reload every 5 min

# Minimum bets before we adjust a signal's weight (avoid overfitting on noise)
_MIN_BETS = 8
# Bayesian prior: assume each signal starts at 50% WR with this many pseudo-observations
_PRIOR_WINS = 5
_PRIOR_TOTAL = 10

# Signal tags we track in reason_codes
_SIGNAL_TAGS = {
    "news_pressure": ["NEWS_PRESSURE_HIGH"],
    "x_buzz": ["X_BUZZ_HIGH"],
    "llm_boost": ["LLM_BOOST", "LLM_BOOST_HIGH"],
    "llm_suppress": ["LLM_SUPPRESS", "LLM_SUPPRESS_HIGH"],
    "event_llm_boost": ["EVENT_LLM_BOOST"],
    "event_llm_suppress": ["EVENT_LLM_SUPPRESS"],
    "poly_signal": ["POLY_DIVERGE", "POLY_HIGHER", "POLY_LOWER"],
    "wallet_flow": ["WALLET_FLOW_UP", "WALLET_FLOW_DOWN"],
    "smart_money": ["SMART_MONEY_UP", "SMART_MONEY_DOWN"],
    "wh_schedule": ["WH_KEYWORD", "WH_CONTEXT"],
    "rolling_rate": ["ROLLING_N3", "ROLLING_N5", "ROLLING_N10"],
    "cooccurrence": ["COOCCUR_BOOST", "COOCCUR_SUPPRESS"],
    "hazard_model": ["HAZARD_MODEL"],
    "bias_map": ["BIAS_MAP_RATE", "BIAS_MAP_OVERPRICED"],
}

# Speaker normalization
_SPEAKER_GROUPS = {
    "trump": "trump",
    "leavitt": "leavitt",
    "fed": "fed", "powell": "fed",
    "mamdani": "mamdani",
    "nba": "sports", "ncaab": "sports", "mlb": "sports", "mma": "sports",
    "nfl": "sports", "nhl": "sports",
}


@dataclass(frozen=True)
class SignalPerformance:
    """Performance stats for one (speaker_group, signal, side) combination."""
    speaker_group: str
    signal: str
    side: str
    wins: int
    total: int
    pnl: float
    win_rate: float
    weight: float  # computed adaptive weight


@dataclass
class SignalWeightStore:
    """Hot-reloadable store for adaptive signal weights."""

    _path: Path = field(default_factory=lambda: _DATA_PATH)
    _weights: dict[str, dict[str, float]] = field(default_factory=dict)
    _performance: list[SignalPerformance] = field(default_factory=list)
    _loaded_at: float = 0.0
    _computed_at: str = ""

    @classmethod
    def from_json(cls, path: Path = _DATA_PATH) -> SignalWeightStore:
        store = cls(_path=path)
        store._reload()
        return store

    def _reload(self) -> None:
        now = time.monotonic()
        if now - self._loaded_at < _RELOAD_SEC and self._weights:
            return
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._weights = data.get("weights", {})
            self._computed_at = data.get("computed_at", "")
            self._loaded_at = now
            total_entries = sum(len(v) for v in self._weights.values())
            logger.info(
                "SignalWeightStore: loaded %d speaker groups, %d signal weights",
                len(self._weights), total_entries,
            )
        except Exception as exc:
            logger.warning("Failed to load signal weights: %s", exc)

    def get_weight(self, speaker: str, signal: str) -> float:
        """Return the adaptive weight for a signal for this speaker.

        Returns 1.0 (neutral) if no data or insufficient observations.
        Returns < 1.0 if signal is net-harmful for this speaker.
        Returns > 1.0 if signal is net-beneficial (capped at 1.5).
        """
        self._reload()
        spk_group = _SPEAKER_GROUPS.get(speaker.lower().strip(), "other")
        spk_weights = self._weights.get(spk_group)
        if spk_weights is None:
            # Try "all" fallback (global across speakers)
            spk_weights = self._weights.get("_global")
        if spk_weights is None:
            return 1.0
        return spk_weights.get(signal, 1.0)

    def get_all_weights(self, speaker: str) -> dict[str, float]:
        """Return all signal weights for this speaker group."""
        self._reload()
        spk_group = _SPEAKER_GROUPS.get(speaker.lower().strip(), "other")
        return dict(self._weights.get(spk_group, {}))


def _extract_speaker(market_id: str, raw_json_speaker: str | None) -> str:
    """Normalize speaker from market_id or raw_json."""
    if raw_json_speaker:
        return raw_json_speaker.lower().strip()
    mid = market_id.lower()
    if "kxtrump" in mid or "kxpresmention" in mid:
        return "trump"
    if "kxsecpress" in mid or "kxleavitt" in mid:
        return "leavitt"
    if "kxfed" in mid:
        return "fed"
    if "kxmamdani" in mid or "kxnycm" in mid:
        return "mamdani"
    if "kxnba" in mid:
        return "nba"
    if "kxncaab" in mid:
        return "ncaab"
    if "kxmlb" in mid:
        return "mlb"
    if "kxfight" in mid:
        return "mma"
    return "other"


def _compute_weight(wins: int, total: int, baseline_wr: float = 0.50) -> float:
    """Compute adaptive weight from Bayesian-smoothed win rate.

    Uses Beta-Binomial model:
      posterior_wr = (wins + prior_wins) / (total + prior_total)

    Weight = posterior_wr / baseline_wr, clamped to [0.0, 1.5].
    If posterior_wr > baseline → signal is helpful → weight > 1.0.
    If posterior_wr < baseline → signal is harmful → weight < 1.0.
    """
    posterior_wr = (wins + _PRIOR_WINS) / (total + _PRIOR_TOTAL)
    if baseline_wr <= 0:
        baseline_wr = 0.50
    raw_weight = posterior_wr / baseline_wr
    return round(max(0.0, min(1.5, raw_weight)), 3)


def compute_signal_weights(db_path: Path = _DB_PATH, days: int = 30) -> dict:
    """Scan outcome_reviews and compute per-(speaker, signal) adaptive weights.

    Returns the full output dict ready to be written to signal_weights.json.
    """
    if not db_path.exists():
        logger.warning("DB not found: %s", db_path)
        return {}

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(db_path))
    conn.row_factory = _sqlite3.Row

    rows = conn.execute(
        """SELECT market_id, speaker, side, outcome, reason_codes,
                  realized_pnl, raw_json
           FROM outcome_reviews
           WHERE side IN ('BUY_YES', 'BUY_NO')
             AND outcome IN ('yes', 'no')
             AND resolved_ts > datetime('now', ?)
        """,
        (f"-{days} days",),
    ).fetchall()
    conn.close()

    if not rows:
        logger.info("No resolved outcomes in last %d days", days)
        return {}

    # Compute global baseline win rate
    global_wins = sum(
        1 for r in rows
        if (r["outcome"] == "yes" and r["side"] == "BUY_YES")
        or (r["outcome"] == "no" and r["side"] == "BUY_NO")
    )
    global_wr = global_wins / len(rows) if rows else 0.50

    # Accumulate stats per (speaker_group, signal, side)
    from collections import defaultdict
    stats: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {"wins": 0, "total": 0, "pnl": 0.0}
    )

    for r in rows:
        spk = _extract_speaker(r["market_id"], r["speaker"])
        spk_group = _SPEAKER_GROUPS.get(spk, "other")
        side = r["side"]
        codes = r["reason_codes"] or ""
        pnl = r["realized_pnl"] or 0.0
        won = (
            (r["outcome"] == "yes" and side == "BUY_YES")
            or (r["outcome"] == "no" and side == "BUY_NO")
        )

        for signal_name, tags in _SIGNAL_TAGS.items():
            if any(tag in codes for tag in tags):
                key = (spk_group, signal_name, side)
                stats[key]["total"] += 1
                if won:
                    stats[key]["wins"] += 1
                stats[key]["pnl"] += pnl

                # Also accumulate into _global
                gkey = ("_global", signal_name, side)
                stats[gkey]["total"] += 1
                if won:
                    stats[gkey]["wins"] += 1
                stats[gkey]["pnl"] += pnl

    # Compute weights
    weights: dict[str, dict[str, float]] = {}
    performance: list[dict] = []

    for (spk_group, signal, side), s in sorted(stats.items()):
        if s["total"] < _MIN_BETS:
            continue

        w = _compute_weight(s["wins"], s["total"], global_wr)
        wr = s["wins"] / s["total"] if s["total"] else 0.0

        if spk_group not in weights:
            weights[spk_group] = {}

        # Use side-specific key for directional signals
        if signal in ("llm_boost", "llm_suppress", "event_llm_boost", "event_llm_suppress"):
            sig_key = signal  # already directional
        else:
            sig_key = f"{signal}_{side.lower()}" if side else signal

        weights[spk_group][sig_key] = w

        performance.append({
            "speaker_group": spk_group,
            "signal": signal,
            "side": side,
            "wins": s["wins"],
            "total": s["total"],
            "win_rate": round(wr * 100, 1),
            "pnl": round(s["pnl"], 2),
            "weight": w,
        })

    # Sort performance by impact (most bets first)
    performance.sort(key=lambda x: -x["total"])

    result = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "global_wr": round(global_wr * 100, 1),
        "total_outcomes": len(rows),
        "days": days,
        "weights": weights,
        "performance": performance,
    }

    logger.info(
        "Signal weights computed: %d outcomes, global WR=%.1f%%, %d speaker groups, %d signal entries",
        len(rows), global_wr * 100, len(weights),
        sum(len(v) for v in weights.values()),
    )

    # Log notable findings
    for p in performance[:15]:
        if p["speaker_group"] == "_global":
            continue
        status = "HELPFUL" if p["weight"] > 1.05 else "HARMFUL" if p["weight"] < 0.95 else "NEUTRAL"
        logger.info(
            "  %s %-8s %-22s %-8s  n=%3d  WR=%5.1f%%  PnL=$%+.2f  weight=%.3f",
            status, p["speaker_group"], p["signal"], p["side"],
            p["total"], p["win_rate"], p["pnl"], p["weight"],
        )

    return result


def compute_and_save(db_path: Path = _DB_PATH, output_path: Path = _DATA_PATH, days: int = 30) -> None:
    """Compute signal weights and write to JSON."""
    result = compute_signal_weights(db_path, days)
    if not result:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Wrote signal weights to %s", output_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    compute_and_save()
