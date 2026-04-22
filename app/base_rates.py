from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GLOBAL_DEFAULT = 0.45  # empirical global fallback (session 25 calibration); YAML overrides this

# Speaker-specific global defaults based on resolved outcome analysis (Session 65).
# These replace the flat _GLOBAL_DEFAULT for unknown phrases when we have 
# empirical data showing systematic speaker-specific bias patterns.
# Data source: 374 resolved outcomes from past 30 days (2026-03-01 to 2026-03-31).
_SPEAKER_DEFAULTS: dict[str, float] = {
    "nba":     0.65,  # Actual 47.6% + 17pp buffer (arena certainties + base rate underestimation)
    "ncaab":   0.58,  # Actual 43.0% + 15pp buffer  
    "auto":    0.55,  # Actual 42.5% + 13pp buffer (catch-all speakers need higher baseline)
    "trump":   0.55,  # Actual 56.0% but already well-calibrated for known phrases
    "mma":     0.45,  # Actual 28.6% + 17pp buffer (smaller sample, conservative)
    "leavitt": 0.40,  # Actual 30.6% + 9pp buffer (well-covered in corpus)
    "mlb":     0.35,  # Actual 15.4% + 20pp buffer (small sample but structural certainties)
    # Keep conservative defaults for speakers with thin outcome data
    "mamdani": 0.45, "powell": 0.45, "carney": 0.45, "starmer": 0.45, "homan": 0.45,
}


class BaseRateLookup:
    """Loads base_rates.yaml and provides (speaker, event_type, phrase) -> probability."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data or {}
        self._global_default = float(self._data.get("_global_default", _GLOBAL_DEFAULT))

    @classmethod
    def from_yaml(cls, path: Path) -> BaseRateLookup:
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed; using empty base rates")
            return cls()

        if not path.exists():
            logger.info("No base_rates file at %s; using defaults", path)
            return cls()

        with path.open("r") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            data = {}

        def _merge_yaml(target: dict, src_path: Path) -> None:
            """Merge src_path YAML entries into target dict (non-destructive for existing keys)."""
            if not src_path.exists():
                return
            try:
                with src_path.open("r") as f:
                    src_data = yaml.safe_load(f)
                if not isinstance(src_data, dict):
                    return
                for speaker, ctxs in src_data.items():
                    if speaker.startswith("_") or not isinstance(ctxs, dict):
                        continue
                    for ctx, phrases in ctxs.items():
                        if not isinstance(phrases, dict):
                            continue
                        target.setdefault(speaker, {}).setdefault(ctx, {}).update(phrases)
                logger.info("Merged base rates from %s", src_path)
            except Exception as exc:
                logger.warning("Could not load %s: %s", src_path, exc)

        # Layer 1: base_rates_priors.yaml — manually curated priors for new speakers
        # (powell, carney, starmer, homan).  Never overwritten by calibrate_base_rates.
        # Added first so that corpus-derived and empirical rates can override them.
        _merge_yaml(data, path.parent / "base_rates_priors.yaml")

        # Layer 2: auto-generated empirical rates from resolved Kalshi outcomes.
        # base_rates_auto.yaml is written by scripts/compute_base_rates.py and
        # provides Bayesian-smoothed rates derived from resolved Kalshi outcomes.
        # Per-phrase empirical rates override manual guesses where N >= 8.
        _merge_yaml(data, path.parent / "base_rates_auto.yaml")

        logger.info("Loaded base rates from %s", path)
        return cls(data)

    def get(self, speaker: str, event_type: str, phrase: str) -> float:
        phrase_lower = phrase.lower()
        speaker_data = self._data.get(speaker)
        if not isinstance(speaker_data, dict):
            return self._context_prior(None, event_type, speaker)

        # 1. Exact match: speaker.event_type.phrase
        event_data = speaker_data.get(event_type)
        if isinstance(event_data, dict) and phrase_lower in event_data:
            return float(event_data[phrase_lower])

        # 2. "general" bucket — populated from resolved Kalshi market outcomes.
        #    Ground-truth rate across all real speeches; takes priority over the
        #    corpus-derived _default when event type is unknown or phrase isn't
        #    in the specific event-type block.
        if event_type != "general":
            general_data = speaker_data.get("general")
            if isinstance(general_data, dict) and phrase_lower in general_data:
                return float(general_data[phrase_lower])

        # 3. Speaker default: speaker._default.phrase (corpus-derived blended rate)
        default_data = speaker_data.get("_default")
        if isinstance(default_data, dict) and phrase_lower in default_data:
            return float(default_data[phrase_lower])

        # 4. Per-speaker per-context empirical prior — from _context_base block.
        #    E.g. trump.rally averages 57% YES; trump.signing averages 35% YES.
        #    Far more accurate than the flat global default for unknown phrases.
        return self._context_prior(speaker_data, event_type, speaker)

    def _context_prior(self, speaker_data: dict | None, event_type: str, speaker: str = "") -> float:
        """Return the empirical per-context base rate for an unknown phrase.

        Falls back through:
          speaker._context_base.event_type  (empirical from resolved outcomes)
          speaker._context_base.general     (overall speaker average)  
          speaker._global_default           (speaker-specific from YAML)
          _SPEAKER_DEFAULTS[speaker]        (speaker-specific from code)
          _global_default                   (final fallback)
        """
        if isinstance(speaker_data, dict):
            cb = speaker_data.get("_context_base")
            if isinstance(cb, dict):
                # Try the specific context first
                if event_type in cb:
                    return float(cb[event_type])
                # Fall back to general empirical average for this speaker
                if "general" in cb:
                    return float(cb["general"])
            
            # NEW: Check for speaker-specific _global_default in YAML
            speaker_global = speaker_data.get("_global_default")
            if speaker_global is not None:
                return float(speaker_global)
        
        # NEW: Speaker-specific empirical default from code (backup)
        speaker_key = (speaker or "").lower().strip()
        if speaker_key in _SPEAKER_DEFAULTS:
            return _SPEAKER_DEFAULTS[speaker_key]
            
        return self._global_default
