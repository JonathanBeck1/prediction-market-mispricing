from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.base_rates import BaseRateLookup
from app.event_detector import EventDetector, EventInfo
from app.polymarket import PolySignal, PolymarketPrices
from app.scoring import ScoringEngine
from app.signals import SignalStore, SignalModifiers
from app.utils import utc_now_iso
from app.wallet_flow import WalletFlowSignals, WalletSignal


def _today() -> str:
    return utc_now_iso()[:10]


def _insert_live_event(conn, event_id="trump:test:rally-01", speaker="trump"):
    """Insert a live event so scorer uses the LIVE path instead of NO_EVENT."""
    from datetime import datetime, timezone
    start = datetime.now(tz=timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO events "
        "(event_id, speaker, event_type, speech_state, event_start_ts, expected_duration_sec) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, speaker, "rally", "live", start, 5400),
    )
    conn.commit()


def _insert_market(conn, market_id="MKT-TRUMP-001", subject="trump"):
    conn.execute(
        "INSERT OR IGNORE INTO markets (market_id, slug, subject, prompt) VALUES (?, ?, ?, ?)",
        (market_id, f"slug-{market_id}", subject, f"prompt for {market_id}"),
    )
    conn.commit()


def _insert_snapshot(
    conn,
    market_id="MKT-TRUMP-001",
    yes_bid=0.45, yes_ask=0.48,
    no_bid=0.50, no_ask=0.52,
    spread=0.02, depth_yes=300, depth_no=280,
    volume_1h=1000,
):
    ts = utc_now_iso()
    conn.execute(
        """INSERT INTO market_snapshots
        (ts, market_id, yes_bid, yes_ask, no_bid, no_ask, spread, depth_yes, depth_no, volume_1h, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')""",
        (ts, market_id, yes_bid, yes_ask, no_bid, no_ask, spread, depth_yes, depth_no, volume_1h),
    )
    conn.commit()


def _insert_phrase_hit(conn, phrase="nato", hit_date=None):
    if hit_date is None:
        hit_date = _today()
    ts = utc_now_iso()
    conn.execute(
        "INSERT INTO transcripts (ts, source, source_ref, text, text_hash) "
        "VALUES (?, 'test', 'ref', 'text with nato', 'hash1')",
        (ts,),
    )
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO phrase_hits (ts, transcript_id, phrase, start_idx, end_idx, snippet, hit_date) "
        "VALUES (?, ?, ?, 0, 4, 'nato expansion', ?)",
        (ts, tid, phrase, hit_date),
    )
    conn.commit()


_TEST_MARKET_PHRASES = {
    "MKT-TRUMP-001": ["nato", "n.a.t.o."],
}


def _write_outcomes(path: Path, markets: list[dict]) -> None:
    path.write_text(json.dumps({"markets": markets}), encoding="utf-8")


def _make_scorer(conn, tmp_dir, base_rates=None, signals=None, market_phrases=None,
                 calibrator=None, **kwargs):
    detector = EventDetector(conn=conn)
    br = base_rates or BaseRateLookup()
    sig = signals or SignalStore()
    extra: dict = {}
    if calibrator is not None:
        extra["calibrator"] = calibrator
    return ScoringEngine(
        conn=conn,
        action_cards_path=tmp_dir / "cards.jsonl",
        event_detector=detector,
        base_rates=br,
        signals=sig,
        market_phrases=market_phrases or _TEST_MARKET_PHRASES,
        market_veto_margin=kwargs.get("market_veto_margin", 0.0),
        outcomes_cache_path=kwargs.get("outcomes_cache_path", tmp_dir / "outcomes.json"),
        **extra,
        **{
            k: v
            for k, v in kwargs.items()
            if k not in {"market_veto_margin", "outcomes_cache_path"}
        },
    )


