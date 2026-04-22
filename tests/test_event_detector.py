from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.event_detector import EventDetector
from app.utils import utc_now_iso


def _now_iso() -> str:
    return utc_now_iso()


def _insert_event(conn, event_id, speaker="trump", event_type="rally",
                  state="scheduled", scheduled_start_ts=None,
                  expected_duration=5400, event_start_ts=None,
                  last_transcript_ts=None):
    conn.execute(
        "INSERT INTO events (event_id, speaker, event_type, speech_state, "
        "scheduled_start_ts, expected_duration_sec, event_start_ts, last_transcript_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, speaker, event_type, state, scheduled_start_ts,
         expected_duration, event_start_ts, last_transcript_ts),
    )
    conn.commit()


class TestGetActiveEvent:
    def test_returns_none_when_no_events(self, tmp_db):
        detector = EventDetector(conn=tmp_db)
        assert detector.get_active_event("trump") is None

    def test_returns_live_over_scheduled(self, tmp_db):
        _insert_event(tmp_db, "ev-sched", speaker="trump", state="scheduled")
        _insert_event(tmp_db, "ev-live", speaker="trump", state="live")

        detector = EventDetector(conn=tmp_db)
        ev = detector.get_active_event("trump")
        assert ev is not None
        assert ev.event_id == "ev-live"
        assert ev.speech_state == "live"

    def test_ignores_old_ended_events(self, tmp_db):
        """Ended events with no end timestamp (or old dates) are excluded."""
        tmp_db.execute(
            "INSERT INTO events (event_id, speaker, event_type, speech_state, event_end_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ev-ended", "trump", "rally", "ended", "2025-01-01T00:00:00Z"),
        )
        tmp_db.commit()

        detector = EventDetector(conn=tmp_db)
        assert detector.get_active_event("trump") is None

    def test_returns_todays_ended_event(self, tmp_db):
        """Ended events from today are still returned for scoring."""
        _insert_event(tmp_db, "ev-ended", speaker="trump", state="ended")
        tmp_db.execute(
            "UPDATE events SET event_end_ts = datetime('now') WHERE event_id = 'ev-ended'"
        )
        tmp_db.commit()

        detector = EventDetector(conn=tmp_db)
        ev = detector.get_active_event("trump")
        assert ev is not None
        assert ev.speech_state == "ended"

    def test_scopes_by_speaker(self, tmp_db):
        _insert_event(tmp_db, "ev-leavitt", speaker="leavitt", state="live")

        detector = EventDetector(conn=tmp_db)
        assert detector.get_active_event("trump") is None
        ev = detector.get_active_event("leavitt")
        assert ev is not None
        assert ev.speaker == "leavitt"


class TestTransitions:
    def test_scheduled_to_live_on_start_time(self, tmp_db):
        past = (datetime.now(tz=timezone.utc) - timedelta(minutes=5)).isoformat()
        _insert_event(tmp_db, "ev1", state="scheduled", scheduled_start_ts=past)

        detector = EventDetector(conn=tmp_db)
        changes = detector.run_transitions()

        assert len(changes) == 1
        assert changes[0] == ("ev1", "scheduled", "live")

        ev = detector.get_active_event("trump")
        assert ev.speech_state == "live"

    def test_scheduled_stays_when_future(self, tmp_db):
        future = (datetime.now(tz=timezone.utc) + timedelta(hours=2)).isoformat()
        _insert_event(tmp_db, "ev1", state="scheduled", scheduled_start_ts=future)

        detector = EventDetector(conn=tmp_db)
        changes = detector.run_transitions()
        assert len(changes) == 0

    def test_scheduled_to_live_on_transcript(self, tmp_db):
        future = (datetime.now(tz=timezone.utc) + timedelta(hours=2)).isoformat()
        fresh = _now_iso()
        _insert_event(tmp_db, "ev1", state="scheduled",
                      scheduled_start_ts=future, last_transcript_ts=fresh)

        detector = EventDetector(conn=tmp_db, live_freshness_sec=120)
        changes = detector.run_transitions()

        assert len(changes) == 1
        assert changes[0][2] == "live"

    def test_live_to_ended_on_inactivity(self, tmp_db):
        start = (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat()
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(minutes=15)).isoformat()
        _insert_event(tmp_db, "ev1", state="live",
                      event_start_ts=start, last_transcript_ts=old_ts)

        detector = EventDetector(conn=tmp_db, ended_inactive_sec=600)
        changes = detector.run_transitions()

        assert len(changes) == 1
        assert changes[0][2] == "ended"

    def test_live_to_unknown_on_stale(self, tmp_db):
        start = (datetime.now(tz=timezone.utc) - timedelta(minutes=30)).isoformat()
        stale = (datetime.now(tz=timezone.utc) - timedelta(seconds=120)).isoformat()
        _insert_event(tmp_db, "ev1", state="live",
                      event_start_ts=start, last_transcript_ts=stale)

        detector = EventDetector(conn=tmp_db, live_freshness_sec=90, ended_inactive_sec=600)
        changes = detector.run_transitions()

        assert len(changes) == 1
        assert changes[0][2] == "unknown"

    def test_unknown_to_live_on_fresh_transcript(self, tmp_db):
        fresh = _now_iso()
        _insert_event(tmp_db, "ev1", state="unknown", last_transcript_ts=fresh)

        detector = EventDetector(conn=tmp_db, live_freshness_sec=90)
        changes = detector.run_transitions()

        assert len(changes) == 1
        assert changes[0][2] == "live"


