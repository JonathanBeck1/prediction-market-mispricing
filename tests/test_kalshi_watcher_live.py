from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.db import init_db
from app.kalshi_api import KalshiMarket, LiveMarketCatalog
from app.kalshi_watcher_live import (
    LiveKalshiWatcher,
    _parse_dollars,
    _extract_series,
    _guess_speaker,
)


@pytest.fixture()
def live_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = init_db(db_path)
        yield conn
        conn.close()


@pytest.fixture()
def sample_catalog() -> LiveMarketCatalog:
    markets = [
        KalshiMarket(
            ticker="KXTRUMPSAY-26MAR09-WIND",
            event_ticker="KXTRUMPSAY-26MAR09",
            series_ticker="KXTRUMPSAY",
            speaker="trump",
            primary_phrase="wind",
            phrase_variants=("wind", "windmills"),
            event_context="rally",
            rules_primary="If Trump says wind...",
            yes_ask_dollars="0.4500",
            yes_bid_dollars="0.4200",
            no_ask_dollars="0.5500",
            no_bid_dollars="0.5800",
            volume_24h=150,
            open_interest=300,
            close_time="2026-03-09T23:59:59Z",
        ),
        KalshiMarket(
            ticker="KXLEAVITTMENTION-26MAR01-BORDER",
            event_ticker="KXLEAVITTMENTION-26MAR01",
            series_ticker="KXLEAVITTMENTION",
            speaker="leavitt",
            primary_phrase="border",
            phrase_variants=("border",),
            event_context="briefing",
            rules_primary="If Leavitt says border...",
            yes_ask_dollars="0.6000",
            yes_bid_dollars="0.5700",
            no_ask_dollars="0.4000",
            no_bid_dollars="0.4300",
            volume_24h=80,
            open_interest=200,
            close_time="2026-03-01T23:59:59Z",
        ),
    ]
    cat = LiveMarketCatalog(markets=markets)
    cat._build_indexes()
    return cat


class TestParseDollars:
    def test_string(self):
        assert _parse_dollars("0.5600") == 0.56

    def test_int(self):
        assert _parse_dollars(42) == 42.0

    def test_none(self):
        assert _parse_dollars(None) == 0.0

    def test_empty(self):
        assert _parse_dollars("") == 0.0


class TestExtractSeries:
    def test_standard_ticker(self):
        assert _extract_series("KXTRUMPSAY-26MAR09-WIND") == "KXTRUMPSAY"

    def test_empty(self):
        assert _extract_series("") == ""


class TestGuessSpeaker:
    def test_trump(self):
        assert _guess_speaker({"title": "Trump says something"}) == "trump"

    def test_leavitt(self):
        assert _guess_speaker({"title": "Leavitt at briefing"}) == "leavitt"

    def test_unknown(self):
        assert _guess_speaker({"title": "Some market"}) == "auto"


class TestBootstrapMarkets:
    def test_inserts_from_catalog(self, live_db, sample_catalog):
        watcher = LiveKalshiWatcher(
            conn=live_db,
            raw_log_path=Path("/tmp/test.jsonl"),
            interval_sec=10,
            catalog=sample_catalog,
        )
        watcher.bootstrap_markets()

        rows = live_db.execute("SELECT * FROM markets ORDER BY market_id").fetchall()
        assert len(rows) == 2
        assert rows[0]["subject"] == "leavitt"
        assert rows[1]["subject"] == "trump"

    def test_upsert_updates_speaker(self, live_db, sample_catalog):
        live_db.execute(
            "INSERT INTO markets (market_id, slug, subject, prompt) "
            "VALUES ('KXTRUMPSAY-26MAR09-WIND', 'old-slug', 'wrong', 'old')"
        )
        live_db.commit()

        watcher = LiveKalshiWatcher(
            conn=live_db,
            raw_log_path=Path("/tmp/test.jsonl"),
            interval_sec=10,
            catalog=sample_catalog,
        )
        watcher.bootstrap_markets()

        row = live_db.execute(
            "SELECT subject FROM markets WHERE market_id='KXTRUMPSAY-26MAR09-WIND'"
        ).fetchone()
        assert row["subject"] == "trump"


