"""Capture transports. Part 1 needs no browser (fake page, fake browser client); part 2 drives a real headless
Chromium through Playwright and skips cleanly when Playwright or its browsers are not available."""
import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from djs import auth, browser, cli, db, egress, engine, pacer, providers, service, taskqueue
from djs.providers.base import Blocked, Login, LoginRequired, Provider, check_http, get_json, need

PROBE = SimpleNamespace(name="probe", needs_headed=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.delenv("DJS_BROWSER_HEADLESS", raising=False)
    monkeypatch.chdir(tmp_path)  # keeps the repo's own .env out of every test
    return tmp_path


# ============================================================ response adapter and request encoding
class FakePage:
    """The slice of a Playwright page BrowserClient uses: evaluate() answers with `reply` (or raises it)."""

    def __init__(self, reply=None, url="https://site.test/home"):
        self.url, self.reply, self.sent = url, reply or {"status": 200, "headers": [], "text": "{}"}, []

    def evaluate(self, js, arg=None):
        self.sent.append(arg)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def client(page, exit_id="direct", interval=0.0, jitter=0.0):
    return browser.BrowserClient(page, SimpleNamespace(name="probe"), exit_id, interval, jitter)


def test_fetch_result_becomes_a_real_httpx_response(home):
    page = FakePage({"status": 200, "text": '{"code": 0, "name": "岗位"}', "headers": [
        ["content-encoding", "gzip"], ["content-length", "3"], ["transfer-encoding", "chunked"],
        ["content-type", "application/json; charset=gbk"], ["x-id", "7"]]})
    r = client(page).get("/api/list", params={"page": 2})
    assert isinstance(r, httpx.Response) and r.status_code == 200
    assert get_json(r) == {"code": 0, "name": "岗位"} and "岗位" in r.text  # a gbk label must not garble the text
    assert r.headers["x-id"] == "7" and "content-encoding" not in r.headers and "transfer-encoding" not in r.headers
    assert r.headers["content-length"] == str(len(r.content))  # the decoded body's length, not the wire value
    assert str(r.request.url) == "https://site.test/api/list?page=2"  # relative URL resolved against the page


def test_status_codes_reach_the_providers_own_helpers(home):
    blocked = client(FakePage({"status": 403, "text": "", "headers": [["retry-after", "30"]]})).get("/x")
    with pytest.raises(Blocked) as e:
        check_http(blocked)
    assert e.value.retry_after == 30.0
    missing = client(FakePage({"status": 404, "text": "", "headers": []})).get("/x")
    with pytest.raises(httpx.HTTPStatusError):
        missing.raise_for_status()


CALLS = [("GET", "/api", {"params": {"q": "py", "page": 2}}),
         ("GET", "/api?x=1", {}),
         ("GET", "/api?x=1", {"params": {"q": "py"}}),
         ("GET", "https://other.test/a b", {"headers": {"X-Requested-With": "XMLHttpRequest"}}),
         ("POST", "/form", {"data": {"query": "算法", "page": 1}}),
         ("POST", "/json", {"json": {"a": [1, 2], "b": "岗位"}}),
         ("PUT", "/raw", {"content": "plain text"})]


@pytest.mark.parametrize("method, url, kwargs", CALLS)
def test_sends_what_an_httpx_client_would_send(home, method, url, kwargs):
    seen = []
    http = httpx.Client(base_url="https://site.test", transport=httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200)))
    http.request(method, url, **kwargs)
    page = FakePage()
    client(page).request(method, url, **kwargs)
    want, got = seen[0], page.sent[0]
    assert (got["method"], got["url"]) == (method, str(want.url))
    assert (got["body"] or "").encode() == want.content
    for name in ("content-type", "x-requested-with"):  # the headers a page may set; the rest is the browser's own
        assert got["headers"].get(name) == want.headers.get(name)


def test_timeout_is_passed_to_the_page_in_milliseconds(home):
    page = FakePage()
    client(page).get("/a", timeout=5)
    client(page).get("/b")
    assert [sent["timeout"] for sent in page.sent] == [5000, 20000]


