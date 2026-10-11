"""UI JSON API: route table over djs.service plus the few UI-only behaviours (speed presets, background login)."""
from __future__ import annotations

import json
import threading
import webbrowser
from pathlib import Path

from djs import db, providers, service

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


def start_login(conn, provider: str) -> dict:
    if login_state.get(provider, {}).get("state") == "running":
        return {"state": "running"}
    login_state[provider] = {"state": "running"}

    def work():
        c = db.connect()
        try:
            r = service.login(c, provider)
            login_state[provider] = {"state": "ok" if r.get("logged_in") else "failed", "error": None}
        except Exception as e:
            login_state[provider] = {"state": "failed", "error": str(e)}
        finally:
            c.close()
    threading.Thread(target=work, daemon=True).start()
    return {"state": "running"}


def crawl(conn, a) -> dict:
    keyword = (a.get("keyword") or "").strip()
    sites = a.get("providers") or []
    if not keyword:
        raise ValueError("请先填写岗位关键词")
    if not sites:
        raise ValueError("请至少选择一个网站")
    city = (a.get("city") or "").strip() or None
    return {"task_ids": [service.crawl_start(conn, s, keyword=keyword, city=city)["task_id"] for s in sites]}


def open_url(conn, a) -> dict:
    url = a.get("url", "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("只能打开网页链接")
    webbrowser.open(url)
    return {"ok": True}


ROUTES = {
    "GET /api/providers": lambda c, a: {"providers": service.provider_list(c), **_prefs()},
    "GET /api/tasks": lambda c, a: {"tasks": service.task_list(c, limit=60)},
    "GET /api/jobs": lambda c, a: {"jobs": service.jobs_get(c, a.get("task_id"), a.get("provider"), a.get("limit") or 500)},
    "POST /api/crawl": crawl,
    "POST /api/open_url": open_url,
    "POST /api/login": lambda c, a: start_login(c, a.get("provider", "")),
    "GET /api/login_status": lambda c, a: {**service.login_status(c, a["provider"]), "login": login_state.get(a["provider"], {})},
    "POST /api/logout": lambda c, a: service.logout(c, a.get("provider", "")),
    "POST /api/frequency_preset": lambda c, a: apply_preset(c, a.get("preset", "")),
    "GET /api/egress": lambda c, a: {"egress": service.egress_list(c)},
    "POST /api/egress": lambda c, a: service.egress_add(c, (a.get("url") or "").strip(), "*"),
    "DELETE /api/egress": lambda c, a: service.egress_remove(c, a.get("url", "")),
}
