"""Capture transports, as the engine and the service see them: the settings and the profile lock of djs.browser, the
login state read from the user's own browser, and the engine over a browser client that is a stand-in. The client itself is in
test_browser_client.py (fake DevTools endpoint) and test_browser_client_real.py (a real browser)."""
import ast
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from djs import auth, browser, cli, db, egress, engine, pacer, providers, service, taskqueue
from djs.providers.base import Login, LoginRequired, Provider, get_json, need

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.delenv("DJS_BROWSER_HEADLESS", raising=False)
    monkeypatch.chdir(tmp_path)  # keeps the repo's own .env out of every test
    return tmp_path


# ============================================================ configuration helpers
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
    assert browser._headless(SimpleNamespace(name="boss", needs_headed=needs_headed)) is want_headless


@pytest.mark.parametrize("exit_id, expected", [
    (None, []), ("", []), ("direct", []),
    ("http://h:8080", ["--proxy-server=http://h:8080"]),
    ("https://h:3128", ["--proxy-server=https://h:3128"]),
    ("socks5://h:1080", ["--proxy-server=socks5://h:1080"])])
def test_a_proxy_exit_becomes_the_proxy_server_flag(exit_id, expected):
    assert browser._proxy_flags(exit_id) == expected


@pytest.mark.parametrize("exit_id", ["http://u:p%40ss@h:8080", "https://only@h:3128"])
def test_a_proxy_with_a_user_name_is_refused(exit_id):
    with pytest.raises(browser.BrowserUnavailable, match="user name and password"):
        browser._proxy_flags(exit_id)


def imports_of(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_the_app_never_imports_playwright(tmp_path):
    """Not in any module of djs/, and loading the modules the app runs does not pull it in (it stays a dev-only dependency
    of the window's UI tests)."""
    sources = Path(browser.__file__).parent.rglob("*.py")
    assert [path.name for path in sources if any(m.split(".")[0] == "playwright" for m in imports_of(path))] == []
    code = ("import sys; sys.modules['playwright'] = None\n"
            "from djs import auth, browser, engine, providers, service\n"
            "print('loaded')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "DJS_HOME": str(tmp_path)})
    assert (out.returncode, out.stdout.strip()) == (0, "loaded"), out.stderr


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
def test_unreachable_cdp_browser_says_how_to_start_chrome_and_the_login_state_cannot_tell(home, monkeypatch):
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:1")
    assert auth.status("boss")["logged_in"] is False  # status cannot tell, so it says no instead of failing...
    with pytest.raises(browser.BrowserUnavailable, match=r"remote-debugging-port=9222.*DJS_CDP_URL") as e:
        auth.logged_in("boss")  # ...while the engine's gate surfaces the real problem
    assert "http://127.0.0.1:1" in str(e.value)


def test_logged_in_uses_saved_cookies_without_cdp(home):
    assert auth.logged_in("boss") is False
    auth.save_cookies("boss", {"wt2": "x"})
    assert auth.logged_in("boss") is True


def test_logged_in_with_cdp_reads_the_attached_browsers_cookies(home, monkeypatch):
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:9222")
    jar = [{"name": "wt2", "value": "SECRET", "domain": ".other.com"}]
    monkeypatch.setattr(browser, "attached_cookies", lambda: jar)
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
    fake_browser.raises = browser.BrowserUnavailable(browser.no_browser_hint())
    row = run(env, "bonly")
    assert (row["status"], row["last_error"], row["attempts"]) == ("dead", browser.no_browser_hint(), 0)
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
    with pytest.raises(ValueError, match="Google Chrome or Microsoft Edge"):
        service.crawl_start(env.conn, "bonly", keyword="x")
    assert service.crawl_start(env.conn, "dual", keyword="x")["task_id"] == 1  # http needs no browser
    pacer.set_transport(env.conn, "dual", "browser")
    with pytest.raises(ValueError, match="Google Chrome or Microsoft Edge"):
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
    fake_browser.raises = browser.BrowserUnavailable(browser.no_browser_hint())
    with pytest.raises(ValueError, match="Google Chrome or Microsoft Edge"):
        service.job_detail(env.conn, job_id)