def test_in_page_failures_become_the_httpx_errors_the_engine_retries(home):
    sync_api = pytest.importorskip("playwright.sync_api")
    cases = [({"error": "timeout"}, httpx.ReadTimeout), ({"error": "TypeError: Failed to fetch"}, httpx.ConnectError),
             (sync_api.TimeoutError("page.evaluate: Timeout 30000ms exceeded"), httpx.ReadTimeout),
             (sync_api.Error("page.evaluate: Execution context was destroyed"), httpx.ConnectError)]
    for reply, expected in cases:
        with pytest.raises(httpx.HTTPError) as e:
            client(FakePage(reply)).get("/api")
        assert type(e.value) is expected


def test_get_post_and_goto_are_paced_like_http(home, monkeypatch):
    waits = []
    monkeypatch.setattr(pacer, "wait", lambda *args: waits.append(args))
    page = FakePage()
    page.goto = lambda url, **kw: None
    c = browser.BrowserClient(page, SimpleNamespace(name="boss"), "http://p:1", 4.0, 2.0)
    c.get("/a")
    c.post("/b")
    c.goto("https://site.test/")
    assert waits == [("boss", "http://p:1", 4.0, 2.0)] * 3


# ============================================================ configuration helpers
@pytest.mark.parametrize("exit_id, expected", [
    (None, None), ("direct", None),
    ("http://h:8080", {"server": "http://h:8080"}),
    ("http://u:p%40ss@h:8080", {"server": "http://h:8080", "username": "u", "password": "p@ss"}),
    ("https://only@h:3128", {"server": "https://h:3128", "username": "only", "password": ""})])
def test_proxy_url_becomes_playwright_proxy_settings(exit_id, expected):
    assert browser.proxy_settings(exit_id) == expected


def test_cdp_url_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    assert browser.cdp_url() is None
    monkeypatch.setenv("DJS_CDP_URL", "  ")
    assert browser.cdp_url() is None
    monkeypatch.setenv("DJS_CDP_URL", " http://127.0.0.1:9222 ")
    assert browser.cdp_url() == "http://127.0.0.1:9222"


@pytest.mark.parametrize("needs_headed, forced, want_headless", [
    (False, None, True), (True, None, False), (True, "1", True), (False, "0", False), (True, "yes", False)])
def test_headless_follows_the_provider_unless_the_environment_forces_it(
        home, monkeypatch, needs_headed, forced, want_headless):
    if forced is not None:
        monkeypatch.setenv("DJS_BROWSER_HEADLESS", forced)
    seen = []

    @contextmanager
    def fake_open_page(provider_name, headless, proxy=None):
        seen.append((provider_name, headless, proxy))
        yield object()

    monkeypatch.setattr(browser, "open_page", fake_open_page)
    with browser.open_browser(SimpleNamespace(name="boss", needs_headed=needs_headed), "http://u:p@h:1", 0.0, 0.0) as c:
        assert isinstance(c, browser.BrowserClient)
    assert seen == [("boss", want_headless, "http://u:p@h:1")]


def test_launch_context_is_the_single_place_that_knows_the_profile_flags(home):
    seen = {}

    class Ctx:
        def add_init_script(self, js):
            seen["script"] = js

    class Chromium:
        def launch_persistent_context(self, path, **kw):
            seen.update(path=path, **kw)
            return Ctx()

    browser.launch_context(SimpleNamespace(chromium=Chromium()), "boss", False, "http://u:p@h:8080")
    assert seen["path"] == str(home / "profiles" / "boss" / "browser")
    assert (seen["headless"], seen["locale"], seen["viewport"]) == (False, "zh-CN", {"width": 1280, "height": 800})
    assert seen["args"] == ["--disable-blink-features=AutomationControlled"]
    assert seen["proxy"] == {"server": "http://h:8080", "username": "u", "password": "p"}
    assert "webdriver" in seen["script"]


