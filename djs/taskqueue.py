"""Task queue on the tasks table. Every transition is a single UPDATE (atomic)."""
from __future__ import annotations

import json
import random
import time

from djs import providers

LOCK_TTL = 15 * 60
MAX_ATTEMPTS = 8
BACKOFF_CAP = 360


def create(conn, provider: str, payload: dict) -> int:
    prov = providers.get(provider)  # KeyError for unknown provider
    need = "keyword" if prov.kind == "keyword" else "tenant"
    if not payload.get(need):
        raise ValueError(f"{provider}: payload needs {need!r}")
    if prov.requires_city and not payload.get("city"):
        raise ValueError(f"{provider}: city is required")
    mp = payload.get("max_pages")
    if mp is not None and (not isinstance(mp, int) or isinstance(mp, bool) or mp < 1):
        raise ValueError("max_pages must be an integer >= 1")
    cur = conn.execute("INSERT INTO tasks(provider, kind, payload) VALUES (?,?,?)",
                       (provider, prov.kind, json.dumps(payload, ensure_ascii=False)))
    return cur.lastrowid


def claim(conn):
    now = time.time()
    return conn.execute(
        """UPDATE tasks SET status='running', locked_at=? WHERE id=(
             SELECT id FROM tasks
             WHERE (status='pending' AND next_run_at<=?) OR (status='running' AND locked_at<?)
             ORDER BY next_run_at, id LIMIT 1) RETURNING *""",
        (now, now, now - LOCK_TTL)).fetchone()


def _set(conn, task_id: int, status: str, error=None, stats=None, delay: float = 0.0, attempts_inc: int = 0):
    conn.execute(
        "UPDATE tasks SET status=?, last_error=?, stats=COALESCE(?, stats), next_run_at=?,"
        " attempts=attempts+?, locked_at=NULL WHERE id=?",
        (status, error, json.dumps(stats) if stats else None, time.time() + delay, attempts_inc, task_id))


def done(conn, task_id: int, stats: dict):
    _set(conn, task_id, "done", stats=stats)


def dead(conn, task_id: int, error: str):
    _set(conn, task_id, "dead", error)


def waiting_login(conn, task_id: int):
    _set(conn, task_id, "waiting_login", "login required")


def release(conn, task_id: int, error: str, delay: float):
    """Back to pending without burning an attempt (e.g. no egress available)."""
    _set(conn, task_id, "pending", error, delay=delay)


def retry(conn, task_id: int, error: str, retry_after: float | None = None):
    attempts = conn.execute("SELECT attempts FROM tasks WHERE id=?", (task_id,)).fetchone()["attempts"]
    if attempts + 1 >= MAX_ATTEMPTS:
        return _set(conn, task_id, "dead", error, attempts_inc=1)
    backoff = min(2 ** (attempts + 1), BACKOFF_CAP)
    delay = max(backoff * (1 + random.random() * 0.25), retry_after or 0)
    _set(conn, task_id, "pending", error, delay=delay, attempts_inc=1)


def resume_login(conn, provider: str) -> int:
    return conn.execute("UPDATE tasks SET status='pending', next_run_at=0 WHERE provider=? AND status='waiting_login'",
                        (provider,)).rowcount


def as_dict(row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["stats"] = json.loads(d["stats"]) if d["stats"] else None
    return d


def list_(conn, status: str | None = None, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM tasks WHERE (? IS NULL OR status=?) ORDER BY id DESC LIMIT ?",
                        (status, status, limit)).fetchall()
    return [as_dict(r) for r in rows]
