"""What a login does to the engine, the service and the tools: the session goes on every transport, the login-only
transport applies while logged in, a dead session is dropped (not blamed on the exit), a platform that cannot filter by
city is filtered after the fact, and the overview / checks the pages and tools read. Real cookie store and auth module,
fake platforms behind httpx.MockTransport, no browser."""
import asyncio
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from djs import auth, browser, cli, db, egress, engine, pacer, providers, server, service, taskqueue
from djs.providers.base import Login, Provider, get_json, need

SPEC = Login(url="https://site.test/login", method="qr", domain="site.test", cookies_any=("s",), unlocks="能看到更多")
PROBED = Login(url="https://site.test/login", method="qr", domain="site.test", cookies_any=("s",), probe_url="https://site.test/me",
               probe_fail_text="请先登录", unlocks="能看到更多")


class Site(Provider):
    """An optional-login platform on http."""
    name = "plain"
    label = "示例站"
    auth = "optional"
    login = SPEC
    interval, jitter, min_interval = 0.0, 0.0, 0.0
    page_size, first_page = 2, 1
    home = "https://site.test/"

    def fetch(self, client, payload, page):
        return need(get_json(client.get("https://site.test/list", params={"page": page})), "items")

    def job_id(self, job):
        return str(need(job, "id", typ=int))

    def row(self, job, detail):
        return {"title": f"t{job['id']}", "company": "c", "city": job.get("city"), "salary_text": "10-20K", "description": "jd"}


class Late(Site):
    """The logged-in endpoint only answers a real page."""
    name = "late"
    transports = ("http", "browser")
    login_transport = "browser"


class Unlisted(Late):
    name = "unlisted"
    login_transport = "cdp"   # not one of its transports: nothing to switch to


class Strict(Site):
    name = "strict"
    auth = "login"


class Filtered(Site):
    name = "filtered"
    city_filter = "client"
    page_size = 2


class Open(Site):
    name = "open"
    login = None
    auth = "public"


