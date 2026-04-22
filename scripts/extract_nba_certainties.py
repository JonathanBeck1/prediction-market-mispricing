#!/usr/bin/env python3
"""Inject near-certain p_overrides and p_floors for NBA mention markets.

Certainty sources (highest → lowest confidence):
  1. Arena/sponsor names  → p_override = 0.92  (announcers say it every few mins)
  2. Universal boilerplate (Buzzer, Crowd, etc.) → p_floor per phrase
  3. Game-specific team phrases (Cavaliers, Lakers…) → p_floor 0.75

Reads: data/nba_schedule.json (built by fetch_nba_schedule.py)
       data/edge.db — markets table for phrase list per game
Writes: data/event_signals/nba:<event_ticker>.json
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect as _db_connect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / "data" / "edge.db"
SCHEDULE_PATH = REPO_ROOT / "data" / "nba_schedule.json"
SIGNALS_DIR = REPO_ROOT / "data" / "event_signals"
SIGNALS_DIR.mkdir(exist_ok=True)

# ── Universal phrase floors ─────────────────────────────────────────────────
# All NBA mention markets use the same 19-phrase set.
# These are empirical estimates (no resolved data yet); conservative to avoid
# over-betting.  Arena phrases are handled separately as p_override.
_UNIVERSAL_FLOORS: dict[str, float] = {
    # Very high — heard in virtually every broadcast
    "BUZZ": 0.90,   # Buzzer / buzzer-beater
    "CROW": 0.88,   # Crowd noise/reaction
    "INJU": 0.80,   # Injury mention
    "MVP":  0.82,   # MVP award race (peak season)
    # High — almost always discussed
    "PLAY": 0.78,   # Playoffs (March = playoff push)
    "ROOK": 0.72,   # Rookie of the year race
    "DRAF": 0.68,   # Draft (lottery approaching)
    "TRAD": 0.65,   # Trade (post-deadline player moves)
    # Moderate
    "JORD": 0.62,   # Michael Jordan comparison
    "TRIP": 0.58,   # Triple-double
    "TECH": 0.52,   # Technical foul
    "ALLE": 0.50,   # Alley-oop
    # Lower probability  
    "OVER": 0.40,   # Overtime (happens ~22% but discussed speculatively)
    "RETI": 0.38,   # Retirement talk
    "ELBO": 0.32,   # Elbow (specific medical term)
    "AIRB": 0.28,   # Airball (rare/embarrassing play)
}

# ── Arena phrase codes → override probability ───────────────────────────────
# These are sponsor/arena names that appear on court displays and are said
# by announcers constantly throughout the broadcast → near-certain YES.
_ARENA_PHRASE_OVERRIDE: float = 0.92

# ── Team name phrases ────────────────────────────────────────────────────────
# If the market phrase encodes a team name that is playing today,
# boost to p_floor = 0.75 (announcers constantly say team names)
_TEAM_NAME_FLOORS: dict[str, float] = {
    "CAVA": 0.75,   # Cavaliers
    "BUCK": 0.75,   # Bucks
    "LAKE": 0.75,   # Lakers
    "ROCK": 0.75,   # Rockets (or Rocket Mortgage)
    "SPUR": 0.75,   # Spurs
    "CLIP": 0.75,   # Clippers
    "WARR": 0.75,   # Warriors
    "CELT": 0.75,   # Celtics
    "SIXE": 0.75,   # Sixers
    "NUGG": 0.75,   # Nuggets
    "MAGI": 0.75,   # Magic
    "HORN": 0.75,   # Hornets
    "HAWK": 0.75,   # Hawks
    "MAVE": 0.75,   # Mavericks
    "SUNS": 0.75,   # Suns
    "HEAT": 0.75,   # Heat
    "WOLF": 0.75,   # Timberwolves
    "KNIC": 0.75,   # Knicks
    "PIST": 0.75,   # Pistons
    "KING": 0.72,   # Kings
    "PACE": 0.72,   # Pacers
    "GRIZ": 0.72,   # Grizzlies
    "PELO": 0.72,   # Pelicans
    "THUN": 0.72,   # Thunder
    "TRAIL": 0.72,  # Trail Blazers
    "JAZZ": 0.72,   # Jazz
    "WIZA": 0.72,   # Wizards
    "RAPT": 0.72,   # Raptors
    "BULL": 0.72,   # Bulls
}

# Known arena phrase codes (must match what Kalshi uses in market tickers)
_KNOWN_ARENA_CODES: set[str] = {
    "STFA", "TDGA", "SPEC", "MORT", "AMAA", "BALL", "LITC",
    "TOYO", "INTU", "KASE", "FISE", "TARG", "KIA",
}


def _load_schedule() -> dict:
    if not SCHEDULE_PATH.exists():
        return {"games": []}
    return json.loads(SCHEDULE_PATH.read_text())


def _get_phrases_for_event(conn: sqlite3.Connection, event_ticker: str) -> list[str]:
    """Return all phrase_code suffixes for markets in this NBA event."""
    rows = conn.execute(
        "SELECT market_id FROM markets WHERE market_id LIKE ?",
        (f"{event_ticker}%",),
    ).fetchall()
    codes = []
    for (mid,) in rows:
        parts = mid.split("-")
        if len(parts) >= 3:
            codes.append(parts[-1])
    return codes


def _load_existing_signals(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _build_signals_for_game(
    event_ticker: str,
    phrases: list[str],
    arena_phrase: str | None,
) -> dict:
    """Compute p_override and p_floor entries for all phrases in this game."""
    p_override: dict[str, float] = {}
    p_floor: dict[str, float] = {}

    for phrase in phrases:
        code = phrase.upper()

        # 1 — Arena/sponsor name → hard override
        if code in _KNOWN_ARENA_CODES:
            p_override[phrase] = _ARENA_PHRASE_OVERRIDE
            continue

        # Also match via game's arena_phrase field
        if arena_phrase and code == arena_phrase.upper():
            p_override[phrase] = _ARENA_PHRASE_OVERRIDE
            continue

        # 2 — Universal boilerplate floors
        if code in _UNIVERSAL_FLOORS:
            p_floor[phrase] = _UNIVERSAL_FLOORS[code]
            continue

        # 3 — Team name phrases
        matched_team = False
        for team_code, floor_val in _TEAM_NAME_FLOORS.items():
            if code.startswith(team_code) or team_code.startswith(code[:4] if len(code) >= 4 else code):
                p_floor[phrase] = floor_val
                matched_team = True
                break
        if matched_team:
            continue

    return {"p_override": p_override, "p_floor": p_floor}


def run() -> None:
    schedule = _load_schedule()
    games = schedule.get("games", [])

    if not games:
        logger.warning("No games in schedule — run fetch_nba_schedule.py first")
        return

    conn = _db_connect(DB_PATH)
    total_overrides = 0
    total_floors = 0

    for game in games:
        event_ticker = game["event_ticker"]
        arena_phrase = game.get("arena_phrase")

        # Get phrase codes from DB
        phrases = _get_phrases_for_event(conn, event_ticker)
        if not phrases:
            logger.debug("No phrases found for %s — skipping", event_ticker)
            continue

        signals = _build_signals_for_game(event_ticker, phrases, arena_phrase)

        # Use the same safe-filename convention as EventSignalStore._safe_filename:
        # re.sub(r"[^\w\-]", "_", event_id) + ".json"
        # event_id = "auto:nba:KXNBAMENTION-26MAR17CLEMIL"
        # → "auto_nba_KXNBAMENTION-26MAR17CLEMIL.json"
        import re as _re
        event_id = f"auto:nba:{event_ticker}"
        safe_filename = _re.sub(r"[^\w\-]", "_", event_id) + ".json"
        sig_path = SIGNALS_DIR / safe_filename
        existing = _load_existing_signals(sig_path)
        existing.update({
            "event_ticker": event_ticker,
            "p_override":   signals["p_override"],
            "p_floor":      signals["p_floor"],
            "injected_at":  datetime.now(timezone.utc).isoformat(),
            "source":       "extract_nba_certainties",
        })

        sig_path.write_text(json.dumps(existing, indent=2))

        n_ov = len(signals["p_override"])
        n_fl = len(signals["p_floor"])
        total_overrides += n_ov
        total_floors += n_fl

        logger.info(
            "%-35s  arena=%-4s  overrides=%d  floors=%d",
            f"{game['label']} ({event_ticker})",
            arena_phrase or "—",
            n_ov,
            n_fl,
        )

    conn.close()
    logger.info(
        "Done — %d games processed, %d p_overrides, %d p_floors",
        len(games), total_overrides, total_floors,
    )


if __name__ == "__main__":
    run()
