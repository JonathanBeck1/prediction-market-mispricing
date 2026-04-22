#!/usr/bin/env python3
"""Inject near-certain p_overrides and p_floors for MLB mention markets.

Certainty sources (highest → lowest confidence):
  1. Ballpark/stadium names  → p_override = 0.90  (announcers say it every inning)
  2. Universal MLB boilerplate phrases → p_floor per phrase code
  3. Home-team-specific phrases (Ohtani for LA games, etc.) → p_floor 0.65

Reads:  data/edge.db — markets table for phrase list per game
Writes: data/event_signals/auto_mlb_<event_ticker>.json
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
SIGNALS_DIR = REPO_ROOT / "data" / "event_signals"
SIGNALS_DIR.mkdir(exist_ok=True)

_BALLPARK_OVERRIDE = 0.90   # ballpark name spoken every half-inning by announcer
_TEAM_NAME_FLOOR   = 0.72   # home team name mentioned frequently throughout

# ── Ballpark phrase codes → override probability ─────────────────────────────
# These map directly to Kalshi's ticker suffix codes.  Announcers say the
# ballpark sponsor name on nearly every play, so this is near-certain YES.
_BALLPARK_CODES: frozenset[str] = frozenset({
    "ORAC",  # Oracle Park (SF Giants)
    "DODG",  # Dodger Stadium
    "PETC",  # Petco Park (SD Padres)
    "DAIK",  # Daikin Park (Houston Astros)
    "TMOB",  # T-Mobile Park (Seattle Mariners)
    "GRAA",  # Great American Ball Park (Cincinnati)
    "UNIQ",  # UNIQLO Field (Oakland A's — Las Vegas)
    "ORIO",  # Oriole Park / Camden Yards (Baltimore)
    "WRIG",  # Wrigley Field (Cubs)
    "GARA",  # Guaranteed Rate Field (White Sox)
    "FENN",  # Fenway Park (Red Sox)
    "YANK",  # Yankee Stadium
    "GLOB",  # Globe Life Field (Texas Rangers)
    "TROP",  # Tropicana Field (Tampa Bay Rays)
    "LOAN",  # loanDepot park (Miami Marlins)
    "AMAF",  # American Family Field (Milwaukee)
    "COME",  # Comerica Park (Detroit)
    "PROG",  # Progressive Field (Cleveland)
    "RATE",  # Guaranteed Rate Field alt
    "PNCA",  # PNC Park (Pittsburgh)
    "CITI",  # Citi Field (NY Mets)
    "GABP",  # Great American Ball Park alt
    "BUEN",  # Buena Vista (Angels — no stadium phrase on Kalshi yet)
    "CHAR",  # Chase Field (AZ Diamondbacks)
    "TRUS",  # Truist Park (Atlanta Braves)
    "COOR",  # Coors Field (Colorado Rockies)
    "BUCK",  # Busch Stadium (St. Louis Cardinals)
})

# ── Universal MLB boilerplate phrase floors ───────────────────────────────────
# All MLB mention markets use the same ~18-phrase set.
# Empirical estimates from 13 resolved outcomes + baseball domain knowledge.
_UNIVERSAL_FLOORS: dict[str, float] = {
    # Near-certain (happens in almost every game)
    "DOUB": 0.88,   # Double Play — discussed every time it happens or is possible
    "CHAL": 0.85,   # Challenge — managers challenge multiple times per game
    "GRAN": 0.75,   # Grand Slam — discussed speculatively + post-game highlight
    "TRIP": 0.70,   # Triple — rarer but exciting, always commented on
    "TRAD": 0.68,   # Trade — deadline/rumor discussion constant this time of year
    "ERRO": 0.62,   # Error — fielding mistakes get replay discussion
    # Moderate probability
    "MVPP": 0.58,   # MVP (use "MVPP" to distinguish from MVPA if both exist)
    "MVPA": 0.58,   # MVP alternate code
    "BASE": 0.55,   # Bases Loaded — common game state commentary
    "WILD": 0.48,   # Wild Pitch — happens but not every game
    "BUNT": 0.44,   # Bunt — situational play
    "WALK": 0.42,   # Walk Off — exciting but only in close late-game situations
    "WHAT": 0.38,   # What a Catch — exceptional defensive play
    "PITC": 0.35,   # Pitch Clock — violation discussion every few games
    "EXTR": 0.28,   # Extra Inning — only ~10% of games go extra innings
    "ROBO": 0.20,   # Robot (Robot Ump) — editorial reference, less common
    # Ohtani is a special case: high if LA Dodgers are playing, lower otherwise
    "OHTA": 0.45,   # Ohtani — discussed even when Dodgers aren't playing
}

# ── Home-team-specific phrase floors ─────────────────────────────────────────
# Some phrases are tied to the home team (Ohtani at Dodger Stadium, etc.)
# Parse the event ticker like KXMLBMENTION-26APR01LANYA to get "LA" home team.
_HOME_TEAM_BOOSTS: dict[str, dict[str, float]] = {
    "LA":  {"OHTA": 0.80},    # Ohtani — Dodgers home game
    "NYY": {"YANK": 0.90},    # Yankee Stadium
    "NYM": {"CITI": 0.88},    # Citi Field
    "BOS": {"FENN": 0.88},    # Fenway
    "CHC": {"WRIG": 0.87},    # Wrigley
    "SF":  {"ORAC": 0.90},    # Oracle Park
    "HOU": {"DAIK": 0.90},    # Daikin Park
    "SEA": {"TMOB": 0.90},    # T-Mobile Park
    "SD":  {"PETC": 0.88},    # Petco Park
    "TEX": {"GLOB": 0.87},    # Globe Life Field
    "ATL": {"TRUS": 0.87},    # Truist Park
    "COL": {"COOR": 0.88},    # Coors Field
    "ARI": {"CHAR": 0.87},    # Chase Field
    "STL": {"BUCK": 0.87},    # Busch Stadium
    "PIT": {"PNCA": 0.87},    # PNC Park
}


def _get_active_mlb_events(conn: sqlite3.Connection) -> list[str]:
    """Return event tickers for all open MLB mention markets."""
    rows = conn.execute(
        "SELECT DISTINCT market_id FROM markets WHERE market_id LIKE 'KXMLBMENTION%'"
    ).fetchall()
    event_tickers: set[str] = set()
    for (mid,) in rows:
        parts = mid.split("-")
        if len(parts) >= 2:
            et = "-".join(parts[:2])
            event_tickers.add(et)
    return list(event_tickers)


def _get_phrases_for_event(conn: sqlite3.Connection, event_ticker: str) -> list[str]:
    """Return phrase codes for all markets under this event ticker."""
    rows = conn.execute(
        "SELECT market_id FROM markets WHERE market_id LIKE ?",
        (f"{event_ticker}-%",),
    ).fetchall()
    codes = []
    for (mid,) in rows:
        suffix = mid[len(event_ticker) + 1:]
        if suffix:
            codes.append(suffix.upper())
    return codes


def _parse_teams(event_ticker: str) -> tuple[str, str]:
    """Extract (away, home) from ticker like KXMLBMENTION-26APR02NYMSF.

    Kalshi MLB tickers end with YYMONDDAWYHOME (e.g. 26APR02NYMSF).
    Date format: 2-digit year + 3-letter month + 2-digit day (7 chars total).
    Teams follow immediately as concatenated 2-3 letter codes.
    """
    # Strip "KXMLBMENTION-" prefix, leaving e.g. "26APR02NYMSF" or "26MAR252005NYYSF"
    m = re.search(r"KXMLBMENTION-\d{2}[A-Z]{3}\d{2,4}([A-Z]+)$", event_ticker)
    if not m:
        return "", ""
    teams_str = m.group(1)  # e.g. "NYMSF", "NYYSF", "AZLAD", "LAAHOU", "CLESEA"
    # Known MLB team codes help disambiguate splits
    _MLB_CODES = {
        "ARI","ATL","BAL","BOS","CHC","CHW","CIN","CLE","COL","DET",
        "HOU","KC","LAA","LAD","MIA","MIL","MIN","NYM","NYY","OAK",
        "PHI","PIT","SD","SEA","SF","STL","TB","TEX","TOR","WSH",
        "WSN","LA","SEA",
    }
    # Try 3+2, 3+3, 2+2, 2+3 in order of preference
    for away_len in (3, 4, 2):
        away = teams_str[:away_len]
        home = teams_str[away_len:]
        if away in _MLB_CODES and 2 <= len(home) <= 3:
            return away, home
    # Fallback: first valid split
    for split in range(2, len(teams_str) - 1):
        away, home = teams_str[:split], teams_str[split:]
        if 2 <= len(away) <= 4 and 2 <= len(home) <= 3:
            return away, home
    return teams_str, ""


def _parse_home_team(event_ticker: str) -> str:
    """Return the home team code."""
    _, home = _parse_teams(event_ticker)
    return home


def _build_signals(event_ticker: str, phrases: list[str]) -> dict:
    p_override: dict[str, float] = {}
    p_floor: dict[str, float] = {}

    _, home_team = _parse_teams(event_ticker)
    team_boosts = _HOME_TEAM_BOOSTS.get(home_team, {})

    for phrase_code in phrases:
        code = phrase_code.upper()

        # 1 — Ballpark name → hard override
        if code in _BALLPARK_CODES:
            p_override[phrase_code] = _BALLPARK_OVERRIDE
            continue

        # 2 — Team-specific boosts override universal floors
        if code in team_boosts:
            p_floor[phrase_code] = team_boosts[code]
            continue

        # 3 — Universal boilerplate floors
        if code in _UNIVERSAL_FLOORS:
            p_floor[phrase_code] = _UNIVERSAL_FLOORS[code]
            continue

    return {"p_override": p_override, "p_floor": p_floor}


def _load_existing(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def run() -> None:
    conn = _db_connect(DB_PATH)
    event_tickers = _get_active_mlb_events(conn)

    if not event_tickers:
        logger.warning("No active MLB markets found in DB")
        conn.close()
        return

    total_overrides = total_floors = 0

    for et in sorted(set(event_tickers)):
        if not et.startswith("KXMLBMENTION"):
            continue
        phrases = _get_phrases_for_event(conn, et)
        if not phrases:
            continue

        signals = _build_signals(et, phrases)

        event_id = f"auto:mlb:{et}"
        safe_filename = re.sub(r"[^\w\-]", "_", event_id) + ".json"
        sig_path = SIGNALS_DIR / safe_filename
        existing = _load_existing(sig_path)
        existing.update({
            "event_ticker": et,
            "p_override":   signals["p_override"],
            "p_floor":      signals["p_floor"],
            "injected_at":  datetime.now(timezone.utc).isoformat(),
            "source":       "extract_mlb_certainties",
        })
        sig_path.write_text(json.dumps(existing, indent=2))

        n_ov = len(signals["p_override"])
        n_fl = len(signals["p_floor"])
        total_overrides += n_ov
        total_floors += n_fl

        home = _parse_home_team(et) or "?"
        logger.info("%-40s  home=%-3s  overrides=%d  floors=%d", et, home, n_ov, n_fl)

    conn.close()
    logger.info(
        "Done — %d events processed, %d p_overrides, %d p_floors",
        len(event_tickers), total_overrides, total_floors,
    )


if __name__ == "__main__":
    run()