def test_missing_chromium_is_reported_with_the_install_hint(home, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(home / "no-browsers"))
    monkeypatch.setattr(browser, "CHANNELS", (None,))  # an installed Chrome or Edge would be used instead
    with pytest.raises(browser.BrowserUnavailable, match="playwright install chromium"):
        with browser.open_browser(PROBE, "direct", 0.0, 0.0):
            pass


def test_unreachable_cdp_browser_says_how_to_start_chrome(home, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:1")
    with pytest.raises(browser.BrowserUnavailable, match=r"remote-debugging-port=9222.*DJS_CDP_URL") as e:
        with browser.open_browser(PROBE, "direct", 0.0, 0.0):
            pass
    assert "http://127.0.0.1:1" in str(e.value)
    assert auth.status("boss")["logged_in"] is False  # status cannot tell, so it says no instead of failing...
    with pytest.raises(browser.BrowserUnavailable):  # ...while the engine's gate surfaces the real problem
        auth.logged_in("boss")


def test_djs_works_without_playwright_installed(tmp_path):
    code = ("import sys; sys.modules['playwright'] = None\n"
            "from djs import browser, engine, service\n"
            "print(browser.ready())")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "DJS_HOME": str(tmp_path)})
    assert (out.returncode, out.stdout.strip()) == (0, "False"), out.stderr


# ============================================================ profile lock
def test_profile_lock_lets_one_holder_in_and_the_rest_wait(home):
    with browser.profile_lock("boss"):
        with pytest.raises(browser.BrowserBusy, match="正在使用浏览器，请稍后再试") as e:
            with browser.profile_lock("boss", wait=0):
                pass
        assert isinstance(e.value, egress.NoEgressAvailable)  # engine and job_detail already handle it as "retry later"
        with browser.profile_lock("other", wait=0):  # profiles are independent
            pass
    with browser.profile_lock("boss", wait=0):  # released again
        pass
    assert oct(db.profile_dir("boss").stat().st_mode & 0o777) == "0o700"


def test_profile_lock_waits_for_a_holder_that_is_about_to_finish(home):
    release, held = threading.Event(), threading.Event()

    def hold():
        with browser.profile_lock("boss"):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    threading.Timer(0.4, release.set).start()
    start = time.monotonic()
    with browser.profile_lock("boss", wait=5):
        assert 0.3 <= time.monotonic() - start < 3
    holder.join()


def test_profiles_in_use_names_the_held_ones_and_never_waits(home):
    assert browser.profiles_in_use() == []   # no profile folder at all
    with browser.profile_lock("boss"):
        with browser.profile_lock("liepin"):
            pass   # liepin's folder exists now, but nobody holds it
        begun = time.monotonic()
        assert browser.profiles_in_use() == ["boss"]
        assert time.monotonic() - begun < 1   # profile_lock alone would wait three seconds
    assert browser.profiles_in_use() == []


# ============================================================ auth: sessions and the login flow
def jar_context(jar):
    @contextmanager
    def fake_context(provider_name, headless, proxy=None):
        yield SimpleNamespace(cookies=lambda: jar)
    return fake_context


def test_logged_in_uses_saved_cookies_without_cdp(home):
    assert auth.logged_in("boss") is False
    auth.save_cookies("boss", {"wt2": "x"})
    assert auth.logged_in("boss") is True


def test_logged_in_with_cdp_reads_the_attached_browsers_cookies(home, monkeypatch):
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:9222")
    jar = [{"name": "wt2", "value": "SECRET", "domain": ".other.com"}]
    monkeypatch.setattr(browser, "open_context", jar_context(jar))
    auth.save_cookies("boss", {"wt2": "saved"})  # saved cookies are not the user's browser: the browser decides
    assert auth.logged_in("boss") is False
    jar[0]["domain"] = ".zhipin.com"
    assert auth.logged_in("boss") is True and auth.status("boss")["logged_in"] is True
    assert "SECRET" not in json.dumps(auth.status("boss"))


