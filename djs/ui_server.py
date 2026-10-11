"""Local web UI: stdlib HTTP server, one static page, JSON API over djs.service. Loopback only."""
from __future__ import annotations

import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from djs import db, engine, ui_api

UI = Path(__file__).with_name("ui")
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/app.css": ("app.css", "text/css")}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body: bytes, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode())

    def _allowed(self) -> bool:
        ok = {f"{h}:{self.server.server_address[1]}" for h in ("127.0.0.1", "localhost")}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in ok and (origin is None or urlsplit(origin).netloc in ok)

    def _handle(self, method):
        if not self._allowed():
            return self._json(403, {"error": "不允许的访问来源"})
        url = urlsplit(self.path)
        if method == "GET" and url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self._send(200, (UI / name).read_bytes(), ctype)
        route = ui_api.ROUTES.get((method, url.path))
        if route is None:
            return self._json(404, {"error": "找不到该页面"})
        conn = db.connect()
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._json(200, route(conn, parse_qs(url.query), json.loads(raw) if raw else {}))
        except (ValueError, KeyError) as e:
            self._json(400, {"error": str(e).strip("'\"")})
        except Exception as e:
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            conn.close()

    do_GET = lambda self: self._handle("GET")  # noqa: E731
    do_POST = lambda self: self._handle("POST")  # noqa: E731
    do_DELETE = lambda self: self._handle("DELETE")  # noqa: E731


def make_server(port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="djs ui")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)
    engine.start_worker()
    srv = make_server(args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"找岗位助手 running at {url} (Ctrl+C to quit)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
