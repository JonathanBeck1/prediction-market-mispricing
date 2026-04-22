#!/usr/bin/env python3
"""Compute price velocity (momentum) for active Kalshi mention markets.

Price velocity detects smart-money / crowd movement BEFORE the scoring engine
has any fundamental reason to adjust p_literal.  When a YES price moves from
0.30 → 0.55 in 2 hours with no news, that is likely informed buying.

Lookback windows:
  2h:  fastest signal — catches same-session insider activity
  6h:  medium signal — captures same-day trend changes
  24h: slow signal  — identifies multi-day drift (e.g. event cancelled)

Signal thresholds:
  delta_2h  > +0.06  → SMART_MONEY_UP    (strong buying pressure)
  delta_2h  < -0.06  → SMART_MONEY_DOWN  (strong selling / collapse)
  delta_6h  > +0.10  → SMART_MONEY_UP    (sustained buying)
  delta_6h  < -0.10  → SMART_MONEY_DOWN  (sustained selling)
  delta_24h > +0.15  → SMART_MONEY_UP    (multi-day drift up)
  delta_24h < -0.15  → SMART_MONEY_DOWN  (multi-day drift down)

Output: data/price_velocity.json
  {
    "generated_at": "2026-03-16T03:00:00Z",
    "markets": {
      "KXTRUMPSAY-26MAR16-THUG": {
        "current_yes_ask": 0.681,
        "delta_2h": +0.048,
        "delta_6h": -0.022,
        "delta_24h": null,
        "signal": "SMART_MONEY_UP",
        "signal_strength": 0.40,
        "signal_window": "2h"
      }
    }
  }

Runs every 5 minutes via MaintenanceRunner so velocity is always fresh.

IMPORTANT: timestamps in market_snapshots are stored as "2026-03-16T02:54:48+00:00".
All cutoff strings must also include "+00:00" so SQLite's index on (market_id, ts)
is used for lookups instead of a full table scan.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect as _db_connect  # noqa: E402

DB_PATH     = Path("data/edge.db")
OUTPUT_PATH = Path("data/price_velocity.json")

# Lookback windows: (hours, threshold_up, threshold_down, label)
WINDOWS: list[tuple[float, float, float, str]] = [
    (2.0,   0.06,  0.06,  "2h"),
    (6.0,   0.10,  0.10,  "6h"),
    (24.0,  0.15,  0.15,  "24h"),
]

MIN_PRICE        = 0.05   # ignore penny markets
ACTIVE_WINDOW_H  = 0.5    # market must have a snapshot in last 30 min to be "active"


def _cutoff(hours: float, now: datetime) -> str:
    """ISO timestamp with +00:00 suffix — matches stored format, allows index use."""
    return (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _load_open_market_ids(now: datetime) -> set[str]:
    """Load currently open (not yet expired) market IDs from kalshi_markets.json."""
    cache = Path("data/kalshi_markets.json")
    if not cache.exists():
        return set()
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        result = set()
        for m in data.get("markets", []):
            ticker = m.get("ticker")
            if not ticker:
                continue
            # Only include markets that haven't closed yet (or have no close_time)
            close_time = m.get("close_time", "")
            if close_time:
                try:
                    ct = datetime.strptime(
                        close_time[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                    if ct < now:
                        continue  # already expired — skip
                except ValueError:
                    pass
            result.add(ticker)
        return result
    except Exception:
        return set()


def compute_velocity(conn: sqlite3.Connection) -> dict[str, dict]:
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)

    active_cutoff = _cutoff(ACTIVE_WINDOW_H, now)

    # Only score markets that are currently open (from kalshi_markets.json cache)
    open_markets = _load_open_market_ids(now)

    # Get the latest price for every active market in one query
    current_rows = conn.execute("""
        WITH latest AS (
            SELECT market_id, MAX(ts) AS max_ts
            FROM market_snapshots
            WHERE market_id LIKE 'KX%'
              AND yes_ask >= ?
              AND ts >= ?
            GROUP BY market_id
        )
        SELECT s.market_id, s.yes_ask, s.yes_bid, s.ts
        FROM market_snapshots s
        JOIN latest l ON s.market_id = l.market_id AND s.ts = l.max_ts
    """, (MIN_PRICE, active_cutoff)).fetchall()

    # Filter to only currently open markets (exclude ended/settled events)
    if open_markets:
        current_rows = [r for r in current_rows if r["market_id"] in open_markets]

    if not current_rows:
        print("No active open markets with recent snapshots.")
        return {}

    print(f"Active markets: {len(current_rows)}")

    results: dict[str, dict] = {}

    for row in current_rows:
        market_id     = row["market_id"]
        current_price = float(row["yes_ask"])

        entry: dict = {
            "current_yes_ask": round(current_price, 4),
            "current_ts": row["ts"][:19],
            "signal": "NEUTRAL",
            "signal_strength": 0.0,
            "signal_window": None,
        }

        has_any_delta = False

        for hours, thresh_up, thresh_down, label in WINDOWS:
            cutoff = _cutoff(hours, now)

            hist = conn.execute("""
                SELECT yes_ask, ts FROM market_snapshots
                WHERE market_id = ?
                  AND yes_ask >= ?
                  AND ts <= ?
                ORDER BY ts DESC LIMIT 1
            """, (market_id, MIN_PRICE, cutoff)).fetchone()

            if not hist:
                entry[f"delta_{label}"] = None
                continue

            try:
                past_price = float(hist["yes_ask"])
            except (ValueError, TypeError):
                entry[f"delta_{label}"] = None
                continue
            delta = round(current_price - past_price, 4)
            entry[f"delta_{label}"] = delta

            # Record actual lookback duration
            try:
                past_dt = datetime.strptime(
                    hist["ts"][:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                actual_h = round((now - past_dt).total_seconds() / 3600, 2)
                entry[f"lookback_{label}_actual_h"] = actual_h
            except ValueError:
                actual_h = hours

            has_any_delta = True

            # Quality gates for signal classification:
            # 1. Data must be within 2× of requested window (no stale comparison)
            # 2. Past price must be >= 0.35 — avoids flagging natural decay from
            #    already-low prices or nearly-expired markets moving toward 0
            if actual_h > hours * 2:
                continue
            if past_price < 0.35:
                continue  # started too low — not meaningful smart money signal

            # Upgrade signal if this window shows a stronger move
            current_strength = entry["signal_strength"]
            if delta >= thresh_up:
                strength = min(1.0, delta / (thresh_up * 2))
                if strength > current_strength:
                    entry["signal"]          = "SMART_MONEY_UP"
                    entry["signal_strength"] = round(strength, 3)
                    entry["signal_window"]   = label
            elif delta <= -thresh_down:
                # Additional gate for DOWN signals: price must have started
                # above 0.50 — if it was already near the middle, a drop toward
                # 0.30 is natural time decay, not smart money.
                if past_price >= 0.50:
                    strength = min(1.0, abs(delta) / (thresh_down * 2))
                    if strength > current_strength:
                        entry["signal"]          = "SMART_MONEY_DOWN"
                        entry["signal_strength"] = round(strength, 3)
                        entry["signal_window"]   = label

        if has_any_delta:
            results[market_id] = entry

    return results


def print_summary(markets: dict[str, dict]) -> None:
    up   = {k: v for k, v in markets.items() if v.get("signal") == "SMART_MONEY_UP"}
    down = {k: v for k, v in markets.items() if v.get("signal") == "SMART_MONEY_DOWN"}
    neut = len(markets) - len(up) - len(down)
    print(f"Velocity: {len(up)} UP  {len(down)} DOWN  {neut} NEUTRAL  ({len(markets)} total)")

    if up:
        print("\nSMART_MONEY_UP (rising):")
        for mid, v in sorted(up.items(), key=lambda x: -x[1]["signal_strength"])[:10]:
            d = v.get("delta_2h") or v.get("delta_6h") or 0
            print(f"  {mid:<48} cur={v['current_yes_ask']:.3f}  Δ{v['signal_window']}={d:+.3f}  str={v['signal_strength']:.2f}")

    if down:
        print("\nSMART_MONEY_DOWN (falling):")
        for mid, v in sorted(down.items(), key=lambda x: -x[1]["signal_strength"])[:10]:
            d = v.get("delta_2h") or v.get("delta_6h") or 0
            print(f"  {mid:<48} cur={v['current_yes_ask']:.3f}  Δ{v['signal_window']}={d:+.3f}  str={v['signal_strength']:.2f}")


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        sys.exit(1)

    conn = _db_connect(DB_PATH)
    markets = compute_velocity(conn)
    conn.close()

    print_summary(markets)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_count": len(markets),
        "markets": markets,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(output, indent=2))
    tmp.replace(OUTPUT_PATH)
    print(f"\nWrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