def test_default_warmup_loads_the_home_page_only_in_a_browser():
    class Site(Provider):
        home = "https://x.test/"

    opened = []
    Site().warmup(SimpleNamespace(goto=opened.append), {})
    Site().warmup(httpx.Client(), {})  # an http client cannot goto: nothing happens
    Provider().warmup(SimpleNamespace(goto=opened.append), {})  # no home page declared
    assert opened == ["https://x.test/"]


# ============================================================ persistence: transport setting
def test_old_provider_settings_table_gains_the_transport_column(home):
    old = sqlite3.connect(home / "djs.db")
    old.executescript("CREATE TABLE provider_settings (provider TEXT PRIMARY KEY, interval REAL, jitter REAL, "
                      "max_pages INTEGER); INSERT INTO provider_settings VALUES ('nowcoder', 1.5, 0.2, 4); "
                      "PRAGMA user_version = 1;")
    old.close()
    conn = db.connect()
    assert pacer.effective(conn, "nowcoder") == (1.5, 0.2, 4)  # the user's old override survived
    assert pacer.transport(conn, "nowcoder") == "http"
    assert [r["name"] for r in conn.execute("PRAGMA table_info(provider_settings)")][-1] == "transport"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5


def test_adding_a_column_survives_a_concurrent_migrator(home):
    conn = db.connect()

    class Racy:
        """Its table_info check misses the column, as if another process added it a moment ago."""

        def execute(self, sql, *args):
            return [] if sql.startswith("PRAGMA table_info") else conn.execute(sql, *args)

    db._add_column_if_missing(Racy(), "provider_settings", "transport", "TEXT")
    with pytest.raises(sqlite3.OperationalError):  # any other failure still surfaces
        db._add_column_if_missing(Racy(), "no_such_table", "x", "TEXT")


# ============================================================ engine and service over the transports
class Dual(Provider):
    """A platform that can run over either transport."""
    name = "dual"
    interval, jitter, min_interval = 0.0, 0.0, 0.0
    page_size, first_page = 2, 1
    transports = ("http", "browser")
    home = "https://dual.test/"

    def fetch(self, client, payload, page):
        return need(get_json(client.get("https://dual.test/api", params={"page": page})), "items")

    def job_id(self, job):
        return str(need(job, "id", typ=int))

    def detail(self, client, job):
        return {"jd": f"jd{job['id']}"}

    def row(self, job, detail):
        return {"title": f"t{job['id']}", "company": None, "city": None, "salary_text": None,
                "description": detail and detail["jd"]}


class BrowserOnly(Dual):
    name = "bonly"
    transports = ("browser",)


SESSION = Login(url="https://dual.test/login", method="qr", domain="dual.test", cookies_any=("s",), unlocks="能看到更多")


class Optional(Dual):
    name = "opt"
    auth = "optional"
    login = SESSION
    transports = ("browser", "http")


class NeedsLogin(BrowserOnly):
    name = "req"
    auth = "login"
    login = SESSION


class FakeAuth:
    def __init__(self):
        self.session, self.invalidated = False, []

    def logged_in(self, provider):
        return self.session

    def load_cookies(self, provider):
        return {"s": "1"} if self.session else None

    def status(self, provider):
        return {"logged_in": self.session}

    def invalidate(self, provider):
        self.invalidated.append(provider)
        self.session = False


class FakeClient:
    """Stands in for BrowserClient: two pages of jobs, remembers goto(), fails on demand."""

    def __init__(self):
        self.gotos, self.fail = [], None

    def goto(self, url, timeout=30):
        self.gotos.append(url)

    def get(self, url, params=None, **kw):
        if self.fail:
            raise self.fail
        items = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}.get(params["page"], [])
        return httpx.Response(200, json={"items": items}, request=httpx.Request("GET", url))


@pytest.fixture
def env(home, monkeypatch):
    provs = {p.name: p for p in (Dual(), BrowserOnly(), Optional(), NeedsLogin())}
    monkeypatch.setattr(providers, "get", lambda name: provs[name])
    monkeypatch.setattr(providers, "names", lambda: list(provs))
    fake_auth = FakeAuth()
    for module in (engine, service):
        monkeypatch.setattr(module, "auth", fake_auth)
    conn = db.connect()
    yield SimpleNamespace(conn=conn, auth=fake_auth, mp=monkeypatch)
    conn.close()


