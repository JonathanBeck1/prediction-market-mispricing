from __future__ import annotations

from app.transcript_sources import FileTranscriptSource


class TestFileTranscriptSource:
    def test_reads_whole_file(self, tmp_dir):
        f = tmp_dir / "transcript.txt"
        f.write_text("hello world this is a test transcript")

        source = FileTranscriptSource(path=f)
        record = source.fetch()
        assert record is not None
        assert "hello world" in record.text
        assert record.source == "file"

    def test_returns_none_for_missing_file(self, tmp_dir):
        source = FileTranscriptSource(path=tmp_dir / "nope.txt")
        assert source.fetch() is None

    def test_returns_none_for_empty_file(self, tmp_dir):
        f = tmp_dir / "empty.txt"
        f.write_text("")

        source = FileTranscriptSource(path=f)
        assert source.fetch() is None

    def test_ref_overrides_path(self, tmp_dir):
        default_file = tmp_dir / "default.txt"
        default_file.write_text("default text")
        override_file = tmp_dir / "override.txt"
        override_file.write_text("override text")

        source = FileTranscriptSource(path=default_file)
        record = source.fetch(ref=str(override_file))
        assert record is not None
        assert "override text" in record.text

    def test_simulate_growth(self, tmp_dir):
        f = tmp_dir / "growing.txt"
        f.write_text("line one\nline two\nline three\n")

        source = FileTranscriptSource(path=f, simulate_growth=True)

        r1 = source.fetch()
        assert r1 is not None
        assert r1.text == "line one"

        r2 = source.fetch()
        assert r2 is not None
        assert "line two" in r2.text

        r3 = source.fetch()
        assert r3 is not None
        assert "line three" in r3.text

        # Further calls return full text
        r4 = source.fetch()
        assert r4 is not None
        assert "line three" in r4.text