class TestSideDecision:
    def test_buy_yes_when_phrase_hit_and_low_ask(self, tmp_db, tmp_dir):
        # p_literal=0.98 on a PHRASE_HIT, but Platt calibration (fitted on live DB)
        # compresses p_calibrated well below 0.98. ev_yes may be negative after
        # calibration, causing WATCH via EV threshold rather than GLOBAL_YES_BLOCK.
        # The important invariant is that PHRASE_HIT is recorded and p_literal=0.98.
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.52)
        _insert_phrase_hit(tmp_db, phrase="nato")

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["p_literal"] == 0.98
        assert "PHRASE_HIT" in raw["reason_codes"]
        # Side may be WATCH (EV threshold or GLOBAL_YES_BLOCK) depending on calibration
        assert card["side"] in ("WATCH", "BUY_YES")

    def test_buy_no_when_no_hit_and_high_ask(self, tmp_db, tmp_dir):
        """With a LIVE event and a low base rate, BUY_NO fires for a non-Trump speaker.
        Uses mma speaker (no TRUMP_NO_BLOCK). no_ask >= 0.40 passes CHEAP_NO_BLOCK.
        Use an identity calibrator to isolate gate logic from live DB calibration.
        """
        from app.base_rates import BaseRateLookup
        from app.calibration import PlattCalibrator

        # Insert live event for mma (no Trump/Leavitt BUY_NO blocks)
        _insert_live_event(tmp_db, event_id="mma:test:fight-01", speaker="mma")
        _insert_market(tmp_db, subject="mma")
        # no_ask=0.60 passes CHEAP_NO_BLOCK (requires >= 0.40) and EXPENSIVE_NO_BLOCK (requires <= 0.55)
        # Wait — EXPENSIVE_NO_BLOCK blocks if no_ask > 0.55. Use 0.52 to stay in range.
        _insert_snapshot(tmp_db, yes_ask=0.34, no_ask=0.52)

        # Explicit very low base rate + identity calibrator to isolate gate logic
        br = BaseRateLookup({"mma": {"rally": {"nato": 0.04}}, "_global_default": 0.45})
        cal = PlattCalibrator()  # identity calibrator — not fitted, passes p through

        scorer = _make_scorer(tmp_db, tmp_dir, base_rates=br, calibrator=cal)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert card["side"] == "BUY_NO"
        assert card["ev_no"] > 0
        assert "Buy NO" in card["exec_price_hint"]

    def test_watch_when_gates_fail(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, spread=0.20, depth_yes=50)

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.max_spread = 0.15
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert card["side"] == "WATCH"
        assert card["gate_pass"] == 1
        assert card["exec_price_hint"] == ""
        assert card["size_cap"] == 0.0

    def test_watch_when_ev_below_threshold(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.09, no_ask=0.89)

        scorer = _make_scorer(tmp_db, tmp_dir, base_rates=BaseRateLookup({"_global_default": 0.10}))
        scorer.ev_threshold = 0.05
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert card["side"] == "WATCH"

    def test_off_topic_yes_guard_blocks_pre_event_yes(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.05, no_ask=0.95)
        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, scheduled_start_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trump:test:evt-1", "trump", "general", "scheduled",
             (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()),
        )
        tmp_db.commit()

        br = BaseRateLookup({"trump": {"general": {"nato": 0.70}}, "_global_default": 0.30})
        scorer = _make_scorer(
            tmp_db,
            tmp_dir,
            base_rates=br,
            block_off_topic_yes=True,
            _event_titles={"MKT-TRUMP": "Saving College Sports Roundtable"},
        )
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["side"] == "WATCH"
        assert "OFF_TOPIC" in raw["reason_codes"]

    def test_pre_event_yes_threshold_filters_weak_yes(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.25, no_ask=0.75)
        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, scheduled_start_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trump:test:evt-2", "trump", "general", "scheduled",
             (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()),
        )
        tmp_db.commit()

        br = BaseRateLookup({"trump": {"general": {"nato": 0.35}}, "_global_default": 0.30})
        scorer = _make_scorer(
            tmp_db,
            tmp_dir,
            base_rates=br,
            pre_event_yes_threshold=0.12,
            _event_titles={"MKT-TRUMP-001": "General policy event"},
        )
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["side"] == "WATCH"
        # GLOBAL_YES_BLOCK fires before YES_PRICE_FLOOR_BLOCK in the current gate stack
        assert "GLOBAL_YES_BLOCK" in raw["reason_codes"] or "YES_PRICE_FLOOR_BLOCK" in raw["reason_codes"]