@pytest.fixture
def fake_browser(env):
    state = SimpleNamespace(client=FakeClient(), opened=[], raises=None)

    @contextmanager
    def fake_open(prov, exit_id, interval, jitter):
        if state.raises:
            raise state.raises
        state.opened.append((prov.name, exit_id, interval, jitter))
        yield state.client

    env.mp.setattr(browser, "open_browser", fake_open)
    return state


def serve_http(env, handler):
    real = egress.make_client
    env.mp.setattr(egress, "make_client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))


def run(env, provider, **payload):
    tid = taskqueue.create(env.conn, provider, {"keyword": "py", "detail": False, **payload})
    engine.run_task(env.conn, taskqueue.claim(env.conn))
    return taskqueue.get(env.conn, tid)


def test_transport_override_validates_persists_and_clears(env):
    c = env.conn
    assert (pacer.transport(c, "dual"), pacer.transport(c, "bonly")) == ("http", "browser")
    assert pacer.set_transport(c, "dual", "browser") == {"provider": "dual", "transport": "browser"}
    assert pacer.transport(c, "dual") == "browser"
    with pytest.raises(ValueError, match=r"bonly supports transport\(s\) browser, not 'http'"):
        pacer.set_transport(c, "bonly", "http")
    pacer.set_frequency(c, "dual", interval=2)  # the two overrides are independent
    assert pacer.transport(c, "dual") == "browser"
    assert service.set_transport(c, "dual") == {"provider": "dual", "transport": "http"}  # None clears it
    assert pacer.effective(c, "dual")[0] == 2
    c.execute("UPDATE provider_settings SET transport='cdp' WHERE provider='dual'")  # a transport the provider dropped
    assert pacer.transport(c, "dual") == "http"


def test_djs_call_set_transport(env, capsys):
    def call(**args):
        code = cli.main(["call", "set_transport", json.dumps(args)])
        return code, json.loads(capsys.readouterr().out)

    assert call(provider="dual", transport="browser") == (0, {"provider": "dual", "transport": "browser"})
    assert pacer.transport(env.conn, "dual") == "browser"
    code, out = call(provider="bonly", transport="http")
    assert code == 1 and "supports transport(s) browser" in out["error"]
    assert call(provider="dual") == (0, {"provider": "dual", "transport": "http"})


def test_provider_list_tells_which_transport_runs_and_whether_it_can(env, monkeypatch):
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:9222")
    pacer.set_transport(env.conn, "dual", "browser")
    listing = {p["name"]: p for p in service.provider_list(env.conn)}
    assert listing["dual"]["transports"] == ["http", "browser"] and listing["dual"]["transport"] == "browser"
    assert listing["bonly"]["transport"] == "browser" and listing["dual"]["cdp"] is True
    assert "logged_in" not in listing["dual"]  # public platforms have nothing to log in to
    assert listing["opt"]["logged_in"] is False and listing["req"]["logged_in"] is False
    monkeypatch.setattr(browser, "ready", lambda: False)
    assert {p["browser_ready"] for p in service.provider_list(env.conn)} == {False}


def test_engine_opens_a_browser_only_when_the_transport_says_so(env, fake_browser):
    serve_http(env, lambda request: httpx.Response(200, json={"items": [{"id": 9}]}))
    assert run(env, "dual")["status"] == "done" and fake_browser.opened == []  # the default transport is http
    env.mp.setattr(egress, "make_client", lambda *a, **k: pytest.fail("the browser transport has no httpx client"))
    egress.add(env.conn, "http://proxy.test:8080")  # the same exit lease serves both transports
    pacer.set_transport(env.conn, "dual", "browser")
    row = run(env, "dual")
    assert row["status"] == "done" and row["stats"]["jobs_new"] == 3
    assert fake_browser.opened == [("dual", "http://proxy.test:8080", 0.0, 0.0)]
    assert fake_browser.client.gotos == ["https://dual.test/"]  # warmup: the home page, once, before the first page
    assert env.conn.execute("SELECT total_ok FROM egress_health").fetchone()[0] == 1


def test_browser_that_cannot_start_kills_the_task_with_the_actionable_message(env, fake_browser):
    fake_browser.raises = browser.BrowserUnavailable(browser.INSTALL_HINT)
    row = run(env, "bonly")
    assert (row["status"], row["last_error"], row["attempts"]) == ("dead", browser.INSTALL_HINT, 0)
    assert env.conn.execute("SELECT COUNT(*) FROM egress_health").fetchone()[0] == 0  # not the exit's fault


def test_busy_browser_profile_postpones_the_task_without_burning_an_attempt(env, fake_browser):
    fake_browser.raises = browser.BrowserBusy("正在使用浏览器，请稍后再试")
    row = run(env, "bonly")
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 0, "正在使用浏览器，请稍后再试")
    assert row["next_run_at"] > time.time() + 30


