"""UI JSON API: route table over djs.service and djs.results, plus the few UI-only behaviours (speed presets,
background login, opening the data folder)."""
from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from djs import db, providers, results, service, taskqueue

PRESETS = {"steady": 1.0, "standard": 0.7, "fast": 0.5}  # multipliers of each platform's default interval
login_state: dict[str, dict] = {}


def _prefs_file() -> Path:
    return db.home() / "ui_prefs.json"


def _prefs() -> dict:
    f = _prefs_file()
    return json.loads(f.read_text()) if f.exists() else {}


def apply_preset(conn, key: str) -> dict:
    if key not in PRESETS:
        raise ValueError("未知的速度选项")
    for name in providers.names():
        p = providers.get(name)
        if key == "steady":  # clear the override -> back to the platform default
            conn.execute("UPDATE provider_settings SET interval=NULL WHERE provider=?", (name,))
        else:
            service.set_frequency(conn, name, interval=round(max(p.interval * PRESETS[key], p.min_interval), 2))
    _prefs_file().write_text(json.dumps({**_prefs(), "preset": key}))
    return {"preset": key}


def _failed(raw: str) -> dict:
    kind, text = results.classify(raw)
    return {"state": "failed", "kind": kind, "error": text}


def start_login(conn, provider: str) -> dict:
    if login_state.get(provider, {}).get("state") == "running":
        return {"state": "running"}
    login_state[provider] = {"state": "running"}

    def work():
        c = db.connect()
        try:
            login_state[provider] = {"state": "ok"} if service.login(c, provider).get("logged_in") else _failed("")
        except Exception as e:
            login_state[provider] = _failed(str(e))
        finally:
            c.close()
    threading.Thread(target=work, daemon=True).start()
    return {"state": "running"}


def provider_rows(conn, a) -> dict:
    """service.provider_list plus the names (never the values) of the environment variables each platform needs."""
    rows = [{**p, "requires_env": list(providers.get(p["name"]).requires_env)} for p in service.provider_list(conn)]
    return {"providers": rows, **_prefs()}


def crawl(conn, a) -> dict:
    """One quick-mode task (list items only) per chosen site; full text is fetched later, per job, on demand."""
    keyword = (a.get("keyword") or "").strip()
    sites = a.get("providers") or []
    if not keyword:
        raise ValueError("请先填写岗位关键词")
    if not sites:
        raise ValueError("请至少选择一个网站")
    city = (a.get("city") or "").strip() or None
    conn.execute("BEGIN")  # every chosen site starts, or none does
    try:
        ids = [service.crawl_start(conn, s, keyword=keyword, city=city, detail=False)["task_id"] for s in sites]
        conn.execute("COMMIT")
    except Exception as e:  # the service words its refusals in English; the page shows Chinese only
        conn.execute("ROLLBACK")
        raise ValueError(results.friendly_error(str(e))) from e
    return {"task_ids": ids}


def searches(conn, a) -> dict:
    return {"searches": results.annotate_searches(conn, service.searches(conn, a.get("limit") or 20))}


def search_jobs(conn, a) -> dict:
    return results.search_jobs(conn, a.get("keyword"), a.get("city"), a.get("provider"), a.get("sort") or "new",
                               a.get("limit") or 30, a.get("offset") or 0, a.get("complete_only") or False)


def job_detail(conn, a) -> dict:
    """The drawer's text, as plain text: the stored description when there is one (no network), else fetched once."""
    job_id = a.get("job_id")
    try:
        text = results.description(conn, job_id) or service.job_detail(conn, job_id)["description"]
    except Exception as e:  # whatever went wrong, the drawer shows plain Chinese
        raise ValueError(results.friendly_error(str(e))) from e
    return {"job": {"id": job_id, "description": results.plain_text(text)}}


def retry(conn, a) -> dict:
    return {"retried": taskqueue.requeue(conn, a.get("task_ids") or [])}


def add_proxy(conn, a) -> dict:
    url = (a.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("代理地址要以 http:// 或 https:// 开头")
    return service.egress_add(conn, url, "*")


def about(conn, a) -> dict:
    try:
        version = importlib.metadata.version("dear-job-seeker")
    except importlib.metadata.PackageNotFoundError:  # a source checkout that was never installed
        version = "开发版"
    return {"home": str(db.home()), "version": version}


def open_home(conn, a) -> dict:
    """Show the data folder in the system file manager; a machine without one is not worth an error."""
    opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
    try:
        subprocess.Popen([opener, str(db.home())])
    except OSError:
        pass
    return {"ok": True}


def open_url(conn, a) -> dict:
    url = a.get("url", "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("只能打开网页链接")
    webbrowser.open(url)
    return {"ok": True}


ROUTES = {
    "GET /api/providers": provider_rows,
    "GET /api/searches": searches,
    "GET /api/search_jobs": search_jobs,
    "POST /api/job_detail": job_detail,
    "POST /api/crawl": crawl,
    "POST /api/retry": retry,
    "POST /api/open_url": open_url,
    "POST /api/open_home": open_home,
    "GET /api/about": about,
    "POST /api/login": lambda c, a: start_login(c, a.get("provider", "")),
    "GET /api/login_status": lambda c, a: {**service.login_status(c, a["provider"]), "login": login_state.get(a["provider"], {})},
    "POST /api/logout": lambda c, a: service.logout(c, a.get("provider", "")),
    "POST /api/frequency_preset": lambda c, a: apply_preset(c, a.get("preset", "")),
    "GET /api/egress": lambda c, a: {"egress": service.egress_list(c)},
    "POST /api/egress": add_proxy,
    "DELETE /api/egress": lambda c, a: service.egress_remove(c, a.get("url", "")),
}
