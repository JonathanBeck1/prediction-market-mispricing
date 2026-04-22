"""Tests for OpenClaw Browser Relay source and WhatsApp notifier.

Uses subprocess mocking -- these tests don't require the openclaw CLI.
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
import subprocess

from app.transcript_sources import OpenClawTranscriptSource
from app.notifier import WhatsAppNotifier


def _mock_run_success(stdout="", **kwargs):
    result = MagicMock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


def _mock_run_fail(code=1, stderr="error"):
    result = MagicMock()
    result.returncode = code
    result.stdout = ""
    result.stderr = stderr
    return result


class TestOpenClawBrowserSource:
    def test_browser_pipeline_extracts_text(self):
        """Three-step pipeline: start -> open -> evaluate returns text."""
        call_count = {"n": 0}
        def mock_run(args, **kw):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return _mock_run_success()
            # Third call is evaluate, return JSON with text
            return _mock_run_success(stdout=json.dumps({"result": "The president talked about tariffs today."}))

        source = OpenClawTranscriptSource(browser_profile="test")
        with patch.dict("os.environ", {"OPENCLAW_SCRAPE_CMD": ""}, clear=False):
            with patch("app.transcript_sources.subprocess.run", side_effect=mock_run):
                record = source.fetch("https://example.com/transcript")

        assert record is not None
        assert "tariffs" in record.text
        assert record.source == "openclaw"
        assert call_count["n"] == 3

    def test_browser_start_failure_returns_none(self):
        source = OpenClawTranscriptSource()
        with patch.dict("os.environ", {"OPENCLAW_SCRAPE_CMD": ""}, clear=False):
            with patch("app.transcript_sources.subprocess.run", return_value=_mock_run_fail()):
                record = source.fetch("https://example.com")
        assert record is None

    def test_evaluate_empty_text_returns_none(self):
        call_count = {"n": 0}
        def mock_run(args, **kw):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return _mock_run_success()
            return _mock_run_success(stdout=json.dumps({"result": ""}))

        source = OpenClawTranscriptSource()
        with patch.dict("os.environ", {"OPENCLAW_SCRAPE_CMD": ""}, clear=False):
            with patch("app.transcript_sources.subprocess.run", side_effect=mock_run):
                record = source.fetch("https://example.com")
        assert record is None

    def test_no_ref_returns_none(self):
        source = OpenClawTranscriptSource()
        with patch.dict("os.environ", {"OPENCLAW_SCRAPE_CMD": ""}, clear=False):
            record = source.fetch(None)
        assert record is None

    def test_legacy_cmd_still_works(self):
        source = OpenClawTranscriptSource()
        with patch.dict("os.environ", {"OPENCLAW_SCRAPE_CMD": "echo hello"}, clear=False):
            with patch("app.transcript_sources.subprocess.run",
                       return_value=_mock_run_success(stdout="hello world transcript")):
                record = source.fetch("https://example.com")
        assert record is not None
        assert "hello world" in record.text

    def test_cli_not_found_returns_none(self):
        source = OpenClawTranscriptSource()
        with patch.dict("os.environ", {"OPENCLAW_SCRAPE_CMD": ""}, clear=False):
            with patch("app.transcript_sources.subprocess.run",
                       side_effect=FileNotFoundError("openclaw")):
                record = source.fetch("https://example.com")
        assert record is None

    def test_timeout_returns_none(self):
        source = OpenClawTranscriptSource(timeout_sec=5)
        with patch.dict("os.environ", {"OPENCLAW_SCRAPE_CMD": ""}, clear=False):
            with patch("app.transcript_sources.subprocess.run",
                       side_effect=subprocess.TimeoutExpired("cmd", 5)):
                record = source.fetch("https://example.com")
        assert record is None

    def test_parse_evaluate_plain_string(self):
        assert OpenClawTranscriptSource._parse_evaluate_result('"just text"') == "just text"

    def test_parse_evaluate_json_result(self):
        raw = json.dumps({"result": "extracted text here"})
        assert OpenClawTranscriptSource._parse_evaluate_result(raw) == "extracted text here"

    def test_parse_evaluate_raw_fallback(self):
        assert OpenClawTranscriptSource._parse_evaluate_result("not json at all") == "not json at all"


class TestWhatsAppNotifier:
    def test_send_on_buy_yes(self):
        notifier = WhatsAppNotifier(target="+1234567890", enabled=True)
        card = {"side": "BUY_YES", "market_id": "MKT-1", "phrase": "test",
                "subject": "trump", "p_literal": 0.85, "ev_yes": 0.20, "ev_no": -0.10,
                "exec_price_hint": "Buy YES at <= 0.65", "size_cap": 500,
                "scores": {"p_literal": 0.85}, "event": {}, "reason_codes": ["PRE_EVENT"],
                "time_remaining_sec": None}

        with patch("app.notifier.subprocess.run", return_value=_mock_run_success()) as mock:
            result = notifier.notify(card)
        assert result is True
        mock.assert_called_once()
        call_args = mock.call_args[0][0]
        assert "openclaw" in call_args
        assert "message" in call_args
        assert "send" in call_args
        assert "--channel" in call_args
        assert "whatsapp" in call_args

    def test_skip_watch_cards(self):
        notifier = WhatsAppNotifier(target="+1234567890", enabled=True)
        card = {"side": "WATCH"}
        with patch("app.notifier.subprocess.run") as mock:
            result = notifier.notify(card)
        assert result is False
        mock.assert_not_called()

    def test_disabled_does_nothing(self):
        notifier = WhatsAppNotifier(target="+1234567890", enabled=False)
        card = {"side": "BUY_YES"}
        with patch("app.notifier.subprocess.run") as mock:
            result = notifier.notify(card)
        assert result is False
        mock.assert_not_called()

    def test_no_target_returns_false(self):
        notifier = WhatsAppNotifier(target="", enabled=True)
        card = {"side": "BUY_YES"}
        result = notifier.notify(card)
        assert result is False

    def test_send_failure_returns_false(self):
        notifier = WhatsAppNotifier(target="+1234567890", enabled=True)
        card = {"side": "BUY_YES", "market_id": "MKT-1", "phrase": "test",
                "subject": "trump", "p_literal": 0.85, "ev_yes": 0.20, "ev_no": -0.10,
                "exec_price_hint": "Buy YES at <= 0.65", "size_cap": 500,
                "scores": {"p_literal": 0.85}, "event": {}, "reason_codes": [],
                "time_remaining_sec": None}

        with patch("app.notifier.subprocess.run", return_value=_mock_run_fail()):
            result = notifier.notify(card)
        assert result is False

    def test_cli_not_found_returns_false(self):
        notifier = WhatsAppNotifier(target="+1234567890", enabled=True)
        card = {"side": "BUY_NO", "market_id": "MKT-1", "phrase": "test",
                "subject": "trump", "p_literal": 0.05, "ev_yes": -0.40, "ev_no": 0.50,
                "exec_price_hint": "Buy NO at <= 0.45", "size_cap": 300,
                "scores": {"p_literal": 0.05}, "event": {}, "reason_codes": [],
                "time_remaining_sec": None}

        with patch("app.notifier.subprocess.run", side_effect=FileNotFoundError("openclaw")):
            result = notifier.notify(card)
        assert result is False
