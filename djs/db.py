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


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    """Additive migration step: CREATE TABLE IF NOT EXISTS never adds columns to a table that already exists."""
    if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate(conn) -> None:
    """Bring a database written by an older version up to date. Gated on PRAGMA user_version (this runs on every
    connect) and idempotent, so two processes migrating at the same moment is harmless."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        # v1 added task_jobs: link each existing job to the one task that found it
        conn.execute("INSERT OR IGNORE INTO task_jobs(task_id, job_id) "
                     "SELECT task_id, id FROM jobs WHERE task_id IS NOT NULL")
        conn.execute("PRAGMA user_version = 1")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(home() / "djs.db", timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA.read_text())
    _migrate(conn)
    return conn
