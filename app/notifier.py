"""WhatsApp notification via OpenClaw message send."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from app.card_formatter import format_card_text

logger = logging.getLogger(__name__)


@dataclass
class WhatsAppNotifier:
    target: str
    enabled: bool = False
    timeout_sec: float = 15

    def notify(self, card: dict[str, Any]) -> bool:
        """Send an action card to WhatsApp. Returns True on success."""
        if not self.enabled:
            return False
        if not self.target:
            logger.warning("WhatsApp target not configured; skipping notification")
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
        if not self.target:
            logger.warning("WhatsApp target not configured; skipping notification")
            return False
        return self._send(message)

    def _send(self, message: str) -> bool:
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
