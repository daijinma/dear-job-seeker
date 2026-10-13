"""The MCP tools the way a host calls them: over the protocol, in this process (the real server and a client session joined
in memory), so the fake platforms of the other tests still work. Every tool is called, bad arguments are refused, every
result is one JSON object, and a failing tool answers {"error"} instead of taking the server down. Fake platforms behind
httpx.MockTransport, the real cookie store in a temp data folder, a fake login window, no network, no browser."""
import asyncio
import concurrent.futures
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from djs import auth, browser, db, egress, engine, providers, server, service, taskqueue
from djs.providers.base import Login, Provider, get_json, need

TOOLS = {"status", "provider_list", "crawl_start", "search", "wait_for_search", "task_list", "jobs_get", "job_detail",
         "search_list", "login", "login_cancel", "login_status", "login_check", "logout", "set_frequency", "set_transport",
         "egress_add", "egress_remove", "egress_list"}
REQUIRED = {"crawl_start": ["provider"], "search": ["keyword"], "wait_for_search": ["keyword"], "job_detail": ["job_id"],
            "login": ["provider"], "login_cancel": ["provider"], "logout": ["provider"], "set_frequency": ["provider"],
            "set_transport": ["provider"], "egress_add": ["url"], "egress_remove": ["url"]}   # every other argument has a default
ITEMS = [{"id": 1, "title": "Python 后端", "company": "甲科技有限公司", "pay": "15-25K·14薪", "jd": "使用 Django 开发"},
         {"id": 2, "title": "Java 开发", "company": "乙公司", "pay": "8-12K", "jd": "使用 Spring"}]
SESSION = Login(url="https://opt.test/login", method="qr", domain="opt.test", cookies_any=("s",))
COOKIE = {"name": "s", "value": "v", "domain": ".opt.test", "expires": -1}   # what a finished login leaves in the window


class Site(Provider):
    interval, jitter, min_interval = 0.0, 0.0, 0.0
    page_size, first_page = 3, 1

    def fetch(self, client, payload, page):
        return need(get_json(client.get(f"https://{self.name}.test/list", params={"q": payload["keyword"], "page": page})), "items")

    def job_id(self, job):
        return str(need(job, "id", typ=int))

    def row(self, job, detail):
        return {"title": job["title"], "company": job["company"], "city": "杭州", "salary_text": job["pay"],
                "description": job["jd"]}

    def meta(self, job, detail):
        return {"source_url": f"https://{self.name}.test/job/{job['id']}", "apply_url": None, "published_at": None,
                "published_at_kind": "unknown"}


def site(name, label, **attrs):
    return type(name, (Site,), {"name": name, "label": label, **attrs})()


class Client:
    """The server and a client session in memory, on an event loop of their own: test code stays plain and sync, and a
    call that blocks (login) can wait in the background while another is made. Session and server end in one task."""

    def __init__(self):
        self.called, self.pool = set(), concurrent.futures.ThreadPoolExecutor(2)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="mcp-contract-client")
        self.thread.start()
        self.ready, self.stop = concurrent.futures.Future(), None
        self.serving = asyncio.run_coroutine_threadsafe(self._serve(), self.loop)
        self.serving.add_done_callback(lambda done: self.ready.done() or self.ready.set_exception(done.exception()))
        self.session = self.ready.result(30)

    async def _serve(self):
        self.stop = asyncio.Event()
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
            self.ready.set_result(session)
            await self.stop.wait()

    def raw(self, name, args=None):
        """The protocol's own answer (CallToolResult): isError and the content blocks."""
        self.called.add(name)
        return asyncio.run_coroutine_threadsafe(self.session.call_tool(name, args or {}), self.loop).result(60)

    def tools(self):
        return asyncio.run_coroutine_threadsafe(self.session.list_tools(), self.loop).result(30).tools

    @staticmethod
    def json_of(result) -> dict:
        """What a host reads: one text block holding one JSON object."""
        [block] = result.content
        assert block.type == "text"
        got = json.loads(block.text)
        assert isinstance(got, dict)
        return got

    def call(self, name, **args) -> dict:
        result = self.raw(name, args)
        assert not result.isError, result.content
        return self.json_of(result)

    def submit(self, name, **args) -> concurrent.futures.Future:
        """A call that runs in the background (login waits for the user)."""
        return self.pool.submit(self.call, name, **args)

    async def _end_calls_in_flight(self):
        """A call still waiting when the connection ended never gets an answer: end it."""
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in leftovers:
            t.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)

    def close(self):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.serving.result(30)   # the server's lifespan ends here: login windows closed, workers stopped
        asyncio.run_coroutine_threadsafe(self._end_calls_in_flight(), self.loop).result(30)
        asyncio.run_coroutine_threadsafe(self.loop.shutdown_default_executor(), self.loop).result(30)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(30)
        self.loop.close()
        self.pool.shutdown()


