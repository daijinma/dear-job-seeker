"""The crawl client of djs.browser against a real headless browser and a tiny local site (never a real website, never the real
data folder: every profile is in a temporary DJS_HOME). What the fake endpoint of test_browser_client.py cannot show: that a
page loaded by Page.navigate without Page.enable is really complete when goto returns, how a late redirect, a blank page, a
refused connection and a page that navigates away during a fetch end, that the profile keeps a token between two sessions and
the client reads, waits for and clears it, and that nothing a real browser starts survives the session. Skipped where no
browser can be started."""
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from djs import auth, browser, pacer

SITE = SimpleNamespace(name="site", needs_headed=False)
posix = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("ps"), reason="looks at the process list with ps")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CLOSED = free_port()   # nobody listens there


class Handler(BaseHTTPRequestHandler):
    """The pages of the site, by path. /slow?s=N answers after N seconds."""
    protocol_version = "HTTP/1.1"

    def send(self, body: bytes, status=200, kind="text/html; charset=utf-8", **headers):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def page(self, body: str, script: str = ""):
        self.send(f"<html><head><title>{body}</title></head><body>{body}<script>{script}</script></body></html>".encode())

    def respond(self):
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else None
        if url.path == "/page":
            self.page("你好", "document.cookie = 'tok=abc; path=/; max-age=3600'")
        elif url.path == "/echo":
            self.send(json.dumps({"method": self.command, "query": url.query, "body": body,
                                  "headers": {k.lower(): v for k, v in self.headers.items()}}).encode(), kind="application/json")
        elif url.path == "/missing":
            self.send(b'{"code": 404}', 404, "application/json")
        elif url.path == "/slow":
            time.sleep(float(parse_qs(url.query)["s"][0]))
            self.send(b"{}", kind="application/json")
        elif url.path == "/late-redirect":
            self.page("start", "setTimeout(() => location.replace('/final'), 700)")
        elif url.path == "/final":
            self.page("final")
        elif url.path == "/to-blank":
            self.page("leaving", "location.replace('about:blank')")
        elif url.path == "/to-blank-late":
            self.page("leaving", "setTimeout(() => location.replace('about:blank'), 300)")
        elif url.path == "/empty":
            self.send(b"", 204)
        elif url.path == "/to-closed":
            self.send(b"", 302, Location=f"http://127.0.0.1:{CLOSED}/")
        elif url.path == "/stale":
            self.page("stale", "document.cookie = 'tok=STALE; path=/; max-age=3600'")
        elif url.path == "/late-token":
            self.page("late", "setTimeout(() => { document.cookie = 'tok=FRESH; path=/; max-age=3600' }, 800)")
        else:
            self.send(b"", 404)

    do_GET = do_POST = do_PUT = respond

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module", autouse=True)
def browser_works(tmp_path_factory):
    """The whole module is skipped where no browser can be started: none installed, no permission, a sandbox..."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DJS_HOME", str(tmp_path_factory.mktemp("probe")))
        mp.delenv("DJS_CDP_URL", raising=False)
        mp.delenv("DJS_BROWSER_HEADLESS", raising=False)
        try:
            with browser.open_browser(SITE, "direct", 0.0, 0.0):
                pass
        except browser.BrowserUnavailable as e:
            pytest.skip(f"No browser can be started here ({e})")


@pytest.fixture(scope="module")
def crawl(tmp_path_factory):
    """One client in one headless browser for the tests that only look at pages (each starts with its own goto)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DJS_HOME", str(tmp_path_factory.mktemp("crawl")))
        mp.delenv("DJS_CDP_URL", raising=False)
        mp.delenv("DJS_BROWSER_HEADLESS", raising=False)
        with browser.open_browser(SITE, "direct", 0.0, 0.0) as client:
            yield client


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.delenv("DJS_BROWSER_HEADLESS", raising=False)
    monkeypatch.setattr(browser, "QUIT_SECONDS", 2.0)
    yield tmp_path / "home"
    for leftover in list(browser._alive):   # a failed test must not leave a browser running on the developer's machine
        leftover.close()


