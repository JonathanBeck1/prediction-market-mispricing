"""Load wallet-flow alpha signals generated from Polymarket trade flow (V2).

V2 adds:
  - conviction_weighted_flow (price-distance weighting)
  - extreme_bet_count / extreme_bets
  - reputation_weighted_alpha (from /closed-positions profitability)
  - avg_trade_price
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

WALLET_SIGNAL_CACHE = Path("data/wallet_signals.json")


@dataclass(frozen=True)
class WalletSignal:
    phrase: str
    speaker: str
    timeframe: str
    smart_flow_bias: str
    wallet_alpha_score: float
    confidence: float
    kalshi_ticker: str
    trade_count: int
    unique_wallets: int
    total_volume: float
    conviction_weighted_flow: float = 0.0
    avg_trade_price: float = 0.0
    extreme_bet_count: int = 0
    reputation_weighted_alpha: float = 0.0


@dataclass
class WalletFlowSignals:
    _by_phrase: dict[str, list[WalletSignal]] = field(default_factory=dict, repr=False)
    fetched_at: str = ""
    version: int = 1

    @staticmethod
    def _normalize_phrase(phrase: str) -> str:
        low = (phrase or "").lower().strip()
        low = re.sub(r"[\u201c\u201d\"']", "", low)
        low = re.sub(r"[^a-z0-9/\s-]", " ", low)
        return re.sub(r"\s+", " ", low).strip()

    @classmethod
    def from_cache(cls, path: Path = WALLET_SIGNAL_CACHE) -> WalletFlowSignals:
        store = cls()
        if not path.exists():
            logger.info("No wallet-signal cache at %s \u2014 run 'python3 scripts/fetch_wallet_flow.py'", path)
            return store

        data = json.loads(path.read_text(encoding="utf-8"))
        store.fetched_at = str(data.get("fetched_at", ""))
        store.version = int(data.get("version", 1) or 1)
        count = 0
        for row in data.get("signals", []):
            phrase = store._normalize_phrase(str(row.get("phrase", "")))
            if not phrase:
                continue
            signal = WalletSignal(
                phrase=phrase,
                speaker=str(row.get("speaker", "")).lower().strip(),
                timeframe=str(row.get("timeframe", "unknown")).strip().lower(),
                smart_flow_bias=str(row.get("smart_flow_bias", "neutral")).strip().lower(),
                wallet_alpha_score=float(row.get("wallet_alpha_score", 0.0) or 0.0),
                confidence=float(row.get("confidence", 0.0) or 0.0),
                kalshi_ticker=str(row.get("kalshi_ticker", "")),
                trade_count=int(row.get("trade_count", 0) or 0),
                unique_wallets=int(row.get("unique_wallets", 0) or 0),
                total_volume=float(row.get("total_volume", 0.0) or 0.0),
                conviction_weighted_flow=float(row.get("conviction_weighted_flow", 0.0) or 0.0),
                avg_trade_price=float(row.get("avg_trade_price", 0.0) or 0.0),
                extreme_bet_count=int(row.get("extreme_bet_count", 0) or 0),
                reputation_weighted_alpha=float(row.get("reputation_weighted_alpha", 0.0) or 0.0),
            )
            store._by_phrase.setdefault(phrase, []).append(signal)
            count += 1

        logger.info("Loaded %d wallet-flow signals (v%d) from cache (%s)", count, store.version, store.fetched_at[:19])
        return store

    def get_signal(
        self,
        phrase: str,
        speaker: str | None = None,
        timeframe: str | None = None,
    ) -> WalletSignal | None:
        key = self._normalize_phrase(phrase)
        matches = self._by_phrase.get(key, [])
        if not matches:
            return None

        if speaker:
            sm = [m for m in matches if m.speaker == speaker.lower().strip()]
            if sm:
                matches = sm
        if timeframe:
            tfm = [m for m in matches if m.timeframe == timeframe]
            if tfm:
                matches = tfm

        return max(matches, key=lambda m: (m.confidence, abs(m.wallet_alpha_score), m.trade_count))

    @property
    def signal_count(self) -> int:
        return sum(len(v) for v in self._by_phrase.values())