def wait_until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "gave up waiting"
        time.sleep(0.005)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    provs = {p.name: p for p in (
        site("a", "甲站"), site("b", "乙站"),                                          # public; "b" has nothing
        site("opt", "丙站", auth="optional", login=SESSION),                            # works anonymously; a login adds
        site("needy", "丁站", auth="login", login=SESSION),                             # needs the user's login
        site("slow", "戊站", interval=5.0, min_interval=5.0),                           # a floor under the frequency
        site("keyed", "己站", requires_env=("DJS_FAKE_CONTRACT_KEY",)))}                # a key that is not set
    monkeypatch.delenv("DJS_FAKE_CONTRACT_KEY", raising=False)
    monkeypatch.setattr(providers, "get", lambda n: provs[n] if n in provs else (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(providers, "names", lambda: list(provs))
    monkeypatch.setattr(browser, "ready", lambda: False)
    monkeypatch.setattr(browser, "find_browser", lambda: ("chrome", Path("/apps/chrome")))
    monkeypatch.setattr(server, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(auth, "POLL_SECONDS", 0.01)
    real = egress.make_client
    handler = lambda request: httpx.Response(200, json={"items": ITEMS if request.url.host == "a.test" else []})  # noqa: E731
    monkeypatch.setattr(egress, "make_client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    window = SimpleNamespace(cookies=[], opened=[], closed=[])   # the login window: `cookies` is what the user did in it

    @contextmanager
    def plain_window(provider_name, url, headless=False):
        window.opened.append(provider_name)
        try:
            yield SimpleNamespace(cookies=lambda: list(window.cookies), url=lambda: url, closed=lambda: False,
                                  delete_cookies=lambda names, domain: None)
        finally:
            window.closed.append(provider_name)

    monkeypatch.setattr(browser, "plain_window", plain_window)
    conn = db.connect()
    yield SimpleNamespace(conn=conn, mp=monkeypatch, provs=provs, window=window)
    engine.stop_worker(10)
    conn.close()


@pytest.fixture
def updater(monkeypatch):
    """What the updater (djs.update) answers to `status`: a dict a test may change."""
    from djs import update
    answer = {"current": "1.2.6", "latest": None, "state": "none", "note": "", "install_on_quit": False}
    monkeypatch.setattr(update, "current", lambda: dict(answer))
    return answer


@pytest.fixture
def client(app, updater):
    c = Client()
    yield c
    c.close()


# ---- every tool, through the protocol
def test_the_server_lists_the_tools_with_what_a_host_needs_to_call_them(client):
    tools = {t.name: t for t in client.tools()}
    assert set(tools) == TOOLS
    for name, tool in tools.items():
        assert tool.description and tool.inputSchema["type"] == "object", name
        assert tool.inputSchema.get("required", []) == REQUIRED.get(name, []), name


def test_every_tool_answers_a_valid_call_with_one_json_object(client, app):
    call = client.call
    assert call("provider_list")["result"][0]["name"] == "a" and len(call("provider_list")["result"]) == len(app.provs)
    assert call("egress_add", url="127.0.0.1:7890") == {"url": "http://127.0.0.1:7890", "provider": "*"}
    assert [e["id"] for e in call("egress_list")["result"]] == ["http://127.0.0.1:7890"]
    assert call("egress_remove", url="http://127.0.0.1:7890") == {"removed": 1}
    assert call("egress_list")["result"] == []
    assert call("set_frequency", provider="a", interval=0.0, jitter=0.0, max_pages=3) == {
        "provider": "a", "interval": 0.0, "jitter": 0.0, "max_pages": 3}
    assert call("set_transport", provider="a", transport="http") == {"provider": "a", "transport": "http"}

    auth.save_cookies("opt", {"s": "v"})   # a saved login, which the check (no probe configured) finds still good
    assert call("login_status", provider="opt")["state"] == "logged_in"
    assert {r["provider"]: r["state"] for r in call("login_status")["result"]} == {"opt": "logged_in", "needy": "not_logged_in"}
    assert call("login_check", provider="opt")["state"] == "logged_in"
    assert {r["provider"] for r in call("login_check")["result"]} == {"opt", "needy"}
    assert call("logout", provider="opt") == {"provider": "opt", "logged_in": False}
    assert call("login_status", provider="opt")["state"] == "not_logged_in"

    queued = call("crawl_start", provider="b", keyword="go")["task_id"]
    assert [t["id"] for t in call("task_list", status="pending")["result"]] == [queued]
    engine.start_worker()
    started = call("search", keyword="py", city="杭州", providers=["a"])
    assert set(started) == {"keyword", "city", "providers", "task_ids", "skipped"} and started["providers"] == ["a"]
    done = call("wait_for_search", keyword="py", city="杭州", timeout=30)
    assert (done["status"], [(s["provider"], s["outcome"]) for s in done["per_site"]]) == ("done", [("a", "found")])
    assert done["whole"] == 2 and done["task_ids"] == started["task_ids"]
    got = call("jobs_get", keyword="py", city="杭州", title="python", floor_month_salary=15000)
    assert set(got) == {"jobs", "total", "returned", "next_offset", "searching"} and got["searching"] is False
    [job] = got["jobs"]
    assert (job["title"], job["salary_month_min"], job["provider"]) == ("Python 后端", 15000, "a") and got["next_offset"] is None
    detail = call("job_detail", job_id=job["id"])
    assert detail["id"] == job["id"] and detail["description"] == "使用 Django 开发" and "detail" not in detail
    assert "detail" in call("job_detail", job_id=job["id"], raw=True)
    listed = {s["keyword"]: s for s in call("search_list", limit=5)["result"]}
    assert set(listed) == {"py", "go"} and (listed["py"]["city"], listed["py"]["status"], listed["py"]["whole"]) == ("杭州", "done", 2)

    engine.stop_worker(10)   # so that nobody picks up the task below before it is marked
    taskqueue.create(app.conn, "needy", {"keyword": "py"})
    app.conn.execute("UPDATE tasks SET status='waiting_login' WHERE provider='needy'")
    app.window.cookies.append(COOKIE)   # the user logs in as soon as the window is there
    logged = call("login", provider="needy")
    assert (logged["logged_in"], logged["state"], logged["resumed_tasks"]) == (True, "logged_in", 1)
    assert call("login_cancel", provider="needy") == {"provider": "needy", "cancelled": False}   # nothing is waiting any more
    assert call("status")["version"]
    assert client.called == TOOLS   # none was left out


def test_login_cancel_ends_the_login_that_waits_for_the_user_and_the_task_goes_on_waiting(client, app):
    task = taskqueue.create(app.conn, "needy", {"keyword": "py"})
    app.conn.execute("UPDATE tasks SET status='waiting_login' WHERE id=?", (task,))
    waiting = client.submit("login", provider="needy")   # blocks: nobody logs in
    wait_until(lambda: auth.pending_logins())
    assert app.window.opened == ["needy"] and app.window.closed == []   # the window is there
    assert not waiting.done()
    status = client.call("status")   # the server answers meanwhile and says what is open
    assert [w["provider"] for w in status["browser"]["login_windows"]] == ["needy"]
    assert client.call("login_cancel", provider="needy") == {"provider": "needy", "cancelled": True}
    assert waiting.result(10) == {"error": "LoginCancelled: 已取消登录"}   # the host that gave up is not kept waiting
    assert app.window.closed == ["needy"] and auth.pending_logins() == []
    assert client.call("status")["browser"]["login_windows"] == []
    assert client.call("login_cancel", provider="needy") == {"provider": "needy", "cancelled": False}   # it was the only one
    assert client.call("login_status", provider="needy")["logged_in"] is False   # nothing was saved
    assert taskqueue.get(app.conn, task)["status"] == "waiting_login"   # the tasks still wait for a login


def test_login_cancel_stops_only_the_platform_it_names(client, app):
    waiting = client.submit("login", provider="opt")
    wait_until(lambda: auth.pending_logins())
    assert client.call("login_cancel", provider="needy") == {"provider": "needy", "cancelled": False}
    assert not waiting.done() and app.window.closed == []
    client.call("login_cancel", provider="opt")
    assert waiting.result(10)["error"].startswith("LoginCancelled")


def test_a_login_that_is_still_waiting_when_the_connection_ends_is_closed_with_it(app, updater):
    c = Client()
    waiting = c.submit("login", provider="opt")
    wait_until(lambda: auth.pending_logins())
    c.close()   # the host goes away: the server's lifespan closes the window, so no browser outlives the connection
    assert app.window.closed == ["opt"] and auth.pending_logins() == []
    assert waiting in concurrent.futures.wait([waiting], 10).done   # the call ended too, with an answer or with the connection


# ---- what a host is told when it asks for something wrong
@pytest.mark.parametrize("tool, args, field, why", [
    ("search", {}, "keyword", "Field required"),
    ("search", {"keyword": "py", "max_pages": "many"}, "max_pages", "valid integer"),
    ("search", {"keyword": "py", "providers": "a"}, "providers", "valid list"),
    ("search", {"keyword": ["py"]}, "keyword", "valid string"),
    ("wait_for_search", {"keyword": "py", "timeout": "soon"}, "timeout", "valid number"),
    ("jobs_get", {"limit": "many"}, "limit", "valid integer"),
    ("jobs_get", {"complete_only": "perhaps"}, "complete_only", "valid boolean"),
    ("job_detail", {}, "job_id", "Field required"),
    ("job_detail", {"job_id": "x"}, "job_id", "valid integer"),
    ("crawl_start", {"provider": 5}, "provider", "valid string"),
    ("task_list", {"limit": "many"}, "limit", "valid integer"),
    ("login", {}, "provider", "Field required"),
    ("login_cancel", {}, "provider", "Field required"),
    ("login_cancel", {"provider": None}, "provider", "valid string"),
    ("logout", {}, "provider", "Field required"),
    ("set_frequency", {"provider": "a", "force": "maybe"}, "force", "valid boolean"),
    ("set_transport", {"transport": "http"}, "provider", "Field required"),
    ("egress_add", {}, "url", "Field required"),
    ("egress_remove", {}, "url", "Field required"),
])
def test_arguments_that_do_not_fit_the_schema_are_refused_before_the_tool_runs(client, app, tool, args, field, why):
    result = client.raw(tool, args)
    [block] = result.content
    assert result.isError is True and tool in block.text and field in block.text and why in block.text
    assert app.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0 and app.window.opened == []   # nothing ran
    assert client.call("status")["queue"]["pending"] == 0   # and the server is as it was


def test_an_unknown_tool_is_refused_and_the_server_goes_on(client):
    result = client.raw("no_such_tool")
    [block] = result.content
    assert result.isError is True and "no_such_tool" in block.text
    assert client.call("status")["version"]


# ---- a tool that fails answers {"error"}: the call succeeded, the server did not crash
@pytest.mark.parametrize("tool, args, message", [
    ("search", {"keyword": "  "}, "ValueError: keyword is required"),
    ("search", {"keyword": "py", "providers": ["nope"]}, "KeyError: 'nope'"),
    ("search", {"keyword": "py", "providers": ["keyed"]}, "no platform can take this search: 己站"),
    ("crawl_start", {"provider": "nope"}, "KeyError: 'nope'"),
    ("wait_for_search", {"keyword": "never searched"}, "no search for keyword='never searched'"),
    ("jobs_get", {"keyword": "never searched", "city": "杭州"}, "no search for keyword='never searched', city='杭州'"),
    ("jobs_get", {"fields": "huge"}, "fields must be 'summary' or 'full'"),
    ("job_detail", {"job_id": 999}, "ValueError: job 999 not found"),
    ("login", {"provider": "a"}, "ValueError: a does not need login"),
    ("login_status", {"provider": "a"}, "a does not need login"),
    ("login_check", {"provider": "a"}, "a does not need login"),
    ("set_frequency", {"provider": "slow", "interval": 1.0}, "pass force=True to override"),
    ("set_frequency", {"provider": "a", "max_pages": 0}, "max_pages >= 1"),
    ("set_frequency", {"provider": "nope"}, "KeyError: 'nope'"),
    ("set_transport", {"provider": "a", "transport": "browser"}, "supports transport(s) http, not 'browser'"),
    ("egress_add", {"url": "ftp://proxy.test"}, "egress url must be 'direct', http(s)://host:port or host:port"),
    ("egress_add", {"url": "127.0.0.1:7890", "provider": "nope"}, "KeyError: 'nope'"),
])
def test_a_tool_that_fails_says_so_in_an_error_object_and_does_not_raise(client, tool, args, message):
    result = client.raw(tool, args)
    assert result.isError is False   # the host got an answer
    got = client.json_of(result)
    assert set(got) == {"error"} and message in got["error"]
    assert client.call("status")["version"]   # and the server answers the next call


def test_a_search_with_an_unknown_platform_starts_nothing_and_a_platform_that_cannot_take_it_is_skipped(client, app):
    got = client.call("search", keyword="py", providers=["a", "nope"])
    assert set(got) == {"error"} and "KeyError" in got["error"]
    assert app.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0   # "a" did not start either
    got = client.call("search", keyword="py", providers=["a", "keyed"])
    assert got["providers"] == ["a"] and [(s["provider"], s["label"]) for s in got["skipped"]] == [("keyed", "己站")]
    assert "DJS_FAKE_CONTRACT_KEY" in got["skipped"][0]["reason"]


def test_whatever_goes_wrong_inside_a_tool_comes_back_as_an_error_object(client, app):
    def boom(conn):
        raise RuntimeError("boom")

    with app.mp.context() as patch:
        patch.setattr(service, "status", boom)
        assert client.call("status") == {"error": "RuntimeError: boom"}
    with app.mp.context() as patch:   # a database that cannot be opened: the host reads the reason, not a stack trace
        patch.setattr(db, "connect", lambda: (_ for _ in ()).throw(OSError("disk is full")))
        assert client.call("task_list") == {"error": "OSError: disk is full"}
        assert client.call("search", keyword="py") == {"error": "OSError: disk is full"}
    assert client.call("task_list") == {"result": []}   # the server is fine again


def test_a_call_that_waits_does_not_hold_the_other_calls_up(client):
    client.call("search", keyword="py", city="杭州", providers=["a"])   # nobody works the queue yet
    waiting = client.submit("wait_for_search", keyword="py", city="杭州", timeout=30)
    time.sleep(0.05)   # a few polls
    assert not waiting.done()
    assert client.call("status")["queue"]["pending"] == 1   # other calls are served meanwhile
    assert [t["status"] for t in client.call("task_list")["result"]] == ["pending"]
    engine.start_worker()
    assert waiting.result(30)["status"] == "done"


# ---- status over the protocol
def test_status_carries_the_updater_state_and_the_running_app(client, updater):
    got = client.call("status")
    assert got["update"] == {"current": "1.2.6", "latest": None, "state": "none", "note": ""}
    assert set(got["app"]) == {"version", "frozen", "path"} and got["app"]["frozen"] is False
    assert got["app"]["path"] == str(Path(service.__file__).resolve().parent.parent)
    assert got["version"] == got["app"]["version"]   # one version, said twice
    updater.update(latest="1.4.0", state="ready", note="新版本已下载")
    assert client.call("status")["update"] == {"current": "1.2.6", "latest": "1.4.0", "state": "ready", "note": "新版本已下载"}


def test_status_answers_at_once_and_without_the_network(client, monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("status must not touch the network"))
    begun = time.monotonic()
    got = client.call("status")
    assert time.monotonic() - begun < 1
    assert {"version", "app", "update", "data_dir", "workers", "queue", "exits", "browser", "logins", "platforms"} == set(got)
