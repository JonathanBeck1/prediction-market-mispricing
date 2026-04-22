"""Load Polymarket signals with confidence metadata for scorer blending."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

POLY_CACHE = Path("data/polymarket_prices.json")


STRONG_MATCH_REASONS = {"EXACT_PHRASE", "TOKEN_OVERLAP_HIGH", "SUBSTRING_MATCH"}


@dataclass(frozen=True)
class PolySignal:
    phrase: str
    yes_price: float
    speaker: str
    event_title: str
    timeframe: str
    confidence_score: float
    confidence_bucket: str
    quality_score: float
    volume: float
    kalshi_ticker: str
    updated_at: str
    match_reasons: tuple[str, ...] = ()
    book_spread: float = 0.0
    book_depth: float = 0.0

    @property
    def has_strong_match(self) -> bool:
        return bool(set(self.match_reasons) & STRONG_MATCH_REASONS)


@dataclass
class PolymarketPrices:
    """Phrase-keyed store of Polymarket YES prices and confidence."""
    _by_phrase: dict[str, list[PolySignal]] = field(default_factory=dict, repr=False)
    # speaker -> timeframe -> list of signals (for pool fallback)
    _by_speaker_timeframe: dict[str, dict[str, list[PolySignal]]] = field(
        default_factory=dict, repr=False
    )
    fetched_at: str = ""
    usable_markets: int = 0

    @staticmethod
    def _normalize_phrase(phrase: str) -> str:
        low = (phrase or "").lower().strip()
        low = re.sub(r"[“”\"']", "", low)
        low = re.sub(r"[^a-z0-9/\s-]", " ", low)
        low = re.sub(r"\s+", " ", low).strip()
        return low

    @classmethod
    def from_cache(cls, path: Path = POLY_CACHE) -> PolymarketPrices:
        store = cls()
        if not path.exists():
            logger.info("No Polymarket cache at %s — run 'python3 scripts/fetch_polymarket.py'", path)
            return store

        data = json.loads(path.read_text(encoding="utf-8"))
        store.fetched_at = data.get("fetched_at", "")
        store.usable_markets = int(data.get("usable_markets", 0) or 0)
        count = 0
        for event in data.get("events", []):
            speaker = event.get("speaker", "other")
            event_title = event.get("title", "")
            timeframe = event.get("timeframe", "")
            for m in event.get("markets", []):
                phrase = store._normalize_phrase(m.get("phrase_norm") or m.get("phrase", ""))
                if not phrase:
                    continue
                is_usable = bool(m.get("is_usable", True))
                confidence = float(m.get("confidence_score", 0.0) or 0.0)
                if not is_usable and confidence < 0.35:
                    continue
                match_reasons = tuple(m.get("match_reasons", []))
                pp = PolySignal(
                    phrase=phrase,
                    yes_price=float(m.get("yes_price", 0)),
                    speaker=speaker,
                    event_title=event_title,
                    timeframe=timeframe,
                    confidence_score=confidence,
                    confidence_bucket=str(m.get("confidence_bucket", "low")),
                    quality_score=float(m.get("quality_score", 0.0) or 0.0),
                    volume=float(m.get("volume", 0.0) or 0.0),
                    kalshi_ticker=str(m.get("kalshi_ticker", "")),
                    updated_at=str(m.get("updated_at", "")),
                    match_reasons=match_reasons,
                    book_spread=float(m.get("book_spread", 0) or 0),
                    book_depth=round(
                        float(m.get("book_bid_depth", 0) or 0) + float(m.get("book_ask_depth", 0) or 0), 2
                    ),
                )
                store._by_phrase.setdefault(phrase, []).append(pp)
                # Also index by speaker+timeframe for pool fallback
                (
                    store._by_speaker_timeframe
                    .setdefault(speaker, {})
                    .setdefault(timeframe, [])
                    .append(pp)
                )
                count += 1

        logger.info("Loaded %d Polymarket prices from cache (%s)", count, store.fetched_at[:19])
        return store

    def get_best(
        self,
        phrase: str,
        speaker: str | None = None,
        timeframe: str | None = None,
    ) -> PolySignal | None:
        """Get best Polymarket signal by confidence, then price."""
        phrase_key = self._normalize_phrase(phrase)
        matches = self._by_phrase.get(phrase_key, [])
        if not matches:
            return None

        if speaker:
            speaker_matches = [p for p in matches if p.speaker == speaker.lower()]
            if speaker_matches:
                matches = speaker_matches

        if timeframe:
            tf_matches = [p for p in matches if p.timeframe == timeframe]
            if tf_matches:
                matches = tf_matches

        return max(matches, key=lambda p: (p.confidence_score, p.quality_score, p.yes_price))

    def get_pool_signal(
        self,
        speaker: str,
        timeframe: str | None = None,
        min_markets: int = 3,
        min_quality: float = 0.50,
    ) -> PolySignal | None:
        """Return a synthetic signal from the weighted average of all same-speaker
        same-timeframe markets, for use when no exact phrase match exists.

        This gives a calibration anchor: "across all open Polymarket markets
        for Trump this week, the average YES rate is 42%" — useful even without
        a specific phrase match.  Confidence is capped at 0.35 (weak signal).
        """
        speaker_key = (speaker or "").lower()
        speaker_pool = self._by_speaker_timeframe.get(speaker_key, {})
        if not speaker_pool:
            return None

        # Prefer exact timeframe; fall back to all timeframes for this speaker
        candidates: list[PolySignal] = []
        if timeframe and timeframe in speaker_pool:
            candidates = speaker_pool[timeframe]
        if len(candidates) < min_markets:
            # Flatten all timeframes
            for signals in speaker_pool.values():
                candidates.extend(signals)

        # Filter by quality
        good = [p for p in candidates if p.quality_score >= min_quality]
        if len(good) < min_markets:
            good = candidates

        if len(good) < min_markets:
            return None

        # Weighted average by quality score
        total_weight = sum(max(0.01, p.quality_score) for p in good)
        avg_yes = sum(p.yes_price * max(0.01, p.quality_score) for p in good) / total_weight
        avg_vol = sum(p.volume for p in good) / len(good)
        first = good[0]

        return PolySignal(
            phrase="__pool__",
            yes_price=round(avg_yes, 4),
            speaker=speaker_key,
            event_title=f"[pool: {len(good)} markets]",
            timeframe=timeframe or "unknown",
            confidence_score=0.30,   # deliberately low — this is a weak signal
            confidence_bucket="low",
            quality_score=0.30,
            volume=avg_vol,
            kalshi_ticker="",
            updated_at=first.updated_at,
            match_reasons=("POOL_AVERAGE",),
            book_spread=0.0,
            book_depth=0.0,
        )

    def get_signal(
        self,
        phrase: str,
        speaker: str | None = None,
        timeframe: str | None = None,
    ) -> PolySignal | None:
        return self.get_best(phrase, speaker=speaker, timeframe=timeframe)

    def get_yes_price(
        self,
        phrase: str,
        speaker: str | None = None,
        timeframe: str | None = None,
    ) -> float | None:
        """Get the Polymarket YES price for a phrase, or None if not found."""
        p = self.get_best(phrase, speaker=speaker, timeframe=timeframe)
        return p.yes_price if p else None

    @property
    def phrase_count(self) -> int:
        return sum(len(v) for v in self._by_phrase.values())
