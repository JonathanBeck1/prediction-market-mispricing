from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    subject TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_id TEXT NOT NULL,
    yes_bid REAL NOT NULL,
    yes_ask REAL NOT NULL,
    no_bid REAL NOT NULL,
    no_ask REAL NOT NULL,
    spread REAL NOT NULL,
    depth_yes REAL NOT NULL,
    depth_no REAL NOT NULL,
    volume_1h REAL NOT NULL,
    raw_json TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets (market_id)
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_ts
    ON market_snapshots (market_id, ts);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_transcripts_ts ON transcripts (ts);
CREATE INDEX IF NOT EXISTS idx_transcripts_hash ON transcripts (source_ref, text_hash);

CREATE TABLE IF NOT EXISTS phrase_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    transcript_id INTEGER NOT NULL,
    phrase TEXT NOT NULL,
    start_idx INTEGER NOT NULL,
    end_idx INTEGER NOT NULL,
    snippet TEXT NOT NULL,
    hit_date TEXT NOT NULL,
    FOREIGN KEY (transcript_id) REFERENCES transcripts (id)
);
CREATE INDEX IF NOT EXISTS idx_phrase_hits_date_phrase ON phrase_hits (hit_date, phrase);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    speaker TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'other',
    speech_state TEXT NOT NULL DEFAULT 'scheduled',
    scheduled_start_ts TEXT,
    expected_duration_sec INTEGER NOT NULL DEFAULT 5400,
    event_start_ts TEXT,
    event_end_ts TEXT,
    last_transcript_ts TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_speaker_state ON events (speaker, speech_state);

CREATE TABLE IF NOT EXISTS action_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_id TEXT NOT NULL,
    phrase TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'WATCH',
    p_literal REAL NOT NULL,
    yes_ask REAL NOT NULL,
    no_ask REAL NOT NULL DEFAULT 0.0,
    ev_yes REAL NOT NULL,
    ev_no REAL NOT NULL DEFAULT 0.0,
    exec_price_hint TEXT NOT NULL DEFAULT '',
    size_cap REAL NOT NULL DEFAULT 0.0,
    spread_ok INTEGER NOT NULL,
    depth_ok INTEGER NOT NULL,
    gate_pass INTEGER NOT NULL,
    rationale TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets (market_id)
);
CREATE INDEX IF NOT EXISTS idx_action_cards_market_ts ON action_cards (market_id, ts);

CREATE TABLE IF NOT EXISTS outcome_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    prediction_ts TEXT NOT NULL,
    resolved_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_ticker TEXT NOT NULL DEFAULT '',
    speaker TEXT NOT NULL DEFAULT '',
    phrase TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    p_literal REAL NOT NULL,
    yes_ask REAL NOT NULL,
    no_ask REAL NOT NULL,
    ev_yes REAL NOT NULL,
    ev_no REAL NOT NULL,
    outcome TEXT NOT NULL,
    realized_pnl REAL NOT NULL,
    reason_codes TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL,
    UNIQUE (market_id, prediction_ts)
);
CREATE INDEX IF NOT EXISTS idx_outcome_reviews_market ON outcome_reviews (market_id);
CREATE INDEX IF NOT EXISTS idx_outcome_reviews_resolved_ts ON outcome_reviews (resolved_ts);