@pytest.fixture
def waits(monkeypatch):
    seen, real = [], pacer.wait

    def spy(*args):
        seen.append(args)
        real(*args)

    monkeypatch.setattr(pacer, "wait", spy)
    return seen


# ============================================================ one session: pages, fetch, redirects, blanks, errors
def test_goto_and_the_in_page_fetch_answer_like_an_httpx_client(crawl, base, waits):
    c = crawl
    c.goto(f"{base}/page")
    c.wait_cookie("tok")   # written by the page's own script, like an anti-bot token
    assert "你好" in c.html() and c.url == f"{base}/page" and c.eval("n => n * 2", 21) == 42
    assert c.eval("() => navigator.webdriver") is False   # an ordinary browser as far as that tell goes
    width, height = c.eval("() => [innerWidth, innerHeight]")
    assert width == 1280 and height > 700   # the window of experiment E1, not the headless default (756x469)
    assert len(waits) == 1   # only goto touched the network

    echo = c.get("/echo", params={"q": "py", "page": 2}, headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "mine"}).json()
    assert (echo["method"], echo["query"]) == ("GET", "q=py&page=2")
    assert echo["headers"]["x-requested-with"] == "XMLHttpRequest" and "tok=abc" in echo["headers"]["cookie"]
    assert "mine" not in echo["headers"]["user-agent"]   # the browser keeps its own identity
    form = c.post(f"{base}/echo", data={"query": "算法", "page": 1}).json()
    assert form["body"] == "query=%E7%AE%97%E6%B3%95&page=1" and form["headers"]["content-type"] == "application/x-www-form-urlencoded"
    posted = c.post("/echo", json={"a": [1, 2]}).json()
    assert json.loads(posted["body"]) == {"a": [1, 2]} and posted["headers"]["content-type"] == "application/json"
    assert c.request("PUT", "/echo", content="plain text").json()["body"] == "plain text"
    missing = c.get("/missing")
    assert missing.status_code == 404 and missing.json() == {"code": 404} and isinstance(missing, httpx.Response)
    with pytest.raises(httpx.HTTPStatusError):
        missing.raise_for_status()
    assert len(waits) == 1 + 5   # goto, then each request once: html/eval/url/wait_cookie stayed off the network

    with pytest.raises(httpx.ConnectError):
        c.get(f"http://127.0.0.1:{CLOSED}/x")
    with pytest.raises(httpx.ReadTimeout):
        c.get("/slow?s=3", timeout=0.3)
    with pytest.raises(httpx.ReadTimeout):
        c.wait_cookie("never-set", timeout=0.3)
    assert c.get("/echo").status_code == 200   # the tab is fine after all of that


def test_a_page_whose_script_redirects_late_is_settled_before_the_fetch_goes_out(crawl, base):
    crawl.goto(f"{base}/late-redirect")
    crawl.settle(10_000)   # complete and unchanged for a second: the redirect at 700 ms is over
    assert crawl.url == f"{base}/final" and "final" in crawl.html()
    assert crawl.get("/echo").json()["method"] == "GET"   # a relative URL against where it landed


def test_a_navigation_that_loads_nothing_is_a_connection_error_not_an_empty_success(crawl, base):
    with pytest.raises(httpx.ConnectError, match="ERR_ABORTED|empty page"):
        crawl.goto(f"{base}/empty")   # a 204: nothing is loaded, the tab stays on about:blank
    with pytest.raises(httpx.ConnectError):
        crawl.get(f"{base}/echo")   # and a fetch from there is no success either
    with pytest.raises(httpx.ConnectError):
        crawl.goto(f"{base}/to-closed")   # a redirect to a port nobody listens on ends on the browser's error page
    with pytest.raises(httpx.ConnectError):
        crawl.goto(f"http://127.0.0.1:{CLOSED}/")
    crawl.goto(f"{base}/page")   # the client is fine after all of that
    assert crawl.get("/echo").status_code == 200


