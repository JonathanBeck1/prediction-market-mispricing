from __future__ import annotations

import datetime
import json
import tempfile
from pathlib import Path

from app import dashboard
from app.db import init_db


def _insert_market(conn, market_id: str, subject: str = "trump") -> None:
    conn.execute(
        """
        INSERT INTO markets (market_id, slug, subject, prompt)
        VALUES (?, ?, ?, ?)
        """,
        (market_id, market_id.lower(), subject, "test prompt"),
    )


def _insert_snapshot(
    conn,
    market_id: str,
    *,
    title: str,
    event_ticker: str,
    status: str = "active",
    yes_ask: str = "0.41",
    no_ask: str = "0.59",
    yes_sub_title: str = "",
) -> None:
    raw_api = {
        "ticker": market_id,
        "title": title,
        "event_ticker": event_ticker,
        "status": status,
        "yes_ask_dollars": yes_ask,
        "no_ask_dollars": no_ask,
        "yes_sub_title": yes_sub_title,
    }
    now_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "ts": now_ts,
        "market_id": market_id,
        "yes_bid": 0.40,
        "yes_ask": float(yes_ask),
        "no_bid": 0.58,
        "no_ask": float(no_ask),
        "spread": 0.01,
        "depth_yes": 100.0,
        "depth_no": 100.0,
        "volume_1h": 100.0,
        "raw_api": raw_api,
    }
    conn.execute(
        """
        INSERT INTO market_snapshots (
            ts, market_id, yes_bid, yes_ask, no_bid, no_ask,
            spread, depth_yes, depth_no, volume_1h, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_ts,
            market_id,
            payload["yes_bid"],
            payload["yes_ask"],
            payload["no_bid"],
            payload["no_ask"],
            payload["spread"],
            payload["depth_yes"],
            payload["depth_no"],
            payload["volume_1h"],
            json.dumps(payload),
        ),
    )


def test_coverage_flags_untracked_non_phrase_market() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dashboard.EVENTS_CACHE = Path(tmp) / "kalshi_events.json"
        conn = init_db(Path(tmp) / "edge.db")
        mid = "KXDJTCONF-26MAR09-TEST"
        _insert_market(conn, mid, subject="trump")
        _insert_snapshot(
            conn,
            mid,
            title="Trump press conference market",
            event_ticker="KXDJTCONF-26MAR09",
            status="active",
            yes_sub_title="",
        )
        conn.commit()

        coverage = dashboard._query_coverage(conn, market_cache={}, all_cards=[])
        assert coverage["open_event_count"] == 1
        assert coverage["untracked_event_count"] == 1
        evt = coverage["events"][0]
        assert evt["has_scored"] is False
        assert "NO_ACTION_CARD" in evt["reason_codes"]
        assert "NOT_IN_CACHE" in evt["reason_codes"]
        assert "NON_PHRASE_MARKET" in evt["reason_codes"]
        conn.close()


def test_coverage_marks_no_poly_when_scored_without_poly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dashboard.EVENTS_CACHE = Path(tmp) / "kalshi_events.json"
        conn = init_db(Path(tmp) / "edge.db")
        mid = "KXTRUMPSAY-26MAR16-TEST"
        _insert_market(conn, mid, subject="trump")
        _insert_snapshot(
            conn,
            mid,
            title='Will Trump say "Tariff" before Mar 16, 2026?',
            event_ticker="KXTRUMPSAY-26MAR16",
            status="active",
            yes_sub_title="Tariff",
        )
        conn.commit()

        all_cards = [{
            "market_id": mid,
            "poly_yes": None,
        }]
        market_cache = {
            mid: {
                "ticker": mid,
                "event_ticker": "KXTRUMPSAY-26MAR16",
                "is_phrase_market": True,
                "primary_phrase": "Tariff",
            }
        }
        coverage = dashboard._query_coverage(conn, market_cache=market_cache, all_cards=all_cards)
        assert coverage["open_event_count"] == 1
        assert coverage["tracked_event_count"] == 1
        evt = coverage["events"][0]
        assert evt["has_scored"] is True
        assert "NO_POLY_MATCH" in evt["reason_codes"]
        conn.close()


def test_coverage_includes_events_cache_when_no_snapshots() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        events_path = Path(tmp) / "kalshi_events.json"
        events_path.write_text(
            json.dumps(
                {
                    "fetched_at": "2026-03-10T17:00:00Z",
                    "events": [
                        {
                            "event_ticker": "KXLEAVITT-26MAR10",
                            "title": "What will Karoline Leavitt say in the next press briefing?",
                            "speaker": "leavitt",
                            "status": "open",
                            "open_markets": 3,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        dashboard.EVENTS_CACHE = events_path
        conn = init_db(Path(tmp) / "edge.db")
        coverage = dashboard._query_coverage(conn, market_cache={}, all_cards=[])
        assert coverage["open_event_count"] == 1
        assert coverage["untracked_event_count"] == 1
        evt = coverage["events"][0]
        assert evt["event_ticker"] == "KXLEAVITT-26MAR10"
        assert "EVENT_DISCOVERED_NO_SNAPSHOTS" in evt["reason_codes"]
        assert "NO_ACTION_CARD" in evt["reason_codes"]
        conn.close()


def test_coverage_keeps_unresolved_speaker_visible() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dashboard.EVENTS_CACHE = Path(tmp) / "kalshi_events.json"
        conn = init_db(Path(tmp) / "edge.db")
        mid = "KXUNK-26MAR16-TEST"
        _insert_market(conn, mid, subject="unknown")
        _insert_snapshot(
            conn,
            mid,
            title='Will spokesperson say "Emergency" at next statement?',
            event_ticker="KXUNK-26MAR16",
            status="active",
            yes_sub_title="Emergency",
        )
        conn.commit()

        coverage = dashboard._query_coverage(conn, market_cache={}, all_cards=[])
        assert coverage["open_event_count"] == 1
        assert coverage["filtered_unknown_speaker_market_count"] == 1
        evt = coverage["events"][0]
        assert evt["speaker"] == "auto"
        assert "SPEAKER_UNRESOLVED" in evt["reason_codes"]
        conn.close()
