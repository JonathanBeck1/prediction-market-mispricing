#!/usr/bin/env python3
"""Compute empirical hazard rates from corpus transcripts.

Builds h(t, phrase) = probability density of phrase being said at time t
given it hasn't been said yet. Replaces the static exponential decay in 
scoring.py with phrase-specific hazard functions derived from real speech patterns.

Analysis:
1. For each transcript, find first mention position for each phrase
2. Convert to fraction of transcript duration (proxy for speech time)  
3. Bin into 10% buckets and compute empirical hazard rates
4. Smooth to avoid sparse-data noise

Reads:  data/edge.db (transcripts, phrase_hits)
Writes: data/phrase_hazard_rates.json
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect as _db_connect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / "data" / "edge.db"
OUTPUT_PATH = REPO_ROOT / "data" / "phrase_hazard_rates.json"

# Hazard computation parameters
N_BUCKETS = 10
MIN_EVENTS = 3
SMOOTHING_WEIGHT = 0.3

# Default event durations by type (seconds)
DURATIONS = {
    "briefing": 2700, "rally": 5400, "interview": 3600, "presser": 3600,
    "remarks": 3600, "address": 3600, "signing": 1800, "announcement": 2700,
    "general": 3600,
}


def _parse_source_ref(source_ref: str) -> tuple[str, str, str]:
    """Parse corpus:speaker/event_type_YYYY-MM-DD_01.txt -> (speaker, event_type, date)."""
    if not source_ref or ":" not in source_ref:
        return "", "", ""
    
    path = source_ref.split(":", 1)[-1]  # Remove "corpus:" prefix
    if "/" not in path:
        return "", "", ""
        
    speaker = path.split("/")[0]
    filename = path.split("/")[-1]
    
    # Parse event_type_2026-03-30_01.txt
    match = re.match(r"(\w+)_(\d{4}-\d{2}-\d{2})_\d+\.txt", filename)
    if match:
        event_type, date = match.groups()
        return speaker, event_type, date
    
    return speaker, "general", ""


def _compute_hazard_rates(events: list[dict]) -> list[float]:
    """Compute 10-bucket hazard rates from event first-mention fractions."""
    if len(events) < MIN_EVENTS:
        # Fallback to uniform hazard (similar to current static decay)
        return [0.12] * N_BUCKETS
    
    bucket_size = 1.0 / N_BUCKETS
    mentions = [0] * N_BUCKETS
    survivors = [0] * N_BUCKETS
    
    for event in events:
        frac = event.get("first_mention_frac")
        if frac is None:
            # Never mentioned - survives all buckets
            for i in range(N_BUCKETS):
                survivors[i] += 1
        else:
            # Mentioned - survives buckets until mention
            bucket = min(int(frac / bucket_size), N_BUCKETS - 1)
            for i in range(bucket):
                survivors[i] += 1
            mentions[bucket] += 1
            survivors[bucket] += 1
    
    # Compute hazard rates with neighbor smoothing
    rates = []
    for i in range(N_BUCKETS):
        if survivors[i] > 0:
            rate = mentions[i] / survivors[i]
        else:
            rate = 0.1
        
        # Smooth with neighbors
        if i > 0 and survivors[i-1] > 0:
            prev_rate = mentions[i-1] / survivors[i-1]
            rate = (1-SMOOTHING_WEIGHT) * rate + SMOOTHING_WEIGHT * prev_rate
            
        rates.append(round(rate, 4))
    
    return rates


def run() -> None:
    conn = _db_connect(DB_PATH)
    
    # Get first mention index per transcript per phrase
    query = """
    SELECT t.id, t.source_ref, ph.phrase, MIN(ph.start_idx) as first_idx, LENGTH(t.text) as text_len
    FROM transcripts t
    JOIN phrase_hits ph ON ph.transcript_id = t.id
    GROUP BY t.id, ph.phrase
    """
    
    mention_data = conn.execute(query).fetchall()
    
    # Build phrase events grouped by (speaker, event_type, phrase)
    events_by_phrase = defaultdict(list)
    
    for transcript_id, source_ref, phrase, first_idx, text_len in mention_data:
        speaker, event_type, date = _parse_source_ref(source_ref or "")
        if not speaker:
            continue
        
        frac = first_idx / text_len if text_len > 0 else 0.0
        duration = DURATIONS.get(event_type, 3600)
        
        events_by_phrase[(speaker, event_type, phrase)].append({
            "transcript_id": transcript_id,
            "first_mention_frac": frac,
            "duration_sec": duration,
            "date": date,
        })
    
    conn.close()
    
    # Compute hazard rates
    hazard_data = {}
    total_computed = 0
    
    for (speaker, event_type, phrase), events in events_by_phrase.items():
        if len(events) >= MIN_EVENTS:
            rates = _compute_hazard_rates(events)
            key = f"{speaker}.{event_type}.{phrase.lower()}"
            hazard_data[key] = {
                "buckets": rates,
                "n_events": len(events),
                "speaker": speaker,
                "event_type": event_type,
                "phrase": phrase,
            }
            total_computed += 1
    
    # Save results
    output = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "n_buckets": N_BUCKETS,
        "bucket_size": 1.0 / N_BUCKETS,
        "min_events": MIN_EVENTS,
        "total_phrases": total_computed,
        "phrases": hazard_data,
    }
    
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    logger.info("Computed hazard rates: %d phrases with ≥%d events each", total_computed, MIN_EVENTS)
    
    # Show top phrases by event count
    by_count = sorted(events_by_phrase.items(), key=lambda x: len(x[1]), reverse=True)
    logger.info("Top phrases by event count:")
    for (speaker, event_type, phrase), events in by_count[:10]:
        logger.info("  %s.%s.%s: %d events", speaker, event_type, phrase, len(events))


if __name__ == "__main__":
    run()