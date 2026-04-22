from __future__ import annotations

import pytest

from app.phrase_matcher import PhraseMatcher, PhraseHit, store_phrase_hits


FIXTURE_TEXT = (
    "The President discussed NATO expansion during the rally. "
    "He also mentioned tariffs on Chinese goods and the Federal Reserve's rate decision. "
    "No mention of border security in this segment. "
    "Later, Karoline Leavitt addressed immigration policy and the economy during her press briefing. "
    "Zohran Mamdani spoke about rent freeze proposals, public transit improvements, and housing affordability."
)


class TestPhraseMatcher:
    def test_finds_exact_phrases(self):
        matcher = PhraseMatcher(["nato", "tariffs", "rent freeze"])
        hits = matcher.find_hits(FIXTURE_TEXT)
        phrases_found = {h.phrase for h in hits}
        assert "nato" in phrases_found
        assert "tariffs" in phrases_found
        assert "rent freeze" in phrases_found

    def test_boundary_safe_no_substring_hits(self):
        """'nato' must NOT match inside 'donato' or 'natorious'."""
        matcher = PhraseMatcher(["nato"])
        text = "Senator Donato spoke about natorious policies at the NATO summit."
        hits = matcher.find_hits(text)
        assert len(hits) == 1
        assert hits[0].snippet.lower().count("nato summit") >= 1 or "NATO" in hits[0].snippet

    def test_no_false_positives_for_partial_words(self):
        matcher = PhraseMatcher(["tariff"])
        text = "The tariffication of goods is complex."
        hits = matcher.find_hits(text)
        assert len(hits) == 0, "Should not match 'tariff' inside 'tariffication'"

    def test_case_insensitive(self):
        matcher = PhraseMatcher(["federal reserve"])
        text = "The FEDERAL RESERVE announced new policy."
        hits = matcher.find_hits(text)
        assert len(hits) == 1

    def test_multiple_occurrences(self):
        matcher = PhraseMatcher(["economy"])
        text = "The economy is strong. Experts say the economy will grow."
        hits = matcher.find_hits(text)
        assert len(hits) == 2

    def test_empty_text_returns_no_hits(self):
        matcher = PhraseMatcher(["nato", "tariff"])
        assert matcher.find_hits("") == []

    def test_no_phrases_returns_no_hits(self):
        matcher = PhraseMatcher([])
        assert matcher.find_hits(FIXTURE_TEXT) == []

    def test_snippet_includes_context(self):
        matcher = PhraseMatcher(["public transit"])
        hits = matcher.find_hits(FIXTURE_TEXT)
        assert len(hits) == 1
        assert "public transit" in hits[0].snippet.lower()
        assert len(hits[0].snippet) > len("public transit")

    def test_hits_sorted_by_position(self):
        matcher = PhraseMatcher(["nato", "tariffs", "rent freeze"])
        hits = matcher.find_hits(FIXTURE_TEXT)
        positions = [h.start_idx for h in hits]
        assert positions == sorted(positions)

    def test_ignores_speaker_label_prefix_hits(self):
        matcher = PhraseMatcher(["president", "trump"])
        text = (
            "President Trump: Thank you everybody for being here.\n"
            "We discussed President Biden and what Trump said yesterday.\n"
        )
        hits = matcher.find_hits(text)
        # Do not count 'President Trump:' label, but count in spoken content.
        phrases = [h.phrase for h in hits]
        assert phrases.count("president") == 1
        assert phrases.count("trump") == 1


class TestStorePhraseHits:
    def test_stores_and_counts(self, tmp_db):
        conn = tmp_db
        conn.execute(
            "INSERT INTO transcripts (ts, source, source_ref, text, text_hash) "
            "VALUES ('2026-03-01T00:00:00', 'test', 'ref', 'text', 'hash')"
        )
        conn.commit()

        hits = [
            PhraseHit(phrase="nato", start_idx=0, end_idx=4, snippet="nato expansion"),
            PhraseHit(phrase="tariff", start_idx=50, end_idx=56, snippet="tariff on goods"),
        ]
        count = store_phrase_hits(conn, transcript_id=1, hits=hits, ts="2026-03-01T12:00:00")
        assert count == 2

        rows = conn.execute("SELECT * FROM phrase_hits").fetchall()
        assert len(rows) == 2
        assert rows[0]["phrase"] == "nato"
        assert rows[1]["phrase"] == "tariff"
        assert rows[0]["hit_date"] == "2026-03-01"

    def test_empty_hits_stores_nothing(self, tmp_db):
        count = store_phrase_hits(tmp_db, transcript_id=1, hits=[], ts="2026-03-01T00:00:00")
        assert count == 0


class TestFixtureDeterminism:
    """M1 acceptance: known fixture inputs produce deterministic expected hits."""

    EXPECTED_PHRASES = {
        "nato", "tariffs", "federal reserve", "border security",
        "immigration", "economy", "press briefing",
        "rent freeze", "public transit", "housing",
    }

    def test_fixture_produces_all_expected_hits(self):
        matcher = PhraseMatcher(list(self.EXPECTED_PHRASES))
        hits = matcher.find_hits(FIXTURE_TEXT)
        found = {h.phrase for h in hits}
        assert found == self.EXPECTED_PHRASES, f"Missing: {self.EXPECTED_PHRASES - found}"

    def test_fixture_is_deterministic(self):
        """Running the matcher twice on the same text yields identical results."""
        matcher = PhraseMatcher(list(self.EXPECTED_PHRASES))
        hits_a = matcher.find_hits(FIXTURE_TEXT)
        hits_b = matcher.find_hits(FIXTURE_TEXT)
        assert [(h.phrase, h.start_idx, h.end_idx) for h in hits_a] == [
            (h.phrase, h.start_idx, h.end_idx) for h in hits_b
        ]
