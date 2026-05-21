from __future__ import annotations

from pathlib import Path


def test_watchdog_script_preserves_runner_lock_inode():
    script = Path("scripts/watchdog.sh").read_text(encoding="utf-8")

    assert 'rm -f "$RUNNER_LOCK"' not in script
    assert "runner.paused" in script
