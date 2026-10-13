"""UI JSON API: route table over djs.service and djs.results, plus the few UI-only behaviours (speed presets,
background login, exporting a table, showing files and folders)."""
from __future__ import annotations

import functools
import json
import re
import subprocess
import sys
import threading
import time
import webbrowser
from itertools import count
from pathlib import Path

import httpx

from djs import connect, db, log, providers, results, service, taskqueue, update

PRESETS = {"steady": 1.0, "standard": 0.7, "fast": 0.5}  # multipliers of each platform's default interval
REVEAL_LABEL = "在访达中显示" if sys.platform == "darwin" else "在文件夹中显示"
CLOSE_DELAY = 0.5   # seconds between the answer to 「安装并重启」 and the window closing
LOG = log.get("ui")
login_state: dict[str, dict] = {}
_result_read = False   # what the last install came to is shown once per start of the app


def _prefs_file() -> Path:
    return db.home() / "ui_prefs.json"


def _prefs() -> dict:
    f = _prefs_file()
    return json.loads(f.read_text()) if f.exists() else {}


def _save_prefs(**kw) -> None:
    """Merge these top-level keys into the preferences file."""
    _prefs_file().write_text(json.dumps({**_prefs(), **kw}))


def apply_preset(conn, key: str) -> dict:
    if key not in PRESETS:
        raise ValueError("未知的速度选项")
    for name in providers.names():
        p = providers.get(name)
        if key == "steady":  # clear the override -> back to the platform default
            conn.execute("UPDATE provider_settings SET interval=NULL WHERE provider=?", (name,))
        else:
            service.set_frequency(conn, name, interval=round(max(p.interval * PRESETS[key], p.min_interval), 2))
    _save_prefs(preset=key)
    return {"preset": key}


def _failed(raw: str) -> dict:
    kind, text = results.classify(raw)
    return {"state": "failed", "kind": kind, "error": text}


def start_login(conn, provider: str) -> dict:
    if login_state.get(provider, {}).get("state") == "running":
        return {"state": "running"}
    run = login_state[provider] = {"state": "running"}

    def work():
        c = db.connect()
        try:
            outcome = {"state": "ok"} if service.login(c, provider).get("logged_in") else _failed("")
        except Exception as e:
            outcome = _failed(str(e))
        finally:
            c.close()
        if login_state.get(provider) is run:  # a cancelled or replaced run must not overwrite the newer state
            login_state[provider] = outcome
    threading.Thread(target=work, daemon=True).start()
    return {"state": "running"}


def cancel_login(conn, provider: str) -> dict:
    """The user pressed cancel: the waiting window closes and the dialog's state is forgotten."""
    service.login_cancel(conn, provider)
    login_state.pop(provider, None)
    return {"state": "cancelled"}


def check_login(conn, a) -> dict:
    """Ask the platform whether the saved login still works; the page reads a failure to ask as bad network."""
    try:
        return service.login_check(conn, a["provider"])
    except httpx.HTTPError as e:
        raise ValueError(results.friendly_error(f"{type(e).__name__}: {e}")) from e


def provider_rows(conn, a) -> dict:
    """service.provider_list plus the names (never the values) of the environment variables each platform needs, and
    the login overview (one read of the login state serves both)."""
    logins = service.login_overview()
    rows = [{**p, "requires_env": list(providers.get(p["name"]).requires_env)} for p in service.provider_list(conn, logins)]
    return {"providers": rows, "logins": logins, **_prefs()}


def crawl(conn, a) -> dict:
    """One task per chosen site that can take the search; the engine completes each job that still lacks a core
    element (detail 'auto'). `skipped`: the sites that could not, each with its reason in plain Chinese."""
    keyword, city = service.search_key(a.get("keyword"), a.get("city"))
    sites = a.get("providers") or []
    if not keyword:
        raise ValueError("请先填写岗位关键词")
    if not sites:
        raise ValueError("请至少选择一个网站")
    try:
        started = service.crawl_many(conn, sites, keyword, city or None)
    except Exception as e:  # the service words its refusals in English; the page shows Chinese only
        raise ValueError(results.friendly_error(str(e))) from e
    return {"task_ids": started["task_ids"],
            "skipped": [{**s, "reason": results.skip_reason(s["reason"], city)} for s in started["skipped"]]}