class TestTransitionToMethods:
    def test_transition_to_live(self, tmp_db):
        _insert_event(tmp_db, "ev1", state="scheduled")

        detector = EventDetector(conn=tmp_db)
        detector.transition_to_live("ev1")

        ev = detector.get_active_event("trump")
        assert ev.speech_state == "live"
        assert ev.event_start_ts is not None

    def test_transition_to_ended(self, tmp_db):
        _insert_event(tmp_db, "ev1", state="live")

        detector = EventDetector(conn=tmp_db)
        detector.transition_to_ended("ev1")

        row = tmp_db.execute("SELECT * FROM events WHERE event_id = 'ev1'").fetchone()
        assert row["speech_state"] == "ended"
        assert row["event_end_ts"] is not None

    def test_update_transcript_ts(self, tmp_db):
        _insert_event(tmp_db, "ev1", state="live")

        detector = EventDetector(conn=tmp_db)
        detector.update_transcript_ts("trump")

        ev = detector.get_active_event("trump")
        assert ev.last_transcript_ts is not None


class TestLoadEventsFromYaml:
    def test_loads_from_yaml(self, tmp_db, tmp_dir):
        yaml_path = tmp_dir / "events.yaml"
        yaml_path.write_text(
            "events:\n"
            '  - event_id: "trump:test:rally-01"\n'
            "    speaker: trump\n"
            "    event_type: rally\n"
            '    scheduled_start_ts: "2026-03-10T19:00:00Z"\n'
            "    expected_duration_sec: 5400\n"
        )

        detector = EventDetector(conn=tmp_db)
        count = detector.load_events_from_yaml(yaml_path)

        assert count == 1
        ev = detector.get_active_event("trump")
        assert ev is not None
        assert ev.event_type == "rally"

    def test_missing_file_returns_zero(self, tmp_db, tmp_dir):
        detector = EventDetector(conn=tmp_db)
        count = detector.load_events_from_yaml(tmp_dir / "nope.yaml")
        assert count == 0

    def test_upsert_updates_existing(self, tmp_db, tmp_dir):
        yaml_path = tmp_dir / "events.yaml"
        yaml_path.write_text(
            "events:\n"
            '  - event_id: "ev1"\n'
            "    speaker: trump\n"
            "    event_type: rally\n"
            "    expected_duration_sec: 3600\n"
        )

        detector = EventDetector(conn=tmp_db)
        detector.load_events_from_yaml(yaml_path)

        yaml_path.write_text(
            "events:\n"
            '  - event_id: "ev1"\n'
            "    speaker: trump\n"
            "    event_type: briefing\n"
            "    expected_duration_sec: 1800\n"
        )
        detector.load_events_from_yaml(yaml_path)

        ev = detector.get_active_event("trump")
        assert ev.event_type == "briefing"
        assert ev.expected_duration_sec == 1800
