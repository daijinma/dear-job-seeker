"""Runs one task end to end and hosts the per-platform worker threads."""
from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager

import httpx

from djs import auth, browser, db, egress, pacer, providers, taskqueue
from djs.providers.base import Blocked, Delisted, FetchError, LoginRequired

NO_EGRESS_DELAY = 60
POLL_SECONDS = 1
ERROR_BACKOFF = 5
# jobs columns a provider derives from a list item (row) and its metadata (meta)
FIELDS = ("title", "company", "city", "salary_text", "description", "source_url", "published_at", "published_at_kind")


def _job_fields(prov, job, detail) -> dict:
    """The derived jobs columns for a list item, plus its detail once that has been fetched."""
    derived = {**prov.row(job, detail), **prov.meta(job, detail)}
    return {k: derived.get(k) for k in FIELDS}


def _dump(detail) -> str | None:
    return json.dumps(detail, ensure_ascii=False) if detail else None


def _login_expired(prov, exc) -> bool:
    """A login provider answering "log in": the cookie is dead (for a public provider that answer is a block)."""
    return isinstance(exc, LoginRequired) and prov.auth == "login"


@contextmanager
def _open(transport: str, prov, exit_id: str, interval: float, jitter: float, cookies):
    """The client for a platform's transport: an httpx client, or a browser page that acts like one."""
    if transport == "http":
        with egress.make_client(exit_id, prov, interval, jitter, cookies) as client:
            yield client
    else:
        with browser.open_browser(prov, exit_id, interval, jitter) as client:
            yield client


@contextmanager
def leased_client(conn, prov, key: str, payload: dict):
    """Paced client on a leased exit, over the platform's transport (httpx or browser). A 'login' provider needs a
    live session first; 'optional' ones never wait for one (an http client still carries a saved cookie).
    Reports the exit's health afterwards. A dead login is the login's fault, not the exit's, so it only
    invalidates the cookie."""
    transport = pacer.transport(conn, prov.name)
    if prov.auth == "login" and not auth.logged_in(prov.name):
        raise LoginRequired(f"no login for {prov.name}")
    cookies = auth.load_cookies(prov.name) if prov.auth != "public" and transport == "http" else None
    exit_id = egress.lease(conn, prov.name, key)
    interval, jitter, _ = pacer.effective(conn, prov.name)
    try:
        with _open(transport, prov, exit_id, interval, jitter, cookies) as client:
            prov.warmup(client, payload)
            yield client
    except (FetchError, httpx.HTTPError) as e:
        if _login_expired(prov, e):
            auth.invalidate(prov.name)
        else:
            egress.report(conn, exit_id, prov.name, ok=False, blocked=isinstance(e, Blocked),
                          retry_after=getattr(e, "retry_after", None))
        raise
    egress.report(conn, exit_id, prov.name, ok=True)


def fetch_detail(client, prov, job):
    """Returns (detail, error). Delisted is a normal end state; Blocked propagates so the caller can back off."""
    try:
        return prov.detail(client, job), None
    except Delisted:
        return None, None
    except Blocked:
        raise
    except FetchError as e:
        return None, e


def _insert(conn, task_id, prov, jid, job, detail, err) -> int:
    fields = _job_fields(prov, job, detail)
    cur = conn.execute(
        f"INSERT INTO jobs(provider, source_job_id, task_id, payload, detail, detail_error, {', '.join(FIELDS)}) "
        f"VALUES (?,?,?,?,?,?,{', '.join('?' * len(FIELDS))})",
        (prov.name, jid, task_id, json.dumps(job, ensure_ascii=False), _dump(detail), err and str(err),
         *fields.values()))
    return cur.lastrowid


def update_job(conn, job_id, prov, job, detail) -> None:
    """Store a detail fetched after the fact. A column only changes when the provider now knows a value."""
    sets = ", ".join(f"{k}=COALESCE(?, {k})" for k in FIELDS)
    conn.execute(f"UPDATE jobs SET detail=?, detail_error=NULL, {sets} WHERE id=?",
                 (_dump(detail), *_job_fields(prov, job, detail).values(), job_id))


