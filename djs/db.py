"""SQLite access. One connection per thread; autocommit so every statement is atomic on its own."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def home() -> Path:
    p = Path(os.environ.get("DJS_HOME") or Path.home() / ".dear-job-seeker")
    p.mkdir(parents=True, exist_ok=True)
    return p


def profile_dir(provider: str, create: bool = False) -> Path:
    """Private per-provider directory: saved cookies (auth) and the browser profile (browser); mode 0700."""
    d = home() / "profiles" / provider
    if create:
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    return d


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    """Additive migration step: CREATE TABLE IF NOT EXISTS never adds columns to a table that already exists."""
    if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):  # another process added it between our check and the ALTER
                raise


# jobs columns added by schema v3 (the data contract), v4 (monthly salary, duplicate fingerprint) and v5 (the welfare
# tags); a fresh database gets them from schema.sql
JOBS_V3 = (("district", "TEXT"), ("salary_min", "INTEGER"), ("salary_max", "INTEGER"), ("salary_unit", "TEXT"),
           ("salary_months", "INTEGER"), ("education", "TEXT"), ("experience", "TEXT"), ("missing", "TEXT"),
           ("complete", "INTEGER NOT NULL DEFAULT 0"))
JOBS_V4 = (("salary_month_min", "INTEGER"), ("salary_month_max", "INTEGER"), ("fingerprint", "TEXT"))
JOBS_V5 = (("benefits", "TEXT"),)
VERSION = 5


def _backfill_jobs(conn) -> None:
    """Fill the derived columns of every stored job with the code the crawl uses (imported late: it imports this
    package)."""
    from djs import jobs, providers
    for (job_pk,) in conn.execute("SELECT id FROM jobs").fetchall():  # ids only: the raw payloads can be large
        row = conn.execute("SELECT provider, payload, detail FROM jobs WHERE id=?", (job_pk,)).fetchone()
        try:
            jobs.rederive(conn, job_pk, providers.get(row["provider"]), json.loads(row["payload"]),
                          json.loads(row["detail"]) if row["detail"] else None)
        except Exception:  # a platform since removed, a row without payload: judge it by what is stored
            jobs.refresh(conn, job_pk)


def _migrate_jobs(conn) -> None:
    """The derived columns of jobs (v3, v4 and v5) in one transaction, so a crash half way leaves the old version and
    the next start does it all again. The index on a column added here is created here too: schema.sql runs first, and
    an old database has no such column yet."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for column, ddl in (*JOBS_V3, *JOBS_V4, *JOBS_V5):
            _add_column_if_missing(conn, "jobs", column, ddl)
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint)")
        _backfill_jobs(conn)
        conn.execute(f"PRAGMA user_version = {VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _migrate(conn) -> None:
    """Bring a database written by an older version up to date. Gated on PRAGMA user_version (this runs on every
    connect) and idempotent, so two processes migrating at the same moment is harmless."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        # v1 added task_jobs: link each existing job to the one task that found it
        conn.execute("INSERT OR IGNORE INTO task_jobs(task_id, job_id) "
                     "SELECT task_id, id FROM jobs WHERE task_id IS NOT NULL")
        conn.execute("PRAGMA user_version = 1")
    if version < 2:
        _add_column_if_missing(conn, "provider_settings", "transport", "TEXT")  # v2: per-platform capture transport
        conn.execute("PRAGMA user_version = 2")
    if version < VERSION:
        # v3 columns (the data contract), v4 (monthly salary, fingerprint), v5 (welfare tags), derived for stored jobs
        _migrate_jobs(conn)


_init_lock = threading.Lock()
_initialized: set[str] = set()


def _init(conn) -> None:
    """WAL mode, schema and migrations. Switching to WAL needs an exclusive lock and fails at once with
    'database is locked' (the busy timeout does not cover it), so retry while another process holds it."""
    for attempt in range(50):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA.read_text())
            _migrate(conn)
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 49:
                raise
            time.sleep(0.1)


def connect() -> sqlite3.Connection:
    path = home() / "djs.db"
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL + schema once per process and file: twelve worker threads starting together used to race here and one
    # would die; the user_version check stays per connect (cheap) so a newer schema from another process is honoured
    with _init_lock:
        if str(path) not in _initialized:
            _init(conn)
            _initialized.add(str(path))
        else:
            _migrate(conn)
    return conn
