"""Online Bayesian Base Rate Updater — Beta-Bernoulli conjugate priors.

Instead of periodic batch recalibration of base rates, this module maintains
a Beta distribution posterior for each (speaker, phrase) pair that updates
incrementally as outcomes resolve.

Key advantages over the current batch approach:
  1. Instant adaptation: each new outcome shifts the posterior immediately
  2. Proper uncertainty: we know BOTH the expected rate AND our confidence
  3. Natural shrinkage: phrases with few observations regress toward the prior
  4. No stale data: recency weighting via exponential decay on pseudo-counts

Model:
  p(phrase | speaker) ~ Beta(alpha, beta)
  
  Prior: alpha_0 = prior_rate * strength, beta_0 = (1 - prior_rate) * strength
  After N observations with k YES outcomes:
    alpha_post = alpha_0 + k * decay_weight
    beta_post = beta_0 + (N - k) * decay_weight

  The posterior mean (alpha / (alpha + beta)) is the Bayesian base rate.
  The posterior variance gives us uncertainty bounds.

Output: data/bayesian_rates.json (hot-reloaded by scorer).
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

_DATA_PATH = Path("data/bayesian_rates.json")
_DB_PATH = Path("data/edge.db")
_RELOAD_SEC = 300.0

# Prior strength: how many pseudo-observations the prior is worth.
# Higher = more conservative (slower to move from prior).
# 10 means "treat the prior as equivalent to 10 observed outcomes."
_PRIOR_STRENGTH = 10.0

# Recency half-life in days: outcomes older than this count half as much
_RECENCY_HALFLIFE_DAYS = 60.0

# Minimum observations before we report a rate (below this, return None → use fallback)
_MIN_OBSERVATIONS = 3


@dataclass(frozen=True)
class BayesianRate:
    """Posterior summary for one (speaker, phrase) pair."""
    alpha: float
    beta: float
    n_obs: int  # raw observation count (before decay)

    @property
    def mean(self) -> float:
        """Posterior mean = expected YES rate."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        """Posterior variance — measures uncertainty."""
        a, b = self.alpha, self.beta
        return (a * b) / ((a + b) ** 2 * (a + b + 1))

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    @property
    def ci_low(self) -> float:
        """Approximate 90% credible interval lower bound."""
        return max(0.0, self.mean - 1.645 * self.std)

    @property
    def ci_high(self) -> float:
        """Approximate 90% credible interval upper bound."""
        return min(1.0, self.mean + 1.645 * self.std)

    @property
    def confidence(self) -> float:
        """Confidence score: tighter interval = higher confidence. Range [0, 1]."""
        width = self.ci_high - self.ci_low
        return round(max(0.0, 1.0 - width), 3)


@dataclass
class BayesianRateStore:
    """Hot-reloadable store for Bayesian base rates with uncertainty."""

    _path: Path = field(default_factory=lambda: _DATA_PATH)
    _rates: dict[str, BayesianRate] = field(default_factory=dict)
    _speaker_priors: dict[str, float] = field(default_factory=dict)
    _global_mean: float = 0.45
    _loaded_at: float = 0.0

    @classmethod
    def from_json(cls, path: Path = _DATA_PATH) -> BayesianRateStore:
        store = cls(_path=path)
        store._reload()
        return store

    def _reload(self) -> None:
        now = time.monotonic()
        if now - self._loaded_at < _RELOAD_SEC and self._rates:
            return
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            rates: dict[str, BayesianRate] = {}
            for entry in data.get("rates", []):
                key = entry["key"]
                rates[key] = BayesianRate(
                    alpha=entry["alpha"],
                    beta=entry["beta"],
                    n_obs=entry["n_obs"],
                )
            self._rates = rates
            self._speaker_priors = data.get("speaker_priors", {})
            self._global_mean = data.get("global_mean", 0.45)
            self._loaded_at = now
            logger.info("BayesianRateStore: loaded %d rates", len(rates))
        except Exception as exc:
            logger.warning("Failed to load Bayesian rates: %s", exc)

    def get(self, speaker: str, phrase: str) -> BayesianRate | None:
        """Return the Bayesian posterior for (speaker, phrase), or None if insufficient data."""
        self._reload()
        key = f"{speaker.lower().strip()}.{phrase.lower().strip()}"
        rate = self._rates.get(key)
        if rate is None:
            return None
        if rate.n_obs < _MIN_OBSERVATIONS:
            return None
        return rate

    def get_rate_with_uncertainty(self, speaker: str, phrase: str) -> tuple[float | None, float | None, float | None]:
        """Return (mean, ci_low, ci_high) or (None, None, None) if no data."""
        rate = self.get(speaker, phrase)
        if rate is None:
            return None, None, None
        return rate.mean, rate.ci_low, rate.ci_high

    def get_speaker_prior(self, speaker: str) -> float:
        """Return the speaker-level empirical prior, falling back to global mean."""
        self._reload()
        return self._speaker_priors.get(speaker.lower().strip(), self._global_mean)


def compute_bayesian_rates(
    db_path: Path = _DB_PATH,
    prior_strength: float = _PRIOR_STRENGTH,
    halflife_days: float = _RECENCY_HALFLIFE_DAYS,
) -> dict:
    """Compute Beta-Bernoulli posteriors from kalshi_outcomes.json + outcome_reviews."""
    outcomes_path = Path("data/kalshi_outcomes.json")
    if not outcomes_path.exists():
        logger.warning("kalshi_outcomes.json not found")
        return {}

    data = json.loads(outcomes_path.read_text(encoding="utf-8"))
    markets = data.get("markets", [])
    if not markets:
        return {}

    now = datetime.now(timezone.utc)
    decay_lambda = math.log(2) / (halflife_days * 86400) if halflife_days > 0 else 0

    # Accumulate weighted observations per (speaker, phrase)
    from collections import defaultdict
    accum: dict[str, dict] = defaultdict(
        lambda: {"weighted_yes": 0.0, "weighted_total": 0.0, "raw_count": 0}
    )

    for m in markets:
        phrase = (m.get("primary_phrase") or "").lower().strip()
        result = m.get("result")
        speaker = (m.get("speaker") or "auto").lower().strip()
        if not phrase or result not in ("yes", "no"):
            continue

        # Recency decay
        resolved_at_str = m.get("resolved_at") or m.get("close_time") or ""
        weight = 1.0
        if resolved_at_str and decay_lambda > 0:
            try:
                resolved_at = datetime.fromisoformat(resolved_at_str.replace("Z", "+00:00"))
                if resolved_at.tzinfo is None:
                    resolved_at = resolved_at.replace(tzinfo=timezone.utc)
                age_sec = max(0, (now - resolved_at).total_seconds())
                weight = math.exp(-decay_lambda * age_sec)
            except (ValueError, TypeError):
                weight = 0.5

        key = f"{speaker}.{phrase}"
        accum[key]["weighted_total"] += weight
        if result == "yes":
            accum[key]["weighted_yes"] += weight
        accum[key]["raw_count"] += 1

    # Also pull from outcome_reviews for the most recent data
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT speaker, phrase, outcome, resolved_ts
               FROM outcome_reviews
               WHERE outcome IN ('yes', 'no')
                 AND resolved_ts > datetime('now', '-90 days')
            """
        ).fetchall()
        conn.close()

        for r in rows:
            phrase = (r["phrase"] or "").lower().strip()
            speaker = (r["speaker"] or "auto").lower().strip()
            outcome = r["outcome"]
            if not phrase:
                continue

            resolved_str = r["resolved_ts"] or ""
            weight = 1.0
            if resolved_str and decay_lambda > 0:
                try:
                    resolved_at = datetime.fromisoformat(resolved_str.replace("Z", "+00:00"))
                    if resolved_at.tzinfo is None:
                        resolved_at = resolved_at.replace(tzinfo=timezone.utc)
                    age_sec = max(0, (now - resolved_at).total_seconds())
                    weight = math.exp(-decay_lambda * age_sec)
                except (ValueError, TypeError):
                    weight = 0.8

            key = f"{speaker}.{phrase}"
            accum[key]["weighted_total"] += weight
            if outcome == "yes":
                accum[key]["weighted_yes"] += weight
            accum[key]["raw_count"] += 1

    # Compute speaker-level empirical priors (hierarchical Bayesian)
    # Instead of a flat 0.45 global prior, each speaker gets a prior
    # based on their observed overall YES rate.  Phrases with few
    # observations shrink toward their speaker's average, not toward
    # a meaningless global constant.
    speaker_totals: dict[str, dict] = defaultdict(
        lambda: {"weighted_yes": 0.0, "weighted_total": 0.0}
    )
    for key, a in accum.items():
        spk = key.split(".")[0]
        speaker_totals[spk]["weighted_yes"] += a["weighted_yes"]
        speaker_totals[spk]["weighted_total"] += a["weighted_total"]

    speaker_priors: dict[str, float] = {}
    for spk, st in speaker_totals.items():
        if st["weighted_total"] > 0:
            speaker_priors[spk] = st["weighted_yes"] / st["weighted_total"]
        else:
            speaker_priors[spk] = 0.45
    global_mean = 0.45
    if speaker_totals:
        total_yes = sum(s["weighted_yes"] for s in speaker_totals.values())
        total_all = sum(s["weighted_total"] for s in speaker_totals.values())
        if total_all > 0:
            global_mean = total_yes / total_all

    # Compute Beta posteriors
    rates_out: list[dict] = []
    for key, a in accum.items():
        if a["raw_count"] < 1:
            continue

        spk = key.split(".")[0]
        prior_rate = speaker_priors.get(spk, global_mean)
        alpha_prior = prior_rate * prior_strength
        beta_prior = (1 - prior_rate) * prior_strength

        alpha_post = alpha_prior + a["weighted_yes"]
        beta_post = beta_prior + (a["weighted_total"] - a["weighted_yes"])

        rate = BayesianRate(alpha=round(alpha_post, 3), beta=round(beta_post, 3), n_obs=a["raw_count"])

        rates_out.append({
            "key": key,
            "alpha": rate.alpha,
            "beta": rate.beta,
            "n_obs": rate.n_obs,
            "mean": round(rate.mean, 4),
            "ci_low": round(rate.ci_low, 4),
            "ci_high": round(rate.ci_high, 4),
            "confidence": rate.confidence,
        })

    rates_out.sort(key=lambda x: -x["n_obs"])

    speaker_priors_out = {
        spk: round(rate, 4) for spk, rate in speaker_priors.items()
    }

    result = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "prior_strength": prior_strength,
        "halflife_days": halflife_days,
        "total_phrases": len(rates_out),
        "global_mean": round(global_mean, 4),
        "speaker_priors": speaker_priors_out,
        "rates": rates_out,
    }

    logger.info("Computed Bayesian rates for %d (speaker, phrase) pairs", len(rates_out))
    high_conf = [r for r in rates_out if r["confidence"] >= 0.7]
    logger.info("  High confidence (>=0.7): %d phrases", len(high_conf))

    return result


def compute_and_save(db_path: Path = _DB_PATH, output_path: Path = _DATA_PATH) -> None:
    result = compute_bayesian_rates(db_path)
    if not result:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Wrote Bayesian rates to %s", output_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    compute_and_save()
