from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.market_catalog import MOCK_MARKETS
from app.utils import append_jsonl, utc_now_iso

logger = logging.getLogger(__name__)


@dataclass
class MockKalshiWatcher:
    conn: sqlite3.Connection
    raw_log_path: Path
    interval_sec: float
    mock_enabled: bool
    _tick: int = 0

    def bootstrap_markets(self) -> None:
        if not self.mock_enabled:
            raise RuntimeError("Only mock mode is implemented in M0. Set KALSHI_MOCK=1.")

        self.conn.executemany(
            """
            INSERT OR IGNORE INTO markets (market_id, slug, subject, prompt)
            VALUES (:market_id, :slug, :subject, :prompt)
            """,
            MOCK_MARKETS,
        )
        self.conn.commit()
        logger.info("Bootstrapped %s mock markets", len(MOCK_MARKETS))

    def _build_snapshot(self, market_id: str) -> dict[str, float]:
        bucket = int(datetime.utcnow().strftime("%H"))  # deterministic hourly bucket
        seed = (sum(ord(c) for c in market_id) + self._tick + bucket) % 100
        center = 0.25 + (seed / 100.0) * 0.55
        spread = 0.01 + ((seed % 5) * 0.0025)
        yes_ask = round(min(0.99, center + spread / 2), 4)
        yes_bid = round(max(0.01, center - spread / 2), 4)
        no_bid = round(max(0.01, 1.0 - yes_ask), 4)
        no_ask = round(min(0.99, 1.0 - yes_bid), 4)
        depth_yes = round(150 + (seed * 13) % 600, 2)
        depth_no = round(140 + (seed * 11) % 580, 2)
        volume_1h = round(250 + (seed * 17) % 2000, 2)
        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "spread": round(yes_ask - yes_bid, 4),
            "depth_yes": depth_yes,
            "depth_no": depth_no,
            "volume_1h": volume_1h,
        }

    def run_once(self) -> int:
        rows = self.conn.execute("SELECT market_id FROM markets ORDER BY market_id").fetchall()
        ts = utc_now_iso()
        inserted = 0

        for row in rows:
            market_id = row["market_id"]
            snap = self._build_snapshot(market_id)
            raw_payload = {"ts": ts, "market_id": market_id, **snap}
            self.conn.execute(
                """
                INSERT INTO market_snapshots (
                    ts, market_id, yes_bid, yes_ask, no_bid, no_ask, spread, depth_yes, depth_no, volume_1h, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    market_id,
                    snap["yes_bid"],
                    snap["yes_ask"],
                    snap["no_bid"],
                    snap["no_ask"],
                    snap["spread"],
                    snap["depth_yes"],
                    snap["depth_no"],
                    snap["volume_1h"],
                    json.dumps(raw_payload, ensure_ascii=True),
                ),
            )
            append_jsonl(self.raw_log_path, raw_payload)
            inserted += 1

        self.conn.commit()
        self._tick += 1
        logger.info("Watcher inserted %s market snapshots at %s", inserted, ts)
        return inserted

