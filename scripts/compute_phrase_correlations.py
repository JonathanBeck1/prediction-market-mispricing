#!/usr/bin/env python3
"""Compute cross-phrase Phi correlation matrix from historical outcomes.

Identifies which phrase pairs tend to co-occur (both YES) or anti-correlate
within the same event. Used for portfolio concentration risk and Kelly haircuts.

Output: data/phrase_correlations.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from app.phrase_correlation import compute_and_save

if __name__ == "__main__":
    compute_and_save()