-- Persistent bet journal: one row per market per day, never pruned.
-- Written by scripts/archive_bet_decisions.py every 6 hours BEFORE
-- action_cards are pruned.  Survives DB resets and window contract lifetimes
-- (7-31 days).  outcome_tracker.py reads this table to build outcome_reviews.
CREATE TABLE IF NOT EXISTS bet_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    market_id TEXT NOT NULL,
    archive_date TEXT NOT NULL,   -- YYYY-MM-DD of the scoring day
    phrase TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,           -- BUY_YES | BUY_NO
    p_literal REAL NOT NULL,
    yes_ask REAL NOT NULL,
    no_ask REAL NOT NULL DEFAULT 0.0,
    ev_yes REAL NOT NULL DEFAULT 0.0,
    ev_no REAL NOT NULL DEFAULT 0.0,
    first_seen_ts TEXT NOT NULL,  -- timestamp of first BUY card for this market
    reason_codes TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (market_id, archive_date)  -- one entry per market per scoring day
);
CREATE INDEX IF NOT EXISTS idx_bet_journal_market ON bet_journal (market_id);
CREATE INDEX IF NOT EXISTS idx_bet_journal_date ON bet_journal (archive_date);
"""


def heal_wal(db_path: Path) -> bool:
    """Attempt to recover from a stale/corrupt WAL+SHM state.

    Strategy (data-preserving):
      1. Try a normal open + quick_check.  If it passes, done.
      2. Try a PRAGMA wal_checkpoint(TRUNCATE) to merge WAL into main DB.
         This preserves all data.  If it succeeds, done.
      3. Only as last resort: back up the current DB+WAL, then remove
         the sidecar files.  This may lose un-checkpointed writes.

    Returns True if healing was performed, False if DB was already fine.
    """
    import shutil
    import time

    wal_path = db_path.with_suffix(db_path.suffix + "-wal")
    shm_path = db_path.with_suffix(db_path.suffix + "-shm")

    # ── Step 1: fast-path healthy check ──────────────────────────
    try:
        c = sqlite3.connect(str(db_path), timeout=5.0)
        result = c.execute("PRAGMA quick_check").fetchone()[0]
        if result == "ok":
            try:
                c.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
            c.close()
            return False
        c.close()
        logger.warning("db.heal_wal: quick_check returned '%s'", result)
    except Exception as exc:
        logger.warning("db.heal_wal: DB inaccessible (%s)", exc)

    # ── Step 2: try safe checkpoint to merge WAL into main DB ────
    try:
        c = sqlite3.connect(str(db_path), timeout=10.0)
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result = c.execute("PRAGMA quick_check").fetchone()[0]
        c.close()
        if result == "ok":
            logger.info("db.heal_wal: recovered via WAL checkpoint (data preserved)")
            return True
    except Exception as exc:
        logger.warning("db.heal_wal: checkpoint failed (%s), escalating", exc)

    # ── Step 3: last resort — back up, then remove sidecars ──────
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = db_path.parent
    for src in (db_path, wal_path, shm_path):
        if src.exists():
            dst = backup_dir / f"{src.name}.pre_heal_{stamp}"
            try:
                shutil.copy2(str(src), str(dst))
                logger.info("db.heal_wal: backed up %s -> %s", src.name, dst.name)
            except OSError as e:
                logger.error("db.heal_wal: backup of %s failed: %s", src, e)

    removed = []
    for p in (shm_path, wal_path):
        if p.exists():
            try:
                p.unlink()
                removed.append(p.name)
            except OSError as e:
                logger.error("db.heal_wal: could not remove %s: %s", p, e)

    if removed:
        logger.warning("db.heal_wal: removed sidecar files (last resort): %s", removed)

    try:
        c = sqlite3.connect(str(db_path), timeout=10.0)
        result = c.execute("PRAGMA quick_check").fetchone()[0]
        c.close()
        if result == "ok":
            logger.info("db.heal_wal: DB recovered after sidecar removal")
            return True
        raise RuntimeError(f"DB quick_check returned '{result}' after heal")
    except Exception as exc:
        logger.error("db.heal_wal: DB still corrupt after all heal attempts: %s", exc)
        raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotently ensure all tables and indexes exist.

    Uses CREATE TABLE/INDEX IF NOT EXISTS, so it's safe to call on every
    connection — it adds missing objects without touching existing data.
    """
    conn.executescript(SCHEMA_SQL)


def connect(
    db_path: Path,
    timeout: float = 30.0,
    allow_checkpoint: bool = False,
) -> sqlite3.Connection:
    """Open a SQLite connection with safe WAL settings.

    Automatically ensures the full schema exists on every connection so that
    restored backups or partially-created DBs never cause 'no such table' errors.

    Only the long-running runner process should pass allow_checkpoint=True.
    Short-lived scripts should use the default (False) so they never trigger
    a WAL checkpoint — concurrent checkpoints from multiple processes are the
    primary cause of 'database disk image is malformed' corruption.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if allow_checkpoint:
        conn.execute("PRAGMA wal_autocheckpoint=500")
    else:
        conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_schema(conn)
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create/open the DB for the long-running runner process (checkpointing enabled).

    Runs heal_wal() first so that a stale WAL/SHM left by a previous crash
    does not prevent startup.  connect() handles schema via ensure_schema().
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        heal_wal(db_path)
    return connect(db_path, allow_checkpoint=True)