def searches(conn, a) -> dict:
    return {"searches": results.annotate_searches(conn, service.searches(conn, a.get("limit") or 20))}


def search_jobs(conn, a) -> dict:
    return results.search_jobs(conn, a.get("keyword"), a.get("city"), a.get("provider"), a.get("sort") or "mixed",
                               a.get("limit") or 30, a.get("offset") or 0, a.get("complete_only", True),
                               a.get("min_month_salary"))


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
    """A proxy for every site, typed as the user's proxy app shows it (egress.normalize); "direct" is no proxy."""
    url = a.get("url") or ""
    if url.strip() != "direct":   # "direct" is no proxy at all
        try:
            return service.egress_add(conn, url, "*")
        except ValueError:
            pass
    raise ValueError("代理地址的格式不对，例如 http://127.0.0.1:7890")


def about(conn, a) -> dict:
    now = update.current()
    return {"home": str(db.home()), "version": service.app_version() or "开发版",   # no version: a source checkout
            "latest": now["latest"], "update_state": now["state"]}


def _show(command: list[str]) -> dict:
    """Run the system's file manager / browser opener; a machine without one is not worth an error."""
    try:
        subprocess.Popen(command)
    except OSError:
        pass
    return {"ok": True}


def open_home(conn, a) -> dict:
    """Show the data folder in the system file manager."""
    return _show([{"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open"), str(db.home())])


def open_logs(conn, a) -> dict:
    """Show the folder of the log file in the system file manager (made first: a log that was never written has none)."""
    folder = log.path().parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return _show([{"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open"), str(folder)])


def open_url(conn, a) -> dict:
    url = a.get("url", "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("只能打开网页链接")
    webbrowser.open(url)
    return {"ok": True}


# ---- connecting the AI assistants on this computer
def connect_prefs() -> dict:
    """`enabled`: the assistants switched on (written again at every start); with nothing saved, all of them. A 1.2.6
    file (`{on, asked}`) counts as a saved choice once the user answered or switched something on."""
    saved = _prefs().get("connect") or {}
    known = {t.key for t in connect.TARGETS}
    if "enabled" in saved:
        keys = saved["enabled"]
    elif saved.get("asked") or saved.get("on"):
        keys = saved.get("on", [])
    else:
        return {"enabled": [t.key for t in connect.TARGETS]}
    return {"enabled": [k for k in keys if k in known]}


def connect_state(conn=None, a=None) -> dict:
    return {**connect_prefs(), **connect.status()}


def connect_set(conn, a) -> dict:
    """One assistant's switch: write or remove our entry there, and remember the choice."""
    key = a.get("key") or ""
    connect.find(key)   # an unknown one changes nothing, not even the saved choice
    enabled = connect_prefs()["enabled"]
    if a.get("on"):
        connect.connect(key)
        _save_prefs(connect={"enabled": [*enabled, key] if key not in enabled else enabled})
    else:
        _save_prefs(connect={"enabled": [k for k in enabled if k != key]})   # first: a file we cannot read must not keep the switch on
        connect.disconnect(key)
    return connect_state()


# ---- updating the app (the work is djs.update's; these only carry it to the window)
def _update_route(fn):
    """An update route that fails leaves a line in the log; the window only gets the plain Chinese answer."""
    @functools.wraps(fn)
    def route(conn, a):
        try:
            return fn(conn, a)
        except Exception as e:
            LOG.warning("%s failed: %s", fn.__name__, e, exc_info=not isinstance(e, ValueError))
            raise
    return route


def _update_now() -> dict:
    ok, reason = update.can_install()
    return {**update.current(), "can_install": ok, "reason": reason}


@_update_route
def update_state(conn, a) -> dict:
    """The state of the update, and whether it can be installed here. `result` (the text of how the last install went)
    comes with the first answer after the app started and never again."""
    global _result_read
    state = {**_update_now(), "result": None}
    if not _result_read:
        _result_read = True
        last = update.last_result()
        state["result"] = last["text"] if last else None
    return state


@_update_route
def update_check(conn, a) -> dict:
    """The user asked: look now. `found`: an update is downloading or ready."""
    return {"found": bool(update.check(manual=True)["found"]), **_update_now()}


@_update_route
def update_later(conn, a) -> dict:
    update.later()
    return _update_now()


@_update_route
def update_reveal(conn, a) -> dict:
    """Show the downloaded build in the file manager, for an app that cannot be replaced in place (the user moves it by
    hand)."""
    build = update.staged()
    if build is None:
        raise ValueError(update.WHY_NOT_READY)
    command = {"darwin": ["open", "-R", str(build)], "win32": ["explorer", f"/select,{build}"]}
    return _show(command.get(sys.platform, ["xdg-open", str(build.parent)]))


def _close_window() -> None:
    """Close the app's window a moment from now, so that the answer to the page goes first; webview.start() then returns
    and main() leaves the way it always does (the update is applied there)."""
    def close():
        try:
            import webview
            for window in list(webview.windows):
                window.destroy()
        except Exception:
            LOG.exception("could not close the window to install the update")
    timer = threading.Timer(CLOSE_DELAY, close)
    timer.daemon = True
    timer.start()


@_update_route
def update_install(conn, a) -> dict:
    """Install the downloaded update: the app quits and the new build takes its place. ValueError says why it cannot."""
    update.install_now()
    _close_window()
    return {"ok": True}


# ---- exporting a table: a file in the Downloads folder, no save dialog
def downloads() -> Path:
    return Path.home() / "Downloads"


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M")


def _name_part(text: str) -> str:
    """A piece of a file name: nothing a file system refuses, not too long."""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text).strip(" .")[:40] or "_"


