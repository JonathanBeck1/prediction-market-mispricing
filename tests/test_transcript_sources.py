from __future__ import annotations

from app.transcript_sources import FallbackTranscriptSource, TranscriptRecord


class _OkSource:
    name = "ok"

    def __init__(self, text: str):
        self._text = text

    def fetch(self, ref=None):
        return TranscriptRecord(source=self.name, source_ref=ref, text=self._text)


class _NoneSource:
    name = "none"

    def fetch(self, ref=None):
        return None


class _ErrorSource:
    name = "error"

    def fetch(self, ref=None):
        raise ConnectionError("boom")


class TestFallbackTranscriptSource:
    def test_returns_first_success(self):
        fb = FallbackTranscriptSource(
            sources=[_OkSource("first"), _OkSource("second")]
        )
        result = fb.fetch("http://test")
        assert result is not None
        assert result.text == "first"

    def test_skips_none_sources(self):
        fb = FallbackTranscriptSource(
            sources=[_NoneSource(), _OkSource("fallback")]
        )
        result = fb.fetch("http://test")
        assert result is not None
        assert result.text == "fallback"

    def test_skips_erroring_sources(self):
        fb = FallbackTranscriptSource(
            sources=[_ErrorSource(), _OkSource("survived")]
        )
        result = fb.fetch("http://test")
        assert result is not None
        assert result.text == "survived"

    def test_all_fail_returns_none(self):
        fb = FallbackTranscriptSource(
            sources=[_NoneSource(), _ErrorSource()]
        )
        result = fb.fetch("http://test")
        assert result is None

    def test_empty_sources_returns_none(self):
        fb = FallbackTranscriptSource(sources=[])
        assert fb.fetch("http://test") is None
