from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pathlib import Path

from app.base_rates import BaseRateLookup
from app.config import Settings, load_settings
from app.db import init_db, connect as db_connect
from app.event_detector import EventDetector
from app.kalshi_api import LiveMarketCatalog
from app.kalshi_watcher import MockKalshiWatcher
from app.kalshi_watcher_live import LiveKalshiWatcher
from app.maintenance import MaintenanceRunner, build_default_maintenance_tasks
from app.market_family import uses_event_timing
from app.market_catalog import MARKET_PHRASES, SUBJECT_PHRASES, all_market_phrases
from app.notifier import WhatsAppNotifier
from app.phrase_matcher import PhraseMatcher
from app.live_settlements import LiveSettlements
from app.window_state import WindowStateCache
from app.rolling_rates import RollingRatesCache
from app.price_velocity import PriceVelocityCache
from app.wh_schedule import WHScheduleCache
from app.phrase_trends import PhraseTrendCache
from app.phrase_cooccurrence import CooccurrenceCache
from app.bias_map import BiasMapCache
from app.phrase_hazard import PhraseHazardCache
from app.signal_learner import SignalWeightStore
from app.bayesian_rates import BayesianRateStore
from app.phrase_correlation import PhraseCorrelationStore
from app.polymarket import PolymarketPrices
from app.bayesian_scorer import BayesianScorer
from app.scoring import ScoringEngine
from app.signals import SignalStore
from app.event_signals import EventSignalStore
from app.transcript_ingestor import TranscriptIngestor
from app.transcript_sources import (
    DirectHTTPTranscriptSource,
    FallbackTranscriptSource,
    OpenClawTranscriptSource,
    TranscriptSource,
)
from app.watchdog import Watchdog
from app.wallet_flow import WalletFlowSignals

logger = logging.getLogger(__name__)


def _build_transcript_source(settings: Settings) -> TranscriptSource:
    """Build a FallbackTranscriptSource with directhttp first, openclaw second."""
    sources: list[TranscriptSource] = []

    openclaw = OpenClawTranscriptSource(
        timeout_sec=settings.openclaw_timeout_sec,
        browser_profile=settings.openclaw_browser_profile,
    )
    directhttp = DirectHTTPTranscriptSource(timeout_sec=settings.transcript_http_timeout_sec)

    if settings.transcript_source in ("directhttp", "fallback", ""):
        sources.append(directhttp)
        sources.append(openclaw)
    elif settings.transcript_source == "openclaw":
        sources.append(openclaw)
        sources.append(directhttp)
    else:
        sources.append(directhttp)
        sources.append(openclaw)

    if len(sources) == 1:
        return sources[0]
    return FallbackTranscriptSource(sources=sources)


def _event_type_for_context(context: str) -> str:
    ctx = (context or "").strip().lower()
    mapping = {
        "rally": "rally",
        "briefing": "briefing",
        "interview": "interview",
        "townhall": "townhall",
        "general": "general",
        "address": "address",
        "announcement": "announcement",
        "presser": "presser",
    }
    return mapping.get(ctx, "other")