class TestRunOnce:
    def _fake_api_response(self, ticker: str, speaker: str) -> dict:
        return {
            "ticker": ticker,
            "event_ticker": f"{ticker.rsplit('-', 1)[0]}",
            "title": f"{speaker} market",
            "rules_primary": "",
            "yes_bid_dollars": "0.4000",
            "yes_ask_dollars": "0.4500",
            "no_bid_dollars": "0.5500",
            "no_ask_dollars": "0.6000",
            "yes_bid_size_fp": "200.00",
            "yes_ask_size_fp": "150.00",
            "volume_24h": 100,
            "status": "open",
        }

    def test_inserts_snapshots(self, live_db, sample_catalog, tmp_dir):
        watcher = LiveKalshiWatcher(
            conn=live_db,
            raw_log_path=tmp_dir / "snaps.jsonl",
            interval_sec=10,
            catalog=sample_catalog,
        )
        watcher.bootstrap_markets()

        api_markets = {
            "KXTRUMPSAY-26MAR09-WIND": self._fake_api_response("KXTRUMPSAY-26MAR09-WIND", "trump"),
            "KXLEAVITTMENTION-26MAR01-BORDER": self._fake_api_response("KXLEAVITTMENTION-26MAR01-BORDER", "leavitt"),
        }

        def fake_fetch(series_ticker):
            result = []
            for ticker, data in api_markets.items():
                if ticker.startswith(series_ticker):
                    result.append(data)
            return result

        with patch.object(watcher, "_fetch_series_markets", side_effect=fake_fetch):
            count = watcher.run_once()

        assert count == 2
        snaps = live_db.execute("SELECT * FROM market_snapshots").fetchall()
        assert len(snaps) == 2

        snap = live_db.execute(
            "SELECT * FROM market_snapshots WHERE market_id='KXTRUMPSAY-26MAR09-WIND'"
        ).fetchone()
        assert snap["yes_ask"] == 0.45
        assert snap["yes_bid"] == 0.40

    def test_handles_api_failure_gracefully(self, live_db, sample_catalog, tmp_dir):
        watcher = LiveKalshiWatcher(
            conn=live_db,
            raw_log_path=tmp_dir / "snaps.jsonl",
            interval_sec=10,
            catalog=sample_catalog,
        )
        watcher.bootstrap_markets()

        with patch.object(watcher, "_fetch_series_markets", return_value=[]):
            count = watcher.run_once()

        assert count == 0

    def test_discovers_new_markets(self, live_db, sample_catalog, tmp_dir):
        watcher = LiveKalshiWatcher(
            conn=live_db,
            raw_log_path=tmp_dir / "snaps.jsonl",
            interval_sec=10,
            catalog=sample_catalog,
        )
        watcher.bootstrap_markets()

        new_market = {
            "ticker": "KXTRUMPSAY-26MAR16-TARIFF",
            "event_ticker": "KXTRUMPSAY-26MAR16",
            "title": "Trump tariff mention",
            "rules_primary": "trump says tariff",
            "yes_bid_dollars": "0.30",
            "yes_ask_dollars": "0.35",
            "no_bid_dollars": "0.65",
            "no_ask_dollars": "0.70",
            "volume_24h": 50,
            "status": "open",
        }

        def fake_fetch(series_ticker):
            if series_ticker == "KXTRUMPSAY":
                return [new_market]
            return []

        with patch.object(watcher, "_fetch_series_markets", side_effect=fake_fetch):
            count = watcher.run_once()

        assert count == 1
        row = live_db.execute(
            "SELECT subject FROM markets WHERE market_id='KXTRUMPSAY-26MAR16-TARIFF'"
        ).fetchone()
        assert row is not None
        assert row["subject"] == "trump"

    def test_jsonl_written(self, live_db, sample_catalog, tmp_dir):
        watcher = LiveKalshiWatcher(
            conn=live_db,
            raw_log_path=tmp_dir / "snaps.jsonl",
            interval_sec=10,
            catalog=sample_catalog,
        )
        watcher.bootstrap_markets()

        api_market = self._fake_api_response("KXTRUMPSAY-26MAR09-WIND", "trump")

        with patch.object(watcher, "_fetch_series_markets", return_value=[api_market]):
            watcher.run_once()

        jsonl_path = tmp_dir / "snaps.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["market_id"] == "KXTRUMPSAY-26MAR09-WIND"
