"""Load real Kalshi markets from the local cache (data/kalshi_markets.json).

The cache is populated by running: python3 scripts/fetch_markets.py

When KALSHI_MOCK=1, falls back to the hardcoded mock catalog.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_PATH = Path("data/kalshi_markets.json")

# Series prefixes that should never be scored — sports, entertainment, earnings.
_BLOCKED_SERIES_PREFIXES: tuple[str, ...] = (
    # Sports broadcast series are now tracked (priors added session 46).
    # "KXNBA", "KXNCAAB", "KXFIGHT" removed from block list.
    "KXWBC",
    "KXSURVIVORMENTION",
    "KXENTMENTION",
    "KXJENSENMENTION",
    "KXEARNINGS",
    "KXTOPSONG",
)


def _is_blocked_series(series_ticker: str) -> bool:
    return series_ticker.upper().startswith(_BLOCKED_SERIES_PREFIXES)


def _normalize_phrase(phrase: str) -> str:
    return phrase.strip().lower()


def _is_usable_phrase(phrase: str) -> bool:
    p = _normalize_phrase(phrase)
    if not p:
        return False
    if len(p) <= 2:
        return False
    return True


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    speaker: str
    primary_phrase: str
    phrase_variants: tuple[str, ...]
    event_context: str
    rules_primary: str
    yes_ask_dollars: str
    yes_bid_dollars: str
    no_ask_dollars: str
    no_bid_dollars: str
    volume_24h: int
    open_interest: int
    close_time: str
    open_time: str = ""
    market_status: str = ""   # "active", "open", "closed", "settled", etc.
    is_phrase_market: bool = True


@dataclass
class LiveMarketCatalog:
    """Replaces the hardcoded MOCK_MARKETS / MARKET_PHRASES with real data."""
    markets: list[KalshiMarket] = field(default_factory=list)
    _by_ticker: dict[str, KalshiMarket] = field(default_factory=dict, repr=False)
    _by_speaker: dict[str, list[KalshiMarket]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_cache(cls, path: Path = CACHE_PATH) -> LiveMarketCatalog:
        if not path.exists():
            logger.warning("No market cache at %s -- run 'python3 scripts/fetch_markets.py'", path)
            return cls()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse market cache %s: %s", path, exc)
            return cls()
        markets = []
        skipped = 0
        for m in data.get("markets", []):
            try:
                ticker = m["ticker"]
                if not ticker:
                    skipped += 1
                    continue
                # Skip sports, entertainment, and earnings series entirely.
                if _is_blocked_series(m.get("series_ticker", "")):
                    skipped += 1
                    continue
                km = KalshiMarket(
                    ticker=ticker,
                    event_ticker=m.get("event_ticker", ""),
                    series_ticker=m.get("series_ticker", ""),
                    speaker=m.get("speaker", ""),
                    primary_phrase=m.get("primary_phrase", ""),
                    phrase_variants=tuple(m.get("phrase_variants", [])),
                    event_context=m.get("event_context", ""),
                    rules_primary=m.get("rules_primary", ""),
                    yes_ask_dollars=m.get("yes_ask_dollars", ""),
                    yes_bid_dollars=m.get("yes_bid_dollars", ""),
                    no_ask_dollars=m.get("no_ask_dollars", ""),
                    no_bid_dollars=m.get("no_bid_dollars", ""),
                    volume_24h=m.get("volume_24h", 0),
                    open_interest=m.get("open_interest", 0),
                    close_time=m.get("close_time", ""),
                    open_time=m.get("open_time", ""),
                    market_status=str(m.get("status", "") or "").lower(),
                    is_phrase_market=bool(
                        m.get(
                            "is_phrase_market",
                            bool(m.get("primary_phrase") or m.get("phrase_variants")),
                        )
                    ),
                )
                markets.append(km)
            except Exception:
                skipped += 1

        cat = cls(markets=markets)
        cat._build_indexes()
        logger.info(
            "Loaded %d live markets from cache (%s)%s",
            len(markets),
            data.get("fetched_at", "?"),
            f", skipped={skipped}" if skipped else "",
        )
        return cat

    def _build_indexes(self) -> None:
        self._by_ticker = {m.ticker: m for m in self.markets}
        self._by_speaker = {}
        for m in self.markets:
            self._by_speaker.setdefault(m.speaker, []).append(m)

    def get(self, ticker: str) -> KalshiMarket | None:
        return self._by_ticker.get(ticker)

    def for_speaker(self, speaker: str) -> list[KalshiMarket]:
        return self._by_speaker.get(speaker, [])

    @property
    def speakers(self) -> list[str]:
        return sorted(self._by_speaker.keys())

    def all_phrases(self) -> list[str]:
        """Deduplicated flat list of every resolution phrase (lowercase)."""
        seen: set[str] = set()
        result: list[str] = []
        for m in self.markets:
            for v in m.phrase_variants:
                low = _normalize_phrase(v)
                if not _is_usable_phrase(low):
                    continue
                if low not in seen:
                    seen.add(low)
                    result.append(low)
        return result

    def phrases_for_speaker(self, speaker: str) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for m in self.for_speaker(speaker):
            for v in m.phrase_variants:
                low = _normalize_phrase(v)
                if not _is_usable_phrase(low):
                    continue
                if low not in seen:
                    seen.add(low)
                    result.append(low)
        return result

    def market_phrases_map(self) -> dict[str, list[str]]:
        """Return {ticker: [phrase_variant_1, ...]} compatible with old MARKET_PHRASES."""
        out: dict[str, list[str]] = {}
        for m in self.markets:
            filtered = []
            for v in m.phrase_variants:
                low = _normalize_phrase(v)
                if _is_usable_phrase(low):
                    filtered.append(low)
            if filtered:
                out[m.ticker] = filtered
        return out

    def to_mock_format(self) -> tuple[list[dict], dict[str, list[str]]]:
        """Convert to the old (MOCK_MARKETS, MARKET_PHRASES) format for backward compat."""
        mock_markets = []
        market_phrases = {}
        for m in self.markets:
            mock_markets.append({
                "market_id": m.ticker,
                "slug": m.ticker.lower(),
                "subject": m.speaker,
                "prompt": f"Will {m.speaker} say \"{m.primary_phrase}\"?",
            })
            filtered = []
            for v in m.phrase_variants:
                low = _normalize_phrase(v)
                if _is_usable_phrase(low):
                    filtered.append(low)
            if filtered:
                market_phrases[m.ticker] = filtered
        return mock_markets, market_phrases
