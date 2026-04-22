from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.utils import utc_now_iso

EVENT_SIGNALS_DIR = Path("data/event_signals")

logger = logging.getLogger(__name__)

VALID_STATES = ("scheduled", "live", "ended", "unknown")
VALID_TYPES = (
    "rally", "briefing", "interview", "townhall", "general",
    "address", "announcement", "presser",
    # Specific-event types from KXPRESMENTION / KXTRUMPMENTION outcomes
    "visit",     # company/facility visit (e.g. "visit at Thermo Fisher")
    "remarks",   # domestic travel remarks (e.g. "remarks in Kentucky")
    "summit",    # bilateral/diplomatic meetings
    "signing",   # signing ceremonies / EO signings
    "other",
    # Extended types for non-political markets
    "press_conference", "oral_argument", "hearing", "earnings_call",
    "pmq", "bilateral_remarks",
    # Sports broadcast
    "nba_broadcast",
)


@dataclass(frozen=True)
class EventInfo:
    event_id: str
    speaker: str
    event_type: str
    speech_state: str
    scheduled_start_ts: str | None
    expected_duration_sec: int
    event_start_ts: str | None
    event_end_ts: str | None
    last_transcript_ts: str | None


@dataclass
class EventDetector:
    conn: sqlite3.Connection
    live_freshness_sec: float = 90
    ended_inactive_sec: float = 600

    def load_events_from_yaml(self, yaml_path: Path) -> int:
        """Load events from a YAML file into the events table. Returns count loaded."""
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed; cannot load events.yaml")
            return 0

        if not yaml_path.exists():
            logger.info("No events file at %s", yaml_path)
            return 0

        with yaml_path.open("r") as f:
            data = yaml.safe_load(f)

        if not data or "events" not in data:
            return 0

        count = 0
        for ev in data["events"]:
            self._upsert_event(ev)
            self._propagate_human_overrides(ev)
            count += 1

        self.conn.commit()
        logger.info("Loaded %d events from %s", count, yaml_path)
        return count

    def _upsert_event(self, ev: dict[str, Any]) -> None:
        event_id = ev["event_id"]
        speaker = ev.get("speaker", "")
        event_type = ev.get("event_type", "other")
        if event_type not in VALID_TYPES:
            event_type = "other"

        self.conn.execute(
            """
            INSERT INTO events (event_id, speaker, event_type, speech_state,
                                scheduled_start_ts, expected_duration_sec, notes)
            VALUES (?, ?, ?, 'scheduled', ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                speaker = excluded.speaker,
                event_type = excluded.event_type,
                scheduled_start_ts = excluded.scheduled_start_ts,
                expected_duration_sec = excluded.expected_duration_sec,
                notes = excluded.notes
            """,
            (
                event_id,
                speaker,
                event_type,
                ev.get("scheduled_start_ts"),
                ev.get("expected_duration_sec", 5400),
                ev.get("notes", ""),
            ),
        )

    def _propagate_human_overrides(self, ev: dict[str, Any]) -> None:
        """Write p_overrides / p_floors from events.yaml into the event signal JSON.

        This allows humans to author high-confidence overrides directly in
        events.yaml without needing to manually edit per-event signal files.
        The EventSignalStore will pick them up automatically on next hot-reload.

        events.yaml format:
          kalshi_event_ticker: KXTRUMPMENTION-26MAR24   # optional — links to auto-seeded event
          p_overrides:
            kristi: 0.95          # bypasses frequency formula entirely
            noem: 0.90
          p_floors:
            deport: 0.65          # minimum probability for domain phrases
            illegal alien: 0.70

        If kalshi_event_ticker is set, overrides are also written to the matching
        auto-seeded signal file (auto_{speaker}_{ticker}.json).  This ensures the
        scorer picks them up even when the auto-seeded event beats the manual one
        in get_active_event() ticker-suffix matching.
        """
        event_id = ev.get("event_id", "")
        raw_overrides = ev.get("p_overrides", {})
        raw_floors    = ev.get("p_floors", {})

        if not (raw_overrides or raw_floors):
            return
        if not event_id:
            return

        EVENT_SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

        # Determine all target filenames:
        # 1. The manual event's own signal file
        # 2. The auto-seeded event file (if kalshi_event_ticker is specified)
        target_ids: list[tuple[str, str]] = []

        safe_manual = re.sub(r"[^\w\-]", "_", event_id) + ".json"
        target_ids.append((event_id, safe_manual))

        kalshi_ticker = ev.get("kalshi_event_ticker", "").strip()
        if kalshi_ticker:
            speaker = ev.get("speaker", "")
            auto_event_id = f"auto:{speaker}:{kalshi_ticker}"
            safe_auto = re.sub(r"[^\w\-]", "_", auto_event_id) + ".json"
            target_ids.append((auto_event_id, safe_auto))

        # Normalise p_overrides once (same values written to all target files)
        validated_overrides: dict[str, Any] = {}
        for phrase, val in (raw_overrides or {}).items():
            key = str(phrase).lower().strip()
            try:
                pv = float(val) if not isinstance(val, dict) else float(val.get("p_override", 0))
            except (TypeError, ValueError):
                continue
            if 0.0 < pv <= 1.0:
                validated_overrides[key] = {
                    "p_override": round(pv, 3),
                    "reason": val.get("reason", "Human override from events.yaml") if isinstance(val, dict) else "Human override from events.yaml",
                }

        # Normalise p_floors
        validated_floors: dict[str, Any] = {}
        for phrase, val in (raw_floors or {}).items():
            key = str(phrase).lower().strip()
            try:
                pv = float(val) if not isinstance(val, dict) else float(val.get("p_floor", 0))
            except (TypeError, ValueError):
                continue
            if 0.30 <= pv <= 0.92:
                validated_floors[key] = {
                    "p_floor": round(pv, 3),
                    "reason": val.get("reason", "Domain floor from events.yaml") if isinstance(val, dict) else "Domain floor from events.yaml",
                }

        # Write to each target file (manual event + auto-seeded event if ticker linked)
        for target_event_id, safe_name in target_ids:
            path = EVENT_SIGNALS_DIR / safe_name

            # Load existing file (preserve LLM adjustments) or start fresh
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            else:
                existing = {
                    "event_id": target_event_id,
                    "speaker": ev.get("speaker", ""),
                    "source": "human_override",
                    "adjustments": {},
                }

            # Merge: human overrides take precedence over LLM-generated entries
            existing["p_overrides"] = {**existing.get("p_overrides", {}), **validated_overrides}
            existing["p_floors"]    = {**existing.get("p_floors", {}), **validated_floors}

            path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(
                "Propagated human overrides for %s: %d overrides, %d floors → %s",
                target_event_id, len(validated_overrides), len(validated_floors), path.name,
            )

    def get_active_event(self, speaker: str, event_ticker: str | None = None) -> EventInfo | None:
        """Get the most relevant event for a speaker.

        If *event_ticker* is provided, prefer the event whose event_id
        contains that ticker (auto-seeded events use the format
        ``auto:<speaker>:<event_ticker>``).  Falls back to any active
        event for the speaker when no ticker-specific match is found.
        """
        if event_ticker:
            suffix = f":{event_ticker}"
            row = self.conn.execute(
                """
                SELECT * FROM events
                WHERE speaker = ?
                  AND event_id LIKE '%' || ?
                  AND (speech_state IN ('live', 'scheduled', 'unknown')
                       OR (speech_state = 'ended' AND date(event_end_ts) = date('now')))
                ORDER BY
                    CASE speech_state
                        WHEN 'live' THEN 1 WHEN 'unknown' THEN 2
                        WHEN 'scheduled' THEN 3 WHEN 'ended' THEN 4
                    END,
                    scheduled_start_ts ASC
                LIMIT 1
                """,
                (speaker, suffix),
            ).fetchone()
            if row:
                return self._row_to_event(row)

        row = self.conn.execute(
            """
            SELECT * FROM events
            WHERE speaker = ?
              AND (speech_state IN ('live', 'scheduled', 'unknown')
                   OR (speech_state = 'ended' AND date(event_end_ts) = date('now')))
            ORDER BY
                CASE speech_state
                    WHEN 'live' THEN 1
                    WHEN 'unknown' THEN 2
                    WHEN 'scheduled' THEN 3
                    WHEN 'ended' THEN 4
                END,
                scheduled_start_ts ASC
            LIMIT 1
            """,
            (speaker,),
        ).fetchone()

        if not row:
            return None
        return self._row_to_event(row)

    def _row_to_event(self, row: sqlite3.Row) -> EventInfo:
        return EventInfo(
            event_id=row["event_id"],
            speaker=row["speaker"],
            event_type=row["event_type"],
            speech_state=row["speech_state"],
            scheduled_start_ts=row["scheduled_start_ts"],
            expected_duration_sec=row["expected_duration_sec"],
            event_start_ts=row["event_start_ts"],
            event_end_ts=row["event_end_ts"],
            last_transcript_ts=row["last_transcript_ts"],
        )

    def get_all_active_events(self) -> list[EventInfo]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE speech_state IN ('scheduled', 'live', 'unknown') "
            "ORDER BY scheduled_start_ts"
        ).fetchall()
        return [
            EventInfo(
                event_id=r["event_id"], speaker=r["speaker"],
                event_type=r["event_type"], speech_state=r["speech_state"],
                scheduled_start_ts=r["scheduled_start_ts"],
                expected_duration_sec=r["expected_duration_sec"],
                event_start_ts=r["event_start_ts"],
                event_end_ts=r["event_end_ts"],
                last_transcript_ts=r["last_transcript_ts"],
            )
            for r in rows
        ]

    def transition_to_live(self, event_id: str) -> None:
        now = utc_now_iso()
        self.conn.execute(
            "UPDATE events SET speech_state = 'live', event_start_ts = ? WHERE event_id = ?",
            (now, event_id),
        )
        self.conn.commit()
        logger.info("Event %s -> live at %s", event_id, now)

    def transition_to_ended(self, event_id: str) -> None:
        now = utc_now_iso()
        self.conn.execute(
            "UPDATE events SET speech_state = 'ended', event_end_ts = ? WHERE event_id = ?",
            (now, event_id),
        )
        self.conn.commit()
        logger.info("Event %s -> ended at %s", event_id, now)

    def transition_to_unknown(self, event_id: str) -> None:
        self.conn.execute(
            "UPDATE events SET speech_state = 'unknown' WHERE event_id = ?",
            (event_id,),
        )
        self.conn.commit()
        logger.info("Event %s -> unknown", event_id)

    def update_transcript_ts(self, speaker: str) -> None:
        """Mark that fresh transcript data arrived for this speaker."""
        now = utc_now_iso()
        self.conn.execute(
            """
            UPDATE events SET last_transcript_ts = ?
            WHERE speaker = ? AND speech_state IN ('live', 'scheduled', 'unknown')
            """,
            (now, speaker),
        )
        self.conn.commit()

    def run_transitions(self) -> list[tuple[str, str, str]]:
        """Check all active events and apply state transitions. Returns list of (event_id, old, new)."""
        now_dt = datetime.now(tz=timezone.utc)
        changes: list[tuple[str, str, str]] = []

        for ev in self.get_all_active_events():
            old_state = ev.speech_state
            new_state = self._compute_transition(ev, now_dt)
            if new_state and new_state != old_state:
                if new_state == "live":
                    self.transition_to_live(ev.event_id)
                elif new_state == "ended":
                    self.transition_to_ended(ev.event_id)
                elif new_state == "unknown":
                    self.transition_to_unknown(ev.event_id)
                changes.append((ev.event_id, old_state, new_state))

        return changes

    def _compute_transition(self, ev: EventInfo, now_dt: datetime) -> str | None:
        if ev.speech_state == "scheduled":
            return self._transition_from_scheduled(ev, now_dt)
        if ev.speech_state == "live":
            return self._transition_from_live(ev, now_dt)
        if ev.speech_state == "unknown":
            return self._transition_from_unknown(ev, now_dt)
        return None

    def _transition_from_scheduled(self, ev: EventInfo, now_dt: datetime) -> str | None:
        if ev.last_transcript_ts:
            age = self._seconds_since(ev.last_transcript_ts, now_dt)
            if age < self.live_freshness_sec:
                return "live"

        if ev.scheduled_start_ts:
            start = datetime.fromisoformat(ev.scheduled_start_ts)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if now_dt >= start:
                return "live"

        return None

    def _transition_from_live(self, ev: EventInfo, now_dt: datetime) -> str | None:
        if not ev.last_transcript_ts:
            elapsed = self._elapsed_since_start(ev, now_dt)
            if elapsed and elapsed > ev.expected_duration_sec + self.ended_inactive_sec:
                return "ended"
            return None

        age = self._seconds_since(ev.last_transcript_ts, now_dt)
        if age > self.ended_inactive_sec:
            return "ended"
        if age > self.live_freshness_sec:
            return "unknown"
        return None

    def _transition_from_unknown(self, ev: EventInfo, now_dt: datetime) -> str | None:
        if ev.last_transcript_ts:
            age = self._seconds_since(ev.last_transcript_ts, now_dt)
            if age < self.live_freshness_sec:
                return "live"
            if age > self.ended_inactive_sec:
                return "ended"
        return None

    @staticmethod
    def _seconds_since(ts_iso: str, now_dt: datetime) -> float:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0, (now_dt - ts).total_seconds())

    @staticmethod
    def _elapsed_since_start(ev: EventInfo, now_dt: datetime) -> float | None:
        start_ts = ev.event_start_ts or ev.scheduled_start_ts
        if not start_ts:
            return None
        start = datetime.fromisoformat(start_ts)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return max(0, (now_dt - start).total_seconds())
