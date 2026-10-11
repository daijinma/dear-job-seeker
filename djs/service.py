"""Tool-level operations (one function per MCP tool). server.py only wraps these."""
from __future__ import annotations

import functools
import importlib.metadata
import json
import threading
import time

import httpx

from djs import auth, browser, db, egress, engine, jobs, pacer, providers, query, taskqueue
from djs.normalize import CORE
from djs.providers.base import Blocked, FetchError, LoginRequired

JOB_COLS = ("id, provider, source_job_id, task_id, title, company, city, district, education, experience, "
            "salary_text, salary_min, salary_max, salary_unit, salary_months, salary_month_min, salary_month_max, "
            "description, source_url, published_at, published_at_kind, missing, complete, detail_error, "
            "first_seen_at, last_seen_at")
# What jobs_get returns per job (fields 'summary'): short enough that a few dozen fit in a client's context. 'full'
# swaps the description_snippet for the whole description.
SUMMARY = ("id", "provider", "label", "title", "company", "city", "district", "salary_text", "salary_month_min",
           "salary_month_max", "salary_unit", "salary_months", "education", "experience", "source_url", "missing",
           "also_on")
SNIPPET = 120
MAX_LIMIT = 100


def _with_login() -> list[tuple[str, object]]:
    """(name, provider) of every platform that has a login."""
    found = ((name, providers.get(name)) for name in providers.names())
    return [(name, p) for name, p in found if p.login is not None]


def _login_row(name: str, prov, status: dict) -> dict:
    return {"provider": name, "label": providers.label(name), "auth": prov.auth, **status}


def login_overview() -> list[dict]:
    """Every platform that has a login: its state (logged_in, expired or not_logged_in), when the login was saved and
    when it ends, how the user logs in, and what logging in improves. One call, no network."""
    return [_login_row(name, p, auth.status(name)) for name, p in _with_login()]


def provider_list(conn, logins: list[dict] | None = None) -> list[dict]:
    """`logins` = a login_overview() the caller already has."""
    out, browser_ready, cdp = [], browser.ready(), browser.cdp_url() is not None
    logged_in = {row["provider"]: row["logged_in"] for row in (login_overview() if logins is None else logins)}
    for name in providers.names():
        p = providers.get(name)
        interval, jitter, max_pages = pacer.effective(conn, name)
        d = {"name": name, "label": providers.label(name), "kind": p.kind, "auth": p.auth, "requires_city": p.requires_city,
             "interval": interval, "jitter": jitter, "max_pages": max_pages,
             "min_interval": p.min_interval, "page_cap": p.page_cap, "note": p.note,
             "transports": list(p.transports), "transport": pacer.transport(conn, name, logged_in.get(name, False)),
             "needs_headed": p.needs_headed, "browser_ready": browser_ready, "cdp": cdp,
             "configured": not p.missing_env(), "missing_env": p.missing_env()}
        if name in logged_in:
            d["logged_in"] = logged_in[name]
        out.append(d)
    return out


def crawl_start(conn, provider, keyword=None, city=None, tenant=None, max_pages=None, detail="auto") -> dict:
    """detail: 'auto' requests the detail only for jobs still missing a core element, 'always' for every job without a
    stored detail, 'never' stores the list items only (job_detail loads one on demand). True / False mean always /
    never."""
    prov = providers.get(provider)
    missing = prov.missing_env()
    if missing:
        raise ValueError(f"{provider} needs environment variable(s) {', '.join(missing)}; "
                         "set them in the MCP client config env block (see README)")
    session = bool(prov.login_transport) and auth.logged_in(provider)  # only then does the login change the transport
    if pacer.transport(conn, provider, session) != "http" and not browser.ready():
        raise ValueError(browser.INSTALL_HINT)  # the task would only die later with the same message
    payload = {k: v for k, v in (("keyword", keyword), ("city", city), ("tenant", tenant), ("max_pages", max_pages),
                                 ("detail", detail)) if v is not None}
    return {"task_id": taskqueue.create(conn, provider, payload)}


