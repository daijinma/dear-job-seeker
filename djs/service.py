"""Tool-level operations (one function per MCP tool). server.py only wraps these."""
from __future__ import annotations

import json

from djs import auth, egress, pacer, providers, taskqueue

JOB_COLS = ("id, provider, source_job_id, task_id, title, company, city, salary_text, description, source_url, "
            "published_at, published_at_kind, detail_error, first_seen_at, last_seen_at")


def provider_list(conn) -> list[dict]:
    out = []
    for name in providers.names():
        p = providers.get(name)
        interval, jitter, max_pages = pacer.effective(conn, name)
        d = {"name": name, "kind": p.kind, "auth": p.auth, "requires_city": p.requires_city,
             "interval": interval, "jitter": jitter, "max_pages": max_pages,
             "min_interval": p.min_interval, "page_cap": p.page_cap, "note": p.note}
        if p.auth == "login":
            d["logged_in"] = auth.status(name)["logged_in"]
        out.append(d)
    return out


def crawl_start(conn, provider, keyword=None, city=None, tenant=None, max_pages=None) -> dict:
    payload = {k: v for k, v in (("keyword", keyword), ("city", city), ("tenant", tenant), ("max_pages", max_pages))
               if v is not None}
    return {"task_id": taskqueue.create(conn, provider, payload)}


def task_list(conn, status=None, limit=20) -> list[dict]:
    return taskqueue.list_(conn, status, limit)


def jobs_get(conn, task_id=None, provider=None, limit=50, with_detail=False) -> list[dict]:
    cols = JOB_COLS + (", detail" if with_detail else "")
    rows = conn.execute(
        f"SELECT {cols} FROM jobs WHERE (? IS NULL OR task_id=?) AND (? IS NULL OR provider=?) ORDER BY id DESC LIMIT ?",
        (task_id, task_id, provider, provider, limit)).fetchall()
    jobs = [dict(r) for r in rows]
    for j in jobs:
        if with_detail and j["detail"]:
            j["detail"] = json.loads(j["detail"])
    return jobs


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