def export_csv(conn, a) -> dict:
    """Write the table the page built into the Downloads folder as 找岗位-<关键词>-<城市>-<日期时间>.csv (never over an
    older file) and say where it is."""
    text = a.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("没有可以导出的内容")
    base = "-".join(_name_part(p) for p in ("找岗位", a.get("keyword") or "", a.get("city") or "全国", stamp()))
    try:
        downloads().mkdir(parents=True, exist_ok=True)
        for n in count(1):
            path = downloads() / (f"{base}.csv" if n == 1 else f"{base}-{n}.csv")
            try:
                with open(path, "x", encoding="utf-8", newline="") as f:  # newline="": keep the CSV's own \r\n
                    f.write(text)
                return {"path": str(path), "reveal": REVEAL_LABEL}
            except FileExistsError:
                continue
    except OSError as e:
        raise ValueError("没能保存到「下载」文件夹，请检查它的权限") from e


def reveal(conn, a) -> dict:
    """Show an exported file in the file manager (Finder / Explorer; elsewhere its folder). Only what export_csv wrote."""
    path = Path(a.get("path") or "")
    if path.parent.resolve() != downloads().resolve() or not path.is_file():
        raise ValueError("找不到这个文件")
    command = {"darwin": ["open", "-R", str(path)], "win32": ["explorer", f"/select,{path}"]}
    return _show(command.get(sys.platform, ["xdg-open", str(path.parent)]))


ROUTES = {
    "GET /api/providers": provider_rows,
    "GET /api/searches": searches,
    "GET /api/search_jobs": search_jobs,
    "POST /api/job_detail": job_detail,
    "POST /api/crawl": crawl,
    "POST /api/retry": retry,
    "POST /api/open_url": open_url,
    "POST /api/open_home": open_home,
    "POST /api/open_logs": open_logs,
    "POST /api/export_csv": export_csv,
    "POST /api/reveal": reveal,
    "GET /api/about": about,
    "GET /api/connect": connect_state,
    "POST /api/connect": connect_set,
    "GET /api/update": update_state,
    "POST /api/update_check": update_check,
    "POST /api/update_later": update_later,
    "POST /api/update_install": update_install,
    "POST /api/update_reveal": update_reveal,
    "POST /api/login": lambda c, a: start_login(c, a.get("provider", "")),
    "POST /api/login_cancel": lambda c, a: cancel_login(c, a.get("provider", "")),
    "POST /api/login_check": check_login,
    "GET /api/login_status": lambda c, a: {**service.login_status(c, a["provider"]), "login": login_state.get(a["provider"], {})},
    "POST /api/logout": lambda c, a: service.logout(c, a.get("provider", "")),
    "POST /api/frequency_preset": lambda c, a: apply_preset(c, a.get("preset", "")),
    "GET /api/egress": lambda c, a: {"egress": service.egress_list(c)},
    "POST /api/egress": add_proxy,
    "DELETE /api/egress": lambda c, a: service.egress_remove(c, a.get("url", "")),
}
