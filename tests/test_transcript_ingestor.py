from __future__ import annotations

from pathlib import Path

from app.phrase_matcher import PhraseMatcher
from app.transcript_ingestor import TranscriptIngestor, _text_hash
from app.transcript_sources import TranscriptRecord


class _FakeSource:
    name = "fake"

    def __init__(self, text: str, require_ref: bool = False):
        self._text = text
        self._require_ref = require_ref

    def fetch(self, ref=None):
        if self._text is None:
            return None
        if self._require_ref and not ref:
            return None
        return TranscriptRecord(source="fake", source_ref=ref, text=self._text)


class _FailingSource:
    name = "failing"

    def fetch(self, ref=None):
        raise ConnectionError("simulated network failure")


class TestTextHash:
    def test_deterministic(self):
        assert _text_hash("hello") == _text_hash("hello")

    def test_different_text_different_hash(self):
        assert _text_hash("hello") != _text_hash("world")

    def test_length(self):
        assert len(_text_hash("test")) == 16


class TestTranscriptDedup:
    def test_duplicate_text_skipped(self, tmp_db, tmp_dir):
        ing = TranscriptIngestor(
            conn=tmp_db,
            source=_FakeSource("The president mentioned NATO today."),
            matcher=PhraseMatcher(["nato"]),
            transcript_urls=["http://test"],
            raw_log_path=tmp_dir / "log.jsonl",
        )
        n1 = ing.run_once()
        n2 = ing.run_once()
        assert n1 == 1
        assert n2 == 0, "Duplicate text should be skipped"

        count = tmp_db.execute("SELECT COUNT(*) as c FROM transcripts").fetchone()["c"]
        assert count == 1

    def test_different_text_not_skipped(self, tmp_db, tmp_dir):
        source1 = _FakeSource("First batch about NATO.")
        ing1 = TranscriptIngestor(
            conn=tmp_db, source=source1, matcher=PhraseMatcher(["nato"]),
            transcript_urls=["http://test"], raw_log_path=tmp_dir / "log.jsonl",
        )
        assert ing1.run_once() == 1

        source2 = _FakeSource("Second batch about tariffs.")
        ing2 = TranscriptIngestor(
            conn=tmp_db, source=source2, matcher=PhraseMatcher(["tariff"]),
            transcript_urls=["http://test"], raw_log_path=tmp_dir / "log.jsonl",
        )
        assert ing2.run_once() == 1

        count = tmp_db.execute("SELECT COUNT(*) as c FROM transcripts").fetchone()["c"]
        assert count == 2

    def test_dedup_per_source_ref(self, tmp_db, tmp_dir):
        """Same text from different source_refs should both be stored."""
        text = "The economy is growing."
        ing = TranscriptIngestor(
            conn=tmp_db, source=_FakeSource(text), matcher=PhraseMatcher([]),
            transcript_urls=["http://source-a", "http://source-b"],
            raw_log_path=tmp_dir / "log.jsonl",
        )
        n = ing.run_once()
        assert n == 2

    def test_phrase_hits_not_duplicated(self, tmp_db, tmp_dir):
        ing = TranscriptIngestor(
            conn=tmp_db,
            source=_FakeSource("Discussion of NATO and tariffs."),
            matcher=PhraseMatcher(["nato", "tariffs"]),
            transcript_urls=["http://test"],
            raw_log_path=tmp_dir / "log.jsonl",
        )
        ing.run_once()
        ing.run_once()

        hits = tmp_db.execute("SELECT COUNT(*) as c FROM phrase_hits").fetchone()["c"]
        assert hits == 2, "Dedup should prevent double phrase hits"


class _GrowingSource:
    """Simulates a live caption stream that grows over time."""
    name = "growing"

    def __init__(self):
        self._chunks = []

    def add_chunk(self, text: str):
        self._chunks.append(text)

    def fetch(self, ref=None):
        full_text = " ".join(self._chunks)
        if not full_text:
            return None
        return TranscriptRecord(source="growing", source_ref=ref, text=full_text)


