"""Runs one task end to end and hosts the background worker thread."""
from __future__ import annotations

import json
import threading

import httpx

from djs import auth, db, egress, pacer, providers, taskqueue
from djs.providers.base import Blocked, Delisted, FetchError, LoginRequired

NO_EGRESS_DELAY = 60
POLL_SECONDS = 2


def _detail(client, prov, job, stats):
    """Returns (detail, error). Delisted is a normal end state; Blocked propagates so the job is retried."""
    try:
        detail = prov.detail(client, job)
    except Delisted:
        detail = None
    except Blocked:
        raise
    except FetchError as e:
        stats["details_failed"] += 1
        return None, str(e)
    stats["details_ok"] += 1
    return detail, None


def _store(conn, task_id, prov, client, job, stats):
    jid = prov.job_id(job)
    seen = conn.execute("UPDATE jobs SET last_seen_at=strftime('%s','now') WHERE provider=? AND source_job_id=?",
                        (prov.name, jid)).rowcount
    if seen:
        stats["jobs_seen"] += 1
        return
    detail, err = _detail(client, prov, job, stats)
    row, meta = prov.row(job, detail), prov.meta(job, detail)
    conn.execute(
        """INSERT INTO jobs(provider, source_job_id, task_id, title, company, city, salary_text, description,
             source_url, published_at, published_at_kind, payload, detail, detail_error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (prov.name, jid, task_id, row.get("title"), row.get("company"), row.get("city"), row.get("salary_text"),
         row.get("description"), meta.get("source_url"), meta.get("published_at"), meta.get("published_at_kind"),
         json.dumps(job, ensure_ascii=False), json.dumps(detail, ensure_ascii=False) if detail else None, err))
    stats["jobs_new"] += 1


def _crawl(conn, task_id, prov, client, payload, max_pages) -> dict:
    stats = {"pages": 0, "jobs_new": 0, "jobs_seen": 0, "details_ok": 0, "details_failed": 0}
    limit = max_pages if prov.page_cap is None else min(max_pages, prov.page_cap)
    for page in range(prov.first_page, prov.first_page + limit):
        items = prov.fetch(client, payload, page)
        stats["pages"] += 1
        for job in items:
            _store(conn, task_id, prov, client, job, stats)
        if len(items) < prov.page_size:  # empty or short page = last page
            break
    return stats


def _fail(conn, task_id, prov, exit_id, exc):
    if isinstance(exc, LoginRequired) and prov.auth == "login":
        auth.invalidate(prov.name)
        return taskqueue.waiting_login(conn, task_id)
    blocked = isinstance(exc, Blocked)
    egress.report(conn, exit_id, prov.name, ok=False, blocked=blocked, retry_after=getattr(exc, "retry_after", None))
    taskqueue.retry(conn, task_id, f"{type(exc).__name__}: {exc}", getattr(exc, "retry_after", None))


def run_task(conn, task) -> None:
    prov = providers.get(task["provider"])
    if prov.missing_env():
        return taskqueue.dead(conn, task["id"], f"missing environment variable(s): {', '.join(prov.missing_env())}")
    cookies = auth.load_cookies(prov.name) if prov.auth == "login" else None
    if prov.auth == "login" and cookies is None:
        return taskqueue.waiting_login(conn, task["id"])
    try:
        exit_id = egress.lease(conn, prov.name, str(task["id"]))
    except egress.NoEgressAvailable as e:
        return taskqueue.release(conn, task["id"], str(e), NO_EGRESS_DELAY)
    payload = json.loads(task["payload"])
    interval, jitter, max_pages = pacer.effective(conn, prov.name)
    try:
        with egress.make_client(exit_id, prov, interval, jitter, cookies) as client:
            stats = _crawl(conn, task["id"], prov, client, payload, payload.get("max_pages") or max_pages)
    except (FetchError, httpx.HTTPError) as e:
        return _fail(conn, task["id"], prov, exit_id, e)
    egress.report(conn, exit_id, prov.name, ok=True)
    taskqueue.done(conn, task["id"], stats)


def tick(conn) -> bool:
    task = taskqueue.claim(conn)
    if task is None:
        return False
    try:
        run_task(conn, task)
    except Exception as e:  # never let one bad task kill the worker
        taskqueue.retry(conn, task["id"], f"{type(e).__name__}: {e}")
    return True


def _loop(stop: threading.Event):
    conn = db.connect()
    while not stop.is_set():
        if not tick(conn):
            stop.wait(POLL_SECONDS)


_lock = threading.Lock()
_thread: threading.Thread | None = None


def start_worker() -> None:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, args=(threading.Event(),), daemon=True, name="djs-worker")
            _thread.start()