def crawl_many(conn, sites: list[str], keyword: str, city: str | None, max_pages=None) -> dict:
    """One crawl task on each site that can take the search, as {task_ids, skipped}. A site that cannot (no such city, no
    city where one is needed, keys or browser missing) refuses with a ValueError before it writes anything: it is named
    in `skipped` with {provider, label, reason} and the others start. Raises only when no site can start, with every
    reason (an unknown site is a KeyError and cancels the whole search).
    IMMEDIATE: a transaction that reads first and writes later fails at once with 'database is locked' whenever a
    worker commits in between (the busy timeout does not help it), so the write lock is taken up front."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids, skipped = [], []
        for site in sites:
            try:
                ids.append(crawl_start(conn, site, keyword=keyword, city=city, max_pages=max_pages)["task_id"])
            except ValueError as e:
                skipped.append({"provider": site, "label": providers.label(site), "reason": str(e)})
        if not ids:
            raise ValueError("no platform can take this search: " + "; ".join(f"{s['label']}: {s['reason']}" for s in skipped))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"task_ids": ids, "skipped": skipped}


def _takes_city(name: str, city: str) -> bool:
    """The platform can search this city (or none: a nationwide search, unless it requires a city)."""
    try:
        providers.get(name).city_code(city or None)
    except ValueError:
        return False
    return True


def _works_as_is(p: dict, city: str) -> bool:
    """A provider_list row a search can use without asking the user anything: no login required, no browser window, its
    keys set, a capture transport that can run now, and a city it can search (or none, where it takes none)."""
    return (p["auth"] != "login" and not p["needs_headed"] and p["configured"]
            and (p["transport"] == "http" or p["browser_ready"]) and _takes_city(p["name"], city))


def default_sites(conn, city: str) -> list[str]:
    """The platforms a search uses when none are named: the ones that work as they are, like the app's preselection."""
    return [p["name"] for p in provider_list(conn) if _works_as_is(p, city)]


def search(conn, keyword, city=None, sites=None, max_pages=1) -> dict:
    """Start a search: one crawl task on each of `sites`, or on every platform usable right now. Returns at once, with
    the platforms it started on and, in `skipped`, the named ones that could not take it (see crawl_many)."""
    keyword, city = search_key(keyword, city)
    if not keyword:
        raise ValueError("keyword is required")
    chosen = list(sites) if sites else default_sites(conn, city)
    if not chosen:
        raise ValueError("no platform is usable right now (see provider_list)")
    started = crawl_many(conn, chosen, keyword, city or None, max_pages)
    left_out = {s["provider"] for s in started["skipped"]}
    return {"keyword": keyword, "city": city or None, "providers": [p for p in chosen if p not in left_out], **started}


def task_list(conn, status=None, limit=20) -> list[dict]:
    return taskqueue.list_(conn, status, limit)


def load_job(row) -> dict:
    """A jobs row as the tools return it: `missing` as a list, `complete` as a bool, `detail` as parsed JSON."""
    job = dict(row)
    job["missing"] = jobs.gaps(job["missing"])
    job["complete"] = bool(job["complete"])
    if job.get("detail"):
        job["detail"] = json.loads(job["detail"])
    return job


def _login_hides_salary(provider: str, logged_in) -> bool:
    try:
        return providers.get(provider).login is not None and not logged_in(provider)
    except KeyError:  # a platform that was removed after the job was stored
        return False


def _being_completed(job: dict, names: list[str], logged_in) -> bool:
    """A queued or running task has yet to request this job's detail (`detail_due`), the platform has one, and it
    could supply what the job lacks."""
    if not job.get("detail_due"):
        return False
    try:
        prov = providers.get(job["provider"])
    except KeyError:  # a platform that was removed after the job was stored
        return False
    session = prov.login is not None and logged_in(job["provider"])
    return prov.has_detail and prov.detail_can_fill(names, session)


