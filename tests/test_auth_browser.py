"""auth.login with a real Chromium (headless instead of headed, so no window opens on the developer's screen) against a
tiny local site that sets its login cookie by script. What the fake page of test_auth.py cannot show: what Playwright
really reports for cookies and expiries, that the saved cookies are the site's, that a leftover cookie of a dead
session is really cleared, and that the window is really gone (the profile lock is free again) after success and
after cancel. Skipped where Playwright's browser is not available."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from djs import auth, browser, providers
from djs.providers.base import Login, Provider

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def chromium_works():
    try:
        with sync_api.sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as e:  # no driver, no browser download, a sandbox that forbids launching...
        pytest.skip(f"Chromium cannot be launched here ({type(e).__name__})")


class Site(BaseHTTPRequestHandler):
    """/login sets cookie `tok` after 300 ms to the value of ?v= (never, with ?v=none); /seen counts the page loads."""
    loads = threading.Event()

    def do_GET(self):
        Site.loads.set()
        value = self.path.partition("v=")[2] or "SECRET"
        script = "" if value == "none" else f"setTimeout(() => {{ document.cookie = 'tok={value}; max-age=3600; path=/'; }}, 300)"
        body = f"<html><body>log in here<script>{script}</script></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def world(tmp_path, monkeypatch, chromium_works):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.setattr(browser, "CHANNELS", (None,))   # Playwright's own Chromium, whatever else is installed
    monkeypatch.setattr(auth, "POLL_SECONDS", 0.05)
    real_open_page = browser.open_page
    monkeypatch.setattr(browser, "open_page", lambda name, headless, proxy=None: real_open_page(name, True, proxy))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    spec = Login(url=f"{base}/login", method="qr", domain="127.0.0.1", cookies_any=("tok",), valid_days=3)
    site = type("site", (Provider,), {"name": "site", "label": "site", "auth": "optional", "login": spec})()
    monkeypatch.setattr(providers, "get", lambda n: site if n == "site" else (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(providers, "names", lambda: ["site"])
    Site.loads.clear()
    yield base
    server.shutdown()


def window_is_gone():
    """The browser profile can only be locked again once the window has been closed."""
    with browser.profile_lock("site", wait=0):
        return True


def test_a_real_login_is_detected_saved_with_its_expiry_and_the_window_closes(world):
    started = time.time()
    got = auth.login("site", timeout=30)
    assert (got["logged_in"], got["state"]) == (True, "logged_in")
    assert auth.load_cookies("site") == {"tok": "SECRET"}   # what the site set, nothing else
    saved = json.loads((auth.profile_dir("site") / "cookies.json").read_text())
    assert saved["expires"]["tok"] == pytest.approx(started + 3600, abs=60)   # max-age=3600 became a real expiry
    assert got["expires_at"] == pytest.approx(saved["expires"]["tok"])   # and the session ends with its login cookie
    assert window_is_gone()


def test_a_cookie_left_by_a_dead_session_does_not_pass_for_a_login(world):
    with browser.open_page("site", headless=False) as page:   # the profile holds the old session's cookie
        page.goto(f"{world}/login?v=OLD")
        page.wait_for_function("document.cookie.includes('tok=OLD')")
    auth.save_cookies("site", {"tok": "OLD"})
    auth.invalidate("site")   # the platform told us it is dead
    assert auth.status("site")["state"] == "expired"
    got = auth.login("site", timeout=30)   # the new login page sets NEW; OLD must not be taken for it
    assert got["state"] == "logged_in" and auth.load_cookies("site") == {"tok": "SECRET"}
    assert window_is_gone()


def test_cancel_really_closes_the_window(world):
    outcome = {}

    def run():
        try:
            auth.login("site", timeout=60)
        except BaseException as e:   # the thread reports what login raised
            outcome["error"] = e

    # the page never sets a cookie (/login?v=none is not the configured url: use a spec that points at it)
    providers.get("site").login = Login(url=f"{world}/login?v=none", method="qr", domain="127.0.0.1", cookies_any=("tok",))
    thread = threading.Thread(target=run)
    thread.start()
    assert Site.loads.wait(30)   # the window is open and showing the page
    time.sleep(0.3)
    with pytest.raises(browser.BrowserBusy):
        window_is_gone()   # while the login waits, the window owns the profile
    assert auth.cancel_login("site") is True
    thread.join(30)
    assert not thread.is_alive() and isinstance(outcome["error"], auth.LoginCancelled)
    assert window_is_gone() and auth.status("site")["state"] == "not_logged_in"
