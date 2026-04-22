#!/usr/bin/env python3
"""Fetch today's NBA game schedule and upsert events into edge.db.

Uses the free BallDontLie API (no key needed) for game schedule.
Falls back to parsing existing KXNBAMENTION market IDs from the DB
if the API is unavailable.

Produces/updates:
  data/nba_schedule.json  — today's + upcoming games with arena info
  edge.db events table    — one row per NBA game event
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from app.db import connect as _db_connect  # noqa: E402
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / "data" / "edge.db"
SCHEDULE_PATH = REPO_ROOT / "data" / "nba_schedule.json"

# ── Team → arena mapping ────────────────────────────────────────────────────
# phrase_code = Kalshi's ticker suffix for this arena (None = no specific phrase)
_TEAM_ARENA: dict[str, dict] = {
    "ATL": {"arena": "State Farm Arena",          "phrase_code": "STFA", "city": "Atlanta"},
    "BOS": {"arena": "TD Garden",                 "phrase_code": "TDGA", "city": "Boston"},
    "BKN": {"arena": "Barclays Center",           "phrase_code": None,   "city": "Brooklyn"},
    "CHA": {"arena": "Spectrum Center",           "phrase_code": "SPEC", "city": "Charlotte"},
    "CHI": {"arena": "United Center",             "phrase_code": None,   "city": "Chicago"},
    "CLE": {"arena": "Rocket Mortgage FieldHouse","phrase_code": "MORT", "city": "Cleveland"},
    "DAL": {"arena": "American Airlines Center",  "phrase_code": "AMAA", "city": "Dallas"},
    "DEN": {"arena": "Ball Arena",                "phrase_code": "BALL", "city": "Denver"},
    "DET": {"arena": "Little Caesars Arena",      "phrase_code": "LITC", "city": "Detroit"},
    "GSW": {"arena": "Chase Center",              "phrase_code": None,   "city": "Golden State"},
    "HOU": {"arena": "Toyota Center",             "phrase_code": "TOYO", "city": "Houston"},
    "IND": {"arena": "Gainbridge Fieldhouse",     "phrase_code": None,   "city": "Indiana"},
    "LAC": {"arena": "Intuit Dome",               "phrase_code": "INTU", "city": "LA Clippers"},
    "LAL": {"arena": "Crypto.com Arena",          "phrase_code": None,   "city": "LA Lakers"},
    "MEM": {"arena": "FedExForum",                "phrase_code": None,   "city": "Memphis"},
    "MIA": {"arena": "Kaseya Center",             "phrase_code": "KASE", "city": "Miami"},
    "MIL": {"arena": "Fiserv Forum",              "phrase_code": "FISE", "city": "Milwaukee"},
    "MIN": {"arena": "Target Center",             "phrase_code": "TARG", "city": "Minnesota"},
    "NOP": {"arena": "Smoothie King Center",      "phrase_code": None,   "city": "New Orleans"},
    "NYK": {"arena": "Madison Square Garden",     "phrase_code": None,   "city": "New York"},
    "OKC": {"arena": "Paycom Center",             "phrase_code": None,   "city": "Oklahoma City"},
    "ORL": {"arena": "Kia Center",                "phrase_code": "KIA",  "city": "Orlando"},
    "PHI": {"arena": "Wells Fargo Center",        "phrase_code": None,   "city": "Philadelphia"},
    "PHX": {"arena": "Footprint Center",          "phrase_code": None,   "city": "Phoenix"},
    "POR": {"arena": "Moda Center",               "phrase_code": None,   "city": "Portland"},
    "SAC": {"arena": "Golden 1 Center",           "phrase_code": None,   "city": "Sacramento"},
    "SAS": {"arena": "Frost Bank Center",         "phrase_code": None,   "city": "San Antonio"},
    "TOR": {"arena": "Scotiabank Arena",          "phrase_code": None,   "city": "Toronto"},
    "UTA": {"arena": "Delta Center",              "phrase_code": None,   "city": "Utah"},
    "WAS": {"arena": "Capital One Arena",         "phrase_code": None,   "city": "Washington"},
}

# BallDontLie team abbreviation → our code
_BDL_TO_CODE: dict[str, str] = {
    "ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "CHA": "CHA", "CHI": "CHI",
    "CLE": "CLE", "DAL": "DAL", "DEN": "DEN", "DET": "DET", "GSW": "GSW",
    "HOU": "HOU", "IND": "IND", "LAC": "LAC", "LAL": "LAL", "MEM": "MEM",
    "MIA": "MIA", "MIL": "MIL", "MIN": "MIN", "NOP": "NOP", "NYK": "NYK",
    "OKC": "OKC", "ORL": "ORL", "PHI": "PHI", "PHX": "PHX", "POR": "POR",
    "SAC": "SAC", "SAS": "SAS", "TOR": "TOR", "UTA": "UTA", "WAS": "WAS",
}

# 3-letter codes embedded in Kalshi event tickers (e.g. CLEMIL → CLE + MIL)
_TICKER_TEAM_CODES: dict[str, str] = {
    "CLE": "CLE", "MIL": "MIL", "LAL": "LAL", "HOU": "HOU", "SAS": "SAS",
    "LAC": "LAC", "GSW": "GSW", "BOS": "BOS", "PHI": "PHI", "DEN": "DEN",
    "ORL": "ORL", "CHA": "CHA", "ATL": "ATL", "DAL": "DAL", "PHX": "PHX",
    "MIA": "MIA", "MIN": "MIN", "NYK": "NYK", "DET": "DET", "SAC": "SAC",
    "IND": "IND", "MEM": "MEM", "NOP": "NOP", "BKN": "BKN", "OKC": "OKC",
    "POR": "POR", "UTA": "UTA", "WAS": "WAS", "TOR": "TOR", "CHI": "CHI",
}


def _fetch_balldontlie(date_str: str) -> list[dict]:
    """Fetch games from BallDontLie free API for a given date (YYYY-MM-DD)."""
    url = f"https://api.balldontlie.io/v1/games?dates[]={date_str}&per_page=30"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kalshi-edge/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return data.get("data", [])
    except Exception as exc:
        logger.warning("BallDontLie API error: %s", exc)
        return []


def _parse_teams_from_ticker(ticker: str) -> tuple[str | None, str | None]:
    """Extract (away_code, home_code) from KXNBAMENTION-26MAR17CLEMIL."""
    m = re.search(r"\d{2}[A-Z]{3}\d{2}([A-Z]{2,3})([A-Z]{2,3})$", ticker)
    if not m:
        return None, None
    raw_away = m.group(1)
    raw_home = m.group(2)
    # Try direct match, then try splitting 6-char codes
    away = _TICKER_TEAM_CODES.get(raw_away)
    home = _TICKER_TEAM_CODES.get(raw_home)
    if not away and len(raw_away) == 3:
        away = raw_away
    if not home and len(raw_home) == 3:
        home = raw_home
    return away, home


def _game_from_ticker(ticker: str, phrases: list[str]) -> dict | None:
    """Build a game dict from a Kalshi event ticker."""
    away_code, home_code = _parse_teams_from_ticker(ticker)
    if not away_code or not home_code:
        return None

    away_info = _TEAM_ARENA.get(away_code, {"city": away_code, "arena": "", "phrase_code": None})
    home_info = _TEAM_ARENA.get(home_code, {"city": home_code, "arena": "", "phrase_code": None})

    # Extract date from ticker  (e.g. 26MAR17 → 2026-03-17)
    dm = re.search(r"(\d{2})([A-Z]{3})(\d{2})", ticker)
    game_date = None
    if dm:
        yr, mon_str, day = dm.group(1), dm.group(2), dm.group(3)
        months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                  "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
        mon = months.get(mon_str)
        if mon:
            game_date = f"20{yr}-{mon:02d}-{int(day):02d}"

    # Arena phrase comes only from the home team's known mapping.
    # Do NOT infer from phrase list — universal phrases (e.g. MORT) appear in
    # every game regardless of venue and would produce false arena mappings.
    arena_phrase = home_info.get("phrase_code")  # None if arena has no Kalshi code

    return {
        "event_ticker": ticker,
        "game_date":    game_date,
        "away_team":    away_code,
        "home_team":    home_code,
        "away_city":    away_info.get("city", away_code),
        "home_city":    home_info.get("city", home_code),
        "arena":        home_info.get("arena", ""),
        "arena_phrase": arena_phrase,
        "phrases":      phrases,
        "label":        f"{away_info.get('city', away_code)} @ {home_info.get('city', home_code)}",
    }


def build_games_from_db() -> list[dict]:
    """Read existing KXNBAMENTION markets from DB and build game list."""
    conn = _db_connect(DB_PATH)
    rows = conn.execute(
        "SELECT market_id FROM markets WHERE market_id LIKE 'KXNBAMENTION%'"
    ).fetchall()
    conn.close()

    # Group by event ticker
    from collections import defaultdict
    by_event: dict[str, list[str]] = defaultdict(list)
    for (market_id,) in rows:
        parts = market_id.split("-")
        if len(parts) >= 3:
            et = "-".join(parts[:2])
            phrase_code = parts[-1]
            by_event[et].append(phrase_code)

    games = []
    for ticker, phrases in sorted(by_event.items()):
        g = _game_from_ticker(ticker, phrases)
        if g:
            games.append(g)
    return games


def seed_events(games: list[dict]) -> None:
    """Upsert NBA game events into edge.db."""
    conn = _db_connect(DB_PATH)
    now_iso = datetime.now(timezone.utc).isoformat()

    for g in games:
        # Use the same auto:<speaker>:<ticker> format that the runner uses
        # so signal files line up correctly with the scoring engine.
        event_id = f"auto:nba:{g['event_ticker']}"
        # Determine speech_state based on game date
        game_date = g.get("game_date")
        today = datetime.now(timezone.utc).date().isoformat()
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()

        if game_date and game_date < today:
            state = "ended"
        elif game_date == today:
            state = "live"   # scoring engine will update
        else:
            state = "scheduled"

        sched_ts = f"{game_date}T19:00:00+00:00" if game_date else None

        conn.execute("""
            INSERT INTO events (event_id, speaker, event_type, speech_state,
                                scheduled_start_ts, expected_duration_sec, notes, created_at)
            VALUES (?, 'nba', 'nba_broadcast', ?, ?, 7200, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                speech_state=excluded.speech_state,
                scheduled_start_ts=excluded.scheduled_start_ts,
                notes=excluded.notes
        """, (
            event_id,
            state,
            sched_ts,
            f"NBA game: {g['label']} at {g['arena']}",
            now_iso,
        ))

    conn.commit()
    conn.close()
    logger.info("Seeded/updated %d NBA events", len(games))


def main() -> None:
    games = build_games_from_db()
    if not games:
        logger.warning("No NBA markets found in DB — run fetch_markets first")
        return

    logger.info("Found %d NBA games in DB", len(games))

    seed_events(games)

    # Save schedule JSON for other scripts to consume
    SCHEDULE_PATH.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "games": games,
    }, indent=2))
    logger.info("Saved schedule to %s", SCHEDULE_PATH)

    for g in games:
        logger.info("  %s  arena=%s  arena_phrase=%s  state=%s",
                    g["label"], g["arena"], g.get("arena_phrase", "-"),
                    "ended" if g.get("game_date", "") < datetime.now(timezone.utc).date().isoformat() else "active")


if __name__ == "__main__":
    main()