def _why(job: dict, element: str, logged_in, completing: bool) -> str:
    """Why one core element is absent from a job."""
    if element == "salary" and "面议" in (job.get("salary_text") or ""):
        return "面议"
    if element == "salary" and _login_hides_salary(job["provider"], logged_in):
        return "需登录才能看到薪资"
    if element == "description" and job.get("detail_error"):
        return "详情获取失败"
    if completing:
        return "正在补全详情"
    return f"平台未提供{CORE[element]}"


def reasons(job: dict, logged_in=None) -> list[str]:
    """Why each core element a job lacks is absent, in a few Chinese words each (empty when nothing is missing).
    `job["missing"]` is the stored comma-joined text or a list; `logged_in(provider)` defaults to the saved login
    state. A job whose detail is still to be fetched (`job["detail_due"]`, see query.DUE) is being completed, not
    lacking."""
    names = jobs.gaps(job["missing"]) if isinstance(job["missing"], str) else job["missing"]
    logged_in = logged_in or login_lookup()
    completing = bool(names) and _being_completed(job, names, logged_in)
    return list(dict.fromkeys(_why(job, name, logged_in, completing) for name in names))   # "being completed" once


def incomplete_reason(job: dict, logged_in=None) -> str | None:
    """The reasons of one job as a single line, or None when nothing is missing."""
    return "；".join(reasons(job, logged_in)) or None


def login_lookup():
    """logged_in(provider) that asks for the login state once per platform, not once per job."""
    return functools.cache(lambda provider: auth.status(provider)["logged_in"])


def explain(rows: list[dict]) -> list[dict]:
    """Adds `incomplete_reason` to each job (and takes the `detail_due` flag it was worked out from)."""
    logged_in = login_lookup()
    for job in rows:
        job["incomplete_reason"] = incomplete_reason(job, logged_in)
        job.pop("detail_due", None)
    return rows