def test_network_errors_from_a_browser_client_retry_and_count_against_the_exit(env, fake_browser):
    fake_browser.client.fail = httpx.ReadTimeout("slow")
    row = run(env, "bonly")
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, "ReadTimeout: slow")
    assert env.conn.execute("SELECT total_fail FROM egress_health").fetchone()[0] == 1


def test_login_platforms_wait_for_a_session_but_optional_ones_never_do(env, fake_browser):
    assert run(env, "req")["status"] == "waiting_login" and fake_browser.opened == []
    env.auth.session = True
    assert run(env, "req")["status"] == "done"
    env.auth.session = False
    assert run(env, "opt")["status"] == "done"  # anonymous browsing is fine for an optional platform
    assert env.auth.invalidated == []


@pytest.mark.parametrize("name, status, attempts", [("opt", "pending", 1), ("req", "waiting_login", 0)])
def test_a_dead_session_waits_for_a_login_or_is_dropped_by_an_optional_platform(env, fake_browser, name, status, attempts):
    env.auth.session = True
    fake_browser.client.fail = LoginRequired("code 7")
    row = run(env, name)
    assert (row["status"], row["attempts"], env.auth.invalidated) == (status, attempts, [name])  # optional: retried without it
    assert env.conn.execute("SELECT COUNT(*) FROM egress_health WHERE blocked_until > 0").fetchone()[0] == 0  # not the exit's fault


def test_optional_platform_over_http_still_carries_a_saved_cookie(env):
    pacer.set_transport(env.conn, "opt", "http")
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, json={"items": []})

    serve_http(env, handler)
    env.auth.session = True
    assert run(env, "opt")["status"] == "done"
    env.auth.session = False
    assert run(env, "opt")["status"] == "done"
    assert seen == ["s=1", None]


def test_crawl_start_refuses_early_when_a_browser_platform_cannot_run(env, monkeypatch):
    monkeypatch.setattr(browser, "ready", lambda: False)
    with pytest.raises(ValueError, match="playwright install chromium"):
        service.crawl_start(env.conn, "bonly", keyword="x")
    assert service.crawl_start(env.conn, "dual", keyword="x")["task_id"] == 1  # http needs no browser
    pacer.set_transport(env.conn, "dual", "browser")
    with pytest.raises(ValueError, match="Playwright"):
        service.crawl_start(env.conn, "dual", keyword="x")
    assert env.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_job_detail_over_the_browser_warms_up_and_explains_what_is_missing(env, fake_browser):
    pacer.set_transport(env.conn, "dual", "browser")
    run(env, "dual")
    job_id = service.jobs_get(env.conn, complete_only=False)["jobs"][0]["id"]
    fake_browser.client.gotos.clear()
    assert service.job_detail(env.conn, job_id)["description"] == "jd3"
    assert fake_browser.client.gotos == ["https://dual.test/"]
    env.conn.execute("UPDATE jobs SET detail=NULL, detail_error=NULL")
    fake_browser.raises = browser.BrowserUnavailable(browser.INSTALL_HINT)
    with pytest.raises(ValueError, match="playwright install chromium"):
        service.job_detail(env.conn, job_id)


