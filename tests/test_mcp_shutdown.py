"""A client that goes away while a login window is open (it closes the connection, or stops the server with SIGTERM): the
MCP server closes the window and exits at once, instead of staying alive for as long as the `login` call would wait.
The server is the real one, over stdio; the platform and the browser are fakes (tests/fake_devtools.py)."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

import fake_devtools

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake browser is a script")

SERVER = """
from pathlib import Path
from djs import browser, providers
from djs.providers.base import Login, Provider

browser.find_browser = lambda: ('chrome', Path({browser!r}))
spec = Login(url='https://site.test/login', method='qr', domain='site.test', cookies_any=('tok',))
site = type('site', (Provider,), {{'name': 'site', 'label': 'site', 'auth': 'optional', 'login': spec}})()
providers.get = lambda n: site if n == 'site' else (_ for _ in ()).throw(KeyError(n))
providers.names = lambda: ['site']
from djs import server
server.main()
"""


def send(server, message):
    server.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    server.stdin.flush()


@pytest.mark.parametrize("how", ["closes the connection", "sends SIGTERM"])
def test_a_client_that_goes_away_during_a_login_gets_its_window_closed(tmp_path, how):
    home, cwd = tmp_path / "home", tmp_path / "cwd"   # cwd: the server autoloads ./.env, which must not be a real one
    cwd.mkdir()
    code = tmp_path / "server.py"
    code.write_text(SERVER.format(browser=str(fake_devtools.script(tmp_path / "fake-chrome"))))
    server = subprocess.Popen([sys.executable, str(code)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, cwd=cwd, env={**os.environ, "DJS_HOME": str(home)})
    pid = None
    try:
        send(server, {"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        assert json.loads(server.stdout.readline())["id"] == 1
        send(server, {"method": "notifications/initialized"})
        send(server, {"id": 2, "method": "tools/call", "params": {"name": "login", "arguments": {"provider": "site"}}})
        pid_file = home / "profiles" / "site" / "browser" / "pid"
        deadline = time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(pid_file.read_text())   # the login window is open: a browser process is running
        os.kill(pid, 0)
        if how == "sends SIGTERM":
            server.send_signal(signal.SIGTERM)
        else:
            server.stdin.close()
        assert server.wait(30) == 0   # the server follows at once; it does not wait for the login to time out
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)   # and the window went with it
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)   # only if the test failed: no fake browser left behind
            except ProcessLookupError:
                pass
