"""Tests for scripts/rev_transcript_cleaner.py

Covers:
- Inline-strong Rev format (classic)
- Structured-segment Rev format (newer)
- Text cleaning: timestamps, brackets, speaker labels
- Speaker filtering: only target speaker's turns extracted
- Date inference from URL slug and page title
- Event-type inference from URL slug
- File naming: auto-increment, no overwrite
- --stdout mode (no file written)
- Batch --urls-file parsing helper
"""
from __future__ import annotations

import os
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make sure the project root is on the path
PROJ_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJ_ROOT))

from scripts.rev_transcript_cleaner import (
    ParseResult,
    SpeakerTurn,
    _load_urls_file,
    choose_output_path,
    clean_text,
    filter_and_join,
    infer_date_from_page,
    infer_date_from_slug,
    infer_event_type,
    parse_html,
    process_source,
    write_exclusive,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── Helpers ────────────────────────────────────────────────────────────────────
def _inline_html(content: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>Trump Rally February 15 2026</title></head>
<body><div class="fl-callout-text">{content}</div></body></html>"""


def _structured_html(segments: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>Trump Signing March 16 2026</title></head>
<body><div class="EditorContainer">{segments}</div></body></html>"""


# ── Fixture-based tests ────────────────────────────────────────────────────────
class TestFixtureInlineFormat(unittest.TestCase):
    """Tests against the real fixture HTML file (inline-strong format)."""

    def setUp(self) -> None:
        html_path = FIXTURES / "rev_sample_inline.html"
        self.html = html_path.read_text(encoding="utf-8")
        self.result = parse_html(self.html)

    def test_detects_trump_speaker(self) -> None:
        trump_labels = {"donald trump", "president trump"}
        detected = set(self.result.all_speakers)
        self.assertTrue(
            trump_labels & detected,
            f"Expected Trump speaker label in {detected}",
        )

    def test_excludes_non_trump_speakers(self) -> None:
        # Crowd and Reporter are in the fixture
        trump_norm = {"donald trump", "president trump", "trump", "president donald trump"}
        output = filter_and_join(self.result.turns, trump_norm)
        self.assertNotIn("Reporter", output)
        self.assertNotIn("Crowd", output)

    def test_contains_trump_words(self) -> None:
        trump_norm = {"donald trump", "president trump", "trump", "president donald trump"}
        output = filter_and_join(self.result.turns, trump_norm)
        self.assertIn("drill, baby, drill", output.lower())
        self.assertIn("record numbers", output.lower())

    def test_timestamps_removed(self) -> None:
        trump_norm = {"donald trump", "president trump", "trump", "president donald trump"}
        output = filter_and_join(self.result.turns, trump_norm)
        # No (00:00) style timestamps
        import re
        self.assertIsNone(re.search(r"\(\s*\d+:\d+\s*\)", output), "Timestamp found in output")

    def test_bracket_artifacts_removed(self) -> None:
        trump_norm = {"donald trump", "president trump", "trump", "president donald trump"}
        output = filter_and_join(self.result.turns, trump_norm)
        self.assertNotIn("[applause]", output)
        self.assertNotIn("[inaudible]", output)
        self.assertNotIn("[crosstalk]", output)


class TestFixtureStructuredFormat(unittest.TestCase):
    """Tests against the real fixture HTML file (structured-segment format)."""

    def setUp(self) -> None:
        html_path = FIXTURES / "rev_sample_structured.html"
        self.html = html_path.read_text(encoding="utf-8")
        self.result = parse_html(self.html)

    def test_detects_trump_speaker(self) -> None:
        detected = set(self.result.all_speakers)
        self.assertIn("donald trump", detected)

    def test_excludes_reporter(self) -> None:
        trump_norm = {"donald trump"}
        output = filter_and_join(self.result.turns, trump_norm)
        self.assertNotIn("Next question", output)

    def test_contains_trump_energy_text(self) -> None:
        trump_norm = {"donald trump"}
        output = filter_and_join(self.result.turns, trump_norm)
        self.assertIn("energy", output.lower())
        self.assertIn("border security", output.lower())


# ── Unit tests: text cleaning ──────────────────────────────────────────────────
class TestCleanText(unittest.TestCase):

    def test_removes_inline_timestamp(self) -> None:
        self.assertNotIn("(00:00)", clean_text("Hello (00:00) world"))

    def test_removes_long_timestamp(self) -> None:
        self.assertNotIn("(1:23:45)", clean_text("Text (1:23:45) continues"))

    def test_removes_bracket_applause(self) -> None:
        self.assertNotIn("[applause]", clean_text("Thank you [applause] so much"))

    def test_removes_bracket_inaudible(self) -> None:
        self.assertNotIn("[inaudible", clean_text("We will [inaudible 3 words] do it"))

    def test_removes_bracket_crosstalk(self) -> None:
        self.assertNotIn("[crosstalk]", clean_text("Yes, but [crosstalk] we need"))

    def test_preserves_paragraph_breaks(self) -> None:
        text = "First paragraph.\n\nSecond paragraph."
        result = clean_text(text)
        self.assertIn("\n\n", result)

    def test_collapses_excess_blank_lines(self) -> None:
        text = "Para one.\n\n\n\n\nPara two."
        result = clean_text(text)
        self.assertNotIn("\n\n\n", result)

    def test_strips_leading_whitespace(self) -> None:
        result = clean_text("   hello world   ")
        self.assertEqual(result, "hello world")


# ── Unit tests: date inference ─────────────────────────────────────────────────
class TestDateInference(unittest.TestCase):

    def test_iso_date_in_slug(self) -> None:
        self.assertEqual(infer_date_from_slug("trump-rally-2026-02-15"), "2026-02-15")

    def test_mdy2_date_in_slug(self) -> None:
        self.assertEqual(infer_date_from_slug("executive-orders-3-16-26"), "2026-03-16")

    def test_mdy4_date_in_slug(self) -> None:
        self.assertEqual(infer_date_from_slug("trump-address-2-15-2026"), "2026-02-15")

    def test_no_date_in_slug(self) -> None:
        self.assertIsNone(infer_date_from_slug("trump-rally-speech"))

    def test_date_from_page_title(self) -> None:
        from bs4 import BeautifulSoup
        html = "<html><head><title>Trump Rally February 15, 2026 | Rev</title></head></html>"
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(infer_date_from_page(soup), "2026-02-15")

    def test_date_from_og_title(self) -> None:
        from bs4 import BeautifulSoup
        html = '<html><head><meta property="og:title" content="Trump Speech March 16 2026"/></head></html>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(infer_date_from_page(soup), "2026-03-16")


# ── Unit tests: event type inference ──────────────────────────────────────────
class TestEventTypeInference(unittest.TestCase):

    def test_rally(self) -> None:
        self.assertEqual(infer_event_type("trump-rally-iowa"), "rally")

    def test_signing(self) -> None:
        self.assertEqual(infer_event_type("executive-orders-signing"), "signing")

    def test_briefing(self) -> None:
        self.assertEqual(infer_event_type("press-briefing-jan-5"), "briefing")

    def test_interview(self) -> None:
        self.assertEqual(infer_event_type("fox-news-interview"), "interview")

    def test_address(self) -> None:
        self.assertEqual(infer_event_type("state-of-the-union-address"), "address")

    def test_presser(self) -> None:
        self.assertEqual(infer_event_type("news-conference-nato"), "presser")

    def test_summit(self) -> None:
        self.assertEqual(infer_event_type("g7-summit-remarks"), "summit")

    def test_fallback_general(self) -> None:
        self.assertEqual(infer_event_type("some-unknown-slug"), "general")

    def test_title_hint(self) -> None:
        self.assertEqual(infer_event_type("unknown", "Trump Rally Transcript"), "rally")


# ── Unit tests: file naming ────────────────────────────────────────────────────
class TestChooseOutputPath(unittest.TestCase):

    def test_creates_first_seq(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.rev_transcript_cleaner.CORPUS_ROOT", Path(tmp)):
                path = choose_output_path("trump", "rally", "2026-02-15")
                self.assertEqual(path.name, "rally_2026-02-15_01.txt")

    def test_auto_increments(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.rev_transcript_cleaner.CORPUS_ROOT", Path(tmp)):
                # Create first file
                p1 = choose_output_path("trump", "rally", "2026-02-15")
                p1.parent.mkdir(parents=True, exist_ok=True)
                p1.write_text("first", encoding="utf-8")
                # Second should be _02
                p2 = choose_output_path("trump", "rally", "2026-02-15")
                self.assertEqual(p2.name, "rally_2026-02-15_02.txt")

    def test_write_exclusive_refuses_overwrite(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            write_exclusive(path, "hello")
            with self.assertRaises(SystemExit):
                write_exclusive(path, "world")


# ── Integration tests: process_source with fixture HTML ───────────────────────
class TestProcessSourceStdout(unittest.TestCase):
    """End-to-end test using the inline fixture and --stdout mode."""

    def test_inline_fixture_stdout(self) -> None:
        html = (FIXTURES / "rev_sample_inline.html").read_text(encoding="utf-8")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = process_source(
                html=html,
                url="https://www.rev.com/transcripts/trump-rally-2-15-26",
                target_speakers=["donald trump", "president trump", "trump", "president donald trump"],
                corpus_speaker="trump",
                event_type_override=None,
                date_override=None,
                stdout_mode=True,
            )
        self.assertTrue(ok)
        output = buf.getvalue()
        self.assertIn("drill, baby, drill", output.lower())
        self.assertNotIn("[applause]", output)
        self.assertNotIn("(00:", output)

    def test_structured_fixture_stdout(self) -> None:
        html = (FIXTURES / "rev_sample_structured.html").read_text(encoding="utf-8")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = process_source(
                html=html,
                url=None,
                target_speakers=["donald trump"],
                corpus_speaker="trump",
                event_type_override="signing",
                date_override="2026-03-16",
                stdout_mode=True,
            )
        self.assertTrue(ok)
        output = buf.getvalue()
        self.assertIn("energy", output.lower())
        self.assertNotIn("[applause]", output)

    def test_no_target_speaker_returns_false(self) -> None:
        html = (FIXTURES / "rev_sample_inline.html").read_text(encoding="utf-8")
        import io, sys
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            ok = process_source(
                html=html,
                url=None,
                target_speakers=["nobody special xyz"],
                corpus_speaker="trump",
                event_type_override="rally",
                date_override="2026-02-15",
                stdout_mode=True,
            )
        self.assertFalse(ok)


# ── Unit tests: urls-file loader ───────────────────────────────────────────────
class TestLoadUrlsFile(unittest.TestCase):

    def test_strips_blanks_and_comments(self) -> None:
        import tempfile
        content = textwrap.dedent("""\
            # Comment
            https://www.rev.com/transcripts/url-one
            
            # Another comment
            https://www.rev.com/transcripts/url-two
        """)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            tmp = f.name
        try:
            urls = _load_urls_file(tmp)
            self.assertEqual(len(urls), 2)
            self.assertEqual(urls[0], "https://www.rev.com/transcripts/url-one")
        finally:
            os.unlink(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