def _snippet(text) -> str:
    """The start of a description as one line of at most SNIPPET characters."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= SNIPPET else flat[:SNIPPET - 1] + "…"


def _shaped(job: dict, fields: str) -> dict:
    """One job as jobs_get returns it: the SUMMARY fields, the description (whole, or its start) and, for a job that
    lacks something, why."""
    out = {name: job[name] for name in SUMMARY}
    if fields == "full":
        out["description"] = job["description"]
    else:
        out["description_snippet"] = _snippet(job["description"])
    if job["missing"]:
        out["incomplete_reason"] = job["incomplete_reason"]
    return out


def search_key(keyword, city) -> tuple[str, str]:
    """A search is told by its keyword and city, as typed minus the spaces around them ('' = no city)."""
    return (keyword or "").strip(), (city or "").strip()


def _tasks_of(conn, keyword: str, city: str) -> list[dict]:
    tasks = taskqueue.of_search(conn, keyword, city)
    if not tasks:
        raise ValueError(f"no search for keyword={keyword!r}, city={city or None!r}; start one with search")
    return tasks


def jobs_get(conn, keyword=None, city=None, task_id=None, provider=None, min_month_salary=None, max_month_salary=None,
             title=None, text=None, complete_only=True, dedupe=True, fields="summary", limit=20, offset=0) -> dict:
    """One page of stored jobs, as {jobs, total, returned, next_offset} (next_offset None on the last page). Where from:
    the search (keyword and city as given to `search`), a task and/or a platform; none = every stored job. Filters: the
    job's city, the pay per month overlapping min_month_salary..max_month_salary, `title` (every word in the job title:
    the role, e.g. "Python 后端"), `text` (every word in the title or the description: skills that may sit anywhere,
    e.g. "Django"). By default only complete jobs, each job once (`also_on` lists the other platforms that carry it).
    With a search, `searching` says whether its tasks still run."""
    if fields not in ("summary", "full"):
        raise ValueError(f"fields must be 'summary' or 'full', not {fields!r}")
    keyword, city = search_key(keyword, city)
    tasks = _tasks_of(conn, keyword, city) if keyword else None
    limit, offset = max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))
    wh = query.where(search=(keyword, city) if keyword else None, task_id=task_id, provider=provider, city=city,
                     min_month_salary=min_month_salary, max_month_salary=max_month_salary, title=title, text=text,
                     complete_only=complete_only)
    rows, total = query.find(conn, wh, sort="newest", limit=limit, offset=offset, dedupe=dedupe, description=True)
    for job in rows:
        job["missing"], job["label"] = jobs.gaps(job["missing"]), providers.label(job["provider"])
    found = [_shaped(job, fields) for job in explain(rows)]
    after = offset + len(found)
    out = {"jobs": found, "total": total, "returned": len(found), "next_offset": after if after < total else None}
    if tasks is not None:
        out["searching"] = any(t["status"] in taskqueue.ACTIVE for t in tasks)
    return out


def _job(conn, job_id: int) -> dict:
    row = conn.execute(f"SELECT {JOB_COLS}, detail, {query.DUE} FROM jobs j WHERE j.id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"job {job_id} not found")
    return explain([load_job(row)])[0]


def _friendly(prov, exc: Exception) -> str:
    label = providers.label(prov.name)
    if isinstance(exc, LoginRequired):
        return f"{label}: login required; log in and try again"
    if isinstance(exc, Blocked):
        return f"{label} is limiting requests right now; try again later"
    return f"{label}: could not load the detail ({type(exc).__name__}: {exc})"


def job_detail(conn, job_id: int) -> dict:
    """The stored job with its full detail. Fetched from the platform once (same egress and pacing as a crawl), the
    columns derived from it again, then served from the database."""
    job = _job(conn, job_id)
    if job["detail"] is not None:
        return job
    prov = providers.get(job["provider"])
    if not prov.has_detail:  # the list item was all there is
        return job
    item = json.loads(conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
    try:
        with engine.leased_client(conn, prov, f"job:{job_id}", {}) as client:
            detail, err = engine.fetch_detail(client, prov, item)
        if err:
            raise err
    except (egress.NoEgressAvailable, browser.BrowserUnavailable) as e:
        raise ValueError(str(e)) from e
    except (FetchError, httpx.HTTPError) as e:
        raise ValueError(_friendly(prov, e)) from e
    jobs.update(conn, job_id, prov, item, detail)
    return _job(conn, job_id)


def _search_status(statuses: set[str]) -> str:
    if statuses & set(taskqueue.ACTIVE):
        return "running"
    return "waiting_login" if "waiting_login" in statuses else "done"


def _site(name: str, task: dict, found: dict) -> dict:
    """One platform of a search, from its newest task: status, how many jobs (complete or not), and the outcome (found,
    no_results, failed, waiting_login or running, with the completion progress while it runs)."""
    jobs_n, complete = found.get(name, (0, 0))
    return {"provider": name, "label": providers.label(name), "status": task["status"], "jobs": jobs_n,
            "complete": complete, "incomplete": jobs_n - complete, "last_error": task["last_error"],
            **taskqueue.summary(task["status"], jobs_n, task["stats"])}


def _search(conn, keyword, city, tasks: list[dict]) -> dict:
    """One search: `jobs_total` adds up each platform's own records, `whole` counts the complete jobs, each job once
    (a job on two platforms counts once, as in the results page and in jobs_get)."""
    ids = [t["id"] for t in tasks]
    found = {r["provider"]: (r["jobs"], r["complete"]) for r in conn.execute(
        "SELECT t.provider, COUNT(DISTINCT tj.job_id) AS jobs, "
        "COUNT(DISTINCT CASE WHEN j.complete THEN tj.job_id END) AS complete "
        "FROM task_jobs tj JOIN tasks t ON t.id=tj.task_id JOIN jobs j ON j.id=tj.job_id "
        f"WHERE tj.task_id IN ({','.join('?' * len(ids))}) GROUP BY t.provider", ids)}
    newest = {t["provider"]: t for t in tasks}  # tasks arrive in id order, so the last one per platform is the newest
    return {"keyword": keyword, "city": city or None, "task_ids": ids,
            "created_at": max(t["created_at"] for t in tasks),
            "status": _search_status({t["status"] for t in tasks}),
            "jobs_total": sum(n for n, _ in found.values()),
            "whole": query.count(conn, query.where(search=(keyword, city))),
            "per_site": [_site(name, t, found) for name, t in newest.items()]}


def searches(conn, limit=20) -> list[dict]:
    """Recent searches, newest first. Tasks with the same keyword and city belong to one search."""
    groups: dict[tuple, list[dict]] = {}
    for t in taskqueue.by_search(conn, limit):
        groups.setdefault((t["keyword"], t["city"]), []).append(t)
    return [_search(conn, keyword, city, tasks) for (keyword, city), tasks in groups.items()]


def search_status(conn, keyword, city=None) -> dict:
    """One search, told by the keyword and city given to `search`: like an entry of `searches`."""
    keyword, city = search_key(keyword, city)
    return _search(conn, keyword, city, _tasks_of(conn, keyword, city))


def login(conn, provider) -> dict:
    status = auth.login(provider, timeout=180)
    resumed = taskqueue.resume_login(conn, provider) if status.get("logged_in") else 0
    return {**status, "resumed_tasks": resumed}


def login_cancel(conn, provider) -> dict:
    return {"provider": provider, "cancelled": auth.cancel_login(provider)}


def login_status(conn, provider=None) -> dict | list[dict]:
    """One platform's login state, or (no platform) the overview of every platform that has a login."""
    return auth.status(provider) if provider else login_overview()


