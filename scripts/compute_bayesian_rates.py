#!/usr/bin/env python3
"""Compute Bayesian base rates with uncertainty intervals.

Builds Beta-Bernoulli posteriors from historical outcomes for every
(speaker, phrase) pair. Provides both point estimates AND confidence bands.

Output: data/bayesian_rates.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from app.bayesian_rates import compute_and_save

if __name__ == "__main__":
    compute_and_save()
