from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.db import init_db


@pytest.fixture()
def tmp_db():
    """Yield a fresh SQLite connection with full schema, cleaned up after test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = init_db(db_path)
        yield conn
        conn.close()


@pytest.fixture()
def tmp_dir():
    """Yield a temporary directory path, cleaned up after test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)