def _expected_duration_for_context(context: str) -> int:
    ctx = (context or "").strip().lower()
    if ctx == "briefing":
        return 2700
    if ctx == "interview":
        return 3600
    if ctx == "announcement":
        return 3600
    if ctx == "rally":
        return 5400
    if ctx == "townhall":
        return 5400
    if ctx == "wh_live":
        return 5400
    return 5400


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _seed_events_from_live_markets(conn: sqlite3.Connection, catalog: LiveMarketCatalog) -> int:
    """Auto-create scheduled events from live market event tickers.

    Creates one event per unique (speaker, event_ticker) pair so the scorer
    can match each market to its specific event and use the correct base rates.

    Key logic:
    - If ANY market in the group is currently "active" on Kalshi → seed as 'live'
      (market is active = event is happening or about to happen right now)
    - Otherwise use the market's open_time as scheduled_start (preferred over
      close_time minus duration, which gives a wrong future timestamp)
    - Falls back to close_time minus duration only if open_time is unavailable
    """
    now_dt = datetime.now(tz=timezone.utc)
    inserted = 0
    updated = 0

    # Group markets by (speaker, event_ticker), tracking whether any are active
    grouped: dict[tuple[str, str], list] = {}
    for m in catalog.markets:
        if not m.event_ticker:
            continue
        if not uses_event_timing(m.series_ticker):
            continue
        close_dt = _parse_iso(m.close_time)
        if close_dt is None:
            continue
        if close_dt < (now_dt - timedelta(hours=1)):
            continue
        key = (m.speaker, m.event_ticker)
        grouped.setdefault(key, []).append(m)

    for (_, event_ticker), markets in grouped.items():
        # Use first market for metadata; check if any market is active
        m = markets[0]
        close_dt = _parse_iso(m.close_time)
        if close_dt is None:
            continue

        any_active = any(
            mk.market_status in ("active", "open") for mk in markets
        )
        expected_duration = _expected_duration_for_context(m.event_context)
        event_type = _event_type_for_context(m.event_context)
        event_id = f"auto:{m.speaker}:{event_ticker}"
        notes = f"Auto-seeded from live market context={m.event_context} ticker={m.ticker}"

        if any_active:
            # Market is active right now → event is live
            speech_state = "live"
            event_start_ts = now_dt.isoformat()
            # Calculate a reasonable scheduled_start from open_time or close-duration
            open_dt = _parse_iso(m.open_time) if m.open_time else None
            scheduled_start = (open_dt or (close_dt - timedelta(seconds=expected_duration))).isoformat()

            # Insert as live; also upgrade existing 'scheduled' events that are now active
            existing = conn.execute(
                "SELECT speech_state FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    """
                    INSERT INTO events (
                        event_id, speaker, event_type, speech_state,
                        scheduled_start_ts, event_start_ts, expected_duration_sec, notes
                    ) VALUES (?, ?, ?, 'live', ?, ?, ?, ?)
                    """,
                    (event_id, m.speaker, event_type, scheduled_start,
                     event_start_ts, expected_duration, notes),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
                    logger.info(
                        "Auto-seeded live event %s (market is active on Kalshi)", event_id
                    )
            elif existing[0] in ("scheduled", "ended", "unknown"):
                # Re-open any non-live event when the Kalshi market is still active
                conn.execute(
                    "UPDATE events SET speech_state='live', event_start_ts=?, event_end_ts=NULL WHERE event_id=?",
                    (event_start_ts, event_id),
                )
                updated += 1
                logger.info(
                    "Reopened event %s: %s → live (Kalshi market still active)", event_id, existing[0]
                )
        else:
            # Market not yet open — seed as scheduled using open_time if available
            open_dt = _parse_iso(m.open_time) if m.open_time else None
            scheduled_start = (open_dt or (close_dt - timedelta(seconds=expected_duration))).isoformat()

            cur = conn.execute(
                """
                INSERT INTO events (
                    event_id, speaker, event_type, speech_state,
                    scheduled_start_ts, expected_duration_sec, notes
                ) VALUES (?, ?, ?, 'scheduled', ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, m.speaker, event_type, scheduled_start,
                 expected_duration, notes),
            )
            if cur.rowcount and cur.rowcount > 0:
                inserted += 1

    total = inserted + updated
    if total > 0:
        conn.commit()
    if inserted > 0:
        logger.info("Event sync: auto-seeded %d new events from refreshed market cache.", inserted)
    if updated > 0:
        logger.info("Event sync: upgraded %d events to live (Kalshi market now active).", updated)
    return total


_HEARTBEAT_PATH = Path("data/logs/runner.local.log")

# SQLite error messages that indicate the connection itself is broken and must
# be reopened.  These arise when the DB file is swapped (e.g. after a .recover
# rebuild) while the runner holds an old connection, or when the -wal/-shm files
# are stale from a prior SIGKILL.  Catching them here lets the loop reopen a
# fresh connection rather than dying silently.
_DB_CONN_ERRORS = (
    "file is not a database",
    "database disk image is malformed",
    "cannot operate on a closed database",
)


from typing import Any

_SERVICE_MAP: dict[str, Any] = {}  # populated after App is defined


def _is_db_conn_error(exc: BaseException) -> bool:
    # ProgrammingError ("Cannot operate on a closed database") is NOT a subclass
    # of DatabaseError — it must be checked separately.  This fires when the
    # auto-reconnect handler closes the old connection but db_connect() then
    # fails (DB still corrupt), leaving conn in a closed-but-unset state.
    return isinstance(
        exc, (sqlite3.DatabaseError, sqlite3.OperationalError, sqlite3.ProgrammingError)
    ) and any(msg in str(exc).lower() for msg in _DB_CONN_ERRORS)


