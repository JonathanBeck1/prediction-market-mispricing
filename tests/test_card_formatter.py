from __future__ import annotations

from app.card_formatter import format_card_text, format_card_oneliner, _fmt_duration


def _sample_card(**overrides):
    base = {
        "ts": "2026-03-02T15:00:00+00:00",
        "market_id": "MKT-TRUMP-004",
        "subject": "trump",
        "phrase": "tariffs",
        "side": "BUY_YES",
        "p_literal": 0.78,
        "scores": {
            "p_literal": 0.78,
            "base_rate": 0.85,
            "time_decay": None,
            "news_pressure": 1.2,
            "x_buzz": 1.0,
            "ev_yes": 0.13,
            "ev_no": -0.08,
        },
        "liquidity": {
            "yes_ask": 0.65,
            "no_ask": 0.38,
            "spread": 0.02,
            "depth_yes": 500,
            "depth_no": 450,
            "spread_ok": True,
            "depth_ok": True,
            "gate_pass": True,
        },
        "yes_ask": 0.65,
        "no_ask": 0.38,
        "ev_yes": 0.13,
        "ev_no": -0.08,
        "exec_price_hint": "Buy YES at <= 0.65",
        "size_cap": 500,
        "spread_ok": True,
        "depth_ok": True,
        "gate_pass": True,
        "reason_codes": ["PRE_EVENT", "HIGH_BASE_RATE", "NEWS_PRESSURE_HIGH", "GATE_PASS"],
        "event": {
            "event_id": "trump:2026-03-05:rally-01",
            "speech_state": "scheduled",
            "event_type": "rally",
            "starts_in_sec": 12000,
        },
        "time_remaining_sec": None,
        "rationale": "side=BUY_YES ...",
    }
    base.update(overrides)
    return base


class TestFormatCardText:
    def test_pre_event_card(self):
        text = format_card_text(_sample_card())
        assert ">> BUY YES" in text
        assert "MKT-TRUMP-004" in text
        assert "tariffs" in text
        assert "scheduled" in text
        assert "starts in 3h 20m" in text
        assert "EV: +0.13" in text
        assert "PRE_EVENT" in text

    def test_live_card(self):
        card = _sample_card(
            side="BUY_NO",
            exec_price_hint="Buy NO at <= 0.30",
            ev_no=0.56,
            time_remaining_sec=1080.0,
            event={"event_id": "t:test", "speech_state": "live", "event_type": "rally"},
            reason_codes=["LIVE", "EVENT_ENDING_SOON", "GATE_PASS"],
        )
        text = format_card_text(card)
        assert ">> BUY NO" in text
        assert "LIVE" in text
        assert "18m remaining" in text

    def test_no_event_card(self):
        card = _sample_card(
            side="WATCH",
            exec_price_hint="",
            event={},
            reason_codes=["NO_EVENT", "GATE_PASS"],
        )
        text = format_card_text(card)
        assert "-- WATCH" in text
        assert "none scheduled" in text

    def test_ended_card(self):
        card = _sample_card(
            side="BUY_NO",
            event={"event_id": "t:test", "speech_state": "ended", "event_type": "rally"},
            reason_codes=["EVENT_ENDED", "GATE_PASS"],
        )
        text = format_card_text(card)
        assert "ENDED" in text


class TestFormatCardOneliner:
    def test_basic_format(self):
        line = format_card_oneliner(_sample_card())
        assert "[BUY_YES]" in line
        assert "MKT-TRUMP-004" in line
        assert "tariffs" in line
        assert "p=0.780" in line

    def test_watch_format(self):
        line = format_card_oneliner(_sample_card(side="WATCH", exec_price_hint=""))
        assert "[WATCH]" in line


class TestFmtDuration:
    def test_seconds(self):
        assert _fmt_duration(45) == "45s"

    def test_minutes(self):
        assert _fmt_duration(300) == "5m"

    def test_hours_and_minutes(self):
        assert _fmt_duration(12000) == "3h 20m"

    def test_exact_hours(self):
        assert _fmt_duration(7200) == "2h"