def _store(conn, task_id, prov, client, job, stats, with_detail) -> None:
    jid = prov.job_id(job)
    known = conn.execute(
        "UPDATE jobs SET last_seen_at=strftime('%s','now') WHERE provider=? AND source_job_id=? RETURNING id",
        (prov.name, jid)).fetchone()
    if known:
        stats["jobs_seen"] += 1
        job_pk = known["id"]
    else:
        detail = err = None
        if with_detail:
            detail, err = fetch_detail(client, prov, job)
            stats["details_failed" if err else "details_ok"] += 1
        job_pk = _insert(conn, task_id, prov, jid, job, detail, err)
        stats["jobs_new"] += 1
    conn.execute("INSERT OR IGNORE INTO task_jobs(task_id, job_id) VALUES (?,?)", (task_id, job_pk))


def _crawl(conn, task_id, prov, client, payload, max_pages) -> dict:
    stats = {"pages": 0, "jobs_new": 0, "jobs_seen": 0, "details_ok": 0, "details_failed": 0}
    with_detail = payload.get("detail", True)
    limit = max_pages if prov.page_cap is None else min(max_pages, prov.page_cap)
    for page in range(prov.first_page, prov.first_page + limit):
        items = prov.fetch(client, payload, page)
        stats["pages"] += 1
        for job in items:
            _store(conn, task_id, prov, client, job, stats, with_detail)
        taskqueue.progress(conn, task_id, stats)
        if len(items) < prov.page_size:  # empty or short page = last page
            break
    return stats


def run_task(conn, task) -> None:
    prov = providers.get(task["provider"])
    if prov.missing_env():
        return taskqueue.dead(conn, task["id"], f"missing environment variable(s): {', '.join(prov.missing_env())}")
    payload = json.loads(task["payload"])
    max_pages = payload.get("max_pages") or pacer.effective(conn, prov.name)[2]
    try:
        with leased_client(conn, prov, str(task["id"]), payload) as client:
            stats = _crawl(conn, task["id"], prov, client, payload, max_pages)
    except browser.BrowserUnavailable as e:  # retrying cannot install Playwright or start the user's Chrome
        return taskqueue.dead(conn, task["id"], str(e))
    except egress.NoEgressAvailable as e:  # also a busy browser profile: try again later, attempt not burned
        return taskqueue.release(conn, task["id"], str(e), NO_EGRESS_DELAY)
    except (FetchError, httpx.HTTPError) as e:
        if _login_expired(prov, e):
            return taskqueue.waiting_login(conn, task["id"])
        return taskqueue.retry(conn, task["id"], f"{type(e).__name__}: {e}", getattr(e, "retry_after", None))
    taskqueue.done(conn, task["id"], stats)


def tick(conn, provider: str | None = None) -> bool:
    """Claim and run one task (of `provider`, or of any). False when there was nothing to run."""
    task = taskqueue.claim(conn, provider)
    if task is None:
        return False
    try:
        run_task(conn, task)
    except Exception as e:  # never let one bad task kill the worker
        taskqueue.retry(conn, task["id"], f"{type(e).__name__}: {e}")
    return True


_lock = threading.Lock()
_stop = threading.Event()
_threads: dict[str, threading.Thread] = {}


def _loop(provider: str) -> None:
    """Runs until stop_worker(). Any error (e.g. a locked database) means reconnect after a pause, never exit:
    a worker that dies silently retires its platform for the rest of the session."""
    conn = None
    while not _stop.is_set():
        try:
            conn = conn or db.connect()
            if not tick(conn, provider):
                _stop.wait(POLL_SECONDS)
        except Exception as e:
            print(f"djs worker {provider}: {type(e).__name__}: {e}", file=sys.stderr)
            if conn is not None:
                conn.close()
            conn = None
            _stop.wait(ERROR_BACKOFF)
    if conn is not None:
        conn.close()


def start_worker() -> None:
    """One daemon thread per platform: pacing is per platform, so a slow site must not hold up the others.
    Idempotent: only missing or dead threads are started."""
    with _lock:
        _stop.clear()
        for name in providers.names():
            if name not in _threads or not _threads[name].is_alive():
                _threads[name] = threading.Thread(target=_loop, args=(name,), daemon=True, name=f"djs-worker-{name}")
                _threads[name].start()


def stop_worker(wait: float = 5.0) -> None:
    """Ask the workers to stop after their current task; give them `wait` seconds in total."""
    with _lock:
        _stop.set()
        deadline = time.monotonic() + wait
        for t in _threads.values():
            t.join(max(0.0, deadline - time.monotonic()))