async def _service_loop(
    name: str,
    interval: float,
    stop_event: asyncio.Event,
    fn,
    *,
    app: "App | None" = None,
) -> None:
    loop = asyncio.get_running_loop()
    _consecutive_db_errors = 0
    while not stop_event.is_set():
        try:
            # Run in executor so blocking I/O (HTTP, time.sleep) never freezes
            # the event loop and starves other service loops (scorer, watchdog).
            await loop.run_in_executor(None, fn)
            _consecutive_db_errors = 0
        except Exception as _exc:
            logger.exception("%s loop error", name)
            if app is not None and _is_db_conn_error(_exc):
                _consecutive_db_errors += 1
                logger.warning(
                    "%s: DB connection error (%d consecutive) — reopening connection.",
                    name, _consecutive_db_errors,
                )
                # Each service loop has its own DB connection.  Reconnect
                # only the connection(s) belonging to THIS service loop.
                _service_obj = _SERVICE_MAP.get(name, lambda a: [])
                _targets = _service_obj(app)
                try:
                    new_conn = db_connect(app.settings.db_path, allow_checkpoint=False)
                    for _target in _targets:
                        if _target is not None and hasattr(_target, "conn"):
                            try:
                                _target.conn.close()
                            except Exception:
                                pass
                            _target.conn = new_conn
                    logger.info("%s: DB connection reopened successfully.", name)
                    _consecutive_db_errors = 0
                except Exception as _reopen_exc:
                    logger.error("%s: Failed to reopen DB connection: %s", name, _reopen_exc)
                    await asyncio.sleep(min(30 * _consecutive_db_errors, 120))
        # Touch heartbeat so the launchd watchdog knows the runner is alive.
        try:
            _HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
            _HEARTBEAT_PATH.touch()
        except OSError:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


def _sync_events_from_cache(conn: sqlite3.Connection) -> int:
    catalog = LiveMarketCatalog.from_cache()
    if not catalog.markets:
        return 0
    inserted = _seed_events_from_live_markets(conn, catalog)
    if inserted > 0:
        logger.info("Event sync: auto-seeded %d new events from refreshed market cache.", inserted)
    return inserted


@dataclass
class App:
    settings: Settings
    conn: sqlite3.Connection
    watcher: MockKalshiWatcher | LiveKalshiWatcher
    ingestor: TranscriptIngestor
    scorer: ScoringEngine | BayesianScorer
    event_detector: EventDetector
    maintenance: MaintenanceRunner | None
    watchdog: Watchdog | None


# Map each service-loop name to the objects whose .conn should be refreshed.
_SERVICE_MAP.update({
    "watcher": lambda a: [a.watcher],
    "scorer": lambda a: [a.scorer, a.event_detector],
    "ingestor": lambda a: [a.ingestor],
    "watchdog": lambda a: [a.watchdog] if a.watchdog else [],
    "event_seed_sync": lambda a: [a.event_detector],
    "maintenance": lambda a: [],
})


def _build_watcher(settings: Settings, conn) -> tuple[MockKalshiWatcher | LiveKalshiWatcher, dict[str, list[str]]]:
    """Build the appropriate watcher and return (watcher, market_phrases_map)."""
    raw_log = settings.raw_dir / "kalshi_snapshots.jsonl"

    catalog = LiveMarketCatalog.from_cache()
    catalog_phrases = catalog.market_phrases_map() if catalog.markets else {}

    if settings.kalshi_mock:
        watcher = MockKalshiWatcher(
            conn=conn,
            raw_log_path=raw_log,
            interval_sec=settings.watcher_interval_sec,
            mock_enabled=True,
        )
        watcher.bootstrap_markets()
        merged = {**MARKET_PHRASES, **catalog_phrases}
        return watcher, merged

    watcher = LiveKalshiWatcher(
        conn=conn,
        raw_log_path=raw_log,
        interval_sec=settings.watcher_interval_sec,
        catalog=catalog,
    )
    watcher.bootstrap_markets()
    phrases_map = catalog_phrases if catalog_phrases else MARKET_PHRASES
    return watcher, phrases_map


def _make_conn(db_path: Path, label: str) -> sqlite3.Connection:
    """Create a dedicated connection for a service loop.

    Each service loop (watcher, scorer, ingestor, watchdog) gets its own
    connection so they never share SQLite internal state across threads.
    WAL mode handles concurrency between connections automatically.
    """
    conn = db_connect(db_path, allow_checkpoint=False)
    logger.info("Opened dedicated DB connection for %s", label)
    return conn