class Probed(Site):
    name = "probed"
    login = PROBED


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # server.main() autoloads ./.env, which must not be the developer's real one
    provs = {p.name: p for p in (Site(), Late(), Unlisted(), Strict(), Filtered(), Open(), Probed())}
    monkeypatch.setattr(providers, "get", lambda n: provs[n] if n in provs else (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(providers, "names", lambda: list(provs))
    conn = db.connect()
    yield SimpleNamespace(conn=conn, mp=monkeypatch, provs=provs, seen=[], pages={}, status=lambda request: 200)
    conn.close()


def serve(env, pages=None, status=None):
    """The platforms' network: every request is recorded as (provider, cookie header); `status(request)` decides the code."""
    pages = pages or {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}
    real = egress.make_client

    def handler(request):
        env.seen.append(request.headers.get("cookie"))
        code = (status or env.status)(request)
        if code != 200:
            return httpx.Response(code)
        return httpx.Response(200, json={"items": pages.get(int(request.url.params["page"]), [])})

    env.mp.setattr(egress, "make_client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))


class FakeBrowserClient:
    def __init__(self, items):
        self.gotos, self.items = [], items

    def goto(self, url, timeout=30):
        self.gotos.append(url)

    def get(self, url, params=None, **kw):
        return httpx.Response(200, json={"items": self.items.get(params["page"], [])}, request=httpx.Request("GET", url))


@pytest.fixture
def real_browser(env):
    """open_browser() hands out a fake page; opened lists (provider, exit) of every browser the engine started."""
    state = SimpleNamespace(opened=[], client=FakeBrowserClient({1: [{"id": 7}]}))

    @contextmanager
    def fake_open(prov, exit_id, interval, jitter):
        state.opened.append(prov.name)
        yield state.client

    env.mp.setattr(browser, "open_browser", fake_open)
    env.mp.setattr(browser, "ready", lambda: True)
    return state


def run(env, provider, **payload):
    tid = taskqueue.create(env.conn, provider, {"keyword": "py", **payload})
    engine.run_task(env.conn, taskqueue.claim(env.conn, provider))
    return taskqueue.get(env.conn, tid)


def login(provider, cookies=None):
    auth.save_cookies(provider, cookies or {"s": "1"})


# ============================================================ the session on every transport
def test_an_optional_platform_carries_the_saved_session_on_http_and_goes_anonymous_without_one(env):
    serve(env)
    assert run(env, "plain")["status"] == "done"
    login("plain")
    assert run(env, "plain")["status"] == "done"
    assert env.seen == [None, None, "s=1", "s=1"]   # two pages each; only the logged-in run sent the cookie


def test_a_platform_without_a_login_never_gets_a_cookie(env):
    serve(env)
    assert run(env, "open")["status"] == "done" and set(env.seen) == {None}


def test_a_login_platform_waits_for_its_session_and_then_runs_with_it(env):
    serve(env)
    row = run(env, "strict")
    assert row["status"] == "waiting_login" and env.seen == []
    login("strict")
    taskqueue.resume_login(env.conn, "strict")
    row = taskqueue.get(env.conn, row["id"])
    engine.run_task(env.conn, taskqueue.claim(env.conn, "strict"))
    assert taskqueue.get(env.conn, row["id"])["status"] == "done" and set(env.seen) == {"s=1"}


# ============================================================ the transport made for logged-in crawling
def test_transport_is_the_choice_else_the_login_one_else_the_first(env):
    c = env.conn
    assert pacer.transport(c, "late", False) == "http" and pacer.transport(c, "late", True) == "browser"
    pacer.set_transport(c, "late", "http")   # the user chose, and a choice is a choice even if it is the default
    assert pacer.transport(c, "late", True) == "http"
    pacer.set_transport(c, "late", "browser")
    assert pacer.transport(c, "late", False) == "browser"
    pacer.set_transport(c, "late", None)
    assert pacer.transport(c, "late", True) == "browser"   # cleared again
    assert pacer.transport(c, "unlisted", True) == "http"   # a login transport it does not have is ignored
    assert pacer.transport(c, "plain", True) == "http"      # none declared


def test_while_logged_in_the_engine_uses_the_login_transport_unless_the_user_chose(env, real_browser):
    serve(env)
    assert run(env, "late")["status"] == "done" and real_browser.opened == []   # logged out: the default, http
    login("late")
    row = run(env, "late")
    assert row["status"] == "done" and real_browser.opened == ["late"] and row["stats"]["jobs_new"] >= 1
    assert real_browser.client.gotos == ["https://site.test/"]   # the page loaded first, as for any browser run
    pacer.set_transport(env.conn, "late", "http")
    env.seen.clear()
    assert run(env, "late")["status"] == "done" and real_browser.opened == ["late"] and "s=1" in env.seen   # http again, with the session
    pacer.set_transport(env.conn, "late", None)
    auth.invalidate("late")
    run(env, "late")
    assert real_browser.opened == ["late"]   # the session is dead: back to the default transport


def test_a_login_transport_the_platform_does_not_have_changes_nothing(env, real_browser):
    serve(env)
    login("unlisted")
    assert run(env, "unlisted")["status"] == "done" and real_browser.opened == [] and "s=1" in env.seen


def test_the_tools_report_the_transport_that_will_run(env):
    login("late")
    listing = {p["name"]: p for p in service.provider_list(env.conn)}
    assert (listing["late"]["transport"], listing["late"]["logged_in"]) == ("browser", True)
    assert listing["late"]["transports"] == ["http", "browser"]
    assert (listing["plain"]["transport"], listing["plain"]["logged_in"]) == ("http", False)
    assert "logged_in" not in listing["open"]   # no login, nothing to say about one
    auth.logout("late")
    assert {p["name"]: p["transport"] for p in service.provider_list(env.conn)}["late"] == "http"


def test_a_search_is_refused_early_when_the_login_transport_needs_a_browser_that_is_not_there(env, monkeypatch):
    monkeypatch.setattr(browser, "ready", lambda: False)
    assert service.crawl_start(env.conn, "late", keyword="x")["task_id"] == 1   # logged out: http needs no browser
    login("late")
    with pytest.raises(ValueError, match="playwright install chromium"):
        service.crawl_start(env.conn, "late", keyword="x")
    assert env.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


# ============================================================ a dead session
def rejects_the_session(request):
    return 401 if request.headers.get("cookie") else 200


def test_an_optional_platform_drops_a_dead_session_and_goes_on_without_it(env):
    serve(env, status=rejects_the_session)
    login("plain")
    row = run(env, "plain")
    assert (row["status"], row["attempts"]) == ("pending", 1)   # an ordinary retry ...
    assert auth.status("plain")["state"] == "expired"           # ... and the session is marked dead
    assert env.conn.execute("SELECT COUNT(*) FROM egress_health WHERE blocked_until > 0").fetchone()[0] == 0   # the exit did nothing wrong
    env.conn.execute("UPDATE tasks SET next_run_at=0")
    engine.run_task(env.conn, taskqueue.claim(env.conn, "plain"))
    assert taskqueue.get(env.conn, row["id"])["status"] == "done" and env.seen[-1] is None   # the retry ran anonymously


def test_a_login_platform_with_a_dead_session_waits_for_a_new_login(env):
    serve(env, status=rejects_the_session)
    login("strict")
    row = run(env, "strict")
    assert (row["status"], row["attempts"]) == ("waiting_login", 0) and auth.status("strict")["state"] == "expired"
    assert env.conn.execute("SELECT COUNT(*) FROM egress_health WHERE blocked_until > 0").fetchone()[0] == 0


def test_without_a_session_a_login_answer_is_a_block_as_before(env):
    serve(env, status=lambda request: 401)
    row = run(env, "plain")
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert env.conn.execute("SELECT COUNT(*) FROM egress_health WHERE blocked_until > 0").fetchone()[0] == 1   # nothing to drop: the exit pays
    assert auth.status("plain")["state"] == "not_logged_in"


def test_a_dead_session_in_a_browser_run_is_dropped_the_same_way(env, real_browser):
    real_browser.client.get = lambda *a, **k: httpx.Response(401, request=httpx.Request("GET", "https://site.test/"))
    login("late")
    row = run(env, "late")
    assert (row["status"], auth.status("late")["state"]) == ("pending", "expired")


# ============================================================ cities the site cannot filter
CITIES = {1: [{"id": 1, "city": "杭州"}, {"id": 2, "city": "上海"}], 2: [{"id": 3, "city": "杭州、上海"}, {"id": 4, "city": None}],
          3: [{"id": 5, "city": "上海"}]}


def stored_titles(env):
    return sorted(r["title"] for r in env.conn.execute("SELECT title FROM jobs"))


def test_a_site_that_cannot_filter_by_city_is_filtered_after_each_page(env):
    serve(env, CITIES)
    row = run(env, "filtered", city="杭州市")   # "市" is dropped like everywhere else
    assert stored_titles(env) == ["t1", "t3"]   # placed in 杭州, also one of several places; nothing without a city
    stats = row["stats"]
    assert (stats["pages"], stats["filtered"], stats["jobs_new"]) == (3, 3, 2)   # page 1 kept 1 of 2 and the loop still went on
    assert env.conn.execute("SELECT COUNT(*) FROM task_jobs").fetchone()[0] == 2   # dropped rows are not linked to the task either


def test_without_a_city_nothing_is_filtered_and_nothing_is_counted(env):
    serve(env, CITIES)
    row = run(env, "filtered")
    assert len(stored_titles(env)) == 5 and "filtered" not in row["stats"]


def test_only_a_platform_that_says_so_is_filtered_by_the_engine(env):
    serve(env, {1: [{"id": 1, "city": "上海"}]})
    row = run(env, "plain", city="杭州")   # city_filter 'none': the site's own business
    assert stored_titles(env) == ["t1"] and "filtered" not in row["stats"]


# ============================================================ what the pages and tools read
def test_the_overview_lists_every_platform_that_has_a_login_with_its_state(env):
    login("late")
    auth.save_cookies("strict", {"s": "1"})
    auth.invalidate("strict")
    rows = {r["provider"]: r for r in service.login_overview()}
    assert set(rows) == {"plain", "late", "unlisted", "strict", "filtered", "probed"}   # not the one without a login
    assert (rows["late"]["state"], rows["strict"]["state"], rows["plain"]["state"]) == ("logged_in", "expired", "not_logged_in")
    assert (rows["late"]["label"], rows["strict"]["auth"], rows["late"]["method"], rows["late"]["unlocks"]) == ("示例站", "login", "qr", "能看到更多")
    assert list(rows) == [n for n in providers.names() if n != "open"]   # registry order


def test_login_status_is_one_platform_or_all_and_check_asks_the_platform(env, monkeypatch):
    assert [r["provider"] for r in service.login_status(env.conn)][0] == "plain"
    assert service.login_status(env.conn, "late")["state"] == "not_logged_in"
    with pytest.raises(ValueError, match="open does not need login"):
        service.login_status(env.conn, "open")
    login("probed")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, text="请先登录", request=httpx.Request("GET", url)))
    assert service.login_check(env.conn, "probed")["state"] == "expired"


def test_checking_all_reports_the_platform_it_could_not_ask_and_goes_on(env, monkeypatch):
    login("probed")
    login("plain")

    def down(url, **kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", down)
    rows = {r["provider"]: r for r in service.check_all_logins()}
    assert rows["probed"]["state"] == "logged_in" and rows["probed"]["error"].startswith("ConnectError")   # unchanged, and says why
    assert rows["plain"]["state"] == "logged_in" and "error" not in rows["plain"]   # no probe: nothing to ask


def test_the_startup_check_runs_in_the_background_and_never_holds_startup_back(env, monkeypatch):
    login("probed")
    release, asked = threading.Event(), []

    def slow(provider):
        asked.append(provider)
        release.wait(5)
        return auth.status(provider)

    monkeypatch.setattr(auth, "check", slow)
    started = time.monotonic()
    service.start_login_check()
    assert time.monotonic() - started < 0.5   # it returned while the first check was still waiting
    deadline = time.monotonic() + 5
    while not asked and time.monotonic() < deadline:
        time.sleep(0.01)
    thread = next(t for t in threading.enumerate() if t.name == "djs-login-check")
    assert thread.daemon and thread.is_alive() and asked == ["plain"]
    release.set()
    thread.join(5)
    assert not thread.is_alive() and asked == ["plain", "late", "unlisted", "strict", "filtered", "probed"]


def test_the_server_starts_the_background_check_before_it_serves(env, monkeypatch):
    order = []
    monkeypatch.setattr(engine, "start_worker", lambda: order.append("workers"))
    monkeypatch.setattr(service, "start_login_check", lambda: order.append("check"))
    monkeypatch.setattr(server.mcp, "run", lambda transport: order.append("serve"))
    monkeypatch.setattr(server.sys, "argv", ["djs"])
    server.main()
    assert order == ["workers", "check", "serve"]


# ============================================================ the tools
def call(name, **args):
    out = asyncio.run(server.mcp.call_tool(name, args))
    return json.loads(out[0].text)


def window_with_login(monkeypatch):
    @contextmanager
    def open_page(provider_name, headless, proxy=None):
        yield SimpleNamespace(context=SimpleNamespace(cookies=lambda: [{"name": "s", "value": "v", "domain": ".site.test", "expires": -1}],
                                                       clear_cookies=lambda name: None),
                              url="https://site.test/login", goto=lambda url, **kw: None, is_closed=lambda: False)

    monkeypatch.setattr(browser, "open_page", open_page)


def test_the_login_tools(env, monkeypatch):
    rows = call("login_status")["result"]
    assert [r["provider"] for r in rows][:2] == ["plain", "late"] and {r["state"] for r in rows} == {"not_logged_in"}
    assert call("login_status", provider="late")["state"] == "not_logged_in"
    assert "does not need login" in call("login_status", provider="open")["error"]
    assert "KeyError" in call("login_status", provider="no-such")["error"]
    taskqueue.create(env.conn, "strict", {"keyword": "py"})
    env.conn.execute("UPDATE tasks SET status='waiting_login'")
    window_with_login(monkeypatch)
    got = call("login", provider="strict")
    assert (got["logged_in"], got["state"], got["resumed_tasks"]) == (True, "logged_in", 1)   # the window opened, the login was seen
    assert call("login_status", provider="strict")["state"] == "logged_in"
    assert call("login_check", provider="strict")["state"] == "logged_in"
    assert {r["provider"] for r in call("login_check")["result"]} == {"plain", "late", "unlisted", "strict", "filtered", "probed"}
    assert call("logout", provider="strict")["logged_in"] is False and call("login_status", provider="strict")["state"] == "not_logged_in"
    assert "does not need login" in call("login", provider="open")["error"]


def test_djs_call_runs_the_login_tools_like_any_other(env, monkeypatch, capsys):
    window_with_login(monkeypatch)
    assert cli.main(["call", "login", '{"provider": "plain"}']) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "logged_in"
    assert cli.main(["call", "login_status", '{"provider": "plain"}']) == 0
    assert json.loads(capsys.readouterr().out)["logged_in"] is True
    assert cli.main(["call", "login_status"]) == 0
    assert len(json.loads(capsys.readouterr().out)["result"]) == 6
    assert cli.main(["call", "login", '{"provider": "open"}']) == 1
    assert "does not need login" in json.loads(capsys.readouterr().out)["error"]
