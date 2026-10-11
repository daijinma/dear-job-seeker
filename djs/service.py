"""Tool-level operations (one function per MCP tool). server.py only wraps these."""
from __future__ import annotations

import functools
import json

import httpx

from djs import auth, browser, egress, engine, jobs, pacer, providers, taskqueue
from djs.normalize import CORE
from djs.providers.base import Blocked, FetchError, LoginRequired

JOB_COLS = ("id, provider, source_job_id, task_id, title, company, city, district, education, experience, "
            "salary_text, salary_min, salary_max, salary_unit, salary_months, description, source_url, "
            "published_at, published_at_kind, missing, complete, detail_error, first_seen_at, last_seen_at")


def _label(prov) -> str:
    return prov.label or prov.name


def provider_list(conn) -> list[dict]:
    out, browser_ready, cdp = [], browser.ready(), browser.cdp_url() is not None
    for name in providers.names():
        p = providers.get(name)
        interval, jitter, max_pages = pacer.effective(conn, name)
        d = {"name": name, "label": _label(p), "kind": p.kind, "auth": p.auth, "requires_city": p.requires_city,
             "interval": interval, "jitter": jitter, "max_pages": max_pages,
             "min_interval": p.min_interval, "page_cap": p.page_cap, "note": p.note,
             "transports": list(p.transports), "transport": pacer.transport(conn, name),
             "browser_ready": browser_ready, "cdp": cdp,
             "configured": not p.missing_env(), "missing_env": p.missing_env()}
        if p.auth != "public":
            d["logged_in"] = auth.status(name)["logged_in"]
        out.append(d)
    return out


def crawl_start(conn, provider, keyword=None, city=None, tenant=None, max_pages=None, detail="auto") -> dict:
    """detail: 'auto' requests the detail only for jobs still missing a core element, 'always' for every job without a
    stored detail, 'never' stores the list items only (job_detail loads one on demand). True / False mean always /
    never."""
    missing = providers.get(provider).missing_env()
    if missing:
        raise ValueError(f"{provider} needs environment variable(s) {', '.join(missing)}; "
                         "set them in the MCP client config env block (see README)")
    if pacer.transport(conn, provider) != "http" and not browser.ready():
        raise ValueError(browser.INSTALL_HINT)  # the task would only die later with the same message
    payload = {k: v for k, v in (("keyword", keyword), ("city", city), ("tenant", tenant), ("max_pages", max_pages),
                                 ("detail", detail)) if v is not None}
    return {"task_id": taskqueue.create(conn, provider, payload)}


def task_list(conn, status=None, limit=20) -> list[dict]:
    return taskqueue.list_(conn, status, limit)


def _load(row) -> dict:
    job = dict(row)
    job["missing"] = [name for name in (job["missing"] or "").split(",") if name]
    job["complete"] = bool(job["complete"])
    if job.get("detail"):
        job["detail"] = json.loads(job["detail"])
    return job


def _login_hides_salary(provider: str, logged_in) -> bool:
    try:
        return providers.get(provider).auth != "public" and not logged_in(provider)
    except KeyError:  # a platform that was removed after the job was stored
        return False


def _why(job: dict, element: str, logged_in) -> str:
    """Why one core element is absent from a job."""
    if element == "salary" and "面议" in (job.get("salary_text") or ""):
        return "面议"
    if element == "salary" and _login_hides_salary(job["provider"], logged_in):
        return "需登录才能看到薪资"
    if element == "description" and job.get("detail_error"):
        return "详情获取失败"
    return f"平台未提供{CORE[element]}"


def incomplete_reason(job: dict, logged_in=None) -> str | None:
    """Why a job lacks core elements, in a few Chinese words (None when nothing is missing). `job["missing"]` is the
    stored comma-joined text or a list; `logged_in(provider)` defaults to the saved login state."""
    names = job["missing"].split(",") if isinstance(job["missing"], str) else job["missing"]
    logged_in = logged_in or (lambda provider: auth.status(provider)["logged_in"])
    return "；".join(_why(job, name, logged_in) for name in names if name) or None


def _explained(rows: list[dict]) -> list[dict]:
    """Adds `incomplete_reason` to each job; the login state is asked once per platform, not once per job."""
    logged_in = functools.cache(lambda provider: auth.status(provider)["logged_in"])
    for job in rows:
        job["incomplete_reason"] = incomplete_reason(job, logged_in)
    return rows


def jobs_get(conn, task_id=None, provider=None, limit=50, with_detail=False, complete_only=True) -> list[dict]:
    """Stored jobs, newest first. By default only the complete ones (every core element present); complete_only=False
    adds the rest, each with `missing` and `incomplete_reason`."""
    cols = JOB_COLS + (", detail" if with_detail else "")
    rows = conn.execute(
        f"SELECT {cols} FROM jobs WHERE (? IS NULL OR id IN (SELECT job_id FROM task_jobs WHERE task_id=?))"
        " AND (? IS NULL OR provider=?) AND (? = 0 OR complete = 1) ORDER BY id DESC LIMIT ?",
        (task_id, task_id, provider, provider, int(bool(complete_only)), limit)).fetchall()
    return _explained([_load(r) for r in rows])


def _job(conn, job_id: int) -> dict:
    row = conn.execute(f"SELECT {JOB_COLS}, detail FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"job {job_id} not found")
    return _explained([_load(row)])[0]


def _friendly(prov, exc: Exception) -> str:
    label = _label(prov)
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
    if statuses & {"pending", "running"}:
        return "running"
    return "waiting_login" if "waiting_login" in statuses else "done"


def _site(name: str, task: dict, found: dict) -> dict:
    jobs_n, complete = found.get(name, (0, 0))
    return {"provider": name, "label": _label(providers.get(name)), "status": task["status"], "jobs": jobs_n,
            "complete": complete, "incomplete": jobs_n - complete, "last_error": task["last_error"]}


def _search(conn, keyword, city, tasks: list[dict]) -> dict:
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
            "per_site": [_site(name, t, found) for name, t in newest.items()]}


def searches(conn, limit=20) -> list[dict]:
    """Recent searches, newest first. Tasks with the same keyword and city belong to one search."""
    groups: dict[tuple, list[dict]] = {}
    for t in taskqueue.by_search(conn, limit):
        groups.setdefault((t["keyword"], t["city"]), []).append(t)
    return [_search(conn, keyword, city, tasks) for (keyword, city), tasks in groups.items()]


def login(conn, provider) -> dict:
    if providers.get(provider).auth == "public":
        raise ValueError(f"{provider} does not need login")
    status = auth.login(provider, timeout=180)
    resumed = taskqueue.resume_login(conn, provider) if status.get("logged_in") else 0
    return {**status, "resumed_tasks": resumed}


def login_status(conn, provider) -> dict:
    return auth.status(provider)


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
