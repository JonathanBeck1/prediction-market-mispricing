"""Compute rolling phrase YES-rate trends for VOCAB_TRENDING_UP/DOWN signals.

Reads kalshi_outcomes.json and computes rolling 30/60/90-day YES rates per
(speaker, phrase). Flags when the 30-day rate diverges from the 90-day rate
by >15 percentage points, indicating a meaningful vocab shift.

Output: data/phrase_trends.json
Signal injected: VOCAB_TRENDING_UP (×1.10) / VOCAB_TRENDING_DOWN (×0.90)
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parent.parent
_OUTCOMES_PATH = _REPO / "data" / "kalshi_outcomes.json"
_OUTPUT_PATH   = _REPO / "data" / "phrase_trends.json"

# Minimum resolved markets in a window before we trust the rate
MIN_N_SHORT = 3   # 30-day window
MIN_N_LONG  = 8   # 90-day window

# Flag trend when |rate_30d - rate_90d| > this threshold
TREND_THRESHOLD = 0.15


def _parse_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # Handle both "2026-03-25T16:44:20.835725Z" and "2026-03-25"
        ts_clean = ts.replace("Z", "+00:00")
        if "T" in ts_clean:
            return datetime.fromisoformat(ts_clean)
        return datetime.fromisoformat(ts_clean + "T00:00:00+00:00")
    except ValueError:
        return None


def _load_outcomes() -> list[dict]:
    if not _OUTCOMES_PATH.exists():
        logger.warning("kalshi_outcomes.json not found at %s", _OUTCOMES_PATH)
        return []
    try:
        data = json.loads(_OUTCOMES_PATH.read_text(encoding="utf-8"))
        return data.get("markets", []) if isinstance(data, dict) else data
    except Exception as exc:
        logger.error("Failed to load outcomes: %s", exc)
        return []


def compute_trends() -> dict:
    outcomes = _load_outcomes()
    if not outcomes:
        return {}

    now = datetime.now(timezone.utc)
    cutoffs = {
        "30d":  now - timedelta(days=30),
        "60d":  now - timedelta(days=60),
        "90d":  now - timedelta(days=90),
        "180d": now - timedelta(days=180),
    }

    # Bucket outcomes by (speaker, phrase) with timestamps
    # key -> list of (settlement_dt, result)
    hits: dict[tuple[str, str], list[tuple[datetime, str]]] = defaultdict(list)

    for m in outcomes:
        result = m.get("result", "")
        if result not in ("yes", "no"):
            continue
        phrase = (m.get("primary_phrase") or "").lower().strip()
        speaker = (m.get("speaker") or "unknown").lower().strip()
        if not phrase:
            continue
        dt = _parse_dt(m.get("settlement_ts") or m.get("close_time"))
        if dt is None:
            continue
        hits[(speaker, phrase)].append((dt, result))

    trends: dict[str, dict] = {}

    for (speaker, phrase), records in hits.items():
        # Sort by date ascending
        records.sort(key=lambda x: x[0])

        # Compute YES rate per window
        rates: dict[str, float | None] = {}
        counts: dict[str, int] = {}
        for label, cutoff in cutoffs.items():
            window = [(dt, r) for dt, r in records if dt >= cutoff]
            n = len(window)
            counts[label] = n
            if n > 0:
                yes_n = sum(1 for _, r in window if r == "yes")
                rates[label] = round(yes_n / n, 4)
            else:
                rates[label] = None

        # All-time rate
        all_n = len(records)
        all_yes = sum(1 for _, r in records if r == "yes")
        rate_all = round(all_yes / all_n, 4) if all_n else None

        # Determine trend flag
        r30 = rates.get("30d")
        r90 = rates.get("90d")
        trend_flag = None
        trend_delta = None

        if (
            r30 is not None
            and r90 is not None
            and counts["30d"] >= MIN_N_SHORT
            and counts["90d"] >= MIN_N_LONG
        ):
            delta = r30 - r90
            trend_delta = round(delta, 4)
            if delta >= TREND_THRESHOLD:
                trend_flag = "VOCAB_TRENDING_UP"
            elif delta <= -TREND_THRESHOLD:
                trend_flag = "VOCAB_TRENDING_DOWN"

        # Only store entries with meaningful data
        if all_n < 3:
            continue

        key = f"{speaker}:{phrase}"
        trends[key] = {
            "speaker":     speaker,
            "phrase":      phrase,
            "n_total":     all_n,
            "rate_all":    rate_all,
            "rates":       rates,
            "counts":      counts,
            "trend_flag":  trend_flag,
            "trend_delta": trend_delta,
            "last_seen":   records[-1][0].isoformat(),
        }

    # Summary stats
    up_count   = sum(1 for v in trends.values() if v["trend_flag"] == "VOCAB_TRENDING_UP")
    down_count = sum(1 for v in trends.values() if v["trend_flag"] == "VOCAB_TRENDING_DOWN")

    logger.info(
        "Phrase trends: %d phrases tracked, %d trending UP, %d trending DOWN",
        len(trends), up_count, down_count,
    )

    return {
        "computed_at": now.isoformat(),
        "total_phrases": len(trends),
        "trending_up":   up_count,
        "trending_down": down_count,
        "threshold":     TREND_THRESHOLD,
        "trends":        trends,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("Computing phrase vocabulary trends …")
    result = compute_trends()
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    logger.info(
        "Saved phrase_trends.json — %d phrases, %d up, %d down",
        result.get("total_phrases", 0),
        result.get("trending_up", 0),
        result.get("trending_down", 0),
    )

    # Print top trending phrases
    trends = result.get("trends", {})
    up   = [(k, v) for k, v in trends.items() if v["trend_flag"] == "VOCAB_TRENDING_UP"]
    down = [(k, v) for k, v in trends.items() if v["trend_flag"] == "VOCAB_TRENDING_DOWN"]
    up.sort(key=lambda x: -(x[1]["trend_delta"] or 0))
    down.sort(key=lambda x: (x[1]["trend_delta"] or 0))

    if up:
        print("\n=== TRENDING UP (30d > 90d by ≥15pp) ===")
        for key, v in up[:15]:
            r30 = v["rates"].get("30d", 0) or 0
            r90 = v["rates"].get("90d", 0) or 0
            print(f"  {v['phrase']:30s} [{v['speaker']:8s}] "
                  f"30d={r30:.0%} ({v['counts'].get('30d',0)}n)  "
                  f"90d={r90:.0%} ({v['counts'].get('90d',0)}n)  "
                  f"Δ={v['trend_delta']:+.2f}")
    if down:
        print("\n=== TRENDING DOWN (30d < 90d by ≥15pp) ===")
        for key, v in down[:15]:
            r30 = v["rates"].get("30d", 0) or 0
            r90 = v["rates"].get("90d", 0) or 0
            print(f"  {v['phrase']:30s} [{v['speaker']:8s}] "
                  f"30d={r30:.0%} ({v['counts'].get('30d',0)}n)  "
                  f"90d={r90:.0%} ({v['counts'].get('90d',0)}n)  "
                  f"Δ={v['trend_delta']:+.2f}")


if __name__ == "__main__":
    main()
