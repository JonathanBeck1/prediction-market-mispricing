from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from app.notifier import WhatsAppNotifier

logger = logging.getLogger(__name__)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    raw = str(ts).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Watchdog:
    conn: sqlite3.Connection
    notifier: WhatsAppNotifier | None = None
    max_snapshot_age_sec: float = 300.0
    max_scorer_idle_sec: float = 300.0
    startup_grace_sec: float = 180.0
    consecutive_breaches_to_restart: int = 3
    exit_on_stale: bool = True
    _restart: Callable[[str], None] | None = None
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _consecutive_breaches: int = field(default=0, repr=False)
    _alert_sent_for_current_breach: bool = field(default=False, repr=False)

    def run_once(self) -> None:
        # Give the loops time to warm up after process start/restart.
        if (time.monotonic() - self._started_monotonic) < self.startup_grace_sec:
            return

        now_dt = datetime.now(tz=timezone.utc)
        snap_ts = self.conn.execute("SELECT MAX(ts) FROM market_snapshots").fetchone()[0]
        card_ts = self.conn.execute("SELECT MAX(ts) FROM action_cards").fetchone()[0]
        snap_dt = _parse_ts(snap_ts)
        card_dt = _parse_ts(card_ts)

        reasons: list[str] = []
        snap_age = None
        card_age = None
        if snap_dt is None:
            reasons.append("no market snapshots")
        else:
            snap_age = (now_dt - snap_dt).total_seconds()
            if snap_age > self.max_snapshot_age_sec:
                reasons.append(f"snapshot age {int(snap_age)}s > {int(self.max_snapshot_age_sec)}s")

        if card_dt is None:
            reasons.append("no action cards")
        else:
            card_age = (now_dt - card_dt).total_seconds()
            if card_age > self.max_scorer_idle_sec:
                reasons.append(f"scorer idle {int(card_age)}s > {int(self.max_scorer_idle_sec)}s")

        if not reasons:
            if self._consecutive_breaches > 0:
                logger.info("Watchdog recovered after %d breach(es).", self._consecutive_breaches)
            self._consecutive_breaches = 0
            self._alert_sent_for_current_breach = False
            return

        self._consecutive_breaches += 1
        msg = (
            f"Watchdog breach {self._consecutive_breaches}/{self.consecutive_breaches_to_restart}: "
            + "; ".join(reasons)
        )
        logger.warning(msg)

        if not self._alert_sent_for_current_breach and self.notifier and self.notifier.enabled:
            self.notifier.notify_text(f"[kalshi-edge watchdog] {msg}")
            self._alert_sent_for_current_breach = True

        if self._consecutive_breaches < self.consecutive_breaches_to_restart:
            return
        logger.error("Watchdog breach threshold reached.")
        if self.notifier and self.notifier.enabled:
            self.notifier.notify_text("[kalshi-edge watchdog] Restarting runner after repeated stale-data breaches.")
        if self.exit_on_stale:
            self._do_restart("watchdog stale data")

    def _do_restart(self, reason: str) -> None:
        if self._restart:
            self._restart(reason)
            return
        logger.error("Exiting process for auto-restart: %s", reason)
        os._exit(1)
