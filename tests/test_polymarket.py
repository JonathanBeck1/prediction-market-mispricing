from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.polymarket import PolymarketPrices


def _write_cache(path: Path) -> None:
    payload = {
        "fetched_at": "2026-03-09T12:00:00Z",
        "usable_markets": 3,
        "events": [
            {
                "speaker": "trump",
                "title": "What will Trump say before Mar 16, 2026?",
                "timeframe": "event",
                "markets": [
                    {
                        "phrase": "Tariff",
                        "phrase_norm": "tariff",
                        "yes_price": 0.61,
                        "quality_score": 0.9,
                        "confidence_score": 0.82,
                        "confidence_bucket": "high",
                        "is_usable": True,
                        "kalshi_ticker": "KXTRUMPSAY-26MAR16-TARI",
                        "updated_at": "2026-03-09T11:59:00Z",
                        "volume": 200.0,
                    },
                    {
                        "phrase": "Tariff",
                        "phrase_norm": "tariff",
                        "yes_price": 0.73,
                        "quality_score": 0.2,
                        "confidence_score": 0.2,
                        "confidence_bucket": "low",
                        "is_usable": False,
                        "kalshi_ticker": "",
                        "updated_at": "2026-03-09T11:00:00Z",
                        "volume": 1.0,
                    },
                    {
                        "phrase": "Border",
                        "phrase_norm": "border",
                        "yes_price": 0.42,
                        "quality_score": 0.8,
                        "confidence_score": 0.65,
                        "confidence_bucket": "medium",
                        "is_usable": True,
                        "kalshi_ticker": "KXTRUMPSAY-26MAR16-BORD",
                        "updated_at": "2026-03-09T11:30:00Z",
                        "volume": 150.0,
                    },
                ],
            },
            {
                "speaker": "leavitt",
                "title": "What will Leavitt say during briefing?",
                "timeframe": "event",
                "markets": [
                    {
                        "phrase": "Tariff",
                        "phrase_norm": "tariff",
                        "yes_price": 0.55,
                        "quality_score": 0.8,
                        "confidence_score": 0.78,
                        "confidence_bucket": "medium",
                        "is_usable": True,
                        "kalshi_ticker": "KXSECPRESSMENTION-26MAR29-TARI",
                        "updated_at": "2026-03-09T11:10:00Z",
                        "volume": 120.0,
                    }
                ],
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_from_cache_skips_low_confidence_unusable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "poly.json"
        _write_cache(cache)
        store = PolymarketPrices.from_cache(cache)

        sig = store.get_signal("tariff", speaker="trump", timeframe="event")
        assert sig is not None
        assert sig.yes_price == 0.61
        assert sig.confidence_bucket == "high"


def test_get_best_prefers_speaker_and_timeframe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "poly.json"
        _write_cache(cache)
        store = PolymarketPrices.from_cache(cache)

        trump = store.get_signal("tariff", speaker="trump", timeframe="event")
        leavitt = store.get_signal("tariff", speaker="leavitt", timeframe="event")

        assert trump is not None and leavitt is not None
        assert trump.speaker == "trump"
        assert leavitt.speaker == "leavitt"
        assert trump.yes_price == 0.61
        assert leavitt.yes_price == 0.55


def test_get_yes_price_back_compat() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "poly.json"
        _write_cache(cache)
        store = PolymarketPrices.from_cache(cache)

        assert store.get_yes_price("border", speaker="trump") == 0.42