class TestIncrementalDiffing:
    def test_first_fetch_matches_everything(self, tmp_db, tmp_dir):
        ing = TranscriptIngestor(
            conn=tmp_db,
            source=_FakeSource("The president mentioned NATO today."),
            matcher=PhraseMatcher(["nato"]),
            transcript_urls=["http://test"],
            raw_log_path=tmp_dir / "log.jsonl",
        )
        ing.run_once()
        hits = tmp_db.execute("SELECT COUNT(*) as c FROM phrase_hits").fetchone()["c"]
        assert hits == 1

    def test_growing_text_only_matches_delta(self, tmp_db, tmp_dir):
        source = _GrowingSource()
        source.add_chunk("The economy is strong.")
        matcher = PhraseMatcher(["economy", "nato"])

        ing = TranscriptIngestor(
            conn=tmp_db, source=source, matcher=matcher,
            transcript_urls=["http://live"], raw_log_path=tmp_dir / "log.jsonl",
        )

        ing.run_once()
        hits_after_first = tmp_db.execute("SELECT COUNT(*) as c FROM phrase_hits").fetchone()["c"]
        assert hits_after_first == 1

        source.add_chunk("Now discussing NATO expansion.")
        ing.run_once()

        all_hits = tmp_db.execute("SELECT phrase FROM phrase_hits ORDER BY id").fetchall()
        phrases = [r["phrase"] for r in all_hits]
        assert phrases == ["economy", "nato"]

    def test_growing_text_does_not_rematch_old_phrases(self, tmp_db, tmp_dir):
        source = _GrowingSource()
        source.add_chunk("NATO is important.")
        matcher = PhraseMatcher(["nato"])

        ing = TranscriptIngestor(
            conn=tmp_db, source=source, matcher=matcher,
            transcript_urls=["http://live"], raw_log_path=tmp_dir / "log.jsonl",
        )

        ing.run_once()
        source.add_chunk("Still discussing NATO details.")
        ing.run_once()

        all_hits = tmp_db.execute("SELECT * FROM phrase_hits").fetchall()
        assert len(all_hits) == 2, (
            "First ingest finds NATO once, second ingest finds NATO in the delta only"
        )

    def test_completely_new_text_matches_everything(self, tmp_db, tmp_dir):
        """If the text doesn't start with the old text, match everything."""
        source1 = _FakeSource("The economy is strong.")
        ing = TranscriptIngestor(
            conn=tmp_db, source=source1, matcher=PhraseMatcher(["economy", "tariff"]),
            transcript_urls=["http://test"], raw_log_path=tmp_dir / "log.jsonl",
        )
        ing.run_once()

        source2 = _FakeSource("Tariff policy changed today.")
        ing2 = TranscriptIngestor(
            conn=tmp_db, source=source2, matcher=PhraseMatcher(["economy", "tariff"]),
            transcript_urls=["http://test"], raw_log_path=tmp_dir / "log.jsonl",
        )
        ing2.run_once()

        all_hits = tmp_db.execute("SELECT phrase FROM phrase_hits ORDER BY id").fetchall()
        phrases = [r["phrase"] for r in all_hits]
        assert "economy" in phrases
        assert "tariff" in phrases


class TestIngestorFailure:
    def test_fetch_failure_does_not_crash(self, tmp_db, tmp_dir):
        ing = TranscriptIngestor(
            conn=tmp_db, source=_FailingSource(), matcher=PhraseMatcher([]),
            transcript_urls=["http://test"], raw_log_path=tmp_dir / "log.jsonl",
        )
        n = ing.run_once()
        assert n == 0

    def test_none_source_returns_zero(self, tmp_db, tmp_dir):
        ing = TranscriptIngestor(
            conn=tmp_db, source=_FakeSource(None), matcher=PhraseMatcher([]),
            transcript_urls=["http://test"], raw_log_path=tmp_dir / "log.jsonl",
        )
        n = ing.run_once()
        assert n == 0

    def test_empty_urls_idles_cleanly(self, tmp_db, tmp_dir):
        ing = TranscriptIngestor(
            conn=tmp_db,
            source=_FakeSource("text", require_ref=True),
            matcher=PhraseMatcher([]),
            transcript_urls=[],
            raw_log_path=tmp_dir / "log.jsonl",
        )
        n = ing.run_once()
        assert n == 0