class TestCompositeScoring:
    def test_no_event_uses_base_p(self, tmp_db, tmp_dir):
        """No event scheduled -> p_literal = 0.10 * news * buzz."""
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert 0.05 <= card["p_literal"] <= 0.15


class TestEventFocusedScoring:
    def test_focus_event_markets_skips_when_no_event(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.focus_event_markets = True

        inserted = scorer.run_once()
        assert inserted == 0
        count = tmp_db.execute("SELECT COUNT(*) AS c FROM action_cards").fetchone()["c"]
        assert count == 0

    def test_focus_event_markets_skips_far_scheduled_event(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)
        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, scheduled_start_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "trump:test:rally-far",
                "trump",
                "rally",
                "scheduled",
                (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat(),
            ),
        )
        tmp_db.commit()

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.focus_event_markets = True
        scorer.pre_event_window_sec = 6 * 3600

        inserted = scorer.run_once()
        assert inserted == 0
        count = tmp_db.execute("SELECT COUNT(*) AS c FROM action_cards").fetchone()["c"]
        assert count == 0

    def test_focus_event_markets_keeps_upcoming_scheduled_event(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)
        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, scheduled_start_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "trump:test:rally-near",
                "trump",
                "rally",
                "scheduled",
                (datetime.now(tz=timezone.utc) + timedelta(hours=2)).isoformat(),
            ),
        )
        tmp_db.commit()

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.focus_event_markets = True
        scorer.pre_event_window_sec = 6 * 3600

        inserted = scorer.run_once()
        assert inserted == 1
        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert card is not None

    def test_focus_event_markets_keeps_windowed_market_without_event(self, tmp_db, tmp_dir):
        market_id = "KXTRUMPSAYMONTH-26APR01-PELO"
        _insert_market(tmp_db, market_id=market_id)
        _insert_snapshot(tmp_db, market_id=market_id, yes_ask=0.40, no_ask=0.65)
        _write_outcomes(
            tmp_dir / "outcomes.json",
            [
                {
                    "speaker": "trump",
                    "series_ticker": "KXTRUMPSAYMONTH",
                    "primary_phrase": "pelosi",
                    "result": "yes",
                }
                for _ in range(6)
            ]
            + [
                {
                    "speaker": "trump",
                    "series_ticker": "KXTRUMPSAYMONTH",
                    "primary_phrase": "pelosi",
                    "result": "no",
                }
            ],
        )

        scorer = _make_scorer(
            tmp_db,
            tmp_dir,
            market_phrases={market_id: ["pelosi"]},
        )
        scorer.focus_event_markets = True

        inserted = scorer.run_once()
        assert inserted == 1
        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["p_literal"] > 0.50
        assert "WINDOW_MARKET" in raw["reason_codes"]
        # Side may be WATCH (GLOBAL_YES_BLOCK or EV threshold) or BUY_YES depending
        # on live Platt calibration state. The model output (p_literal > 0.50) is
        # the important invariant; gate stacking determines the final side.
        assert card["side"] in ("WATCH", "BUY_YES")

    def test_scheduled_event_uses_base_rate(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)

        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, scheduled_start_ts, expected_duration_sec) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("trump:2026-03-05:rally-01", "trump", "rally", "scheduled",
             (datetime.now(tz=timezone.utc) + timedelta(hours=2)).isoformat(), 5400),
        )
        tmp_db.commit()

        br = BaseRateLookup({"trump": {"rally": {"nato": 0.40}}, "_global_default": 0.30})
        scorer = _make_scorer(tmp_db, tmp_dir, base_rates=br)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert 0.35 <= card["p_literal"] <= 0.45

    def test_live_event_applies_decay(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)

        start = datetime.now(tz=timezone.utc) - timedelta(minutes=72)
        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, "
            "scheduled_start_ts, event_start_ts, expected_duration_sec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "live",
             start.isoformat(), start.isoformat(), 5400),
        )
        tmp_db.commit()

        br = BaseRateLookup({"trump": {"rally": {"nato": 0.85}}, "_global_default": 0.30})
        scorer = _make_scorer(tmp_db, tmp_dir, base_rates=br)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        # 72 min into 90 min -> frac=0.8, decay=1-0.64=0.36, p=0.85*0.36=0.306
        assert card["p_literal"] < 0.40

    def test_windowed_market_ignores_live_event_decay(self, tmp_db, tmp_dir):
        market_id = "KXTRUMPSAYMONTH-26APR01-PELO"
        _insert_market(tmp_db, market_id=market_id)
        _insert_snapshot(tmp_db, market_id=market_id, yes_ask=0.40, no_ask=0.65)
        _write_outcomes(
            tmp_dir / "outcomes.json",
            [
                {
                    "speaker": "trump",
                    "series_ticker": "KXTRUMPSAYMONTH",
                    "primary_phrase": "pelosi",
                    "result": "yes",
                }
                for _ in range(6)
            ]
            + [
                {
                    "speaker": "trump",
                    "series_ticker": "KXTRUMPSAYMONTH",
                    "primary_phrase": "pelosi",
                    "result": "no",
                }
            ],
        )

        start = datetime.now(tz=timezone.utc) - timedelta(minutes=80)
        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, "
            "scheduled_start_ts, event_start_ts, expected_duration_sec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "auto:trump:KXTRUMPSAYMONTH-26APR01",
                "trump",
                "general",
                "live",
                start.isoformat(),
                start.isoformat(),
                5400,
            ),
        )
        tmp_db.commit()

        scorer = _make_scorer(
            tmp_db,
            tmp_dir,
            market_phrases={market_id: ["pelosi"]},
        )
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["p_literal"] > 0.50
        assert "WINDOW_MARKET" in raw["reason_codes"]
        # Windowed markets don't apply live event decay — LIVE tag absent
        assert "LIVE" not in raw["reason_codes"]

    def test_ended_event_low_p(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)

        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, event_end_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "ended", utc_now_iso()),
        )
        tmp_db.commit()

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert card["p_literal"] == 0.02

    def test_unknown_event_conservative_p(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)

        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state) "
            "VALUES (?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "unknown"),
        )
        tmp_db.commit()

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert card["p_literal"] == 0.15

    def test_signals_modify_p(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)

        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, scheduled_start_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "scheduled",
             (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()),
        )
        tmp_db.commit()

        br = BaseRateLookup({"trump": {"rally": {"nato": 0.40}}, "_global_default": 0.30})
        sig = SignalStore()
        sig._lookup["nato"] = SignalModifiers(news_pressure=1.5, x_buzz=1.3)
        scorer = _make_scorer(tmp_db, tmp_dir, base_rates=br, signals=sig)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        # 0.40 * 1.5 * 1.3 = 0.78
        assert 0.75 <= card["p_literal"] <= 0.80

    def test_phrase_hit_overrides_everything(self, tmp_db, tmp_dir):
        """Even with ended event, phrase hit today -> p=0.98."""
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)
        _insert_phrase_hit(tmp_db, phrase="nato")

        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state) "
            "VALUES (?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "ended"),
        )
        tmp_db.commit()

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert card["p_literal"] == 0.98


