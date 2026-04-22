from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.watchdog import Watchdog


def _insert_snapshot(conn, ts: str) -> None:
    conn.execute(
        """
        INSERT INTO markets (market_id, slug, subject, prompt)
        VALUES ('MKT-TEST-1', 'mkt-test-1', 'trump', 'prompt')
        ON CONFLICT(market_id) DO NOTHING
        """
    )
    conn.execute(
        """
        INSERT INTO market_snapshots (
            ts, market_id, yes_bid, yes_ask, no_bid, no_ask,
            spread, depth_yes, depth_no, volume_1h, raw_json
        ) VALUES (?, 'MKT-TEST-1', 0.10, 0.12, 0.88, 0.90, 0.02, 10, 10, 0, '{}')
        """,
        (ts,),
    )
    conn.commit()


def _insert_card(conn, ts: str) -> None:
    conn.execute(
        """
        INSERT INTO action_cards (
            ts, market_id, phrase, side, p_literal,
            yes_ask, no_ask, ev_yes, ev_no,
            exec_price_hint, size_cap, spread_ok, depth_ok, gate_pass, rationale, raw_json
        ) VALUES (?, 'MKT-TEST-1', 'nato', 'WATCH', 0.10, 0.12, 0.90, -0.02, -0.02,
                  '', 0, 1, 1, 1, 'r', '{}')
        """,
        (ts,),
    )
    conn.commit()


def test_watchdog_does_not_restart_when_fresh(tmp_db):
    now = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
    _insert_snapshot(tmp_db, now)
    _insert_card(tmp_db, now)
    restarted = []
    wd = Watchdog(
        conn=tmp_db,
        startup_grace_sec=0,
        max_snapshot_age_sec=300,
        max_scorer_idle_sec=300,
        consecutive_breaches_to_restart=2,
        _restart=lambda reason: restarted.append(reason),
    )
    wd.run_once()
    assert restarted == []


def test_watchdog_restarts_after_consecutive_breaches(tmp_db):
    stale = (datetime.now(tz=timezone.utc) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
    _insert_snapshot(tmp_db, stale)
    _insert_card(tmp_db, stale)
    restarted = []
    wd = Watchdog(
        conn=tmp_db,
        startup_grace_sec=0,
        max_snapshot_age_sec=30,
        max_scorer_idle_sec=30,
        consecutive_breaches_to_restart=2,
        _restart=lambda reason: restarted.append(reason),
    )
    wd.run_once()
    assert restarted == []
    wd.run_once()
    assert restarted == ["watchdog stale data"]