def build_app(settings: Settings) -> App:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    conn = init_db(settings.db_path)

    # Checkpoint the WAL on startup so it never grows unbounded between restarts.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        logger.info("WAL checkpoint (TRUNCATE) completed on startup.")
    except Exception as _wal_exc:
        logger.warning("WAL checkpoint on startup failed: %s", _wal_exc)

    watcher_conn = _make_conn(settings.db_path, "watcher")
    watcher, market_phrases = _build_watcher(settings, watcher_conn)

    speaker_phrases: list[str] = []
    for phrases in SUBJECT_PHRASES.values():
        speaker_phrases.extend(phrases)

    if settings.kalshi_mock:
        resolution_phrases = all_market_phrases()
    else:
        resolution_phrases = list({p for ps in market_phrases.values() for p in ps})
    combined = list(dict.fromkeys(speaker_phrases + resolution_phrases))

    ingestor_conn = _make_conn(settings.db_path, "ingestor")
    ingestor = TranscriptIngestor(
        conn=ingestor_conn,
        source=_build_transcript_source(settings),
        matcher=PhraseMatcher(combined),
        transcript_urls=settings.transcript_urls,
        raw_log_path=settings.raw_dir / "transcripts.jsonl",
    )
    config_dir = Path("config")
    data_dir = settings.data_dir

    scorer_conn = _make_conn(settings.db_path, "scorer")
    event_detector = EventDetector(conn=scorer_conn)
    loaded_events = event_detector.load_events_from_yaml(config_dir / "events.yaml")
    auto_catalog = LiveMarketCatalog.from_cache()
    if auto_catalog.markets:
        auto_seeded = _seed_events_from_live_markets(scorer_conn, auto_catalog)
        if auto_seeded > 0:
            logger.info(
                "Auto-seeded %d scheduled events from live market tickers.",
                auto_seeded,
            )
    if settings.focus_event_markets and loaded_events == 0 and not auto_catalog.markets:
        logger.warning(
            "FOCUS_EVENT_MARKETS=1 but no events loaded from config/events.yaml "
            "and no live market cache available for auto-seeding."
        )

    base_rates = BaseRateLookup.from_yaml(config_dir / "base_rates.yaml")
    signals = SignalStore.from_yaml(data_dir / "signals.yaml")
    event_signals = EventSignalStore.from_dir(data_dir / "event_signals")
    n_event_files = len(list((data_dir / "event_signals").glob("*.json"))) if (data_dir / "event_signals").exists() else 0
    logger.info("Event signals store: %d existing event files", n_event_files)

    notifier = WhatsAppNotifier(
        target=settings.whatsapp_target,
        enabled=settings.whatsapp_enabled,
    )

    poly_prices = PolymarketPrices.from_cache()
    logger.info("Polymarket prices: %d phrases loaded", poly_prices.phrase_count)
    wallet_flow = WalletFlowSignals.from_cache()
    logger.info("Wallet-flow signals: %d phrases loaded", wallet_flow.signal_count)
    live_settlements = LiveSettlements.from_cache()
    logger.info("Live settlements: %d active events", len(live_settlements._events or {}))
    window_state = WindowStateCache.from_cache()
    logger.info("Window state: %d window events loaded", len(window_state._states))
    rolling_rates = RollingRatesCache.from_cache()
    logger.info("Rolling rates: %d phrases loaded", rolling_rates.phrase_count)
    price_velocity = PriceVelocityCache.from_cache()
    logger.info("Price velocity: %d active signals", price_velocity.signal_count)
    wh_schedule = WHScheduleCache.load()
    logger.info("WH schedule: %d events loaded", len(wh_schedule.events))
    phrase_trends = PhraseTrendCache()
    logger.info("Phrase trends cache initialized")
    cooccurrence = CooccurrenceCache()
    logger.info("Co-occurrence cache initialized")
    bias_map = BiasMapCache()
    logger.info("Bias map cache initialized")
    phrase_hazard = PhraseHazardCache()
    logger.info("Phrase hazard cache initialized")
    signal_weight_store = SignalWeightStore.from_json()
    logger.info("Signal weight store initialized")
    bayesian_rate_store = BayesianRateStore.from_json()
    logger.info("Bayesian rate store initialized")
    phrase_corr_store = PhraseCorrelationStore()
    logger.info("Phrase correlation store initialized")

    scorer: ScoringEngine | BayesianScorer
    if settings.use_bayesian_scorer:
        logger.info("Using BayesianScorer (hierarchical Beta-Binomial mispricing detector)")
        scorer = BayesianScorer(
            conn=scorer_conn,
            action_cards_path=settings.action_cards_path,
            event_detector=event_detector,
            bayesian_rates=bayesian_rate_store,
            market_phrases=market_phrases,
            notifier=notifier,
            focus_event_markets=settings.focus_event_markets,
            max_spread=settings.max_spread,
            min_depth=settings.min_depth,
        )
    else:
        logger.info("Using legacy ScoringEngine")
        scorer = ScoringEngine(
            conn=scorer_conn,
            action_cards_path=settings.action_cards_path,
            event_detector=event_detector,
            base_rates=base_rates,
            signals=signals,
            notifier=notifier,
            market_phrases=market_phrases,
            focus_event_markets=settings.focus_event_markets,
            pre_event_window_sec=settings.pre_event_window_sec,
            max_spread=settings.max_spread,
            min_depth=settings.min_depth,
            ev_threshold=settings.ev_threshold,
            poly_prices=poly_prices,
            wallet_signals=wallet_flow,
            live_settlements=live_settlements,
            window_state=window_state,
            rolling_rates=rolling_rates,
            price_velocity=price_velocity,
            wh_schedule=wh_schedule,
            phrase_trends=phrase_trends,
            cooccurrence=cooccurrence,
            bias_map=bias_map,
            phrase_hazard=phrase_hazard,
            signal_weights=signal_weight_store,
            bayesian_rates=bayesian_rate_store,
            phrase_correlations=phrase_corr_store,
            poly_min_confidence=settings.poly_min_confidence,
            wallet_flow_weight=settings.wallet_flow_weight,
            wallet_min_confidence=settings.wallet_min_confidence,
            adaptive_ev_scale=settings.adaptive_ev_scale,
            source_agreement_weight=settings.source_agreement_weight,
            market_veto_margin=settings.market_veto_margin,
            pre_event_yes_threshold=settings.pre_event_yes_threshold,
            block_off_topic_yes=settings.block_off_topic_yes,
            penny_price_threshold=settings.penny_price_threshold,
            market_catalog=auto_catalog if auto_catalog.markets else None,
            event_signals=event_signals,
        )

    maintenance: MaintenanceRunner | None = None
    if settings.maintenance_enabled:
        repo_root = Path(__file__).resolve().parent.parent
        maintenance = MaintenanceRunner(
            repo_root=repo_root,
            tasks=build_default_maintenance_tasks(
                repo_root=repo_root,
                fetch_markets_interval_sec=settings.fetch_markets_interval_sec,
                fetch_poly_interval_sec=settings.fetch_poly_interval_sec,
                fetch_wallet_flow_interval_sec=settings.fetch_wallet_flow_interval_sec,
                fetch_news_interval_sec=settings.fetch_news_interval_sec,
                fetch_x_interval_sec=settings.fetch_x_interval_sec,
                fetch_outcomes_interval_sec=settings.fetch_outcomes_interval_sec,
                record_outcomes_interval_sec=settings.record_outcomes_interval_sec,
            ),
        )
        logger.info("Background maintenance loop enabled.")

    watchdog: Watchdog | None = None
    if settings.watchdog_enabled and not settings.kalshi_mock:
        watchdog_conn = _make_conn(settings.db_path, "watchdog")
        watchdog = Watchdog(
            conn=watchdog_conn,
            notifier=notifier if notifier.enabled else None,
            max_snapshot_age_sec=settings.watchdog_max_snapshot_age_sec,
            max_scorer_idle_sec=settings.watchdog_max_scorer_idle_sec,
            startup_grace_sec=settings.watchdog_startup_grace_sec,
            consecutive_breaches_to_restart=settings.watchdog_breach_limit,
            exit_on_stale=settings.watchdog_exit_on_stale,
        )
        logger.info("Watchdog enabled (snapshot_age<=%ss scorer_idle<=%ss).",
                    int(settings.watchdog_max_snapshot_age_sec),
                    int(settings.watchdog_max_scorer_idle_sec))

    return App(
        settings=settings,
        conn=conn,
        watcher=watcher,
        ingestor=ingestor,
        scorer=scorer,
        event_detector=event_detector,
        maintenance=maintenance,
        watchdog=watchdog,
    )