def check_all_logins() -> list[dict]:
    """Ask every platform that has a login whether the session we saved still works (one request each, only for
    sessions we hold). The answers are saved, so a dead session shows as expired from now on. A platform that cannot
    be reached keeps its state and carries an `error`."""
    rows = []
    for name, p in _with_login():
        try:
            rows.append(_login_row(name, p, auth.check(name)))
        except (httpx.HTTPError, browser.BrowserUnavailable) as e:
            rows.append({**_login_row(name, p, auth.status(name)), "error": f"{type(e).__name__}: {e}"})
    return rows


def login_check(conn, provider=None) -> dict | list[dict]:
    """Check one platform (a network failure is an error), or all of them."""
    return auth.check(provider) if provider else check_all_logins()


def start_login_check() -> None:
    """Once at startup, in the background: a dead session shows as expired before the first search, and startup
    never waits for a network request."""
    threading.Thread(target=check_all_logins, daemon=True, name="djs-login-check").start()


def logout(conn, provider) -> dict:
    auth.logout(provider)
    return {"provider": provider, "logged_in": False}


def set_frequency(conn, provider, interval=None, jitter=None, max_pages=None, force=False) -> dict:
    return pacer.set_frequency(conn, provider, interval, jitter, max_pages, force)


def set_transport(conn, provider, transport=None) -> dict:
    return pacer.set_transport(conn, provider, transport)


def egress_add(conn, url, provider="*") -> dict:
    return egress.add(conn, url, provider)


def egress_remove(conn, url) -> dict:
    return {"removed": egress.remove(conn, url)}


def egress_list(conn) -> list[dict]:
    return egress.list_(conn)


def shutdown() -> None:
    """The app or the MCP server is ending. A login window is a browser process of this process and nothing else would
    close it: cancel the pending logins and wait (a few seconds at most) for their windows to go, then stop the workers."""
    auth.cancel_all()
    engine.stop_worker(5)


