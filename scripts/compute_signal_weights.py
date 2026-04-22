#!/usr/bin/env python3
"""Compute adaptive signal weights from outcome data.

Scans outcome_reviews for resolved bets, groups by (speaker, signal, side),
computes win rates, and produces weights that scale signal impact.

Output: data/signal_weights.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from app.signal_learner import compute_and_save

if __name__ == "__main__":
    compute_and_save()