async def run_app() -> None:
    settings = load_settings()
    app = build_app(settings)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info("Shutdown signal received; stopping loops.")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    mode = "MOCK" if settings.kalshi_mock else "LIVE"
    logger.info(
        "Starting services. mode=%s source=%s urls=%s focus_event_markets=%s pre_event_window_sec=%s maintenance=%s watchdog=%s event_seed_interval_sec=%s",
        mode,
        settings.transcript_source,
        len(settings.transcript_urls),
        settings.focus_event_markets,
        round(settings.pre_event_window_sec, 1),
        settings.maintenance_enabled,
        settings.watchdog_enabled,
        round(settings.event_seed_interval_sec, 1),
    )
    tasks = [
        asyncio.create_task(
            _service_loop("watcher", settings.watcher_interval_sec, stop_event, app.watcher.run_once, app=app)
        ),
        asyncio.create_task(
            _service_loop(
                "ingestor",
                settings.transcript_interval_sec,
                stop_event,
                app.ingestor.run_once,
                app=app,
            )
        ),
        asyncio.create_task(
            _service_loop("scorer", settings.scorer_interval_sec, stop_event, app.scorer.run_once, app=app)
        ),
    ]
    if app.maintenance:
        tasks.append(
            asyncio.create_task(
                _service_loop(
                    "maintenance",
                    settings.maintenance_interval_sec,
                    stop_event,
                    app.maintenance.run_due,
                )
            )
        )
    if app.watchdog:
        tasks.append(
            asyncio.create_task(
                _service_loop(
                    "watchdog",
                    settings.watchdog_interval_sec,
                    stop_event,
                    app.watchdog.run_once,
                )
            )
        )
    if not settings.kalshi_mock:
        tasks.append(
            asyncio.create_task(
                _service_loop(
                    "event_seed_sync",
                    settings.event_seed_interval_sec,
                    stop_event,
                    lambda: _sync_events_from_cache(app.conn),
                    app=app,
                )
            )
        )

    # Periodic WAL checkpoint — runs every 30 minutes.
    # RESTART mode blocks until all readers finish their current read transaction,
    # then checkpoints and resets the WAL write position.  This prevents unbounded
    # WAL growth even when the dashboard process holds long-lived read connections.
    _WAL_CHECKPOINT_INTERVAL = 30 * 60

    async def _wal_checkpoint_loop() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(_WAL_CHECKPOINT_INTERVAL)
            if stop_event.is_set():
                break
            try:
                app.conn.execute("PRAGMA wal_checkpoint(RESTART)")
                app.conn.commit()
                logger.info("Scheduled WAL checkpoint (RESTART) completed.")
            except Exception as _wc_exc:
                logger.warning("Scheduled WAL checkpoint failed: %s", _wc_exc)

    tasks.append(asyncio.create_task(_wal_checkpoint_loop()))

    await stop_event.wait()
    await asyncio.gather(*tasks, return_exceptions=True)
    # Final checkpoint on clean shutdown to keep WAL small for next startup.
    try:
        app.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        app.conn.commit()
        logger.info("Shutdown WAL checkpoint completed.")
    except Exception:
        pass
    app.conn.close()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


