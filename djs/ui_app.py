"""Desktop app: a native pywebview window; the page talks to Python through js_api (no HTTP, no socket)."""
from __future__ import annotations

import argparse
import sys
import threading
import traceback
from pathlib import Path

from djs import connect, db, engine, envfile, log, service, ui_api, update

UI_DIR = Path(__file__).with_name("ui")
LOG = log.get("app")
# One classic script, no modules: the files share a scope and run in this order (a file may only use, at load time,
# what an earlier one defined).
SCRIPTS = ("core.js", "components.js", "connect.js", "update.js", "login.js", "search.js", "results.js", "logins.js", "settings.js", "main.js")


def page() -> str:
    """index.html with CSS/JS inlined, so the window needs no file URL and pywebview starts no internal HTTP server."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    css = (UI_DIR / "app.css").read_text(encoding="utf-8")
    js = "\n".join((UI_DIR / name).read_text(encoding="utf-8") for name in SCRIPTS)
    return html.replace('<link rel="stylesheet" href="app.css">', f"<style>{css}</style>").replace(
        "<!-- scripts -->", f"<script>{js}</script>")


class Api:
    def call(self, route: str, payload: dict | None = None) -> dict:
        fn = ui_api.ROUTES.get(route)
        if fn is None:
            return {"error": "找不到该功能"}
        conn = db.connect()
        try:
            return fn(conn, payload or {})
        except ValueError as e:  # the routes word their refusals in plain Chinese
            return {"error": str(e)}
        except Exception:
            traceback.print_exc()  # for whoever started the app from a terminal; the window shows no exception names
            return {"error": "出了点问题，请稍后再试"}
        finally:
            conn.close()


def sync_connections() -> None:
    """The assistants switched on (all of them until the user chooses) get our entry written or refreshed (the app may
    have moved); never stops the app, a failure goes to the log and to the terminal that started it."""
    try:
        enabled = ui_api.connect_prefs()["enabled"]
        errors = connect.sync(enabled)
        LOG.info("assistants: %d switched on, %d could not be written", len(enabled), len(errors))
        for key, error in errors.items():
            print(f"djs: could not refresh the {key} connection: {error}", file=sys.stderr)
            LOG.warning("could not refresh the %s connection: %s", key, error)
    except Exception:
        traceback.print_exc()
        LOG.exception("the assistants' connections were not refreshed")


def main(argv: list[str] | None = None) -> None:
    log.setup()   # first: everything below can log
    LOG.info("starting %s (%s)", service.app_version() or "from source", sys.platform)
    envfile.load()
    argparse.ArgumentParser(prog="djs ui", description="Open the desktop app.").parse_args(argv)
    try:
        import webview
    except ImportError:
        print("pywebview is not installed: pip install 'dear-job-seeker[ui]'")
        sys.exit(1)
    engine.start_worker()
    service.start_login_check()
    threading.Thread(target=sync_connections, daemon=True).start()   # the window never waits for the config files
    update.start()   # checks in a thread of its own; nothing in a source checkout
    webview.create_window("找岗位助手", html=page(), js_api=Api(), width=1280, height=860, min_size=(960, 640))
    try:
        webview.start()
    finally:
        update.apply_on_exit()   # an update the user chose to install is put in place by a helper that starts here
        service.shutdown()