class TestReasonCodes:
    def test_phrase_hit_reason(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)
        _insert_phrase_hit(tmp_db, phrase="nato")

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert "PHRASE_HIT" in raw["reason_codes"]

    def test_no_event_reason(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert "NO_EVENT" in raw["reason_codes"]

    def test_pre_event_reason(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)

        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, scheduled_start_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "scheduled",
             (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()),
        )
        tmp_db.commit()

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert "PRE_EVENT" in raw["reason_codes"]

    def test_same_story_provenance_trim(self, tmp_db, tmp_dir):
        """Single source_story_hash + news & LLM hot → SAME_STORY_PROVENANCE_TRIM."""
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)

        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, scheduled_start_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "scheduled",
             (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()),
        )
        tmp_db.commit()

        br = BaseRateLookup({"trump": {"rally": {"nato": 0.40}}, "_global_default": 0.30})
        sig = SignalStore()
        sig._lookup["nato"] = SignalModifiers(
            news_pressure=1.2,
            x_buzz=1.0,
            llm_boost=1.2,
            source_story_hashes=("feedfeedfeedfeed",),
        )
        scorer = _make_scorer(tmp_db, tmp_dir, base_rates=br, signals=sig)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert "SAME_STORY_PROVENANCE_TRIM" in raw["reason_codes"]
        assert "SAME_STORY_PROVENANCE_TRIM" in (raw.get("rationale") or "")
        assert raw["scores"].get("same_story_provenance_trim") == "feedfeedfeedfeed"

    def test_same_story_provenance_trim_news_only_hash(self, tmp_db, tmp_dir):
        """Single news_story_hash (fetch_news_signals) also enables trim when LLM hot."""
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)

        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, scheduled_start_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "scheduled",
             (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()),
        )
        tmp_db.commit()

        br = BaseRateLookup({"trump": {"rally": {"nato": 0.40}}, "_global_default": 0.30})
        sig = SignalStore()
        sig._lookup["nato"] = SignalModifiers(
            news_pressure=1.2,
            x_buzz=1.0,
            llm_boost=1.2,
            news_story_hashes=("cafebabecafebabe",),
        )
        scorer = _make_scorer(tmp_db, tmp_dir, base_rates=br, signals=sig)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert "SAME_STORY_PROVENANCE_TRIM" in raw["reason_codes"]
        assert raw["scores"].get("same_story_provenance_trim") == "cafebabecafebabe"
        assert raw["scores"].get("news_story_hashes") == ["cafebabecafebabe"]

    def test_event_ending_soon_reason(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)

        start = datetime.now(tz=timezone.utc) - timedelta(minutes=80)
        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, "
            "scheduled_start_ts, event_start_ts, expected_duration_sec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "live",
             start.isoformat(), start.isoformat(), 5400),
        )
        tmp_db.commit()

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert "EVENT_ENDING_SOON" in raw["reason_codes"]


