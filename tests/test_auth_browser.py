"""auth.login with a real browser (headless instead of headed, so no window opens on the developer's screen) against a tiny
local site that sets its login cookie by script. What the fake window of test_auth.py cannot show: what a browser really
reports for cookies and expiries, that the saved cookies are the site's, that a leftover cookie of a dead session is really
cleared, that nothing is attached to the page and the site sees an ordinary browser, and that the browser process is really
gone (and the profile lock free again) after success, cancel, timeout, a window the user closed and the end of the app.
Skipped where no browser can be started."""
import json
import subprocess
import threading
import time
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from djs import auth, browser, providers, service
from djs.providers.base import Login, Provider


@pytest.fixture(scope="module")
def browser_works(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DJS_HOME", str(tmp_path_factory.mktemp("probe")))
        mp.delenv("DJS_CDP_URL", raising=False)
        try:
            with browser.plain_window("probe", "about:blank", headless=True):
                pass
        except Exception as e:   # no browser, no permission to start one, a sandbox...
            pytest.skip(f"No browser can be started here ({type(e).__name__}: {e})")


class Site(BaseHTTPRequestHandler):
    """/login sets cookie `wd` after 300 ms to what the page saw in navigator.webdriver, and then `tok` to the value of ?v=
    (never, with ?v=none): a login that sees `tok` is sure to see `wd` too. `loads` is set by the first page load."""
    loads = threading.Event()

    def do_GET(self):
        Site.loads.set()
        value = self.path.partition("v=")[2] or "SECRET"
        login = "" if value == "none" else f"document.cookie = 'tok={value}; max-age=3600; path=/';"
        script = f"setTimeout(() => {{ document.cookie = 'wd=' + navigator.webdriver + '; path=/'; {login} }}, 300)"
        body = f"<html><body>log in here<script>{script}</script></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def world(tmp_path, monkeypatch, browser_works):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.setattr(auth, "POLL_SECONDS", 0.05)
    real_window = browser.plain_window
    monkeypatch.setattr(browser, "plain_window", lambda name, url, headless=False: real_window(name, url, True))
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


@pytest.fixture
def started(monkeypatch):
    """Every browser process the window starts."""
    procs, real = [], subprocess.Popen

    def spy(*args, **kwargs):
        procs.append(real(*args, **kwargs))
        return procs[-1]

    monkeypatch.setattr(subprocess, "Popen", spy)
    return procs


def window_is_gone():
    """The browser profile can only be locked again once the window has been closed."""
    with browser.profile_lock("site", wait=0):
        return True


def never_logs_in(base):
    """The login page of the site, but a page that never sets the login cookie."""
    providers.get("site").login = Login(url=f"{base}/login?v=none", method="qr", domain="127.0.0.1", cookies_any=("tok",))


def login_in_thread(timeout=60):
    outcome = {}

    def run():
        try:
            outcome["result"] = auth.login("site", timeout=timeout)
        except BaseException as e:   # the thread reports what login raised
            outcome["error"] = e

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def wait_until(condition, seconds=30):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "gave up waiting"
        time.sleep(0.05)


@contextmanager
def second_connection():
    """Another websocket to the same browser, as any tool on the machine could open one: to look at it, and to close the
    window from outside the way the user would."""
    cdp = browser._Cdp(browser._devtools_address(auth.profile_dir("site") / "browser"))
    try:
        yield cdp
    finally:
        cdp.close()


def page_is_up(cdp):
    """The page has loaded and its script has run (it sets the `wd` cookie first)."""
    return any(c["name"] == "wd" for c in cdp.call("Storage.getCookies")["cookies"])


def first_page(cdp):
    return next(t["targetId"] for t in cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page")


def test_a_real_login_is_detected_saved_with_its_expiry_and_the_browser_is_gone(world, started):
    began = time.time()
    got = auth.login("site", timeout=30)
    assert (got["logged_in"], got["state"]) == (True, "logged_in")
    assert auth.load_cookies("site") == {"tok": "SECRET", "wd": "false"}   # what the site set, and it saw an ordinary browser
    saved = json.loads((auth.profile_dir("site") / "cookies.json").read_text())
    assert saved["expires"]["tok"] == pytest.approx(began + 3600, abs=60)   # max-age=3600 became a real expiry
    assert got["expires_at"] == pytest.approx(saved["expires"]["tok"])   # and the session ends with its login cookie
    [proc] = started
    assert proc.poll() is not None and window_is_gone()


def test_nothing_is_attached_to_the_page(world, started):
    never_logs_in(world)
    thread, outcome = login_in_thread()
    wait_until(lambda: (auth.profile_dir("site") / "browser" / "DevToolsActivePort").exists())
    with second_connection() as cdp:
        wait_until(lambda: page_is_up(cdp))
        pages = [t for t in cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        assert [(t["url"].startswith(world), t["attached"]) for t in pages] == [(True, False)]   # nobody is attached
    assert auth.cancel_login("site") is True
    thread.join(30)
    assert not thread.is_alive()


def test_a_cookie_left_by_a_dead_session_does_not_pass_for_a_login(world, started):
    with browser.plain_window("site", f"{world}/login?v=OLD") as window:   # the profile gets the old session's cookie
        wait_until(lambda: any(c["name"] == "tok" and c["value"] == "OLD" for c in window.cookies()))
    with browser.plain_window("site", "about:blank") as window:
        assert [c["value"] for c in window.cookies() if c["name"] == "tok"] == ["OLD"]   # and it is really kept
    auth.save_cookies("site", {"tok": "OLD"})
    auth.invalidate("site")   # the platform told us it is dead
    assert auth.status("site")["state"] == "expired"
    got = auth.login("site", timeout=30)   # the new login page sets SECRET; OLD must not be taken for it
    assert got["state"] == "logged_in" and auth.load_cookies("site")["tok"] == "SECRET"
    assert window_is_gone() and all(p.poll() is not None for p in started)


def test_cancel_really_closes_the_window(world, started):
    never_logs_in(world)
    thread, outcome = login_in_thread()
    assert Site.loads.wait(30)   # the window is open and showing the page
    with pytest.raises(browser.BrowserBusy):
        window_is_gone()   # while the login waits, the window owns the profile
    assert auth.cancel_login("site") is True
    thread.join(30)
    assert not thread.is_alive() and isinstance(outcome["error"], auth.LoginCancelled)
    assert started[0].poll() is not None and window_is_gone() and auth.status("site")["state"] == "not_logged_in"


def test_a_login_that_times_out_closes_the_window(world, started):
    never_logs_in(world)
    with pytest.raises(TimeoutError, match="秒内没有登录成功，请再试一次"):
        auth.login("site", timeout=1.5)
    assert started[0].poll() is not None and window_is_gone()


@pytest.mark.parametrize("what", ["its page", "the whole browser"])
def test_a_window_the_user_closes_ends_the_login_and_the_browser_goes(world, started, what):
    never_logs_in(world)
    thread, outcome = login_in_thread()
    wait_until(lambda: (auth.profile_dir("site") / "browser" / "DevToolsActivePort").exists())
    with second_connection() as cdp:
        wait_until(lambda: page_is_up(cdp))
        if what == "its page":
            cdp.call("Target.closeTarget", targetId=first_page(cdp))
        else:
            with suppress(browser.BrowserGone):
                cdp.call("Browser.close")
    thread.join(30)
    assert not thread.is_alive() and str(outcome["error"]) == "登录窗口被关闭了，请重新登录"
    assert isinstance(outcome["error"], RuntimeError) and started[0].poll() is not None and window_is_gone()


def test_shutting_down_cancels_a_pending_login_and_leaves_no_browser(world, started):
    never_logs_in(world)
    thread, outcome = login_in_thread()
    wait_until(lambda: auth.pending_logins() and Site.loads.is_set())
    assert [p["provider"] for p in auth.pending_logins()] == ["site"]
    service.shutdown()
    assert auth.pending_logins() == [] and started[0].poll() is not None   # gone by the time shutdown returns
    thread.join(5)
    assert isinstance(outcome["error"], auth.LoginCancelled) and window_is_gone()


def test_a_browser_left_by_a_crashed_run_is_closed_before_the_next_window_opens(world, started):
    profile = auth.profile_dir("site", create=True) / "browser"
    orphan = subprocess.Popen([str(browser.executable()), f"--user-data-dir={profile}", "--remote-debugging-port=0",
                               *browser.WINDOW_FLAGS, "--headless=new", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)   # nobody owns it any more
    try:
        wait_until(lambda: browser._devtools_address(profile) is not None)
        assert auth.login("site", timeout=30)["state"] == "logged_in"   # starts a browser on that very profile
        assert orphan.poll() is not None   # the one left over was closed first
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait()
    assert all(p.poll() is not None for p in started) and window_is_gone()


def test_the_cookie_a_login_window_leaves_in_the_profile_is_readable_by_the_crawl_browser(world):
    auth.login("site", timeout=30)   # the window set tok=SECRET (an hour of life) in the shared profile and closed
    with browser.open_browser(providers.get("site"), "direct", 0.0, 0.0) as client:   # the crawl: same profile, same flags
        assert client.cookie_value("tok", world) == "SECRET"   # so it can decrypt what the login stored
        assert {c["name"]: c["value"] for c in client.cookies(world)} == {"tok": "SECRET"}   # (wd was a session cookie: it went with the window)
    assert window_is_gone()


def test_in_the_users_own_browser_the_login_is_one_tab_and_their_browser_stays(world, tmp_path, monkeypatch):
    users = tmp_path / "users-chrome"   # a browser of "the user's": not ours, started by hand with a debugging port
    proc = subprocess.Popen([str(browser.executable()), f"--user-data-dir={users}", "--remote-debugging-port=0",
                             "--no-first-run", "--use-mock-keychain", "--password-store=basic", "--headless=new", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(lambda: browser._devtools_address(users) is not None)
        address = browser._devtools_address(users)
        monkeypatch.setenv("DJS_CDP_URL", f"http://127.0.0.1:{urlsplit(address).port}")
        assert auth.login("site", timeout=30)["state"] == "logged_in"
        assert auth.load_cookies("site")["tok"] == "SECRET"
        assert proc.poll() is None   # their browser was not closed...
        cdp = browser._Cdp(address)
        pages = [t["url"] for t in cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        cdp.close()
        assert pages == ["about:blank"]   # ...and only the tab of the login went
        assert not (auth.profile_dir("site") / "browser").exists()   # no profile of ours was started
    finally:
        proc.terminate()
        proc.wait(10)
