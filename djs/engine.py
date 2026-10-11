"""Runs one task end to end and hosts the per-platform worker threads."""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager

import httpx

from djs import auth, browser, cities, db, egress, jobs, pacer, providers, taskqueue
from djs.providers.base import Blocked, Delisted, FetchError, LoginRequired

NO_EGRESS_DELAY = 60
POLL_SECONDS = 1
ERROR_BACKOFF = 5


def _login_expired(prov, exc) -> bool:
    """A login provider answering "log in": the session is dead and the task waits for a new one (for an optional
    provider it only means the saved session is dead, and the task goes on without it)."""
    return isinstance(exc, LoginRequired) and prov.auth == "login"


def _session(prov) -> bool:
    """A platform with a login, and the user is logged in to it."""
    return prov.login is not None and auth.logged_in(prov.name)


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
    """Paced client on a leased exit, over the platform's transport (httpx or browser). A platform with a login gets
    its session on every transport (saved cookies on http, the shared profile in a browser); a 'login' platform
    needs one first, an 'optional' one never waits for it. Reports the exit's health afterwards. A dead session is
    the session's fault, not the exit's, so it only invalidates the saved login."""
    session = _session(prov)
    if prov.auth == "login" and not session:
        raise LoginRequired(f"no login for {prov.name}")
    transport = pacer.transport(conn, prov.name, session)
    cookies = auth.load_cookies(prov.name) if session and transport == "http" else None
    exit_id = egress.lease(conn, prov.name, key)
    interval, jitter, _ = pacer.effective(conn, prov.name)
    try:
        with _open(transport, prov, exit_id, interval, jitter, cookies) as client:
            prov.warmup(client, payload)
            yield client
    except (FetchError, httpx.HTTPError) as e:
        if session and isinstance(e, LoginRequired):
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


class Stopped(Exception):
    """The worker was asked to stop: the task goes back to pending as it stands."""


