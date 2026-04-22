"""Live Kalshi watcher that polls the public GET /markets endpoint.

No API key required -- uses only public market data (best bid/ask, volume,
open interest). Replaces MockKalshiWatcher when KALSHI_MOCK=0.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from app.kalshi_api import LiveMarketCatalog, CACHE_PATH
from app.utils import append_jsonl, utc_now_iso

logger = logging.getLogger(__name__)

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Snapshot throttle: only write a new row when price moves by >= this amount
# OR when the last snapshot for this market is older than SNAP_MAX_AGE_SEC.
# This keeps market_snapshots from growing at 2 rows/min per market (880/min total)
# while still capturing every meaningful price move for price-velocity calculations.
SNAP_PRICE_DELTA  = 0.005   # 0.5¢ minimum move to trigger a write
SNAP_MAX_AGE_SEC  = 300     # heartbeat: write at least every 5 minutes regardless

# Seed series tickers — the watcher unions these with whatever series
# appear in the live market cache, so this list doesn't need to be exhaustive.
MENTION_SERIES: dict[str, str] = {
    "KXTRUMPSAY": "trump",
    "KXTRUMPSAYEP": "trump",
    "KXTRUMPMENTION": "trump",
    "KXTRUMPMENTIONB": "trump",
    "KXDJTRALLY": "trump",
    "KXDJTINVESTMENT": "trump",
    "KXDJTWOMENS": "trump",
    "KXDJTCONF": "trump",
    "KXTRUMPSAYMONTH": "trump",
    "KXTRUMPSAYNICKNAME": "trump",
    "KXTRUMPSAYTRUMP": "trump",
    "KXTRUMPLATE": "trump",
    "KXTRUMPMENTIONDURATION": "trump",
    "KXLEAVITTMENTION": "leavitt",
    "KXLEAVITTSMFMENTION": "leavitt",
    "KXSECPRESSMENTION": "leavitt",
    "KXLEAVITTLATE": "leavitt",
    "KXLEAVITTMENTIONDURATION": "leavitt",
    "KXMAMDANIMENTION": "mamdani",
    "KXTRUMPSAYMAM": "mamdani",
    # "KXMENTION": "auto",          # removed: not tracking
    "KXPRESMENTION": "trump",
    "KXFEDMENTION": "powell",
    # "KXWHPRESSBRIEFING": "whitehouse",  # removed: not tracking
    # "KXSTARMERMENTIONB": "starmer",  # removed: not tracking
    # "KXCARNEYMENTION": "carney",      # removed: not tracking
    # "KXHOMANMENTION": "homan",        # removed: not tracking
    "KXNBAMENTION": "nba",
    "KXMLBMENTION": "mlb",
    "KXFIGHTMENTION": "mma",
    # "KXSURVIVORMENTION": "survivor",  # removed: not tracking
    # "KXWBCMENTION": "wbc",            # removed: not tracking
    "KXNCAABMENTION": "ncaab",
    # "KXENTMENTION": "entertainment",  # removed: not tracking
    # "KXEARNINGSMENTIONULTA": "ulta",  # removed: not tracking
    # "KXEARNINGSMENTIONEA": "ea",      # removed: not tracking
    # "KXEARNINGSMENTIONADBE": "adobe", # removed: not tracking
    # "KXJENSENMENTION": "jensen",      # removed: not tracking
}


def _fetch_json(url: str, timeout: float = 20, max_attempts: int = 6) -> dict:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "kalshi-edge-watcher/1.0",
        },
    )
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            last_error = e
            # Kalshi public feed can intermittently throttle.
            if e.code == 429 and attempt < max_attempts - 1:
                time.sleep(min(10.0, 0.5 * (attempt + 1)))
                continue
            raise
        except (URLError, TimeoutError, OSError) as e:
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(min(5.0, 0.4 * (attempt + 1)))
                continue
            raise
    if last_error is not None:
        raise last_error
    return {}


def _parse_dollars(val: str | int | float | None) -> float:
    """Parse a FixedPointDollars string like '0.5600' to a float."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _parse_fp_count(val: str | int | float | None) -> float:
    """Parse a FixedPointCount string like '10.00' to a float."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


@dataclass
class LiveKalshiWatcher:
    conn: sqlite3.Connection
    raw_log_path: Path
    interval_sec: float
    catalog: LiveMarketCatalog = field(default_factory=LiveMarketCatalog)
    api_timeout_sec: float = 20
    _tick: int = 0
    _last_error_ts: float = 0
    _series_refresh_every_ticks: int = 12
    # Throttle cache: market_id → (yes_ask, no_ask, monotonic_time_of_last_write)
    _last_snap: dict = field(default_factory=dict)

    def bootstrap_markets(self) -> None:
        """Load the market catalog from cache and upsert into DB."""
        if not self.catalog.markets:
            self.catalog = LiveMarketCatalog.from_cache(CACHE_PATH)

        if not self.catalog.markets:
            logger.warning(
                "No cached markets. Run 'make fetch-markets' first. "
                "Falling back to empty catalog."
            )
            return

        for m in self.catalog.markets:
            self.conn.execute(
                """
                INSERT INTO markets (market_id, slug, subject, prompt)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    subject = excluded.subject,
                    prompt = excluded.prompt
                """,
                (m.ticker, m.ticker.lower(), m.speaker,
                 f"Will {m.speaker} say \"{m.primary_phrase}\"?"),
            )
        self.conn.commit()
        logger.info("Bootstrapped %d live markets from Kalshi cache", len(self.catalog.markets))

    def _series_to_poll(self) -> list[str]:
        """Union static allowlist with any series present in live cache."""
        dynamic = {m.series_ticker for m in self.catalog.markets if m.series_ticker}
        return sorted(set(MENTION_SERIES.keys()) | dynamic)

    def _fetch_series_markets(self, series_ticker: str) -> list[dict]:
        """Fetch all open markets for a single series from Kalshi API."""
        all_markets: list[dict] = []
        cursor = ""
        pages = 0
        try:
            while True:
                params = {
                    "series_ticker": series_ticker,
                    "status": "open",
                    "limit": "200",
                }
                if cursor:
                    params["cursor"] = cursor
                url = f"{API_BASE}/markets?{urlencode(params)}"
                data = _fetch_json(url, timeout=self.api_timeout_sec)
                markets = data.get("markets", [])
                if not markets:
                    break
                all_markets.extend(markets)
                cursor = data.get("cursor", "")
                pages += 1
                if not cursor or pages >= 100:
                    break
                time.sleep(0.1)
            return all_markets
        except (HTTPError, URLError, TimeoutError, OSError) as e:
            now = time.time()
            if now - self._last_error_ts > 60:
                logger.warning("Kalshi API error for %s: %s", series_ticker, e)
                self._last_error_ts = now
            return []

    def _market_to_snapshot(self, m: dict) -> dict[str, float]:
        """Extract a snapshot dict from a Kalshi API market object."""
        yes_bid = _parse_dollars(m.get("yes_bid_dollars"))
        yes_ask = _parse_dollars(m.get("yes_ask_dollars"))
        no_bid = _parse_dollars(m.get("no_bid_dollars"))
        no_ask = _parse_dollars(m.get("no_ask_dollars"))
        spread = round(yes_ask - yes_bid, 4) if yes_ask > yes_bid else 0.0
        depth_yes = _parse_fp_count(m.get("yes_bid_size_fp", "0"))
        depth_no = _parse_fp_count(m.get("yes_ask_size_fp", "0"))
        volume_1h = float(m.get("volume_24h", 0))

        return {
            "yes_bid": round(yes_bid, 4),
            "yes_ask": round(yes_ask, 4),
            "no_bid": round(no_bid, 4),
            "no_ask": round(no_ask, 4),
            "spread": round(spread, 4),
            "depth_yes": round(depth_yes, 2),
            "depth_no": round(depth_no, 2),
            "volume_1h": round(volume_1h, 2),
        }

    def run_once(self) -> int:
        """Poll Kalshi API and insert snapshots for all mention markets."""
        ts = utc_now_iso()
        inserted = 0
        all_api_markets: dict[str, dict] = {}

        # Periodically refresh from cache so newly listed series are polled
        # without code changes or process restarts.
        if self._tick == 0 or self._tick % self._series_refresh_every_ticks == 0:
            self.catalog = LiveMarketCatalog.from_cache(CACHE_PATH)

        for series_ticker in self._series_to_poll():
            api_markets = self._fetch_series_markets(series_ticker)
            for m in api_markets:
                ticker = m.get("ticker", "")
                if ticker:
                    all_api_markets[ticker] = m
            if api_markets:
                time.sleep(0.3)

        known_tickers = {
            row["market_id"]
            for row in self.conn.execute("SELECT market_id FROM markets").fetchall()
        }

        now_mono = time.monotonic()
        skipped = 0

        for ticker, m in all_api_markets.items():
            if ticker not in known_tickers:
                cached = self.catalog.get(ticker)
                speaker = MENTION_SERIES.get(
                    m.get("event_ticker", "").rsplit("-", 1)[0],
                    _guess_speaker(m)
                )
                series = _extract_series(m.get("ticker", ""))
                speaker = MENTION_SERIES.get(series, speaker)
                if cached and cached.speaker:
                    speaker = cached.speaker
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO markets (market_id, slug, subject, prompt)
                    VALUES (?, ?, ?, ?)
                    """,
                    (ticker, ticker.lower(), speaker,
                     m.get("title", ticker)),
                )
                known_tickers.add(ticker)

            snap = self._market_to_snapshot(m)

            # Throttle: skip write if price hasn't moved and heartbeat hasn't expired.
            # Reduces writes from ~2/min/market to ~12/hr/market at steady state.
            last = self._last_snap.get(ticker)
            if last is not None:
                price_moved = (
                    abs(snap["yes_ask"] - last[0]) >= SNAP_PRICE_DELTA
                    or abs(snap["no_ask"] - last[1]) >= SNAP_PRICE_DELTA
                )
                heartbeat_due = (now_mono - last[2]) >= SNAP_MAX_AGE_SEC
                if not price_moved and not heartbeat_due:
                    skipped += 1
                    continue

            raw_payload = {"ts": ts, "market_id": ticker, **snap, "raw_api": m}
            self.conn.execute(
                """
                INSERT INTO market_snapshots (
                    ts, market_id, yes_bid, yes_ask, no_bid, no_ask,
                    spread, depth_yes, depth_no, volume_1h, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts, ticker,
                    snap["yes_bid"], snap["yes_ask"],
                    snap["no_bid"], snap["no_ask"],
                    snap["spread"], snap["depth_yes"], snap["depth_no"],
                    snap["volume_1h"],
                    json.dumps(raw_payload, ensure_ascii=True, default=str),
                ),
            )
            append_jsonl(self.raw_log_path, {
                "ts": ts, "market_id": ticker, **snap
            })
            self._last_snap[ticker] = (snap["yes_ask"], snap["no_ask"], now_mono)
            inserted += 1

        self.conn.commit()
        self._tick += 1
        if inserted > 0 or skipped > 0:
            logger.info(
                "Live watcher: %d snapshots written, %d skipped (no price move) at %s",
                inserted, skipped, ts,
            )
        else:
            logger.debug("Live watcher: no markets fetched at %s", ts)
        return inserted


def _extract_series(ticker: str) -> str:
    """Extract likely series ticker from a market ticker like KXTRUMPSAY-26MAR09-WIND."""
    parts = ticker.split("-")
    return parts[0] if parts else ""


_WATCHER_SPEAKER_HINTS: list[tuple[str, list[str]]] = [
    ("leavitt", ["leavitt", "press secretary"]),
    ("mamdani", ["mamdani"]),
    ("fed", ["fomc", "federal reserve", "fed chair", "powell"]),
    ("nba", ["nba"]),
    ("trump", ["trump", "donald j. trump"]),
]


def _guess_speaker(m: dict) -> str:
    """Best-effort speaker detection from market title/rules."""
    title = (m.get("title", "")).lower()

    import re
    hit = re.search(r"what will\s+(.+?)\s+(?:say|mention)", title)
    if hit:
        subj = hit.group(1).strip()
        for spk, kws in _WATCHER_SPEAKER_HINTS:
            if any(kw in subj for kw in kws):
                return spk

    text = (title + " " + m.get("rules_primary", "")).lower()
    for spk, kws in _WATCHER_SPEAKER_HINTS:
        if any(kw in text for kw in kws):
            return spk

    ticker = str(m.get("ticker", "")).upper()
    series = ticker.split("-")[0] if ticker else ""
    return MENTION_SERIES.get(series, "auto")