_LOCK_FILE = Path("data/runner.lock")


def _acquire_lock(timeout: float = 30.0) -> bool:
    """Return True if this process successfully acquired the single-instance lock.

    Retries for up to `timeout` seconds using non-blocking flock attempts.
    This handles the common race where the watchdog starts a new runner
    immediately after a crash — the dying process may still hold OS file
    handles for a second or two while Python's cleanup handlers run.
    Retrying ensures the new runner waits for the OS to fully release the
    lock rather than failing fast and exiting (which would cause the watchdog
    to loop-restart rapidly).

    IMPORTANT: we open the lock file WITHOUT rm-ing it first.  The watchdog
    must NOT delete data/runner.lock before starting a new runner; deleting
    the file creates a new inode and the new runner gets a fresh, uncontested
    lock even while the old runner still has the original inode open.
    """
    import os, fcntl, time
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fh = open(_LOCK_FILE, "w")
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(str(os.getpid()))
            fh.flush()
            # Keep the file handle open for the lifetime of the process.
            _acquire_lock._fh = fh  # type: ignore[attr-defined]
            return True
        except (OSError, IOError):
            if time.monotonic() >= deadline:
                fh.close()
                return False
            time.sleep(1.0)


if __name__ == "__main__":
    _setup_logging()
    if not _acquire_lock():
        logger.error(
            "Another runner instance is already running (lock file: %s). "
            "If this is stale, delete it and retry.",
            _LOCK_FILE,
        )
        raise SystemExit(1)
    asyncio.run(run_app())
