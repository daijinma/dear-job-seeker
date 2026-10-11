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
    prov.city_code(payload.get("city"))  # refuse a missing required / unsupported city now, not after 8 retries
    mp = payload.get("max_pages")
    if mp is not None and (not isinstance(mp, int) or isinstance(mp, bool) or mp < 1):
        raise ValueError("max_pages must be an integer >= 1")
    if not isinstance(payload.get("detail", True), bool):
        raise ValueError("detail must be true or false")
    cur = conn.execute("INSERT INTO tasks(provider, kind, payload) VALUES (?,?,?)",
                       (provider, prov.kind, json.dumps(payload, ensure_ascii=False)))
    return cur.lastrowid


def claim(conn, provider: str | None = None):
    """Atomically take the next runnable task (of `provider`, or of any), or None."""
    now = time.time()
    return conn.execute(
        """UPDATE tasks SET status='running', locked_at=? WHERE id=(
             SELECT id FROM tasks
             WHERE ((status='pending' AND next_run_at<=?) OR (status='running' AND locked_at<?))
               AND (? IS NULL OR provider=?)
             ORDER BY next_run_at, id LIMIT 1) RETURNING *""",
        (now, now, now - LOCK_TTL, provider, provider)).fetchone()


def _set(conn, task_id: int, status: str, error=None, stats=None, delay: float = 0.0, attempts_inc: int = 0):
    conn.execute(
        "UPDATE tasks SET status=?, last_error=?, stats=COALESCE(?, stats), next_run_at=?,"
        " attempts=attempts+?, locked_at=NULL WHERE id=?",
        (status, error, json.dumps(stats) if stats else None, time.time() + delay, attempts_inc, task_id))


def progress(conn, task_id: int, stats: dict):
    """Live stats while the task runs; done() writes the final ones."""
    conn.execute("UPDATE tasks SET stats=? WHERE id=?", (json.dumps(stats), task_id))


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


def get(conn, task_id: int) -> dict:
    return as_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def active_ids(conn) -> set[int]:
    """Tasks that still need a worker: queued (also while backing off before a retry) or running."""
    return {r["id"] for r in conn.execute("SELECT id FROM tasks WHERE status IN ('pending','running')")}


def list_(conn, status: str | None = None, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT t.*, (SELECT COUNT(*) FROM task_jobs j WHERE j.task_id=t.id) AS jobs_found "
                        "FROM tasks t WHERE (? IS NULL OR t.status=?) ORDER BY t.id DESC LIMIT ?",
                        (status, status, limit)).fetchall()
    return [as_dict(r) for r in rows]


def by_search(conn, limit: int = 20) -> list[dict]:
    """Tasks of the `limit` most recent searches (one search = one keyword + city), newest search first, then by id."""
    rows = conn.execute(
        """WITH t AS MATERIALIZED (SELECT id, provider, status, last_error, created_at,
                                  json_extract(payload, '$.keyword') AS keyword,
                                  IFNULL(json_extract(payload, '$.city'), '') AS city FROM tasks),
                g AS (SELECT keyword, city, MAX(id) AS last_id FROM t
                      GROUP BY keyword, city ORDER BY last_id DESC LIMIT ?)
           SELECT t.* FROM t JOIN g ON t.keyword IS g.keyword AND t.city = g.city
           ORDER BY g.last_id DESC, t.id""", (limit,)).fetchall()
    return [dict(r) for r in rows]
