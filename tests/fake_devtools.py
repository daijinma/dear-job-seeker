"""A stand-in for a browser's DevTools endpoint, for the tests of the login window in djs.browser: the browser-level
websocket and /json/version on one port, answering the few commands the window uses. Either in a thread of the test
(DevTools), or as the "browser" a test starts as a process (main(): Chrome's flags in, a DevToolsActivePort file out)."""
from __future__ import annotations

import http
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from websockets.sync.server import serve


class DevTools:
    """`pages`: target id -> url of the open pages; `cookies`: the browser's cookie jar; `calls`: every (method, params)
    received. `noise` sends an event before each answer, as a real browser does; `deaf` answers Browser.close but goes
    on running."""

    def __init__(self, url: str = "about:blank", noise: bool = False, deaf: bool = False):
        self.guid, self.noise, self.deaf = uuid.uuid4().hex, noise, deaf
        self.pages, self.cookies, self.calls = {"page-1": url}, [], []
        self.gone, self._sessions = threading.Event(), []
        self.server = serve(self._session, "127.0.0.1", 0, process_request=self._http, max_size=None)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def path(self) -> str:
        return f"/devtools/browser/{self.guid}"

    @property
    def address(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.path}"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def quit(self) -> None:
        """The browser is gone: nothing listens, every connection is closed."""
        if self.gone.is_set():
            return
        self.gone.set()
        self.server.shutdown()
        for ws in list(self._sessions):
            ws.close()

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def _http(self, connection, request):
        if request.path == "/json/version":
            return connection.respond(http.HTTPStatus.OK, json.dumps({"Browser": "Fake/1", "webSocketDebuggerUrl": self.address}))
        if request.path != self.path:
            return connection.respond(http.HTTPStatus.NOT_FOUND, "")
        return None

    def _session(self, ws) -> None:
        self._sessions.append(ws)
        for raw in ws:
            msg = json.loads(raw)
            method, params = msg["method"], msg.get("params", {})
            self.calls.append((method, params))
            if self.noise:
                ws.send(json.dumps({"method": "Target.targetInfoChanged", "params": {}}))
            try:
                reply = {"id": msg["id"], "result": self._run(method, params)}
            except ValueError as e:
                reply = {"id": msg["id"], "error": {"code": -32000, "message": str(e)}}
            ws.send(json.dumps(reply))
            if method == "Browser.close" and not self.deaf:
                threading.Thread(target=self.quit, daemon=True).start()
                return

    def _run(self, method: str, params: dict) -> dict:
        if method == "Target.getTargets":
            pages = [{"targetId": i, "type": "page", "url": url, "attached": False} for i, url in self.pages.items()]
            return {"targetInfos": [*pages, {"targetId": "ui", "type": "browser_ui", "url": "chrome://omnibox-popup/",
                                             "attached": False}]}
        if method == "Target.createTarget":
            target = f"page-{len(self.pages) + 1}"
            self.pages[target] = params["url"]
            return {"targetId": target}
        if method == "Target.closeTarget":
            if self.pages.pop(params["targetId"], None) is None:
                raise ValueError("No target with given id found")
            return {"success": True}
        if method == "Storage.getCookies":
            return {"cookies": [dict(c) for c in self.cookies]}
        if method == "Storage.setCookies":  # a cookie set with an expiry in the past is how a browser drops one
            for new in params["cookies"]:
                self.cookies = [c for c in self.cookies if (c["name"], c["domain"], c["path"]) != (new["name"], new["domain"], new["path"])]
                if new.get("expires", float("inf")) > time.time():
                    self.cookies.append({"secure": False, "httpOnly": False, "expires": -1, **new})
            return {}
        if method == "Test.slow":
            time.sleep(params["seconds"])
            return {}
        if method == "Browser.close":
            return {}
        raise ValueError(f"'{method}' wasn't found")


def script(path: Path) -> Path:
    """An executable file at `path` that runs main(): what a test hands to djs.browser as the browser to start."""
    path.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(Path(__file__).parent)!r})\n"
                    "import fake_devtools\nfake_devtools.main(sys.argv[1:])\n")
    path.chmod(0o755)
    return path


HELPER = "import signal, time\n{ignore}time.sleep(120)\n"   # ends by itself after two minutes, whatever a test leaves behind


def main(argv: list[str]) -> None:
    """The "browser" of the process tests: Chrome's flags in, a DevToolsActivePort file out, until Browser.close.
    FAKE_BROWSER is a comma-separated list of misbehaviours: `exit` quits at once, `silent` never listens, `stuck` ignores
    Browser.close (SIGTERM ends it), `deaf` ignores SIGTERM too (only SIGKILL ends it), `helper` starts a helper process in
    its group (a real browser has renderers, a GPU process...) which SIGTERM ends and which a browser that quits is not
    waiting for, `stubborn-helper` the same with a helper that ignores SIGTERM. The helper's pid is written to
    <profile>/helper-pid."""
    modes = set(os.environ.get("FAKE_BROWSER", "").split(","))
    profile = Path(next(a.partition("=")[2] for a in argv if a.startswith("--user-data-dir=")))
    if "exit" in modes:
        return
    if "silent" in modes:
        time.sleep(3600)
    if "deaf" in modes:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    dev = DevTools(argv[-1], deaf=bool(modes & {"stuck", "deaf"}))
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "pid").write_text(str(os.getpid()))
    for kind in modes & {"helper", "stubborn-helper"}:
        ignore = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if kind == "stubborn-helper" else ""
        helper = subprocess.Popen([sys.executable, "-c", HELPER.format(ignore=ignore)])   # same group as this process
        (profile / "helper-pid").write_text(str(helper.pid))
    (profile / "DevToolsActivePort").write_text(f"{dev.port}\n{dev.path}\n")
    dev.gone.wait()
    (profile / "DevToolsActivePort").unlink(missing_ok=True)
