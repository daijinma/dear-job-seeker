"""Task queue on the tasks table. Every transition is a single UPDATE (atomic)."""
from __future__ import annotations

import json
import random
import time

from djs import providers

LOCK_TTL = 15 * 60
MAX_ATTEMPTS = 8
BACKOFF_CAP = 360
DETAIL_MODES = ("auto", "always", "never")
ACTIVE = ("pending", "running")   # a task that still needs a worker (pending also covers a wait before a retry)
ACTIVE_SQL = "(" + ", ".join(f"'{status}'" for status in ACTIVE) + ")"
# One search = the tasks with the same keyword and city (named parameters :k and :c); a task without a city is the
# search made without one.
SEARCH = "json_extract(payload, '$.keyword') = :k AND IFNULL(json_extract(payload, '$.city'), '') = :c"
_SEARCH_COLS = "id, provider, status, last_error, created_at, stats"


def detail_mode(value) -> str:
    """A task's `detail` setting as one of DETAIL_MODES. auto = the detail request only for jobs still missing a core
    element, always = for every job without a stored detail, never = list items only. True / False are the older
    always / never."""
    if isinstance(value, bool):
        return "always" if value else "never"
    if value in DETAIL_MODES:
        return value
    raise ValueError(f"detail must be one of {', '.join(DETAIL_MODES)} (or true/false), not {value!r}")


def create(conn, provider: str, payload: dict) -> int:
    prov = providers.get(provider)  # KeyError for unknown provider
    need = "keyword" if prov.kind == "keyword" else "tenant"
    if not payload.get(need):
        raise ValueError(f"{provider}: payload needs {need!r}")
    prov.city_code(payload.get("city"))  # refuse a missing required / unsupported city now, not after 8 retries
    mp = payload.get("max_pages")
    if mp is not None and (not isinstance(mp, int) or isinstance(mp, bool) or mp < 1):
        raise ValueError("max_pages must be an integer >= 1")
    detail_mode(payload.get("detail", "auto"))
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


def requeue(conn, task_ids) -> int:
    """The user pressed retry: dead or login-blocked tasks start over with a fresh attempt budget."""
    ids = [int(i) for i in task_ids]
    return conn.execute(
        "UPDATE tasks SET status='pending', attempts=0, next_run_at=0, last_error=NULL, locked_at=NULL "
        f"WHERE status IN ('dead','waiting_login') AND id IN ({','.join('?' * len(ids))})", ids).rowcount


def _stats(raw) -> dict | None:
    return json.loads(raw) if raw else None


def as_dict(row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["stats"] = _stats(d["stats"])
    return d


def outcome(status: str, jobs: int) -> str:
    """How a task (or a platform's newest task in a search) stands, in one word: running (queued, working, or waiting
    to retry), waiting_login, failed, found or no_results. A finished task that found nothing is no_results, never a
    failure."""
    if status in ACTIVE:
        return "running"
    if status == "done":
        return "found" if jobs else "no_results"
    return {"waiting_login": "waiting_login", "dead": "failed"}[status]


def summary(status: str, jobs: int, stats: dict | None) -> dict:
    """{outcome} and, while a queued or running task is in its completion pass, {completing: {done, total}}: how many
    of the details it set out to fetch are in (the engine writes details_total, details_ok and details_failed)."""
    found = {"outcome": outcome(status, jobs)}
    total = (stats or {}).get("details_total")
    if status in ACTIVE and total:
        found["completing"] = {"done": stats.get("details_ok", 0) + stats.get("details_failed", 0), "total": total}
    return found


def get(conn, task_id: int) -> dict:
    return as_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def active_ids(conn) -> set[int]:
    """Tasks that still need a worker: queued (also while backing off before a retry) or running."""
    return {r["id"] for r in conn.execute(f"SELECT id FROM tasks WHERE status IN {ACTIVE_SQL}")}


def list_(conn, status: str | None = None, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT t.*, (SELECT COUNT(*) FROM task_jobs j WHERE j.task_id=t.id) AS jobs_found "
                        "FROM tasks t WHERE (? IS NULL OR t.status=?) ORDER BY t.id DESC LIMIT ?",
                        (status, status, limit)).fetchall()
    return [{**t, **summary(t["status"], t["jobs_found"], t["stats"])} for t in map(as_dict, rows)]


def by_search(conn, limit: int = 20) -> list[dict]:
    """Tasks of the `limit` most recent searches (one search = one keyword + city), newest search first, then by id."""
    rows = conn.execute(
        f"""WITH t AS MATERIALIZED (SELECT {_SEARCH_COLS},
                                   json_extract(payload, '$.keyword') AS keyword,
                                   IFNULL(json_extract(payload, '$.city'), '') AS city FROM tasks),
                 g AS (SELECT keyword, city, MAX(id) AS last_id FROM t
                       GROUP BY keyword, city ORDER BY last_id DESC LIMIT ?)
            SELECT t.* FROM t JOIN g ON t.keyword IS g.keyword AND t.city = g.city
            ORDER BY g.last_id DESC, t.id""", (limit,)).fetchall()
    return [{**dict(r), "stats": _stats(r["stats"])} for r in rows]


def of_search(conn, keyword: str, city: str) -> list[dict]:
    """The tasks of one search, oldest first, in the shape by_search gives."""
    rows = conn.execute(f"SELECT {_SEARCH_COLS} FROM tasks WHERE {SEARCH} ORDER BY id",
                        {"k": keyword, "c": city or ""}).fetchall()
    return [{**dict(r), "stats": _stats(r["stats"])} for r in rows]
