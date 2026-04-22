from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class OutcomeReviewRow:
    market_id: str
    prediction_ts: str
    event_ticker: str
    speaker: str
    phrase: str
    side: str
    p_literal: float
    yes_ask: float
    no_ask: float
    ev_yes: float
    ev_no: float
    outcome: str
    realized_pnl: float
    reason_codes: str
    tags: str
    raw_json: str


def load_outcome_map(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    markets = payload.get("markets", [])
    out: dict[str, dict] = {}
    for row in markets:
        ticker = str(row.get("ticker", "")).strip()
        result = str(row.get("result", "")).strip().lower()
        if not ticker or result not in {"yes", "no"}:
            continue
        out[ticker] = row
    return out


def _extract_tags(reason_codes: list[str]) -> str:
    tracked = [
        "ON_TOPIC",
        "OFF_TOPIC",
        "MARKET_ANCHOR",
        "POLY_DIVERGE",
        "POLY_HIGHER",
        "POLY_LOWER",
    ]
    return ",".join(tag for tag in tracked if tag in reason_codes)


def _realized_pnl(side: str, yes_ask: float, no_ask: float, outcome: str) -> float:
    if side == "BUY_YES":
        return round((1.0 - yes_ask) if outcome == "yes" else -yes_ask, 4)
    if side == "BUY_NO":
        return round((1.0 - no_ask) if outcome == "no" else -no_ask, 4)
    return 0.0


def _parse_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    raw = str(ts).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _select_decision_card(cards: list[sqlite3.Row], decision_mode: str) -> sqlite3.Row | None:
    if not cards:
        return None
    if decision_mode == "last_buy":
        return cards[-1]
    if decision_mode == "best_ev":
        return max(
            cards,
            key=lambda c: float(c["ev_yes"]) if str(c["side"]) == "BUY_YES" else float(c["ev_no"]),
        )
    return cards[0]


def _load_cards_from_bet_journal(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Load persisted bet decisions from bet_journal (survives action_cards pruning).

    Returns a dict mapping market_id → list of card-like dicts, sorted by first_seen_ts.
    """
    # bet_journal may not exist on very old DBs; guard gracefully.
    try:
        rows = conn.execute(
            """
            SELECT market_id, phrase, side, p_literal, yes_ask, no_ask,
                   ev_yes, ev_no, first_seen_ts AS ts, reason_codes, raw_json
            FROM   bet_journal
            WHERE  side IN ('BUY_YES', 'BUY_NO')
            ORDER  BY market_id ASC, first_seen_ts ASC
            """
        ).fetchall()
    except Exception:
        return {}

    by_market: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        by_market.setdefault(str(d["market_id"]), []).append(d)
    return by_market


def build_outcome_rows(
    conn: sqlite3.Connection,
    outcome_map: dict[str, dict],
    decision_mode: str = "first_buy",
) -> list[OutcomeReviewRow]:
    # Primary source: bet_journal (persistent, survives pruning, covers weekly/monthly)
    journal_by_market = _load_cards_from_bet_journal(conn)

    # Fallback source: action_cards (still present within 1-day retention window)
    live_cards = conn.execute(
        """
        SELECT ts, market_id, phrase, side, p_literal, yes_ask, no_ask, ev_yes, ev_no, raw_json
        FROM action_cards
        WHERE side IN ('BUY_YES', 'BUY_NO')
        ORDER BY market_id ASC, ts ASC, id ASC
        """
    ).fetchall()

    # Merge: prefer bet_journal entry; fall back to live action_cards for same-day markets
    cards_by_market: dict[str, list] = dict(journal_by_market)
    for card in live_cards:
        mid = str(card["market_id"])
        if mid not in cards_by_market:
            cards_by_market[mid] = []
        # Only add if not already covered by journal entry for this market
        if mid not in journal_by_market:
            cards_by_market[mid].append(dict(card))

    rows: list[OutcomeReviewRow] = []
    for market_id, outcome_row in outcome_map.items():
        outcome_row = outcome_map.get(market_id)
        if not outcome_row:
            continue

        cards = cards_by_market.get(market_id, [])
        if not cards:
            continue

        close_dt = _parse_dt(str(outcome_row.get("close_time", "")))
        if close_dt is not None:
            pre_close_cards = [c for c in cards if (_parse_dt(str(c["ts"])) or close_dt) <= close_dt]
        else:
            pre_close_cards = cards
        if not pre_close_cards:
            pre_close_cards = cards  # no close_time filter possible; use all

        if not pre_close_cards:
            continue

        # Convert to consistent dicts for _select_decision_card compatibility
        card = pre_close_cards[0] if decision_mode == "first_buy" else pre_close_cards[-1]
        if decision_mode == "best_ev":
            card = max(
                pre_close_cards,
                key=lambda c: float(c["ev_yes"]) if str(c["side"]) == "BUY_YES" else float(c["ev_no"]),
            )

        outcome = str(outcome_row.get("result", "")).lower()
        side = str(card["side"])
        yes_ask = float(card["yes_ask"])
        no_ask = float(card["no_ask"])

        raw: dict = {}
        try:
            raw = json.loads(card["raw_json"])
        except Exception:
            raw = {}

        reason_codes = raw.get("reason_codes", [])
        event = raw.get("event", {}) if isinstance(raw.get("event"), dict) else {}
        realized = _realized_pnl(side, yes_ask, no_ask, outcome)
        rows.append(
            OutcomeReviewRow(
                market_id=market_id,
                prediction_ts=str(card["ts"]),
                event_ticker=str(event.get("event_id", "")).split(":")[-1] if event else "",
                speaker=str(raw.get("subject", "")),
                phrase=str(card["phrase"]),
                side=side,
                p_literal=float(card["p_literal"]),
                yes_ask=yes_ask,
                no_ask=no_ask,
                ev_yes=float(card.get("ev_yes", 0.0)),
                ev_no=float(card.get("ev_no", 0.0)),
                outcome=outcome,
                realized_pnl=realized,
                reason_codes=",".join(reason_codes) if isinstance(reason_codes, list) else str(card.get("reason_codes", "")),
                tags=_extract_tags(reason_codes if isinstance(reason_codes, list) else []),
                raw_json=json.dumps(
                    {
                        **raw,
                        "outcome_review_meta": {
                            "decision_mode": decision_mode,
                            "market_close_time": outcome_row.get("close_time"),
                            "source": "bet_journal" if market_id in journal_by_market else "action_cards",
                        },
                    },
                    ensure_ascii=True,
                ),
            )
        )
    return rows


def upsert_outcome_rows(conn: sqlite3.Connection, rows: list[OutcomeReviewRow]) -> int:
    conn.executescript(
        """
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
        """
    )
    inserted = 0
    for row in rows:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO outcome_reviews (
                market_id, prediction_ts, event_ticker, speaker, phrase,
                side, p_literal, yes_ask, no_ask, ev_yes, ev_no,
                outcome, realized_pnl, reason_codes, tags, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.market_id,
                row.prediction_ts,
                row.event_ticker,
                row.speaker,
                row.phrase,
                row.side,
                row.p_literal,
                row.yes_ask,
                row.no_ask,
                row.ev_yes,
                row.ev_no,
                row.outcome,
                row.realized_pnl,
                row.reason_codes,
                row.tags,
                row.raw_json,
            ),
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    if inserted:
        conn.commit()
    return inserted