@pytest.mark.parametrize("path", ["/to-blank", "/to-blank-late"])
def test_a_page_that_redirects_to_a_blank_page_never_yields_an_empty_success(crawl, base, path):
    try:
        crawl.goto(f"{base}{path}")   # the document may be complete before its script leaves: then the next calls tell
    except httpx.ConnectError:
        return
    crawl.settle(5000)
    assert crawl.url == "about:blank"
    try:
        answer = crawl.get(f"{base}/echo")
    except httpx.ConnectError:
        return
    assert answer.json()["method"] == "GET"   # a blank page made by a script keeps the origin of the one that left: the site's own answer


def test_a_fetch_in_flight_when_the_page_navigates_away_is_a_connection_error(crawl, base):
    crawl.goto(f"{base}/page")
    crawl.eval("() => { setTimeout(() => { location.href = '/final' }, 300) }")
    begun = time.monotonic()
    with pytest.raises(httpx.ConnectError):
        crawl.get("/slow?s=3", timeout=20)   # the page leaves 300 ms into the 3 s the site takes
    assert time.monotonic() - begun < 2.5   # told when the page went, not when the site answered
    crawl.goto(f"{base}/page")
    assert crawl.get("/echo").status_code == 200


def test_each_goto_is_one_tab_and_the_old_one_is_gone(crawl, base):
    def tabs():
        return [t for t in crawl._cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]

    crawl.goto(f"{base}/page")
    first = {t["targetId"] for t in tabs()}
    crawl.goto(f"{base}/final")
    second = {t["targetId"] for t in tabs()}
    assert len(first) == len(second) == 2 and first != second   # the browser's own page, and ours (a new one each time)


# ============================================================ the profile between two sessions
def test_a_stale_token_in_the_profile_is_cleared_and_the_one_the_page_writes_late_is_waited_for(home, base):
    with browser.open_browser(SITE, "direct", 0.0, 0.0) as c:
        c.goto(f"{base}/stale")
        c.wait_cookie("tok")
        assert c.cookie_value("tok", base) == "STALE"
    with browser.open_browser(SITE, "direct", 0.0, 0.0) as c:   # straight away: the profile opens again, with the cookie in it
        assert c.cookie_value("tok", base) == "STALE" and [x["value"] for x in c.cookies(base) if x["name"] == "tok"] == ["STALE"]
        c.clear_cookie("tok")
        assert c.cookie_value("tok", base) is None and not [x for x in c.cookies() if x["name"] == "tok"]
        c.goto(f"{base}/late-token")
        assert c.cookie_value("tok", base) is None   # the page writes it 800 ms after the load
        c.wait_cookie("tok", timeout=10)
        assert c.cookie_value("tok", base) == "FRESH"
        assert [x["domain"] for x in c.cookies(base) if x["name"] == "tok"] == ["127.0.0.1"]
        assert c.cookie_value("tok", "https://elsewhere.test/") is None


# ============================================================ no process left behind
def processes_of(profile) -> list[str]:
    """The processes of the machine that run on `profile` (the browser and everything it started carry its flag)."""
    out = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if f"--user-data-dir={profile}" in line]


