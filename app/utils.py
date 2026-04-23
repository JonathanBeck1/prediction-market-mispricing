from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write text to `path`.

    Writes to a sibling temp file first, then uses os.replace() which is
    atomic on POSIX (guaranteed by rename(2)) and Windows (MoveFileEx with
    MOVEFILE_REPLACE_EXISTING). Concurrent readers either see the old
    file contents or the new file contents — never a partial write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except Exception:
        # Clean up the temp file on failure; don't leave dangling .tmp files
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def atomic_write_json(path: Path, data: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    """Atomically write a JSON-serializable object to `path`. See atomic_write_text."""
    atomic_write_text(path, json.dumps(data, indent=indent, ensure_ascii=ensure_ascii))

