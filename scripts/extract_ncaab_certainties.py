#!/usr/bin/env python3
"""Inject near-certain p_overrides and p_floors for NCAAB mention markets.

NCAAB markets don't have arena-name phrases (Kalshi uses generic broadcast
phrases for college basketball).  This script injects empirically-calibrated
p_floors based on 100 resolved NCAAB outcomes.

Reads:  data/edge.db — markets + outcome_reviews tables
Writes: data/event_signals/auto_ncaab_<event_ticker>.json
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

# ── NCAAB universal p_floors ──────────────────────────────────────────────────
# Calibrated from 100 resolved NCAAB outcome_reviews.
# YES rate by phrase code from resolved Kalshi markets.
#
# Phrase rates (empirical from outcome_reviews):
#   all american  78% YES  — tournament/award talk all season
#   recruit       75% YES  — recruiting is constant NCAAB topic
#   airball       75% YES  — happens in nearly every game
#   double double 56% YES  — stat achievement, discussed when it happens
#   record        50% YES  — breaking records discussed throughout season
#   ankle         50% YES  — common injury, always commented on
#   buzzer        44% YES  — buzzer beaters / end-of-quarter discussion
#   elbow         38% YES  — injury area or foul description
#   draft         25% YES  — NBA draft conversation (March = draft talk)
#   overtime      13% YES  — ~10-15% of games go to OT
#   schedule       0% YES  — schedule discussion surprisingly absent
#   alley-oop      0% YES  — rare in college vs NBA
#
# Conservative: set floors ~5pp below empirical to account for variance.
_UNIVERSAL_FLOORS: dict[str, float] = {
    "ALLA": 0.72,   # All American — tournament season, award talk
    "ALAA": 0.72,   # All American alternate code
    "RECR": 0.70,   # Recruit/Recruiting — constant topic
    "AIRB": 0.68,   # Airball — common embarrassing play
    "DOUB": 0.50,   # Double Double — stat milestone
    "RECO": 0.44,   # Record — season/career records
    "ANKL": 0.44,   # Ankle — injury discussion
    "ANKA": 0.44,   # Ankle alternate
    "BUZZ": 0.38,   # Buzzer — buzzer beater / shot clock
    "ELBO": 0.32,   # Elbow — injury or court zone
    "NIL":  0.55,   # NIL (Name Image Likeness) — major NCAAB topic
    "TRAN": 0.72,   # Transfer — transfer portal is dominant NCAAB story
    "DRAF": 0.20,   # Draft — NBA draft conversation
    "OVER": 0.10,   # Overtime — only ~13% games, but speculatively discussed
    "WALK": 0.06,   # Walk On — rare heartwarming story angle
    "SCHE": 0.02,   # Schedule — basically never discussed mid-game
    "ALLE": 0.05,   # Alley-oop — rare in college, 0% empirically
}

# ── Matchup-specific team name phrases ───────────────────────────────────────
# NCAAB event tickers encode matchup: KXNCAABMENTION-26MAR17TEXNCST
# → away=TEX, home=NCST. Both team names are likely mentioned.
# These are inferred from the ticker; set a moderate floor.
_TEAM_NAME_FLOOR = 0.68    # team names mentioned frequently in every broadcast

# Common NCAAB team name codes (Kalshi uses these as phrase suffixes sometimes)
_KNOWN_TEAM_CODES: frozenset[str] = frozenset({
    "DUKE", "KANS", "KENT", "KENN", "GONZ", "ARIZ", "UCLA", "UCON", "ILLI",
    "NCAR", "IOWA", "MICH", "OHIO", "INDI", "PURD", "TENN", "VILL", "SYRA",
    "BOIS", "SION", "OREG", "WASH", "GTWN", "LOUI", "MARQ", "OKLA",
})


def _get_active_ncaab_events(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT market_id FROM markets WHERE market_id LIKE 'KXNCAABMENTION%'"
    ).fetchall()
    event_tickers: set[str] = set()
    for (mid,) in rows:
        parts = mid.split("-")
        if len(parts) >= 2:
            et = "-".join(parts[:2])
            event_tickers.add(et)
    return list(event_tickers)


def _get_phrases_for_event(conn: sqlite3.Connection, event_ticker: str) -> list[str]:
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
    """Extract (away_team, home_team) from ticker like KXNCAABMENTION-26MAR17TEXNCST."""
    m = re.search(r"KXNCAABMENTION-\d+([A-Z]{2,5})([A-Z]{2,5})$", event_ticker)
    if m:
        return m.group(1), m.group(2)
    return "", ""


def _build_signals(event_ticker: str, phrases: list[str]) -> dict:
    p_floor: dict[str, float] = {}

    away, home = _parse_teams(event_ticker)
    team_codes = {away, home} - {""}

    for phrase_code in phrases:
        code = phrase_code.upper()

        # Team name phrases get a moderate floor
        if code in _KNOWN_TEAM_CODES or any(code.startswith(t[:3]) for t in team_codes if len(t) >= 3):
            p_floor[phrase_code] = _TEAM_NAME_FLOOR
            continue

        # Universal phrase floors from resolved outcome data
        if code in _UNIVERSAL_FLOORS:
            p_floor[phrase_code] = _UNIVERSAL_FLOORS[code]
            continue

    # NCAAB has no ballpark-style overrides (no sponsor arena phrases in markets)
    return {"p_override": {}, "p_floor": p_floor}


def _load_existing(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def run() -> None:
    conn = _db_connect(DB_PATH)
    event_tickers = _get_active_ncaab_events(conn)

    if not event_tickers:
        logger.warning("No active NCAAB markets found in DB")
        conn.close()
        return

    total_floors = 0

    for et in sorted(set(event_tickers)):
        phrases = _get_phrases_for_event(conn, et)
        if not phrases:
            continue

        signals = _build_signals(et, phrases)

        event_id = f"auto:ncaab:{et}"
        safe_filename = re.sub(r"[^\w\-]", "_", event_id) + ".json"
        sig_path = SIGNALS_DIR / safe_filename
        existing = _load_existing(sig_path)
        existing.update({
            "event_ticker": et,
            "p_override":   signals["p_override"],
            "p_floor":      signals["p_floor"],
            "injected_at":  datetime.now(timezone.utc).isoformat(),
            "source":       "extract_ncaab_certainties",
        })
        sig_path.write_text(json.dumps(existing, indent=2))

        n_fl = len(signals["p_floor"])
        total_floors += n_fl
        away, home = _parse_teams(et)
        logger.info("%-42s  %s@%s  floors=%d", et, away, home, n_fl)

    conn.close()
    logger.info(
        "Done — %d events processed, %d p_floors injected",
        len(event_tickers), total_floors,
    )


if __name__ == "__main__":
    run()
