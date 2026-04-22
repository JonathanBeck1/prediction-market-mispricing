from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.wallet_flow import WalletFlowSignals
from scripts.fetch_wallet_flow import _compute_signal


def test_wallet_signal_store_prefers_speaker_and_timeframe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "wallet.json"
        p.write_text(
            json.dumps(
                {
                    "fetched_at": "2026-03-10T00:00:00Z",
                    "signals": [
                        {
                            "phrase": "tariff",
                            "speaker": "trump",
                            "timeframe": "event",
                            "smart_flow_bias": "up",
                            "wallet_alpha_score": 0.5,
                            "confidence": 0.7,
                            "kalshi_ticker": "KX1",
                            "trade_count": 6,
                            "unique_wallets": 3,
                            "total_volume": 90,
                        },
                        {
                            "phrase": "tariff",
                            "speaker": "leavitt",
                            "timeframe": "event",
                            "smart_flow_bias": "down",
                            "wallet_alpha_score": -0.3,
                            "confidence": 0.8,
                            "kalshi_ticker": "KX2",
                            "trade_count": 8,
                            "unique_wallets": 4,
                            "total_volume": 120,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        store = WalletFlowSignals.from_cache(p)
        trump = store.get_signal("tariff", speaker="trump", timeframe="event")
        leavitt = store.get_signal("tariff", speaker="leavitt", timeframe="event")
        assert trump is not None and leavitt is not None
        assert trump.speaker == "trump"
        assert leavitt.speaker == "leavitt"


def test_compute_signal_builds_nonzero_alpha() -> None:
    import time as _time
    now_ts = _time.time()
    trades = [
        {"proxyWallet": "0x1", "outcome": "Yes", "side": "BUY", "size": 100, "price": 0.30},
        {"proxyWallet": "0x1", "outcome": "No", "side": "SELL", "size": 40, "price": 0.70},
        {"proxyWallet": "0x2", "outcome": "No", "side": "BUY", "size": 10, "price": 0.50},
        {"proxyWallet": "0x3", "outcome": "Yes", "side": "BUY", "size": 30, "price": 0.15},
    ]
    signal = _compute_signal(trades, now_ts)
    assert signal["trade_count"] == 4
    assert signal["unique_wallets"] == 3
    assert signal["confidence"] > 0
    assert abs(signal["wallet_alpha_score"]) > 0
    assert signal["avg_trade_price"] > 0


def test_compute_signal_detects_extreme_bets() -> None:
    import time as _time
    now_ts = _time.time()
    trades = [
        {"proxyWallet": "0xa1", "outcome": "Yes", "side": "BUY", "size": 500, "price": 0.05},
        {"proxyWallet": "0xa2", "outcome": "Yes", "side": "BUY", "size": 200, "price": 0.08},
        {"proxyWallet": "0xa3", "outcome": "No", "side": "BUY", "size": 50, "price": 0.50},
    ]
    signal = _compute_signal(trades, now_ts)
    assert signal["extreme_bet_count"] >= 2
    assert len(signal["extreme_bets"]) >= 2


def test_conviction_weight_extreme_prices() -> None:
    from scripts.fetch_wallet_flow import _conviction_weight
    assert _conviction_weight(0.05) > _conviction_weight(0.50)
    assert _conviction_weight(0.95) > _conviction_weight(0.50)
    assert _conviction_weight(None) == 1.0


def test_wallet_signal_v2_fields_loaded() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "wallet_v2.json"
        p.write_text(
            json.dumps({
                "fetched_at": "2026-03-10T00:00:00Z",
                "version": 2,
                "signals": [{
                    "phrase": "iran",
                    "speaker": "leavitt",
                    "timeframe": "event",
                    "smart_flow_bias": "up",
                    "wallet_alpha_score": 0.6,
                    "confidence": 0.8,
                    "kalshi_ticker": "KX99",
                    "trade_count": 20,
                    "unique_wallets": 8,
                    "total_volume": 500,
                    "conviction_weighted_flow": 350.0,
                    "avg_trade_price": 0.25,
                    "extreme_bet_count": 3,
                    "reputation_weighted_alpha": 0.4,
                }],
            }),
            encoding="utf-8",
        )
        store = WalletFlowSignals.from_cache(p)
        assert store.version == 2
        sig = store.get_signal("iran", speaker="leavitt")
        assert sig is not None
        assert sig.conviction_weighted_flow == 350.0
        assert sig.extreme_bet_count == 3
        assert sig.reputation_weighted_alpha == 0.4
        assert sig.avg_trade_price == 0.25
