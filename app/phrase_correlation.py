"""Cross-Phrase Correlation Matrix for portfolio-level position awareness.

When the model recommends BUY_YES on "china" AND "tariff" in the same event,
those aren't independent bets — they're highly correlated (correlation ~0.70
from historical data). Treating them as independent overstates your edge.

This module:
  1. Builds a correlation matrix from historical co-occurrence of phrase outcomes
  2. Computes a "portfolio concentration" penalty when multiple correlated bets
     are active in the same event
  3. Provides a diversification score for the current bet book

Used by the scoring engine to:
  - Flag BUY signals on phrases that are highly correlated with existing BUY signals
  - Apply a Kelly-style correlation haircut to position sizing
  - Surface concentration risk in the dashboard

Output: data/phrase_correlations.json (computed from kalshi_outcomes.json).
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_PATH = Path("data/phrase_correlations.json")
_RELOAD_SEC = 600.0

# Minimum co-observations to compute a meaningful correlation
_MIN_CO_OBS = 5
# Correlation threshold above which we flag portfolio concentration
_HIGH_CORR_THRESHOLD = 0.50


def _phi_coefficient(both_yes: int, a_yes_b_no: int, a_no_b_yes: int, both_no: int) -> float:
    """Compute Phi coefficient (2x2 contingency table correlation).

    Phi = (n11*n00 - n10*n01) / sqrt(n1_*n0_*n_1*n_0)
    Range: [-1, 1]. For binary phrase outcomes, this is the natural correlation.
    """
    n11, n10, n01, n00 = both_yes, a_yes_b_no, a_no_b_yes, both_no
    total = n11 + n10 + n01 + n00
    if total == 0:
        return 0.0
    numer = n11 * n00 - n10 * n01
    r1 = n11 + n10  # row 1 total
    r0 = n01 + n00
    c1 = n11 + n01  # col 1 total
    c0 = n10 + n00
    denom = math.sqrt(max(1, r1) * max(1, r0) * max(1, c1) * max(1, c0))
    return numer / denom if denom > 0 else 0.0


class PhraseCorrelationStore:
    """Hot-reloadable store for phrase-pair correlations."""

    def __init__(self, path: Path = _DATA_PATH) -> None:
        self._path = path
        self._corr: dict[tuple[str, str], float] = {}
        self._high_corr_pairs: list[tuple[str, str, float]] = []
        self._loaded_at = 0.0
        self._reload()

    def _reload(self) -> None:
        now = time.monotonic()
        if now - self._loaded_at < _RELOAD_SEC and self._corr:
            return
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            corr: dict[tuple[str, str], float] = {}
            high: list[tuple[str, str, float]] = []
            for entry in data.get("pairs", []):
                a, b = entry["phrase_a"], entry["phrase_b"]
                phi = entry["phi"]
                corr[(a, b)] = phi
                corr[(b, a)] = phi
                if abs(phi) >= _HIGH_CORR_THRESHOLD:
                    high.append((a, b, phi))
            self._corr = corr
            self._high_corr_pairs = sorted(high, key=lambda x: -abs(x[2]))
            self._loaded_at = now
            logger.info(
                "PhraseCorrelationStore: %d pairs, %d high-corr (>=%.2f)",
                len(data.get("pairs", [])), len(high), _HIGH_CORR_THRESHOLD,
            )
        except Exception as exc:
            logger.warning("Failed to load phrase correlations: %s", exc)

    def get_correlation(self, phrase_a: str, phrase_b: str) -> float | None:
        """Return Phi correlation between two phrases, or None if unknown."""
        self._reload()
        key = (phrase_a.lower().strip(), phrase_b.lower().strip())
        return self._corr.get(key)

    def get_correlated_phrases(self, phrase: str, threshold: float = _HIGH_CORR_THRESHOLD) -> list[tuple[str, float]]:
        """Return all phrases correlated above threshold with this phrase."""
        self._reload()
        phrase_l = phrase.lower().strip()
        result = []
        for (a, b), phi in self._corr.items():
            if a == phrase_l and abs(phi) >= threshold:
                result.append((b, phi))
        result.sort(key=lambda x: -abs(x[1]))
        return result

    def portfolio_concentration(self, active_phrases: list[str]) -> float:
        """Compute portfolio concentration score for a set of active BUY phrases.

        Returns a value in [0, 1]:
          0.0 = fully diversified (no correlation between active bets)
          1.0 = fully concentrated (all bets perfectly correlated)

        Used to flag when the bet book is over-concentrated.
        """
        self._reload()
        if len(active_phrases) <= 1:
            return 0.0

        phrases = [p.lower().strip() for p in active_phrases]
        total_corr = 0.0
        n_pairs = 0

        for i in range(len(phrases)):
            for j in range(i + 1, len(phrases)):
                phi = self._corr.get((phrases[i], phrases[j]))
                if phi is not None:
                    total_corr += abs(phi)
                    n_pairs += 1

        if n_pairs == 0:
            return 0.0
        return round(total_corr / n_pairs, 3)

    def correlation_haircut(self, phrase: str, active_phrases: list[str]) -> float:
        """Return a Kelly-style haircut factor for a new bet on `phrase`
        given the existing active bets.

        Returns 1.0 (no haircut) if uncorrelated.
        Returns < 1.0 if correlated with active bets (position sizing reduction).
        """
        self._reload()
        if not active_phrases:
            return 1.0

        phrase_l = phrase.lower().strip()
        max_corr = 0.0
        for ap in active_phrases:
            phi = self._corr.get((phrase_l, ap.lower().strip()))
            if phi is not None:
                max_corr = max(max_corr, abs(phi))

        # Haircut: (1 - max_correlation * 0.5)
        # Fully correlated (phi=1.0) → 0.5x position
        # Uncorrelated (phi=0.0) → 1.0x position
        return round(max(0.3, 1.0 - max_corr * 0.5), 3)


def compute_phrase_correlations(outcomes_path: Path = Path("data/kalshi_outcomes.json")) -> dict:
    """Build Phi correlation matrix from historical outcomes."""
    if not outcomes_path.exists():
        logger.warning("Outcomes file not found: %s", outcomes_path)
        return {}

    data = json.loads(outcomes_path.read_text(encoding="utf-8"))
    markets = data.get("markets", [])

    # Group outcomes by event (event_ticker)
    events: dict[str, dict[str, str]] = defaultdict(dict)
    for m in markets:
        event = m.get("event_ticker") or ""
        phrase = (m.get("primary_phrase") or "").lower().strip()
        result = m.get("result")
        if event and phrase and result in ("yes", "no"):
            events[event][phrase] = result

    # For each event, build contingency tables for all phrase pairs
    contingency: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])

    for event, phrases in events.items():
        phrase_list = sorted(phrases.keys())
        for i in range(len(phrase_list)):
            for j in range(i + 1, len(phrase_list)):
                a, b = phrase_list[i], phrase_list[j]
                a_yes = phrases[a] == "yes"
                b_yes = phrases[b] == "yes"
                key = (a, b)
                if a_yes and b_yes:
                    contingency[key][0] += 1
                elif a_yes and not b_yes:
                    contingency[key][1] += 1
                elif not a_yes and b_yes:
                    contingency[key][2] += 1
                else:
                    contingency[key][3] += 1

    # Compute Phi for each pair
    pairs: list[dict] = []
    for (a, b), counts in contingency.items():
        n = sum(counts)
        if n < _MIN_CO_OBS:
            continue
        phi = _phi_coefficient(*counts)
        if abs(phi) < 0.05:
            continue  # skip near-zero correlations to save space
        pairs.append({
            "phrase_a": a,
            "phrase_b": b,
            "phi": round(phi, 3),
            "n": n,
            "both_yes": counts[0],
            "both_no": counts[3],
        })

    pairs.sort(key=lambda x: -abs(x["phi"]))

    result = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "total_events": len(events),
        "total_pairs": len(pairs),
        "high_corr_pairs": len([p for p in pairs if abs(p["phi"]) >= _HIGH_CORR_THRESHOLD]),
        "pairs": pairs,
    }

    logger.info(
        "Phrase correlations: %d events, %d pairs (>0.05), %d high-corr (>=%.2f)",
        len(events), len(pairs),
        result["high_corr_pairs"], _HIGH_CORR_THRESHOLD,
    )

    for p in pairs[:10]:
        logger.info("  phi=%+.3f  n=%3d  %s ↔ %s", p["phi"], p["n"], p["phrase_a"], p["phrase_b"])

    return result


def compute_and_save(output_path: Path = _DATA_PATH) -> None:
    result = compute_phrase_correlations()
    if not result:
        return
    from app.utils import atomic_write_json
    atomic_write_json(output_path, result)
    logger.info("Wrote phrase correlations to %s", output_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    compute_and_save()