# ============================================================ part 2: a real Chromium
PAGE = ('<html><head><title>app</title></head><body>你好<script>'
        'document.cookie = "tok=abc; path=/; max-age=3600"</script></body></html>')


@pytest.fixture(scope="session")
def chromium():
    """Path of the full Chromium build; skips the real-browser tests where Playwright or its browsers are missing."""
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        with sync_api.sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
            return pw.chromium.executable_path
    except Exception as e:  # no driver, no browser download, a sandbox that forbids launching...
        pytest.skip(f"Chromium cannot be launched here ({type(e).__name__})")


def app(held):
    """Answers the page's requests like a tiny site (no server, no internet); /api/slow is held back unanswered."""
    def handle(route):
        request, url = route.request, urlsplit(route.request.url)
        if url.path == "/":
            route.fulfill(content_type="text/html; charset=utf-8", body=PAGE)
        elif url.path == "/api/echo":
            route.fulfill(json={"method": request.method, "query": url.query, "body": request.post_data,
                                "headers": request.headers})
        elif url.path == "/api/missing":
            route.fulfill(status=404, json={"code": 404})
        elif url.path.endswith("/down"):
            route.abort()
        elif url.path == "/api/slow":
            held.append(route)
    return handle


@pytest.fixture
def site(home, chromium, monkeypatch):
    """open_browser() on a page served by `app`, plus every pacing call the client made."""
    waits, held, real_wait = [], [], pacer.wait

    def spy(*args):
        waits.append(args)
        real_wait(*args)

    monkeypatch.setattr(pacer, "wait", spy)
    with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:
        c.page.route("**/*", app(held))
        yield SimpleNamespace(client=c, waits=waits)
        for route in held:  # let go of the requests a test left hanging
            route.abort()


def test_browser_client_acts_like_an_httpx_client_inside_a_real_page(site):
    c = site.client
    c.goto("http://app.test/")
    c.wait_cookie("tok")  # written by the page's own script, like Boss's __zp_stoken__
    assert "你好" in c.html() and c.eval("document.title") == "app" and c.eval("n => n * 2", 21) == 42
    assert len(site.waits) == 1  # only goto touched the network

    echo = c.get("/api/echo", params={"q": "py", "page": 2},
                 headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "mine"}).json()
    assert (echo["method"], echo["query"]) == ("GET", "q=py&page=2")
    assert echo["headers"]["x-requested-with"] == "XMLHttpRequest" and "tok=abc" in echo["headers"]["cookie"]
    assert "mine" not in echo["headers"]["user-agent"]  # the browser keeps its own identity
    form = c.post("http://app.test/api/echo", data={"query": "算法", "page": 1}).json()
    assert form["body"] == "query=%E7%AE%97%E6%B3%95&page=1"
    assert form["headers"]["content-type"] == "application/x-www-form-urlencoded"
    posted = c.post("/api/echo", json={"a": [1, 2]}).json()
    assert json.loads(posted["body"]) == {"a": [1, 2]} and posted["headers"]["content-type"] == "application/json"
    missing = c.get("/api/missing")
    assert missing.status_code == 404 and missing.json() == {"code": 404}
    with pytest.raises(httpx.HTTPStatusError):
        missing.raise_for_status()

    with pytest.raises(httpx.ConnectError):
        c.get("/api/down")
    with pytest.raises(httpx.ReadTimeout):
        c.get("/api/slow", timeout=0.3)
    with pytest.raises(httpx.ReadTimeout):
        c.wait_cookie("never-set", timeout=0.3)

    xhr = c.sniff("/api/echo", lambda: c.eval("() => { fetch('/api/echo?from=click'); }"))
    assert xhr.status_code == 200 and xhr.json()["query"] == "from=click"
    with pytest.raises(httpx.ConnectError):  # leaves the page on an error document, so it goes last
        c.goto("http://app.test/down")
    # goto, get, post, post, get, get(down), get(slow), sniff, goto(down): wait_cookie/html/eval stayed unpaced
    assert site.waits == [("probe", "direct", 0.0, 0.0)] * 9


