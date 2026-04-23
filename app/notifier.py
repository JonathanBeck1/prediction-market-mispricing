"""WhatsApp notification via OpenClaw message send."""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from app.card_formatter import format_card_text

logger = logging.getLogger(__name__)

# WhatsApp target must be either:
#   - an international phone number (optional + prefix, 7-15 digits)
#   - an OpenClaw chat/group ID (alphanumeric + _ - . @, max 64 chars)
# Reject anything with leading -- to prevent passing unintended flags to
# `openclaw message send` via subprocess.
_TARGET_RE = re.compile(r'^(\+?[0-9]{7,15}|[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,63})$')


def _valid_target(target: str) -> bool:
    if not target:
        return False
    if target.startswith("-"):
        # argparse-style flag injection
        return False
    return bool(_TARGET_RE.match(target))


@dataclass
class WhatsAppNotifier:
    target: str
    enabled: bool = False
    timeout_sec: float = 15

    def notify(self, card: dict[str, Any]) -> bool:
        """Send an action card to WhatsApp. Returns True on success."""
        if not self.enabled:
            return False
        if not _valid_target(self.target):
            logger.warning(
                "WhatsApp target missing or invalid; skipping. "
                "Set WHATSAPP_TARGET to a phone number (e.g. +15551234567) or chat ID."
            )
            return False

        side = card.get("side", "WATCH")
        if side == "WATCH":
            return False

        message = format_card_text(card)
        return self._send(message)

    def notify_text(self, message: str) -> bool:
        """Send plain text alert to WhatsApp. Returns True on success."""
        if not self.enabled:
            return False
        if not _valid_target(self.target):
            logger.warning(
                "WhatsApp target missing or invalid; skipping. "
                "Set WHATSAPP_TARGET to a phone number or chat ID."
            )
            return False
        return self._send(message)

    def _send(self, message: str) -> bool:
        # Belt-and-suspenders: re-validate inside _send in case someone
        # calls it directly in the future.
        if not _valid_target(self.target):
            logger.warning("WhatsApp _send called with invalid target; aborting")
            return False
        args = [
            "openclaw", "message", "send",
            "--channel", "whatsapp",
            "--target", self.target,
            "--message", message,
        ]
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True,
                timeout=self.timeout_sec, check=False,
            )
            if completed.returncode != 0:
                logger.warning(
                    "WhatsApp send failed code=%s: %s",
                    completed.returncode, completed.stderr.strip()[:200],
                )
                return False
            logger.info("WhatsApp notification sent to %s", self.target)
            return True
        except subprocess.TimeoutExpired:
            logger.warning("WhatsApp send timed out after %ss", self.timeout_sec)
            return False
        except FileNotFoundError:
            logger.warning("openclaw CLI not found in PATH; cannot send WhatsApp")
            return False
