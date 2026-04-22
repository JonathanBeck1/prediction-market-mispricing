from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.kalshi_api import KalshiMarket, LiveMarketCatalog
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