def test_profile_is_locked_while_open_and_shared_between_sessions(home, chromium):
    with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:
        c.page.route("**/*", app([]))
        c.goto("http://app.test/")
        c.wait_cookie("tok")
        with pytest.raises(browser.BrowserBusy):
            with browser.profile_lock("probe", wait=0):  # a login or another crawl has to wait its turn
                pass
    with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:  # the lock is free again, the cookie persisted
        assert "tok" in {cookie["name"] for cookie in c.page.context.cookies(["http://app.test/"])}


def test_works_on_a_worker_thread_while_an_event_loop_is_running(home, chromium):
    def one_request():
        with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:
            c.page.route("**/*", app([]))
            c.goto("http://app.test/")
            return c.get("/api/echo", params={"q": "x"}).json()["query"]

    async def main():  # what the MCP tools do: asyncio.to_thread keeps sync Playwright off the loop's thread
        return await asyncio.to_thread(one_request)

    assert asyncio.run(main()) == "q=x"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tabs(base: str) -> list[str]:
    listing = httpx.get(f"{base}/json/list", trust_env=False, timeout=5).json()
    return sorted(t["url"] for t in listing if t["type"] == "page")


@contextmanager
def users_chrome(exe: str, tmp_path):
    """A Chromium started the way a user would for CDP: its own profile folder, a debugging port, two tabs open."""
    port = free_port()
    proc = subprocess.Popen([exe, f"--remote-debugging-port={port}", f"--user-data-dir={tmp_path / 'chrome'}",
                             "--headless=new", "--no-first-run", "--no-default-browser-check", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(f"{base}/json/version", trust_env=False, timeout=1)
                break
            except httpx.HTTPError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.skip("cannot start a Chromium with a debugging port here")
                time.sleep(0.2)
        httpx.put(f"{base}/json/new?data:text/html,<title>user</title>user", trust_env=False, timeout=5)
        yield base, proc
    finally:
        proc.kill()
        proc.wait(10)


def tabs_soon(base, want, seconds=3.0):
    """The tabs once they are `want`, or as they are after `seconds`: a closed tab leaves the list a moment later."""
    deadline = time.monotonic() + seconds
    while (now := tabs(base)) != want and time.monotonic() < deadline:
        time.sleep(0.1)
    return now


def test_cdp_opens_one_tab_of_the_users_browser_and_leaves_everything_else_alone(home, chromium, monkeypatch):
    from playwright.sync_api import sync_playwright
    if not os.path.exists(chromium):
        pytest.skip("only the headless shell is installed, not the full Chromium build")
    with users_chrome(chromium, home) as (base, proc):
        monkeypatch.setenv("DJS_CDP_URL", base)
        before = tabs(base)
        assert len(before) == 2
        assert auth.logged_in("boss") is False
        with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:
            c.goto("data:text/html,<title>ours</title>ours")
            assert "ours" in c.html()
            during = tabs(base)
            assert len(during) == 3 and set(before) < set(during)  # exactly one new tab, the user's two untouched
            with browser.profile_lock("probe", wait=0):  # an attached browser has no profile of ours to lock
                pass
        assert tabs_soon(base, before) == before and proc.poll() is None  # our tab is gone, the browser still runs

        with sync_playwright() as pw:  # the user logs in to Boss in their own browser
            attached = pw.chromium.connect_over_cdp(base)
            attached.contexts[0].add_cookies([{"name": "wt2", "value": "secret", "domain": ".zhipin.com", "path": "/"}])
            attached.close()
        assert auth.logged_in("boss") is True and "secret" not in json.dumps(auth.status("boss"))
        assert tabs(base) == before  # looking at the login state never opens a tab