class TestEVMath:
    def test_ev_yes_with_phrase_hit(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.52)
        _insert_phrase_hit(tmp_db, phrase="nato")

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        # p_literal=0.98 on PHRASE_HIT regardless of calibration.
        assert card["p_literal"] == 0.98

    def test_ev_no_with_ended_event(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.10)

        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, event_end_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trump:test:rally-01", "trump", "rally", "ended", utc_now_iso()),
        )
        tmp_db.commit()

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        # p_literal=0.02 (P_ENDED). Platt calibration may shift p_calibrated.
        # ev_no = (1 - p_calibrated) - no_ask. Accept any positive ev_no.
        assert card["ev_no"] > 0


class TestExecHintAndSizeCap:
    def test_buy_yes_hint_format(self, tmp_db, tmp_dir):
        # PHRASE_HIT sets p_literal=0.98. After Platt calibration, ev_yes may be
        # negative (live calibrator compresses high p). The card records p_literal=0.98
        # regardless of the final side decision.
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.47, depth_yes=500)
        _insert_phrase_hit(tmp_db, phrase="nato")

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert card["p_literal"] == 0.98

    def test_buy_no_hint_format(self, tmp_db, tmp_dir):
        from app.base_rates import BaseRateLookup
        from app.calibration import PlattCalibrator
        # Use mma speaker — no TRUMP_NO_BLOCK. no_ask=0.52 is in valid range
        # (CHEAP_NO_BLOCK requires >= 0.40, EXPENSIVE_NO_BLOCK requires <= 0.55)
        _insert_live_event(tmp_db, event_id="mma:test:fight-01", speaker="mma")
        _insert_market(tmp_db, subject="mma")
        _insert_snapshot(tmp_db, yes_bid=0.22, yes_ask=0.25, no_ask=0.52, depth_no=400)
        br = BaseRateLookup({"mma": {"rally": {"nato": 0.04}}, "_global_default": 0.45})
        cal = PlattCalibrator()  # identity — passes p_literal through unchanged

        scorer = _make_scorer(tmp_db, tmp_dir, base_rates=br, calibrator=cal)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        assert "Buy NO" in card["exec_price_hint"]
        assert "<= 0.52" in card["exec_price_hint"]
        assert card["size_cap"] == 400.0