# ---- status: what is going on, in one call
def app_version() -> str | None:
    """The installed version of the package, None for a source checkout that was never installed."""
    try:
        return importlib.metadata.version("dear-job-seeker")
    except importlib.metadata.PackageNotFoundError:
        return None


def _running(conn) -> dict[str, dict]:
    """Per platform, the task it runs right now, as {task_id, keyword, city, since, pages, completing}; any process of
    this database may be the one running it."""
    out = {}
    for row in conn.execute("SELECT * FROM tasks WHERE status='running' ORDER BY id"):
        t = taskqueue.as_dict(row)
        stats = t["stats"] or {}
        out[t["provider"]] = {"task_id": t["id"], "keyword": t["payload"].get("keyword") or t["payload"].get("tenant"),
                              "city": t["payload"].get("city"), "since": t["locked_at"], "pages": stats.get("pages", 0),
                              "completing": taskqueue.summary("running", 0, stats).get("completing")}
    return out


def _queue(conn) -> dict[str, int]:
    counts = dict.fromkeys(("pending", "running", "waiting_login", "done", "dead"), 0)
    counts.update({r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")})
    return counts


def _browser_state() -> dict:
    """The browser a login window would start in (or why there is none), and what holds browser profiles right now."""
    try:
        found = browser.find_browser()
        hint = None if found else browser.no_browser_hint()
    except browser.BrowserUnavailable as e:  # no Chrome or Edge, and Playwright cannot say where its Chromium is
        found, hint = None, str(e)
    return {"executable": {"kind": found[0], "path": str(found[1])} if found else None, "hint": hint,
            "playwright": browser.ready(), "cdp_url_set": browser.cdp_url() is not None,
            "login_windows": auth.pending_logins(), "profiles_in_use": browser.profiles_in_use()}


def _problem(task) -> dict | None:
    """Why a platform's newest task failed or waits for a login, in plain words; None when it did neither."""
    from djs import results  # late: results imports this module
    if task is None or task["status"] not in ("dead", "waiting_login"):
        return None
    kind, reason = results.classify(task["last_error"])
    return {"task_id": task["id"], "status": task["status"], "kind": kind, "reason": reason}


def _platform(name: str, newest, cooling: list) -> dict:
    missing = providers.get(name).missing_env()
    return {"configured": not missing, "missing_env": missing, "cooling_down": cooling, "last_error": _problem(newest)}


def _platforms(conn) -> dict[str, dict]:
    """Per platform: its keys (names of the missing ones only), the exits that are cooling down, and its newest task's
    problem, if any."""
    newest = {r["provider"]: r for r in conn.execute(
        "SELECT * FROM tasks WHERE id IN (SELECT MAX(id) FROM tasks GROUP BY provider)")}
    cooling: dict[str, list] = {}
    for r in conn.execute("SELECT egress_id, provider, blocked_until FROM egress_health WHERE blocked_until>? "
                          "ORDER BY blocked_until", (time.time(),)):
        cooling.setdefault(r["provider"], []).append({"exit": egress.mask(r["egress_id"]), "until": r["blocked_until"]})
    return {name: _platform(name, newest.get(name), cooling.get(name, [])) for name in providers.names()}


def status(conn) -> dict:
    """What is going on, in one call: no network, well under a second. `workers` tells, per platform, whether THIS
    process's worker thread is alive and the task the platform runs right now (the app, an MCP server and `djs work`
    each run workers on the same database)."""
    running, alive = _running(conn), engine.workers()
    return {"version": app_version() or "dev", "data_dir": str(db.home()),
            "workers": {name: {"alive_in_this_process": alive[name], "running": running.get(name)} for name in alive},
            "queue": _queue(conn), "browser": _browser_state(),
            "logins": {row["provider"]: {"state": row["state"], "expires_at": row["expires_at"]} for row in login_overview()},
            "platforms": _platforms(conn)}