class _Run:
    """One task being run. Its writes go through `who`: the Claim of the worker that took it, else the bare id (a task run
    by hand, written unconditionally). `stop` is the worker's stop signal, looked at between two requests."""

    def __init__(self, task, claim=None, stop=None):
        self.id, self.who, self.stop = task["id"], claim or task["id"], stop

    def check(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise Stopped

    def progress(self, conn, stats) -> None:
        taskqueue.progress(conn, self.who, {**stats, **jobs.tally(conn, self.id)})


def _store(conn, task_id, prov, job, stats) -> None:
    """One list item: a known job only gets its last-seen time touched, a new one is derived and inserted. Either way
    the task is linked to it."""
    jid = prov.job_id(job)
    known = conn.execute(
        "UPDATE jobs SET last_seen_at=strftime('%s','now') WHERE provider=? AND source_job_id=? RETURNING id",
        (prov.name, jid)).fetchone()
    if known:
        stats["jobs_seen"] += 1
        job_pk = known["id"]
    else:
        job_pk = jobs.insert(conn, task_id, prov, jid, job)
        stats["jobs_new"] += 1
    conn.execute("INSERT OR IGNORE INTO task_jobs(task_id, job_id) VALUES (?,?)", (task_id, job_pk))


def _awaiting(conn, task_id, prov, mode: str) -> list:
    """The jobs the completion pass asks a detail for: with 'always' every job that has none, with 'auto' the incomplete
    ones whose detail could supply what they lack (see Provider.detail_can_fill)."""
    rows = jobs.awaiting_detail(conn, task_id, everything=mode == "always")
    if mode == "always" or not rows:
        return rows
    logged_in = _session(prov)
    return [r for r in rows if prov.detail_can_fill(jobs.gaps(r["missing"]), logged_in)]


def _complete(conn, run, prov, client, mode: str, stats) -> None:
    """The second pass: the detail request, for the task's jobs that still miss a core element ('auto', skipping those
    a detail cannot complete) or for all of them ('always'). Skipped when the list item already is the detail. Blocked
    ends the task (it is retried and carries on with what is still missing); any other failure is kept on the job.
    stats['details_total'] says how many requests the pass will make: readers show the progress with it. A stop request
    ends the pass between two details."""
    if mode == "never" or not prov.has_detail:
        return
    rows = _awaiting(conn, run.id, prov, mode)
    stats["details_total"] = len(rows)
    run.progress(conn, stats)
    for row in rows:
        run.check()
        item = json.loads(row["payload"])
        detail, err = fetch_detail(client, prov, item)
        if err:
            jobs.fail_detail(conn, row["id"], err)
        else:
            jobs.update(conn, row["id"], prov, item, detail)
        stats["details_failed" if err else "details_ok"] += 1
        run.progress(conn, stats)


def _store_page(conn, task_id, prov, items: list, city: str, stats) -> None:
    """One list page. A platform that cannot filter by city (city_filter 'client') still returns every city: only the
    jobs placed in the asked `city` are stored, the rest is counted in stats['filtered']."""
    for job in items:
        if city and city not in (prov.row(job, None).get("city") or ""):
            stats["filtered"] += 1
        else:
            _store(conn, task_id, prov, job, stats)


def _crawl(conn, run, prov, client, payload, max_pages) -> dict:
    """List pages first (stored at once, so results show up early), then the completion pass. A stop request ends it
    between two requests (Stopped)."""
    stats = {"pages": 0, "jobs_new": 0, "jobs_seen": 0, "details_ok": 0, "details_failed": 0}
    city = cities.norm(payload.get("city")) if prov.city_filter == "client" else ""
    if city:
        stats["filtered"] = 0
    limit = max_pages if prov.page_cap is None else min(max_pages, prov.page_cap)
    for page in range(prov.first_page, prov.first_page + limit):
        run.check()
        items = prov.fetch(client, payload, page)
        stats["pages"] += 1
        _store_page(conn, run.id, prov, items, city, stats)
        run.progress(conn, stats)
        if len(items) < prov.page_size:  # empty or short page = last page: the site's own page, not what the filter kept
            break
    _complete(conn, run, prov, client, taskqueue.detail_mode(payload.get("detail", "auto")), stats)
    return {**stats, **jobs.tally(conn, run.id)}


def run_task(conn, task) -> None:
    run = _run_of(task)
    prov = providers.get(task["provider"])
    if prov.missing_env():
        return taskqueue.dead(conn, run.who, f"missing environment variable(s): {', '.join(prov.missing_env())}")
    payload = json.loads(task["payload"])
    max_pages = payload.get("max_pages") or pacer.effective(conn, prov.name)[2]
    try:
        with leased_client(conn, prov, str(run.id), payload) as client:
            stats = _crawl(conn, run, prov, client, payload, max_pages)
    except Stopped:  # asked to stop: back to pending as it stands, for whoever claims it next
        return taskqueue.hand_back(conn, run.who)
    except browser.BrowserUnavailable as e:  # retrying cannot install Playwright or start the user's Chrome
        return taskqueue.dead(conn, run.who, str(e))
    except egress.NoEgressAvailable as e:  # also a busy browser profile: try again later, attempt not burned
        return taskqueue.release(conn, run.who, str(e), NO_EGRESS_DELAY)
    except (FetchError, httpx.HTTPError) as e:
        if _login_expired(prov, e):
            return taskqueue.waiting_login(conn, run.who)
        return taskqueue.retry(conn, run.who, f"{type(e).__name__}: {e}", getattr(e, "retry_after", None))
    taskqueue.done(conn, run.who, stats)


# The tasks this process is running right now, by id: tick registers the one it claimed, so run_task writes through its
# Claim and stop_worker knows what to hand back. A task run by hand (run_task on a row) is not in here.
_held: dict[int, _Run] = {}
_held_lock = threading.Lock()


@contextmanager
def _hold(run: _Run):
    with _held_lock:
        _held[run.id] = run
    try:
        yield
    finally:
        with _held_lock:
            _held.pop(run.id, None)


def _run_of(task) -> _Run:
    with _held_lock:
        return _held.get(task["id"]) or _Run(task)


def _beat(conn, claim) -> None:
    """The sign of life pacer.wait gives while the worker sleeps or queues for its turn. A database that cannot take the
    write right now only costs a missed beat; Lost (the task is someone else's now) has to reach the caller."""
    try:
        taskqueue.beat(conn, claim)
    except sqlite3.Error:
        pass


def tick(conn, provider: str | None = None, stop: threading.Event | None = None) -> bool:
    """Claim and run one task (of `provider`, or of any). False when there was nothing to run. `stop` is the worker's stop
    signal: it ends the task between two requests, and the task goes back to pending as it stands."""
    task = taskqueue.claim(conn, provider)
    if task is None:
        return False
    claim = taskqueue.Claim(task)
    with _hold(_Run(task, claim, stop)), pacer.heartbeat(lambda: _beat(conn, claim)):
        try:
            run_task(conn, task)
        except taskqueue.Lost:
            pass  # handed back or taken over while it ran: it is another claim's now, nothing of ours is left to write
        except Exception as e:  # never let one bad task kill the worker
            taskqueue.retry(conn, claim, f"{type(e).__name__}: {e}")
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
            if not tick(conn, provider, _stop):
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


def workers() -> dict[str, bool]:
    """Per platform: does THIS process have a live worker thread for it (the app, an MCP server and `djs work` each run
    their own on the same database)."""
    with _lock:
        return {name: name in _threads and _threads[name].is_alive() for name in providers.names()}


def stop_worker(wait: float = 5.0) -> None:
    """Ask the workers to stop and give them `wait` seconds in total, then put back to pending whatever tasks this
    process still holds, so that the next process (or the next start) takes them up at once instead of after LOCK_TTL. A
    worker notices the request between two requests of its task and hands the task back itself; this is for one that is
    stuck in a long request."""
    with _lock:
        _stop.set()
        deadline = time.monotonic() + wait
        for t in _threads.values():
            t.join(max(0.0, deadline - time.monotonic()))
        _hand_back_held()


def _hand_back_held() -> None:
    """Every task this process still runs goes back to pending as it stands. Its worker may wake up later: its writes find
    the claim over and are refused (taskqueue.Lost), so it cannot disturb the next holder."""
    with _held_lock:
        left = [run.who for run in _held.values()]
    if not left:
        return
    try:
        conn = db.connect()
        try:
            for claim in left:
                taskqueue.hand_back(conn, claim)
        finally:
            conn.close()
    except Exception as e:  # the process is ending: what is not handed back is taken over after LOCK_TTL
        print(f"djs: could not hand back the running tasks: {type(e).__name__}: {e}", file=sys.stderr)
