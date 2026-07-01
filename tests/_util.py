"""Test helpers: point the Moot at a throwaway data dir per test."""
from __future__ import annotations

import tempfile
from pathlib import Path

from moot import config, db


def fresh_store() -> Path:
    d = Path(tempfile.mkdtemp(prefix="moottest-"))
    config.DATA_DIR = d
    config.DB_PATH = d / "moot.db"
    config.FILES_DIR = d / "archive"
    config.JOIN_CODE = None
    db.init_db()
    return d
