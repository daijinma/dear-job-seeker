"""SQLite access. One connection per thread; autocommit so every statement is atomic on its own."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def home() -> Path:
    p = Path(os.environ.get("DJS_HOME") or Path.home() / ".dear-job-seeker")
    p.mkdir(parents=True, exist_ok=True)
    return p


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(home() / "djs.db", timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA.read_text())
    return conn
