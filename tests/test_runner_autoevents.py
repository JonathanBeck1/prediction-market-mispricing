from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.kalshi_api import KalshiMarket, LiveMarketCatalog
from app import runner
from app.runner import _seed_events_from_live_markets


def _mk(
    ticker: str,
    speaker: str,
    event_ticker: str,
    event_context: str,
    close_time: str,
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        series_ticker=ticker.split("-")[0],
        speaker=speaker,
        primary_phrase="nato",
        phrase_variants=("nato",),
        event_context=event_context,
        rules_primary="If NATO ...",
        yes_ask_dollars="0.50",
        yes_bid_dollars="0.45",
        no_ask_dollars="0.50",
        no_bid_dollars="0.45",
        volume_24h=0,
        open_interest=0,
        close_time=close_time,
    )


def test_seeds_events_from_all_tickers(tmp_db):
    future_close = (datetime.now(tz=timezone.utc) + timedelta(hours=4)).isoformat()
    cat = LiveMarketCatalog(
        markets=[
            _mk(
                ticker="KXTRUMPMENTIONB-26MAR17-NATO",
                speaker="trump",
                event_ticker="KXTRUMPMENTIONB-26MAR17",
                event_context="wh_live",
                close_time=future_close,
            ),
            _mk(
                ticker="KXTRUMPSAY-26MAR09-NATO",
                speaker="trump",
                event_ticker="KXTRUMPSAY-26MAR09",
                event_context="general",
                close_time=future_close,
            ),
        ]
    )

    inserted = _seed_events_from_live_markets(tmp_db, cat)
    assert inserted == 2

    rows = tmp_db.execute(
        "SELECT event_id, speaker, event_type, speech_state "
        "FROM events ORDER BY event_id"
    ).fetchall()
    assert len(rows) == 2

    wh = next(r for r in rows if "KXTRUMPMENTIONB" in r["event_id"])
    assert wh["event_type"] == "other"
    assert wh["speech_state"] == "scheduled"

    gen = next(r for r in rows if "KXTRUMPSAY" in r["event_id"])
    assert gen["event_type"] == "general"
    assert gen["speech_state"] == "scheduled"


def test_seeding_is_idempotent(tmp_db):
    future_close = (datetime.now(tz=timezone.utc) + timedelta(hours=3)).isoformat()
    cat = LiveMarketCatalog(
        markets=[
            _mk(
                ticker="KXSECPRESSMENTION-26MAR15-BORD",
                speaker="leavitt",
                event_ticker="KXSECPRESSMENTION-26MAR15",
                event_context="briefing",
                close_time=future_close,
            ),
        ]
    )

    first = _seed_events_from_live_markets(tmp_db, cat)
    second = _seed_events_from_live_markets(tmp_db, cat)
    assert first == 1
    assert second == 0


def test_skips_stale_markets(tmp_db):
    old_close = (datetime.now(tz=timezone.utc) - timedelta(hours=5)).isoformat()
    cat = LiveMarketCatalog(
        markets=[
            _mk(
                ticker="KXMAMDANIMENTION-26MAR01-ECON",
                speaker="mamdani",
                event_ticker="KXMAMDANIMENTION-26MAR01",
                event_context="announcement",
                close_time=old_close,
            ),
        ]
    )

    inserted = _seed_events_from_live_markets(tmp_db, cat)
    assert inserted == 0
    count = tmp_db.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
    assert count == 0


def test_skips_windowed_monthly_markets(tmp_db):
    future_close = (datetime.now(tz=timezone.utc) + timedelta(days=20)).isoformat()
    cat = LiveMarketCatalog(
        markets=[
            _mk(
                ticker="KXTRUMPSAYMONTH-26APR01-PELO",
                speaker="trump",
                event_ticker="KXTRUMPSAYMONTH-26APR01",
                event_context="general",
                close_time=future_close,
            ),
        ]
    )

    inserted = _seed_events_from_live_markets(tmp_db, cat)
    assert inserted == 0
    count = tmp_db.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
    assert count == 0


def test_run_app_passes_app_context_to_ingestor_loop(monkeypatch):
    captured_app_by_loop: dict[str, object | None] = {}

    class DummyConn:
        def execute(self, *_args, **_kwargs):
            return self

        def commit(self):
            pass

        def close(self):
            pass

    settings = SimpleNamespace(
        kalshi_mock=True,
        transcript_source="directhttp",
        transcript_urls=[],
        focus_event_markets=False,
        pre_event_window_sec=21600.0,
        maintenance_enabled=False,
        watchdog_enabled=False,
        event_seed_interval_sec=300.0,
        watcher_interval_sec=10.0,
        transcript_interval_sec=30.0,
        scorer_interval_sec=10.0,
    )
    app = SimpleNamespace(
        settings=settings,
        conn=DummyConn(),
        watcher=SimpleNamespace(run_once=lambda: None),
        ingestor=SimpleNamespace(run_once=lambda: None),
        scorer=SimpleNamespace(run_once=lambda: None),
        maintenance=None,
        watchdog=None,
    )

    async def fake_service_loop(name, _interval, stop_event, _fn, *, app=None):
        captured_app_by_loop[name] = app
        if len(captured_app_by_loop) == 3:
            stop_event.set()

    monkeypatch.setattr(runner, "load_settings", lambda: settings)
    monkeypatch.setattr(runner, "build_app", lambda _settings: app)
    monkeypatch.setattr(runner, "_service_loop", fake_service_loop)

    asyncio.run(runner.run_app())

    assert captured_app_by_loop["watcher"] is app
    assert captured_app_by_loop["ingestor"] is app
    assert captured_app_by_loop["scorer"] is app
