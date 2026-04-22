#!/usr/bin/env python3
"""Fetch recently settled Kalshi mention-market outcomes for same-event boosting.

When KXTRUMPSAY-26MAR16-WIND settles YES at 2:38 PM, we know Trump is actively
speaking and that windmill was just said.  We can use this as a live signal to:
  1. Confirm the event is in progress (boost ALL open markets in the event slightly)
  2. Boost correlated phrases that often co-occur with the settled phrase
  3. Apply a recency bonus — unsettled markets in the same event are about to resolve

Output: data/live_settlements.json
  {
    "fetched_at": "2026-03-11T14:45:00Z",
    "events": {
      "KXTRUMPSAY-26MAR16": {
        "yes": ["windmill", "transgender", "predict"],
        "no": [],
        "settled_count": 3,
        "latest_settlement": "2026-03-11T14:39:11Z",
        "speaker": "trump"
      }
    }
  }

Designed to run every 2-5 minutes via MaintenanceRunner while events are live.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent.parent))

OUTPUT_PATH = Path("data/live_settlements.json")
API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Series to monitor for early settlements.  Run only these to keep it fast.
SETTLEMENT_SERIES = [
    "KXTRUMPSAY",
    "KXTRUMPSAYEP",
    "KXTRUMPMENTION",
    "KXTRUMPMENTIONB",
    "KXPRESMENTION",
    "KXLEAVITTMENTION",
    "KXSECPRESSMENTION",
    "KXMAMDANIMENTION",
    "KXTRUMPSAYNICKNAME",
    "KXTRUMPSAYMONTH",
]

# Look back at most 12 hours for recent settlements
LOOKBACK_HOURS = 12

SPEAKER_BY_SERIES = {
    "KXTRUMPSAY": "trump", "KXTRUMPSAYEP": "trump",
    "KXTRUMPMENTION": "trump", "KXTRUMPMENTIONB": "trump",
    "KXPRESMENTION": "trump", "KXTRUMPSAYNICKNAME": "trump",
    "KXTRUMPSAYMONTH": "trump",
    "KXLEAVITTMENTION": "leavitt",
    "KXSECPRESSMENTION": "leavitt",
    "KXMAMDANIMENTION": "mamdani",
}


def _fetch(url: str) -> dict:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-edge/1.0"})
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"  WARN {url}: {exc}")
        return {}


def _phrase_from_market(m: dict) -> str:
    phrase = m.get("yes_sub_title", "") or m.get("title", "") or ""
    return phrase.strip().lower()


def fetch_settlements() -> dict:
    """Fetch all recently settled markets and group by event_ticker."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    events: dict[str, dict] = {}

    for series in SETTLEMENT_SERIES:
        speaker = SPEAKER_BY_SERIES.get(series, "auto")
        d = _fetch(f"{API_BASE}/markets?series_ticker={series}&status=settled&limit=100")
        for m in d.get("markets", []):
            close_time_str = m.get("close_time", "") or ""
            if not close_time_str:
                continue
            # Parse the close time
            try:
                ct_str = close_time_str[:19].replace("T", " ")
                close_dt = datetime.strptime(ct_str, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if close_dt < cutoff:
                continue  # Too old — skip

            event_ticker = m.get("event_ticker", "") or m.get("ticker", "").rsplit("-", 1)[0]
            if not event_ticker:
                continue

            result = (m.get("result", "") or "").lower()
            if result not in ("yes", "no"):
                continue

            phrase = _phrase_from_market(m)
            if not phrase:
                continue

            if event_ticker not in events:
                events[event_ticker] = {
                    "yes": [],
                    "no": [],
                    "settled_count": 0,
                    "latest_settlement": close_time_str,
                    "speaker": speaker,
                    "series": series,
                }
            ev = events[event_ticker]
            ev[result].append(phrase)
            ev["settled_count"] += 1
            if close_time_str > ev["latest_settlement"]:
                ev["latest_settlement"] = close_time_str

        time.sleep(0.1)

    return events


def main() -> None:
    events = fetch_settlements()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": LOOKBACK_HOURS,
        "events": events,
    }
    tmp = OUTPUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(output, indent=2))
    tmp.replace(OUTPUT_PATH)

    total_settled = sum(e["settled_count"] for e in events.values())
    print(f"fetch_settlements: {len(events)} active events, {total_settled} recent settlements")
    for et, ev in sorted(events.items(), key=lambda x: -x[1]["settled_count"]):
        yes_phrases = ev["yes"][:5]
        no_phrases = ev["no"][:3]
        latest = ev["latest_settlement"][:16]
        print(
            f"  {et}: YES={yes_phrases} NO={no_phrases[:3]} "
            f"total={ev['settled_count']} latest={latest}"
        )


if __name__ == "__main__":
    main()
