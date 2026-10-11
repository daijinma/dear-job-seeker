"""Tool-level operations (one function per MCP tool). server.py only wraps these."""
from __future__ import annotations

import json

import httpx

from djs import auth, egress, engine, pacer, providers, taskqueue
from djs.providers.base import Blocked, FetchError, LoginRequired

JOB_COLS = ("id, provider, source_job_id, task_id, title, company, city, salary_text, description, source_url, "
            "published_at, published_at_kind, detail_error, first_seen_at, last_seen_at")


def _label(prov) -> str:
    return prov.label or prov.name


def provider_list(conn) -> list[dict]:
    out = []
    for name in providers.names():
        p = providers.get(name)
        interval, jitter, max_pages = pacer.effective(conn, name)
        d = {"name": name, "label": _label(p), "kind": p.kind, "auth": p.auth, "requires_city": p.requires_city,
             "interval": interval, "jitter": jitter, "max_pages": max_pages,
             "min_interval": p.min_interval, "page_cap": p.page_cap, "note": p.note,
             "configured": not p.missing_env(), "missing_env": p.missing_env()}
        if p.auth == "login":
            d["logged_in"] = auth.status(name)["logged_in"]
        out.append(d)
    return out


def crawl_start(conn, provider, keyword=None, city=None, tenant=None, max_pages=None, detail=True) -> dict:
    """detail=False is quick mode: store the list items only; job_detail fetches a job's full text on demand."""
    missing = providers.get(provider).missing_env()
    if missing:
        raise ValueError(f"{provider} needs environment variable(s) {', '.join(missing)}; "
                         "set them in the MCP client config env block (see README)")
    payload = {k: v for k, v in (("keyword", keyword), ("city", city), ("tenant", tenant), ("max_pages", max_pages),
                                 ("detail", detail)) if v is not None}
    return {"task_id": taskqueue.create(conn, provider, payload)}


def task_list(conn, status=None, limit=20) -> list[dict]:
    return taskqueue.list_(conn, status, limit)


def _load(row) -> dict:
    job = dict(row)
    if job.get("detail"):
        job["detail"] = json.loads(job["detail"])
    return job


def jobs_get(conn, task_id=None, provider=None, limit=50, with_detail=False) -> list[dict]:
    cols = JOB_COLS + (", detail" if with_detail else "")
    rows = conn.execute(
        f"SELECT {cols} FROM jobs WHERE (? IS NULL OR id IN (SELECT job_id FROM task_jobs WHERE task_id=?))"
        " AND (? IS NULL OR provider=?) ORDER BY id DESC LIMIT ?",
        (task_id, task_id, provider, provider, limit)).fetchall()
    return [_load(r) for r in rows]


def _job(conn, job_id: int) -> dict:
    row = conn.execute(f"SELECT {JOB_COLS}, detail FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"job {job_id} not found")
    return _load(row)


def _friendly(prov, exc: Exception) -> str:
    label = _label(prov)
    if isinstance(exc, LoginRequired):
        return f"{label}: login required; log in and try again"
    if isinstance(exc, Blocked):
        return f"{label} is limiting requests right now; try again later"
    return f"{label}: could not load the detail ({type(exc).__name__}: {exc})"


def job_detail(conn, job_id: int) -> dict:
    """The stored job with its full detail. Fetched from the platform once (same egress and pacing as a crawl),
    then served from the database."""
    job = _job(conn, job_id)
    if job["detail"] is not None:
        return job
    prov = providers.get(job["provider"])
    item = json.loads(conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
    try:
        with engine.leased_client(conn, prov, f"job:{job_id}") as client:
            detail, err = engine.fetch_detail(client, prov, item)
        if err:
            raise err
    except egress.NoEgressAvailable as e:
        raise ValueError(str(e)) from e
    except (FetchError, httpx.HTTPError) as e:
        raise ValueError(_friendly(prov, e)) from e
    engine.update_job(conn, job_id, prov, item, detail)
    return _job(conn, job_id)


def _search_status(statuses: set[str]) -> str:
    if statuses & {"pending", "running"}:
        return "running"
    return "waiting_login" if "waiting_login" in statuses else "done"


def _search(conn, keyword, city, tasks: list[dict]) -> dict:
    ids = [t["id"] for t in tasks]
    found = {r[0]: r[1] for r in conn.execute(
        "SELECT t.provider, COUNT(DISTINCT j.job_id) FROM task_jobs j JOIN tasks t ON t.id=j.task_id "
        f"WHERE j.task_id IN ({','.join('?' * len(ids))}) GROUP BY t.provider", ids)}
    newest = {t["provider"]: t for t in tasks}  # tasks arrive in id order, so the last one per platform is the newest
    return {"keyword": keyword, "city": city or None, "task_ids": ids,
            "created_at": max(t["created_at"] for t in tasks),
            "status": _search_status({t["status"] for t in tasks}),
            "jobs_total": sum(found.values()),
            "per_site": [{"provider": name, "label": _label(providers.get(name)), "status": t["status"],
                          "jobs": found.get(name, 0), "last_error": t["last_error"]} for name, t in newest.items()]}


def searches(conn, limit=20) -> list[dict]:
    """Recent searches, newest first. Tasks with the same keyword and city belong to one search."""
    groups: dict[tuple, list[dict]] = {}
    for t in taskqueue.by_search(conn, limit):
        groups.setdefault((t["keyword"], t["city"]), []).append(t)
    return [_search(conn, keyword, city, tasks) for (keyword, city), tasks in groups.items()]


def login(conn, provider) -> dict:
    if providers.get(provider).auth != "login":
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


def egress_add(conn, url, provider="*") -> dict:
    return egress.add(conn, url, provider)


def egress_remove(conn, url) -> dict:
    return {"removed": egress.remove(conn, url)}


def egress_list(conn) -> list[dict]:
    return egress.list_(conn)
