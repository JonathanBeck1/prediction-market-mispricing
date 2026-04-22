"""Platt scaling recalibration for p_literal → calibrated probability.

The raw scoring model systematically underestimates probabilities in the
0.5–0.7 range (model=0.6 → actual YES rate 0.91 in live data). This causes
the system to generate BUY_NO signals when the phrase is actually highly
likely, losing most trades.

Platt scaling fits a 2-parameter logistic function:
    p_calibrated = sigmoid(a * logit(p_raw) + b)

on live outcome data from outcome_reviews. With a correct calibration,
the model's p estimate matches the actual frequency of YES outcomes.

The calibrator now fits THREE variants:
  - "default": all outcomes pooled (backwards-compatible fallback)
  - "pre_event": outcomes where the bet was placed pre-event
  - "live": outcomes where the bet was placed during a live event

Pre-event and live bets have fundamentally different dynamics: live bets
have 53% WR vs 18% WR for pre-event in live data.  Separate calibrators
allow each context to correct its own systematic bias.

Parameters refitted from DB every 30 minutes; cached to data/calibration.json.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_CALIB_CACHE_PATH = Path("data/calibration.json")
_DB_PATH          = Path("data/edge.db")
_MIN_SAMPLES      = 200      # minimum outcomes before fitting.
                             # Lowered from 350: live data shows systematic 20-34pp
                             # underestimation even at 394 samples, so we fit earlier
                             # and use the floors below to handle sparse regions.
_MIN_STRATUM      = 150      # minimum per-stratum samples for stratified fit
                             # raised from 60 → 150: small strata overfit badly,
                             # causing negative-slope calibrations that INVERT
                             # the model (p_lit=0.02→p_cal=0.51 at a=-0.115)
_MIN_SLOPE        = 0.10     # reject strata fits with slope < this — near-zero
                             # or negative slopes mean the calibration is
                             # uncorrelated / anti-correlated with true outcomes
_REFIT_INTERVAL   = 1800.0   # refit every 30 min as outcomes accumulate


# ──────────────────────────────────────────────────────────────────────────────
# Math helpers
# ──────────────────────────────────────────────────────────────────────────────

def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50.0, 50.0)))


def _logit_arr(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1.0 - p))


def _fit_logistic(
    p_scores: list[float],
    y_outcomes: list[int],
    n_iter: int = 3000,
    lr: float = 0.03,
) -> tuple[float, float]:
    """Fit P(Y=1) = sigmoid(a*logit(p) + b) via gradient descent.

    Starting point (a=1, b=0) is the identity mapping.
    Regularisation (L2, λ=0.01) prevents overfitting on small datasets.
    """
    X = _logit_arr(np.array(p_scores, dtype=np.float64))
    y = np.array(y_outcomes, dtype=np.float64)
    a, b = 1.0, 0.0
    lam = 0.10  # L2 regularisation — raised from 0.01; prevents overfitting on
                # small datasets (< 1000 rows); identity mapping (a=1, b=0) is
                # the L2 attractor, so stronger regularisation keeps the curve
                # closer to the raw model when data is sparse

    for _ in range(n_iter):
        pred = _sigmoid(a * X + b)
        err  = pred - y
        da   = float(np.mean(err * X)) + lam * a
        db   = float(np.mean(err))
        a -= lr * da
        b -= lr * db

    return float(a), float(b)


def _brier_score(p_scores: list[float], y_outcomes: list[int]) -> float:
    """Mean squared error between predicted probs and binary outcomes."""
    if not p_scores:
        return 0.25
    return float(np.mean((np.array(p_scores) - np.array(y_outcomes, dtype=float)) ** 2))


def kl_divergence(p: float, q: float) -> float:
    """KL(p_model ‖ q_market) for a binary distribution.

    Measures information gain of our model vs the market's price.
    KL > 0.05 = meaningful edge; KL > 0.15 = strong edge.
    Unlike |p - q|, KL correctly weights divergences near 0 and 1.
    """
    p = max(1e-6, min(1 - 1e-6, p))
    q = max(1e-6, min(1 - 1e-6, q))
    return p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q))


def kelly_fraction(ev: float, potential_loss: float) -> float:
    """Kelly fraction = edge / odds (for a binary bet).

    For a YES bet: f = ev_yes / (1 - yes_ask)
    For a NO  bet: f = ev_no  / (1 - no_ask )

    ¼ Kelly recommendation: bet 25% of this fraction of bankroll.
    Values < 0.05 indicate very marginal edge — not worth the variance.
    """
    if potential_loss <= 0:
        return 0.0
    return max(0.0, ev / potential_loss)


# ──────────────────────────────────────────────────────────────────────────────
# Calibrator
# ──────────────────────────────────────────────────────────────────────────────

class PlattCalibrator:
    """Applies Platt scaling to transform raw p_literal → calibrated probability.

    Fit once at startup (or load from cache), refit every 30 min.
    Falls back to identity if fewer than 30 outcomes are available.

    Supports stratified calibration via `calibrate(p, key="pre_event"|"live"|"default")`.
    Strata use separate (a, b) when enough data exists; fall back to "default" otherwise.
    """

    def __init__(self) -> None:
        # "default" params (pooled)
        self._a: float = 1.0
        self._b: float = 0.0
        self._n_samples: int = 0
        self._fitted: bool = False
        self._last_fit: datetime | None = None
        # per-stratum params: key → (a, b, n_samples, fitted)
        self._strata: dict[str, tuple[float, float, int, bool]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    # ── Empirical calibration floors ─────────────────────────────────────────
    # Live data (394 outcomes) shows the model systematically underestimates
    # at p < 0.20 for POLITICAL speakers:
    #   p < 5%:   actual YES = 32.1%  (+29pp gap)
    #   5-10%:    actual YES = 30.2%  (+23pp gap)
    #   10-15%:   actual YES = 28.1%  (+16pp gap)
    #   15-20%:   actual YES = 52.1%  (+34pp gap)
    #
    # SPORTS speakers are EXEMPT from floors.  Their per-phrase base rates are
    # accurate: the aggregate YES rate is high because common phrases (three-
    # pointer, foul) always get said, but rare phrases (overtime, buzzer, walk-
    # off) genuinely have 8-15% rates.  Applying a floor kills ALL sports
    # BUY_NO because the NO_CONVICTION_FLOOR gate blocks p_cal >= 0.20.
    # Sports BUY_NO is our best-performing segment (MLB 85% WR, MMA 70% WR,
    # Leavitt 65% WR, NCAAB 58% WR) — we must preserve it.
    _P_CAL_FLOOR_GLOBAL = 0.20
    _P_CAL_FLOOR_BY_SPEAKER: dict[str, float] = {
        "trump":   0.25,   # Trump actual YES = 52% at 15-20% model bucket
        "auto":    0.22,   # auto actual YES = 42.5% overall
        "leavitt": 0.20,   # Leavitt YES rate lower but still floor helps
        # Fed/Powell: structured press conferences with predictable phrasing.
        # Global 0.20 floor was killing NO_CONVICTION_FLOOR gate on all low-p
        # phrases — same issue as whitehouse.  Lower floor lets BUY_NO signals
        # through on phrases genuinely unlikely at a given FOMC event.
        "fed":    0.10,
        "powell": 0.10,
    }
    # Sports speakers: no floor applied — per-phrase base rates are accurate
    _SPORTS_SPEAKERS_NO_FLOOR: set[str] = {"nba", "ncaab", "mlb", "mma", "nfl"}

    def calibrate(self, p: float, key: str = "default", speaker: str = "") -> float:
        """Return calibrated probability. Falls back to identity if not fitted.

        Args:
            p:      raw p_literal from the scoring formula
            key:    one of "default", "pre_event", "live"
            speaker: speaker key for speaker-specific floor lookup

        Sports speakers are exempt from BOTH Platt calibration AND floors.
        The Platt scaler is fitted on all outcomes (dominated by political
        markets) and maps p=0.02 → 0.33 — correct for Trump but wrong for
        sports where "overtime" p=0.02 genuinely IS near 2%.  Sports base
        rates from the bias map are already accurate phrase-by-phrase.
        """
        spk = (speaker or "").lower().strip()
        if spk in self._SPORTS_SPEAKERS_NO_FLOOR:
            return float(p)

        if not self._fitted:
            return self._apply_floor(float(p), speaker)

        # Try stratum-specific params first
        if key in self._strata:
            a, b, _n, stratum_fitted = self._strata[key]
            if stratum_fitted:
                return self._apply_floor(self._apply(p, a, b), speaker)

        # Fall back to pooled default
        return self._apply_floor(self._apply(p, self._a, self._b), speaker)

    def _apply_floor(self, p_cal: float, speaker: str = "") -> float:
        """Apply empirical calibration floor for political speakers.

        Sports speakers are exempt — their per-phrase base rates are accurate
        and floors would destroy the BUY_NO edge.
        """
        spk = (speaker or "").lower().strip()
        if spk in self._SPORTS_SPEAKERS_NO_FLOOR:
            return p_cal
        floor = self._P_CAL_FLOOR_BY_SPEAKER.get(spk, self._P_CAL_FLOOR_GLOBAL)
        return max(floor, p_cal)

    @staticmethod
    def _apply(p: float, a: float, b: float) -> float:
        p_clipped = max(1e-6, min(1 - 1e-6, float(p)))
        logit_p   = math.log(p_clipped / (1.0 - p_clipped))
        cal       = 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, a * logit_p + b))))
        return max(0.02, min(0.98, cal))

    def has_stratum(self, key: str) -> bool:
        """Return True if *key* has a successfully fitted stratum (not just fallback)."""
        if key not in self._strata:
            return False
        _, _, _n, stratum_fitted = self._strata[key]
        return stratum_fitted

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def params(self) -> tuple[float, float]:
        return self._a, self._b

    @property
    def n_samples(self) -> int:
        return self._n_samples

    def needs_refit(self) -> bool:
        if self._last_fit is None:
            return True
        elapsed = (datetime.now(tz=timezone.utc) - self._last_fit).total_seconds()
        return elapsed > _REFIT_INTERVAL

    # ── Fit / load ────────────────────────────────────────────────────────────

    def fit_from_db(self, db_path: Path = _DB_PATH) -> bool:
        """Fit calibration parameters from outcome_reviews. Returns True if fitted."""
        if not db_path.exists():
            return False
        try:
            from app.db import connect as _db_connect
            conn = _db_connect(db_path)
            # Pull reason_codes and speaker to stratify pre_event/live × trump/non-trump
            rows = conn.execute(
                "SELECT p_literal, outcome, reason_codes, speaker, raw_json FROM outcome_reviews "
                "WHERE outcome IN ('yes','no') AND p_literal IS NOT NULL "
                "ORDER BY prediction_ts"
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.warning("PlattCalibrator: DB read failed: %s", exc)
            return False

        if len(rows) < _MIN_SAMPLES:
            logger.info(
                "PlattCalibrator: only %d outcomes (need ≥%d) — using identity",
                len(rows), _MIN_SAMPLES,
            )
            return False

        # Sample size warning for partial calibration  
        if len(rows) < 400:
            logger.warning(
                "PlattCalibrator: using %d outcomes (optimal ≥400) — calibration may be less stable",
                len(rows),
            )
            
        p_all  = [max(1e-6, min(1 - 1e-6, float(r[0]))) for r in rows]
        y_all  = [1 if r[1] == "yes" else 0 for r in rows]

        # Walk-forward validation: fit on first 80%, validate on last 20%
        split  = max(_MIN_SAMPLES, int(len(rows) * 0.80))
        a_val, b_val = _fit_logistic(p_all[:split], y_all[:split])
        if len(rows) > split:
            val_preds = [self._apply(p, a_val, b_val) for p in p_all[split:]]
            val_brier = _brier_score(val_preds, y_all[split:])
            baseline  = _brier_score(p_all[split:], y_all[split:])
            logger.info(
                "PlattCalibrator: walk-forward validation Brier=%.4f (baseline=%.4f, delta=%+.4f)",
                val_brier, baseline, val_brier - baseline,
            )

        # Full fit on all data
        a, b = _fit_logistic(p_all, y_all)

        # Brier score on full data (for monitoring)
        cal_preds = [self._apply(p, a, b) for p in p_all]
        brier     = _brier_score(cal_preds, y_all)
        raw_brier = _brier_score(p_all, y_all)

        # ── BSS quality gate ──────────────────────────────────────────────
        # If the calibrated curve makes the Brier score WORSE than the raw
        # model, discard the fit entirely.  This can happen when the training
        # data is too small (< 400 rows) or is dominated by a single event.
        # Threshold: allow up to 0.005 degradation (noise tolerance).
        if brier > raw_brier + 0.005:
            logger.warning(
                "PlattCalibrator: calibration DEGRADES Brier raw=%.4f → cal=%.4f "
                "(delta=%+.4f). Discarding fit — using raw p_literal.",
                raw_brier, brier, brier - raw_brier,
            )
            # Reset to identity — do NOT set _fitted=True so scorer bypasses us
            self._a         = 1.0
            self._b         = 0.0
            self._n_samples = len(rows)
            self._fitted    = False
            self._last_fit  = datetime.now(tz=timezone.utc)
            return False

        self._a         = a
        self._b         = b
        self._n_samples = len(rows)
        self._fitted    = True
        self._last_fit  = datetime.now(tz=timezone.utc)

        logger.info(
            "PlattCalibrator: fitted on %d outcomes → a=%.4f b=%.4f | "
            "Brier raw=%.4f → cal=%.4f (delta=%+.4f)",
            len(rows), a, b, raw_brier, brier, brier - raw_brier,
        )

        # ── Per-bucket calibration diagnostics ───────────────────────────────
        # Log actual vs predicted rate per p_literal bucket for monitoring.
        buckets = [(0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 0.50), (0.50, 1.01)]
        for lo, hi in buckets:
            mask = [lo <= p < hi for p in p_all]
            bucket_p = [p for p, m in zip(p_all, mask) if m]
            bucket_y = [yv for yv, m in zip(y_all, mask) if m]
            if bucket_y:
                actual_rate = sum(bucket_y) / len(bucket_y)
                cal_preds_b = [self._apply(p, a, b) for p in bucket_p]
                cal_rate = sum(cal_preds_b) / len(cal_preds_b)
                logger.info(
                    "PlattCalibrator [p=%.2f-%.2f]: n=%d  actual=%.1f%%  cal=%.1f%%  gap=%+.1fpp",
                    lo, hi, len(bucket_y), actual_rate * 100, cal_rate * 100,
                    (cal_rate - actual_rate) * 100,
                )

        # ── Stratified fits: timing × speaker × event_type ───────────────
        # Different combinations have very different systematic biases:
        # Trump rally: ~55% YES; Leavitt briefing: ~18% YES; NBA broadcast: ~64% YES.
        # Extended to include event_type stratification (Session 65).
        strata_raw: dict[str, tuple[list[float], list[int]]] = {
            # Core strata: speaker × timing  
            "pre_event_trump":     ([], []),
            "pre_event_non_trump": ([], []),
            "live_trump":          ([], []),
            "live_non_trump":      ([], []),
            # Event-type specific strata (Session 65)
            "sports_broadcast":    ([], []),  # NBA/MLB/NCAAB broadcast events  
            "briefing":           ([], []),  # Leavitt briefings
            "announcement":       ([], []),  # Mamdani announcements
            "sports_other":       ([], []),  # Sports non-broadcast
            # Legacy keys — kept for backward compat
            "pre_event": ([], []),
            "live":      ([], []),
        }
        for r in rows:
            codes    = (r[2] or "")
            speaker  = (r[3] or "").lower().strip() if len(r) > 3 else ""
            raw_json = (r[4] or "{}") if len(r) > 4 else "{}"
            
            # Extract event_type from raw_json for event-type stratification
            event_type = "general"
            try:
                import json
                data = json.loads(raw_json)
                event_info = data.get("event", {})
                event_type = event_info.get("event_type", "general")
            except Exception:
                pass
            
            is_trump = speaker == "trump"
            is_sports = speaker in ("nba", "ncaab", "mlb", "mma")
            p_clipped = max(1e-6, min(1 - 1e-6, float(r[0])))
            y_val     = 1 if r[1] == "yes" else 0
            
            # Event-type specific strata (highest priority)
            if is_sports and event_type == "nba_broadcast":
                strata_raw["sports_broadcast"][0].append(p_clipped)
                strata_raw["sports_broadcast"][1].append(y_val)
            elif is_sports and event_type == "other":
                strata_raw["sports_other"][0].append(p_clipped) 
                strata_raw["sports_other"][1].append(y_val)
            elif event_type == "briefing" and speaker == "leavitt":
                strata_raw["briefing"][0].append(p_clipped)
                strata_raw["briefing"][1].append(y_val)
            elif event_type == "announcement":
                strata_raw["announcement"][0].append(p_clipped)
                strata_raw["announcement"][1].append(y_val)
            # Original timing × speaker strata (fallback)
            elif "PRE_EVENT" in codes:
                skey = "pre_event_trump" if is_trump else "pre_event_non_trump"
                strata_raw[skey][0].append(p_clipped)
                strata_raw[skey][1].append(y_val)
                # Also populate legacy key for any code that reads it directly
                strata_raw["pre_event"][0].append(p_clipped)
                strata_raw["pre_event"][1].append(y_val)
            elif "LIVE" in codes:
                skey = "live_trump" if is_trump else "live_non_trump"
                strata_raw[skey][0].append(p_clipped)
                strata_raw[skey][1].append(y_val)
                strata_raw["live"][0].append(p_clipped)
                strata_raw["live"][1].append(y_val)

        new_strata: dict[str, tuple[float, float, int, bool]] = {}
        for skey, (sp, sy) in strata_raw.items():
            if len(sp) >= _MIN_STRATUM:
                sa, sb = _fit_logistic(sp, sy)
                # Guard against inverted / flat calibrators.  A near-zero or
                # negative slope means the fitted curve is uncorrelated with
                # actual outcomes (likely a small-data overfitting artefact).
                # Fall back to the global calibrator in that case.
                if sa < _MIN_SLOPE:
                    logger.warning(
                        "PlattCalibrator [%s]: n=%d fitted a=%.4f < %.2f "
                        "(slope too flat/negative — using global fallback)",
                        skey, len(sp), sa, _MIN_SLOPE,
                    )
                    new_strata[skey] = (a, b, len(sp), False)
                    continue
                s_cal  = [self._apply(p, sa, sb) for p in sp]
                s_brier = _brier_score(s_cal, sy)
                s_raw   = _brier_score(sp, sy)
                logger.info(
                    "PlattCalibrator [%s]: n=%d → a=%.4f b=%.4f | "
                    "Brier raw=%.4f → cal=%.4f",
                    skey, len(sp), sa, sb, s_raw, s_brier,
                )
                new_strata[skey] = (sa, sb, len(sp), True)
            else:
                logger.info(
                    "PlattCalibrator [%s]: only %d samples (need ≥%d) — using default",
                    skey, len(sp), _MIN_STRATUM,
                )
                new_strata[skey] = (a, b, len(sp), False)

        self._strata = new_strata
        self._save_cache()
        return True

    def load_cache(self) -> bool:
        """Load previously fitted params. Returns True if successfully loaded."""
        if not _CALIB_CACHE_PATH.exists():
            return False
        try:
            d = json.loads(_CALIB_CACHE_PATH.read_text(encoding="utf-8"))

            # Support both old format {"a":..., "b":...} and new format
            # {"default": {"a":..., "b":...}, "pre_event": {...}, "live": {...}}
            if "default" in d:
                dflt = d["default"]
                self._a         = float(dflt["a"])
                self._b         = float(dflt["b"])
                self._n_samples = int(dflt.get("n_samples", 0))
                ts = dflt.get("fitted_at") or d.get("fitted_at")
            else:
                # Legacy single-variant format
                self._a         = float(d["a"])
                self._b         = float(d["b"])
                self._n_samples = int(d.get("n_samples", 0))
                ts = d.get("fitted_at")

            self._last_fit = datetime.fromisoformat(ts) if ts else None
            self._fitted   = True

            # Load strata if present
            new_strata: dict[str, tuple[float, float, int, bool]] = {}
            for skey in ("pre_event", "live"):
                if skey in d:
                    sd = d[skey]
                    new_strata[skey] = (
                        float(sd["a"]),
                        float(sd["b"]),
                        int(sd.get("n_samples", 0)),
                        bool(sd.get("fitted", True)),
                    )
            self._strata = new_strata

            logger.info(
                "PlattCalibrator: loaded cache (a=%.3f b=%.3f n=%d strata=%s)",
                self._a, self._b, self._n_samples,
                list(k for k, v in new_strata.items() if v[3]),
            )
            return True
        except Exception:
            return False

    def _save_cache(self) -> None:
        try:
            now_iso = self._last_fit.isoformat() if self._last_fit else None
            payload: dict = {
                "default": {
                    "a":         self._a,
                    "b":         self._b,
                    "n_samples": self._n_samples,
                    "fitted_at": now_iso,
                },
                "fitted_at": now_iso,
                # Keep legacy flat keys for backwards compat with external readers
                "a": self._a,
                "b": self._b,
                "n_samples": self._n_samples,
            }
            for skey, (sa, sb, sn, sfitted) in self._strata.items():
                payload[skey] = {
                    "a":         sa,
                    "b":         sb,
                    "n_samples": sn,
                    "fitted":    sfitted,
                }
            _CALIB_CACHE_PATH.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Process-wide singleton
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_CALIBRATOR: PlattCalibrator | None = None


def get_calibrator() -> PlattCalibrator:
    """Return the process-wide PlattCalibrator, fitting from DB if needed."""
    global _DEFAULT_CALIBRATOR
    if _DEFAULT_CALIBRATOR is None:
        _DEFAULT_CALIBRATOR = PlattCalibrator()
        if not _DEFAULT_CALIBRATOR.load_cache():
            _DEFAULT_CALIBRATOR.fit_from_db()
    elif _DEFAULT_CALIBRATOR.needs_refit():
        _DEFAULT_CALIBRATOR.fit_from_db()
    return _DEFAULT_CALIBRATOR
