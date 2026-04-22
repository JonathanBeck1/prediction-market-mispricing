from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptRecord:
    source: str
    source_ref: str | None
    text: str


class TranscriptSource(Protocol):
    name: str

    def fetch(self, ref: str | None = None) -> TranscriptRecord | None:
        """Fetch transcript text from a reference (URL or other identifier)."""


@dataclass
class DirectHTTPTranscriptSource:
    timeout_sec: float = 15
    user_agent: str = "kalshi-mention-edge/1.0"
    name: str = "directhttp"

    def fetch(self, ref: str | None = None) -> TranscriptRecord | None:
        if not ref:
            return None
        headers = {"User-Agent": self.user_agent}
        with httpx.Client(timeout=self.timeout_sec, headers=headers, follow_redirects=True) as client:
            resp = client.get(ref)
            resp.raise_for_status()
            text = resp.text.strip()
            if not text:
                return None
            return TranscriptRecord(source=self.name, source_ref=ref, text=text)


@dataclass
class OpenClawTranscriptSource:
    """Fetches transcript text via OpenClaw Browser Relay.

    Uses the 3-step pipeline:
      1. openclaw browser start (ensure browser session)
      2. openclaw browser open <url>
      3. openclaw browser evaluate --fn "() => document.body.innerText" --json

    Falls back to OPENCLAW_SCRAPE_CMD if set (legacy single-command mode).
    """

    timeout_sec: float = 30
    browser_profile: str = "openclaw"
    name: str = "openclaw"
    _extract_js: str = "() => document.body.innerText"

    def fetch(self, ref: str | None = None) -> TranscriptRecord | None:
        # Legacy mode: if OPENCLAW_SCRAPE_CMD is set, use that directly
        cmd_template = os.getenv("OPENCLAW_SCRAPE_CMD", "").strip()
        if cmd_template:
            return self._fetch_legacy(cmd_template, ref)

        if not ref:
            return None

        profile = os.getenv("OPENCLAW_BROWSER_PROFILE", self.browser_profile)

        if not self._run_cmd(["openclaw", "browser", "--browser-profile", profile, "start"]):
            return None

        if not self._run_cmd(["openclaw", "browser", "--browser-profile", profile, "open", ref]):
            return None

        result = self._run_cmd_output(
            ["openclaw", "browser", "--browser-profile", profile,
             "evaluate", "--fn", self._extract_js, "--json"]
        )
        if result is None:
            return None

        text = self._parse_evaluate_result(result)
        if not text:
            logger.warning("OpenClaw evaluate returned empty text for %s", ref)
            return None

        return TranscriptRecord(source=self.name, source_ref=ref, text=text)

    def _fetch_legacy(self, cmd_template: str, ref: str | None) -> TranscriptRecord | None:
        fmt_ref = ref or ""
        try:
            command = cmd_template.format(url=fmt_ref)
        except Exception as exc:
            logger.warning("Invalid OPENCLAW_SCRAPE_CMD template: %s", exc)
            return None

        try:
            args = shlex.split(command)
            completed = subprocess.run(
                args, capture_output=True, text=True,
                timeout=self.timeout_sec, check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("OpenClaw legacy command timed out after %ss", self.timeout_sec)
            return None
        except FileNotFoundError as exc:
            logger.warning("OpenClaw legacy command not found: %s", exc)
            return None

        if completed.returncode != 0:
            logger.warning("OpenClaw legacy command failed code=%s: %s",
                           completed.returncode, completed.stderr.strip())
            return None

        text = completed.stdout.strip()
        if not text:
            return None
        return TranscriptRecord(source=self.name, source_ref=fmt_ref or None, text=text)

    def _run_cmd(self, args: list[str]) -> bool:
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True,
                timeout=self.timeout_sec, check=False,
            )
            if completed.returncode != 0:
                logger.warning("OpenClaw cmd failed %s: %s", args[:4], completed.stderr.strip()[:200])
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning("OpenClaw cmd timed out: %s", args[:4])
            return False
        except FileNotFoundError:
            logger.warning("openclaw CLI not found in PATH")
            return False

    def _run_cmd_output(self, args: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True,
                timeout=self.timeout_sec, check=False,
            )
            if completed.returncode != 0:
                logger.warning("OpenClaw cmd failed %s: %s", args[:4], completed.stderr.strip()[:200])
                return None
            return completed.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.warning("OpenClaw cmd timed out: %s", args[:4])
            return None
        except FileNotFoundError:
            logger.warning("openclaw CLI not found in PATH")
            return None

    @staticmethod
    def _parse_evaluate_result(raw: str) -> str:
        """Parse JSON output from openclaw browser evaluate --json."""
        import json
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip()

        if isinstance(data, str):
            return data.strip()
        if isinstance(data, dict):
            # evaluate returns {"result": "..."} or {"value": "..."}
            for key in ("result", "value", "data", "text"):
                if key in data and isinstance(data[key], str):
                    return data[key].strip()
        return raw.strip()


@dataclass
class FileTranscriptSource:
    """Reads transcript text from a local file. Useful for testing and replay.

    If the file doesn't exist or is empty, returns None.
    Simulates a growing stream when `simulate_growth` is true:
    each call returns one more line than the previous call.
    """

    path: Path | None = None
    name: str = "file"
    simulate_growth: bool = False
    _lines_served: int = field(default=0, repr=False)

    def fetch(self, ref: str | None = None) -> TranscriptRecord | None:
        file_path = Path(ref) if ref else self.path
        if file_path is None or not file_path.exists():
            return None

        text = file_path.read_text(encoding="utf-8").strip()
        if not text:
            return None

        if self.simulate_growth:
            lines = text.splitlines(keepends=True)
            self._lines_served = min(self._lines_served + 1, len(lines))
            text = "".join(lines[: self._lines_served]).strip()
            if not text:
                return None

        return TranscriptRecord(
            source=self.name, source_ref=str(file_path), text=text
        )


@dataclass
class FallbackTranscriptSource:
    """Tries each source in order; returns the first successful result.

    Logs which stage succeeded or if all stages failed.
    """

    sources: list[TranscriptSource] = field(default_factory=list)
    name: str = "fallback"

    def fetch(self, ref: str | None = None) -> TranscriptRecord | None:
        last_error: Exception | None = None
        for source in self.sources:
            try:
                result = source.fetch(ref)
                if result is not None:
                    logger.debug(
                        "Fallback hit on stage=%s ref=%s", source.name, ref
                    )
                    return result
                logger.debug(
                    "Fallback stage=%s returned None ref=%s", source.name, ref
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Fallback stage=%s failed ref=%s: %s", source.name, ref, exc
                )
                continue

        if last_error:
            logger.warning(
                "All fallback stages exhausted ref=%s last_error=%s", ref, last_error
            )
        else:
            logger.debug("All fallback stages returned None ref=%s", ref)
        return None

