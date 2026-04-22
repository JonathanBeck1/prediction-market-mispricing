from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.kalshi_api import KalshiMarket, LiveMarketCatalog


def _sample_cache_data() -> dict:
    return {
        "fetched_at": "2026-03-01T12:00:00Z",
        "total_markets": 2,
        "markets": [
            {
                "ticker": "KXTRUMPSAY-26MAR09-WIND",
                "event_ticker": "KXTRUMPSAY-26MAR09",
                "series_ticker": "KXTRUMPSAY",
                "speaker": "trump",
                "primary_phrase": "wind",
                "phrase_variants": ["wind", "windmills"],
                "event_context": "rally",
                "rules_primary": "...",
                "yes_ask_dollars": "0.45",
                "yes_bid_dollars": "0.42",
                "no_ask_dollars": "0.55",
                "no_bid_dollars": "0.58",
                "volume_24h": 150,
                "open_interest": 300,
                "close_time": "2026-03-09T23:59:59Z",
            },
            {
                "ticker": "KXLEAVITTMENTION-26MAR01-BORDER",
                "event_ticker": "KXLEAVITTMENTION-26MAR01",
                "series_ticker": "KXLEAVITTMENTION",
                "speaker": "leavitt",
                "primary_phrase": "border",
                "phrase_variants": ["border"],
                "event_context": "briefing",
                "rules_primary": "...",
                "yes_ask_dollars": "0.60",
                "yes_bid_dollars": "0.57",
                "no_ask_dollars": "0.40",
                "no_bid_dollars": "0.43",
                "volume_24h": 80,
                "open_interest": 200,
                "close_time": "2026-03-01T23:59:59Z",
            },
        ],
    }


class TestFromCache:
    def test_loads_markets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "markets.json"
            path.write_text(json.dumps(_sample_cache_data()))
            cat = LiveMarketCatalog.from_cache(path)

        assert len(cat.markets) == 2
        assert cat.markets[0].ticker == "KXTRUMPSAY-26MAR09-WIND"

    def test_missing_file_returns_empty(self):
        cat = LiveMarketCatalog.from_cache(Path("/nonexistent/file.json"))
        assert len(cat.markets) == 0

    def test_indexes_work(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "markets.json"
            path.write_text(json.dumps(_sample_cache_data()))
            cat = LiveMarketCatalog.from_cache(path)

        assert cat.get("KXTRUMPSAY-26MAR09-WIND") is not None
        assert cat.get("NONEXISTENT") is None
        assert len(cat.for_speaker("trump")) == 1
        assert len(cat.for_speaker("leavitt")) == 1
        assert sorted(cat.speakers) == ["leavitt", "trump"]


class TestAllPhrases:
    def test_deduped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "markets.json"
            path.write_text(json.dumps(_sample_cache_data()))
            cat = LiveMarketCatalog.from_cache(path)

        phrases = cat.all_phrases()
        assert "wind" in phrases
        assert "windmills" in phrases
        assert "border" in phrases
        assert len(phrases) == 3

    def test_keeps_generic_identity_phrases(self):
        data = _sample_cache_data()
        data["markets"][0]["phrase_variants"] = ["trump", "president", "wind"]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "markets.json"
            path.write_text(json.dumps(data))
            cat = LiveMarketCatalog.from_cache(path)

        phrases = cat.all_phrases()
        assert "wind" in phrases
        assert "trump" in phrases
        assert "president" in phrases

    def test_phrases_for_speaker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "markets.json"
            path.write_text(json.dumps(_sample_cache_data()))
            cat = LiveMarketCatalog.from_cache(path)

        assert cat.phrases_for_speaker("trump") == ["wind", "windmills"]
        assert cat.phrases_for_speaker("leavitt") == ["border"]


class TestMarketPhrasesMap:
    def test_map_structure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "markets.json"
            path.write_text(json.dumps(_sample_cache_data()))
            cat = LiveMarketCatalog.from_cache(path)

        mp = cat.market_phrases_map()
        assert mp["KXTRUMPSAY-26MAR09-WIND"] == ["wind", "windmills"]
        assert mp["KXLEAVITTMENTION-26MAR01-BORDER"] == ["border"]

    def test_map_keeps_generic_identity_phrases(self):
        data = _sample_cache_data()
        data["markets"][0]["phrase_variants"] = ["trump", "president"]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "markets.json"
            path.write_text(json.dumps(data))
            cat = LiveMarketCatalog.from_cache(path)

        mp = cat.market_phrases_map()
        assert mp["KXTRUMPSAY-26MAR09-WIND"] == ["trump", "president"]


class TestToMockFormat:
    def test_backward_compat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "markets.json"
            path.write_text(json.dumps(_sample_cache_data()))
            cat = LiveMarketCatalog.from_cache(path)

        mock_markets, market_phrases = cat.to_mock_format()
        assert len(mock_markets) == 2
        assert mock_markets[0]["market_id"] == "KXTRUMPSAY-26MAR09-WIND"
        assert market_phrases["KXTRUMPSAY-26MAR09-WIND"] == ["wind", "windmills"]
