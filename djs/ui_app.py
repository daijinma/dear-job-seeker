"""Desktop app: a native pywebview window; the page talks to Python through js_api (no HTTP, no socket)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from djs import db, engine, ui_api

UI_DIR = Path(__file__).with_name("ui")


def page() -> str:
    """index.html with CSS/JS inlined, so the window needs no file URL and pywebview starts no internal HTTP server."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    css = (UI_DIR / "app.css").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    return html.replace('<link rel="stylesheet" href="app.css">', f"<style>{css}</style>").replace(
        '<script src="app.js"></script>', f"<script>{js}</script>")


class Api:
    _window = None

    def call(self, route: str, payload: dict | None = None) -> dict:
        fn = ui_api.ROUTES.get(route)
        if fn is None:
            return {"error": "找不到该功能"}
        conn = db.connect()
        try:
            return fn(conn, payload or {})
        except (ValueError, KeyError) as e:
            return {"error": str(e).strip("'\"")}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            conn.close()

    def save_csv(self, filename: str, text: str) -> dict:
        import webview
        try:
            path = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=filename)
            if not path:
                return {}
            path = path if isinstance(path, str) else path[0]
            Path(path).write_text(text, encoding="utf-8")
            return {"saved": path}
        except Exception as e:
            return {"error": f"保存失败：{e}"}


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(prog="djs ui", description="Open the desktop app.").parse_args(argv)
    try:
        import webview
    except ImportError:
        print("pywebview is not installed: pip install 'dear-job-seeker[ui]'")
        sys.exit(1)
    engine.start_worker()
    api = Api()
    api._window = webview.create_window("找岗位助手", html=page(), js_api=api, width=1280, height=860, min_size=(960, 640))
    webview.start()