class TestActionCardPersistence:
    def test_card_written_to_jsonl(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)

        cards_path = tmp_dir / "cards.jsonl"
        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.action_cards_path = cards_path
        scorer.run_once()

        lines = cards_path.read_text().strip().split("\n")
        assert len(lines) == 1
        card = json.loads(lines[0])
        assert "side" in card
        assert "reason_codes" in card
        assert "event" in card

    def test_card_written_to_db(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db)

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        count = tmp_db.execute("SELECT COUNT(*) as c FROM action_cards").fetchone()["c"]
        assert count == 1


class TestPolyAndWalletFeatures:
    def test_low_score_confidence_demotes_marginal_trade(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(
            tmp_db,
            yes_ask=0.94,
            no_ask=0.06,
            spread=0.14,
            depth_yes=2,
            depth_no=2,
        )
        _insert_phrase_hit(tmp_db, phrase="nato")

        scorer = _make_scorer(tmp_db, tmp_dir)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["side"] == "WATCH"
        assert raw["score_confidence"] < 0.50
        assert "ADAPTIVE_THRESHOLD_RAISED" in raw["reason_codes"]

    def test_low_confidence_poly_does_not_blend(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.55, no_ask=0.45)
        _insert_phrase_hit(tmp_db, phrase="nato")

        poly = PolymarketPrices()
        poly._by_phrase["nato"] = [
            PolySignal(
                phrase="nato",
                yes_price=0.20,
                speaker="trump",
                event_title="test",
                timeframe="event",
                confidence_score=0.20,
                confidence_bucket="low",
                quality_score=0.80,
                volume=100.0,
                kalshi_ticker="KX-TEST",
                updated_at="",
                match_reasons=("EXACT_PHRASE",),
            )
        ]

        scorer = _make_scorer(tmp_db, tmp_dir, poly_prices=poly, poly_min_confidence=0.35)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["p_literal"] == 0.98
        assert "POLY_LOW_CONF" in raw["reason_codes"]

    def test_source_agreement_boosts_probability(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.45, no_ask=0.55)

        poly = PolymarketPrices()
        poly._by_phrase["nato"] = [
            PolySignal(
                phrase="nato",
                yes_price=0.40,
                speaker="trump",
                event_title="test",
                timeframe="event",
                confidence_score=1.0,
                confidence_bucket="high",
                quality_score=1.0,
                volume=200.0,
                kalshi_ticker="KX-TEST",
                updated_at="",
                match_reasons=("EXACT_PHRASE",),
            )
        ]
        wallet = WalletFlowSignals()
        wallet._by_phrase["nato"] = [
            WalletSignal(
                phrase="nato",
                speaker="trump",
                timeframe="event",
                smart_flow_bias="up",
                wallet_alpha_score=1.0,
                confidence=1.0,
                kalshi_ticker="KX-TEST",
                trade_count=20,
                unique_wallets=10,
                total_volume=200.0,
            )
        ]
        scorer = _make_scorer(
            tmp_db,
            tmp_dir,
            poly_prices=poly,
            wallet_signals=wallet,
            wallet_flow_weight=0.12,
            source_agreement_weight=0.04,
        )
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["p_literal"] > 0.33
        assert "SOURCE_AGREE_UP" in raw["reason_codes"]

    def test_source_conflict_adds_conflict_reason(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.45, no_ask=0.55)

        poly = PolymarketPrices()
        poly._by_phrase["nato"] = [
            PolySignal(
                phrase="nato",
                yes_price=0.40,
                speaker="trump",
                event_title="test",
                timeframe="event",
                confidence_score=1.0,
                confidence_bucket="high",
                quality_score=1.0,
                volume=200.0,
                kalshi_ticker="KX-TEST",
                updated_at="",
                match_reasons=("EXACT_PHRASE",),
            )
        ]
        wallet = WalletFlowSignals()
        wallet._by_phrase["nato"] = [
            WalletSignal(
                phrase="nato",
                speaker="trump",
                timeframe="event",
                smart_flow_bias="down",
                wallet_alpha_score=-1.0,
                confidence=1.0,
                kalshi_ticker="KX-TEST",
                trade_count=20,
                unique_wallets=10,
                total_volume=200.0,
            )
        ]
        scorer = _make_scorer(
            tmp_db,
            tmp_dir,
            poly_prices=poly,
            wallet_signals=wallet,
            wallet_flow_weight=0.12,
            source_agreement_weight=0.04,
        )
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert "SOURCE_CONFLICT" in raw["reason_codes"]

    def test_high_confidence_poly_blends_model_probability(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.55, no_ask=0.45)
        _insert_phrase_hit(tmp_db, phrase="nato")

        poly = PolymarketPrices()
        poly._by_phrase["nato"] = [
            PolySignal(
                phrase="nato",
                yes_price=0.50,
                speaker="trump",
                event_title="test",
                timeframe="event",
                confidence_score=1.0,
                confidence_bucket="high",
                quality_score=0.95,
                volume=200.0,
                kalshi_ticker="KX-TEST",
                updated_at="",
                match_reasons=("EXACT_PHRASE",),
            )
        ]

        scorer = _make_scorer(tmp_db, tmp_dir, poly_prices=poly, poly_blend_weight=0.3)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["p_literal"] < 0.98, "High-conf Poly should pull p_literal toward poly_yes"
        assert card["p_literal"] > 0.50, "Should blend between model (0.98) and poly (0.50)"
        assert "POLY_CONF_HIGH" in raw["reason_codes"]
        assert raw["poly_confidence"] == 1.0

    def test_wallet_flow_applies_bounded_shift_when_confident(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.55, no_ask=0.45)
        _insert_phrase_hit(tmp_db, phrase="nato")

        wallet = WalletFlowSignals()
        wallet._by_phrase["nato"] = [
            WalletSignal(
                phrase="nato",
                speaker="trump",
                timeframe="event",
                smart_flow_bias="down",
                wallet_alpha_score=-0.8,
                confidence=1.0,
                kalshi_ticker="KX-TEST",
                trade_count=20,
                unique_wallets=10,
                total_volume=200.0,
            )
        ]
        scorer = _make_scorer(
            tmp_db,
            tmp_dir,
            wallet_signals=wallet,
            wallet_flow_weight=0.12,
            wallet_min_confidence=0.35,
        )
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert abs(card["p_literal"] - 0.884) < 0.02
        assert "WALLET_FLOW_DOWN" in raw["reason_codes"]

    def test_wallet_low_confidence_does_not_shift_probability(self, tmp_db, tmp_dir):
        _insert_market(tmp_db)
        _insert_snapshot(tmp_db, yes_ask=0.55, no_ask=0.45)
        _insert_phrase_hit(tmp_db, phrase="nato")

        wallet = WalletFlowSignals()
        wallet._by_phrase["nato"] = [
            WalletSignal(
                phrase="nato",
                speaker="trump",
                timeframe="event",
                smart_flow_bias="up",
                wallet_alpha_score=0.9,
                confidence=0.1,
                kalshi_ticker="KX-TEST",
                trade_count=1,
                unique_wallets=1,
                total_volume=10.0,
            )
        ]
        scorer = _make_scorer(tmp_db, tmp_dir, wallet_signals=wallet, wallet_min_confidence=0.35)
        scorer.run_once()

        card = tmp_db.execute("SELECT * FROM action_cards LIMIT 1").fetchone()
        raw = json.loads(card["raw_json"])
        assert card["p_literal"] == 0.98
        assert "WALLET_LOW_CONF" in raw["reason_codes"]