def wait_until(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "gave up waiting"
        time.sleep(0.05)


@posix
def test_nothing_of_the_browser_survives_a_session_and_the_profile_opens_again_each_time(home, base):
    profile = home / "profiles" / "site" / "browser"
    for end in (None, RuntimeError):
        with pytest.raises(RuntimeError) if end else nullcontext():
            with browser.open_browser(SITE, "direct", 0.0, 0.0) as c:
                c.goto(f"{base}/page")
                wait_until(lambda: len(processes_of(profile)) > 1)   # a real browser is more than the process we started
                if end:
                    raise end("stop")
        assert processes_of(profile) == [] and not browser._alive
        assert not (profile / "DevToolsActivePort").exists()


@posix
def test_the_exit_handler_closing_the_browser_ends_a_fetch_that_is_waiting_and_leaves_no_process(home, base):
    profile = home / "profiles" / "site" / "browser"
    outcome = []
    with browser.open_browser(SITE, "direct", 0.0, 0.0) as c:
        c.goto(f"{base}/page")

        def fetch():
            try:
                outcome.append(c.get("/slow?s=30", timeout=60))
            except BaseException as e:   # the thread reports it
                outcome.append(e)

        worker = threading.Thread(target=fetch)
        worker.start()
        time.sleep(0.5)   # the fetch is under way, the site is holding it
        begun = time.monotonic()
        browser._close_all()   # what the exit handler does, from another thread
        worker.join(15)
        assert not worker.is_alive() and isinstance(outcome[0], browser.BrowserGone)
        assert time.monotonic() - begun < 10
    assert processes_of(profile) == [] and not browser._alive


# ============================================================ the user's own browser
@contextmanager
def users_chrome(tmp_path):
    """A browser started the way a user would for CDP: its own profile folder, a debugging port, two tabs open."""
    port = free_port()
    proc = subprocess.Popen([str(browser.executable()), f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'chrome'}",
                             "--headless=new", "--no-first-run", "--no-default-browser-check", "--use-mock-keychain",
                             "--password-store=basic", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    address = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(f"{address}/json/version", trust_env=False, timeout=1)
                break
            except httpx.HTTPError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.skip("cannot start a browser with a debugging port here")
                time.sleep(0.2)
        httpx.put(f"{address}/json/new?data:text/html,<title>user</title>user", trust_env=False, timeout=5)
        yield address, proc
    finally:
        proc.kill()
        proc.wait(10)


def tabs(address: str) -> list[str]:
    listing = httpx.get(f"{address}/json/list", trust_env=False, timeout=5).json()
    return sorted(t["url"] for t in listing if t["type"] == "page")


def tabs_soon(address, want, seconds=3.0):
    """The tabs once they are `want`, or as they are after `seconds`: a closed tab leaves the list a moment later."""
    deadline = time.monotonic() + seconds
    while (now := tabs(address)) != want and time.monotonic() < deadline:
        time.sleep(0.1)
    return now


def test_in_the_users_browser_only_our_tab_is_opened_and_closed_and_its_cookies_are_read(home, base, tmp_path, monkeypatch):
    with users_chrome(tmp_path) as (address, proc):
        monkeypatch.setenv("DJS_CDP_URL", address)
        before = tabs(address)
        assert len(before) == 2
        assert auth.logged_in("boss") is False
        with browser.open_browser(SITE, "direct", 0.0, 0.0) as c:
            c.goto(f"{base}/page")
            c.wait_cookie("tok")
            during = tabs(address)
            assert len(during) == 3 and set(before) < set(during)   # exactly one new tab, the user's two untouched
            with browser.profile_lock("site", wait=0):   # an attached browser has no profile of ours to lock
                pass
        assert tabs_soon(address, before) == before and proc.poll() is None   # our tab is gone, the browser still runs
        assert not (home / "profiles" / "site" / "browser").exists()

        cdp = browser._Cdp(browser._browser_socket(address))   # the user logs in to BOSS in their own browser
        cdp.call("Storage.setCookies", cookies=[{"name": "wt2", "value": "secret", "domain": ".zhipin.com", "path": "/"}])
        cdp.close()
        assert {c["name"] for c in browser.attached_cookies()} >= {"wt2", "tok"}
        assert auth.logged_in("boss") is True and "secret" not in json.dumps(auth.status("boss"))
        assert tabs(address) == before   # looking at the login state never opens a tab
