from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.base_rates import BaseRateLookup
from app.event_detector import EventDetector
from app.scoring import ScoringEngine
from app.signals import SignalStore
from app.utils import utc_now_iso


def _insert_market(conn, market_id="MKT-TRUMP-001", subject="trump"):
    conn.execute(
        "INSERT OR IGNORE INTO markets (market_id, slug, subject, prompt) VALUES (?, ?, ?, ?)",
        (market_id, f"slug-{market_id}", subject, f"prompt for {market_id}"),
    )
    conn.commit()


def _insert_snapshot(conn, market_id="MKT-TRUMP-001", yes_ask=0.50, no_ask=0.50,
                     spread=0.02, depth_yes=300, depth_no=280):
    ts = utc_now_iso()
    conn.execute(
        """INSERT INTO market_snapshots
        (ts, market_id, yes_bid, yes_ask, no_bid, no_ask, spread, depth_yes, depth_no, volume_1h, raw_json)
        VALUES (?, ?, 0.45, ?, 0.48, ?, ?, ?, ?, 1000, '{}')""",
        (ts, market_id, yes_ask, no_ask, spread, depth_yes, depth_no),
    )
    conn.commit()


_THROTTLE_MARKET_PHRASES = {
    "MKT-TRUMP-001": ["nato", "n.a.t.o."],
}


def _make_scorer(conn, tmp_dir):
    return ScoringEngine(
        conn=conn,
        action_cards_path=tmp_dir / "cards.jsonl",
        event_detector=EventDetector(conn=conn),
        base_rates=BaseRateLookup(),
        signals=SignalStore(),
        cooldown_sec=120.0,
        material_ev_delta=0.03,
        market_phrases=_THROTTLE_MARKET_PHRASES,
    )


class TestMaterialChangeThrottling:
    def test_first_run_always_material(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)

        scorer = _make_scorer(tmp_db, tmp_dir)
        now = datetime.now(tz=timezone.utc)
        assert scorer._is_material_change("MKT-TRUMP-001", "WATCH", -0.4, 0.4, None, now)

    def test_same_state_not_material(self, tmp_db, tmp_dir):
        scorer = _make_scorer(tmp_db, tmp_dir)
        now = datetime.now(tz=timezone.utc)

        scorer._is_material_change("MKT-1", "BUY_NO", -0.4, 0.4, None, now)
        assert not scorer._is_material_change("MKT-1", "BUY_NO", -0.4, 0.4, None,
                                               now + timedelta(seconds=10))

    def test_side_flip_is_material(self, tmp_db, tmp_dir):
        scorer = _make_scorer(tmp_db, tmp_dir)
        now = datetime.now(tz=timezone.utc)

        scorer._is_material_change("MKT-1", "BUY_NO", -0.4, 0.4, None, now)
        assert scorer._is_material_change("MKT-1", "BUY_YES", 0.5, -0.1, None,
                                           now + timedelta(seconds=5))

    def test_state_change_is_material(self, tmp_db, tmp_dir):
        scorer = _make_scorer(tmp_db, tmp_dir)
        now = datetime.now(tz=timezone.utc)

        scorer._is_material_change("MKT-1", "BUY_NO", -0.4, 0.4, "scheduled", now)
        assert scorer._is_material_change("MKT-1", "BUY_NO", -0.4, 0.4, "live",
                                           now + timedelta(seconds=5))

    def test_ev_threshold_cross_is_material(self, tmp_db, tmp_dir):
        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.ev_threshold = 0.03
        now = datetime.now(tz=timezone.utc)

        scorer._is_material_change("MKT-1", "WATCH", -0.4, 0.02, None, now)
        # ev_no crosses from 0.02 (below) to 0.05 (above)
        assert scorer._is_material_change("MKT-1", "WATCH", -0.4, 0.05, None,
                                           now + timedelta(seconds=5))

    def test_large_ev_delta_is_material(self, tmp_db, tmp_dir):
        scorer = _make_scorer(tmp_db, tmp_dir)
        now = datetime.now(tz=timezone.utc)

        scorer._is_material_change("MKT-1", "BUY_NO", -0.4, 0.10, None, now)
        # ev_no jumps from 0.10 to 0.20 (delta = 0.10 > 0.03)
        assert scorer._is_material_change("MKT-1", "BUY_NO", -0.4, 0.20, None,
                                           now + timedelta(seconds=5))

    def test_cooldown_expiry_is_material(self, tmp_db, tmp_dir):
        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.cooldown_sec = 60.0
        now = datetime.now(tz=timezone.utc)

        scorer._is_material_change("MKT-1", "WATCH", -0.4, 0.4, None, now)
        # Same data, but 61 seconds later -> cooldown expired
        assert scorer._is_material_change("MKT-1", "WATCH", -0.4, 0.4, None,
                                           now + timedelta(seconds=61))

    def test_throttle_actually_reduces_console_output(self, tmp_db, tmp_dir, capsys):
        """Two consecutive run_once calls with no market changes should produce fewer prints on second call."""
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.cooldown_sec = 999.0

        scorer.run_once()
        first_output = capsys.readouterr().out

        scorer.run_once()
        second_output = capsys.readouterr().out

        assert len(first_output) > 0
        assert len(second_output) == 0
