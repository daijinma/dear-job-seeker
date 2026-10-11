import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from djs import db, egress, engine, pacer, providers, server, service, taskqueue
from djs.providers.base import Blocked, Delisted, LoginRequired, Provider, get_json, need


class Fake(Provider):
    name = "fake"
    interval, jitter, min_interval = 0.0, 0.0, 0.5
    page_size, first_page = 2, 1
    page_cap = 3
    delist = ()

    def fetch(self, client, payload, page):
        r = client.get("https://fake.test/list", params={"page": page})
        if r.status_code == 403:
            raise Blocked("403", retry_after=None)
        if r.status_code == 401:
            raise LoginRequired("401")
        return need(get_json(r), "items")

    def job_id(self, job):
        return str(need(job, "id", typ=int))

    def detail(self, client, job):
        if job["id"] in self.delist:
            raise Delisted()
        return {"jd": f"jd{job['id']}"}

    def row(self, job, detail):
        return {"title": f"t{job['id']}", "company": "c", "city": None, "salary_text": None,
                "description": detail and detail["jd"]}


class FakeLogin(Fake):
    name = "fakelogin"
    auth = "login"
    requires_city = True


class FakeAuth:
    def __init__(self):
        self.cookies, self.invalidated = None, []

    def load_cookies(self, p):
        return self.cookies

    def invalidate(self, p):
        self.invalidated.append(p)
        self.cookies = None

    def status(self, p):
        return {"logged_in": self.cookies is not None}

    def login(self, p, timeout=180):
        self.cookies = {"s": "1"}
        return {"logged_in": True}

    def logout(self, p):
        self.cookies = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    provs = {"fake": Fake(), "fakelogin": FakeLogin()}
    monkeypatch.setattr(providers, "get", lambda n: provs[n] if n in provs else (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(providers, "names", lambda: list(provs))
    fa = FakeAuth()
    for mod in (engine, service):
        monkeypatch.setattr(mod, "auth", fa)
    conn = db.connect()
    yield SimpleNamespace(conn=conn, auth=fa, provs=provs, mp=monkeypatch)
    conn.close()


def mock_client(env, handler):
    real = egress.make_client
    env.mp.setattr(egress, "make_client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))


def pages(data):
    """handler serving data[page] -> items; unknown page -> []"""
    return lambda req: httpx.Response(200, json={"items": data.get(int(req.url.params["page"]), [])})


def task(env, provider="fake", **payload):
    payload.setdefault("keyword", "py")
    tid = taskqueue.create(env.conn, provider, payload)
    return env.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def status(env, tid):
    return env.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


# ---- queue ----
def test_create_validation(env):
    with pytest.raises(ValueError):
        taskqueue.create(env.conn, "fake", {})
    with pytest.raises(ValueError):
        taskqueue.create(env.conn, "fakelogin", {"keyword": "x"})
    with pytest.raises(KeyError):
        taskqueue.create(env.conn, "nope", {"keyword": "x"})


def test_claim_atomic_two_threads(env):
    for _ in range(20):
        task(env)
    got, lock = [], threading.Lock()

    def worker():
        c = db.connect()
        while (t := taskqueue.claim(c)) is not None:
            with lock:
                got.append(t["id"])
        c.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(got) == list(range(1, 21))  # each exactly once


def test_claim_reclaims_stale_running(env):
    t = task(env)
    taskqueue.claim(env.conn)
    assert taskqueue.claim(env.conn) is None
    env.conn.execute("UPDATE tasks SET locked_at=? WHERE id=?", (time.time() - 16 * 60, t["id"]))
    assert taskqueue.claim(env.conn)["id"] == t["id"]


def test_retry_backoff_then_dead(env):
    t = task(env)
    for n in range(7):
        taskqueue.retry(env.conn, t["id"], "boom")
        row = status(env, t["id"])
        assert row["status"] == "pending" and row["attempts"] == n + 1
        want = min(2 ** (n + 1), 360)
        delay = row["next_run_at"] - time.time()
        assert want - 1 <= delay <= want * 1.25 + 1
    taskqueue.retry(env.conn, t["id"], "boom")
    assert status(env, t["id"])["status"] == "dead"


# ---- pacer ----
def test_frequency_override_and_floor(env):
    assert pacer.effective(env.conn, "fake") == (0.0, 0.0, pacer.DEFAULT_MAX_PAGES)
    with pytest.raises(ValueError):
        pacer.set_frequency(env.conn, "fake", interval=0.1)
    pacer.set_frequency(env.conn, "fake", interval=2, max_pages=7)
    pacer.set_frequency(env.conn, "fake", jitter=0.5)  # partial update keeps interval
    assert pacer.effective(env.conn, "fake") == (2, 0.5, 7)
    pacer.set_frequency(env.conn, "fake", interval=0.1, force=True)
    assert pacer.effective(env.conn, "fake")[0] == 0.1


def test_pacer_spacing_across_threads(env):
    stamps = []

    def hit():
        pacer.wait("fake", "direct", 0.2, 0)
        stamps.append(time.time())

    ts = [threading.Thread(target=hit) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    stamps.sort()
    assert all(b - a >= 0.18 for a, b in zip(stamps, stamps[1:]))


# ---- egress ----
def test_egress_dedicated_sticky_cooldown(env):
    c = env.conn
    with pytest.raises(ValueError):
        egress.add(c, "socks5://x")
    egress.add(c, "http://a:1")
    egress.add(c, "http://b:1")
    assert {egress.lease(c, "fake", str(k)) for k in range(30)} == {"http://a:1", "http://b:1"}
    assert egress.lease(c, "fake", "k") == egress.lease(c, "fake", "k")  # sticky
    egress.add(c, "http://ded:1", provider="fake")
    assert {egress.lease(c, "fake", str(k)) for k in range(30)} == {"http://ded:1"}
    egress.report(c, "http://ded:1", "fake", ok=False, blocked=True)
    with pytest.raises(egress.NoEgressAvailable):  # no fallback to shared pool
        egress.lease(c, "fake", "k")
    assert egress.list_(c)[0]["id"] == "http://a:1"


def test_egress_fail_streak_cooldown(env):
    c = env.conn
    egress.add(c, "http://a:1")
    for _ in range(2):
        egress.report(c, "http://a:1", "fake", ok=False)
    assert egress.lease(c, "fake", "k") == "http://a:1"
    egress.report(c, "http://a:1", "fake", ok=False)
    with pytest.raises(egress.NoEgressAvailable):
        egress.lease(c, "fake", "k")


def test_login_provider_defaults_direct(env):
    egress.add(env.conn, "http://shared:1")
    assert egress.lease(env.conn, "fakelogin", "k") == "direct"
    egress.add(env.conn, "http://mine:1", provider="fakelogin")
    assert egress.lease(env.conn, "fakelogin", "k") == "http://mine:1"


# ---- engine ----
def test_engine_happy_path_and_dedupe(env):
    mock_client(env, pages({1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}))
    env.provs["fake"].delist = (2,)
    t = task(env)
    engine.run_task(env.conn, t)
    row = status(env, t["id"])
    assert row["status"] == "done"
    assert taskqueue.as_dict(row)["stats"] == {"pages": 2, "jobs_new": 3, "jobs_seen": 0, "details_ok": 3, "details_failed": 0}
    jobs = {j["source_job_id"]: j for j in service.jobs_get(env.conn, task_id=t["id"], with_detail=True)}
    assert jobs["1"]["description"] == "jd1" and jobs["2"]["detail"] is None and jobs["2"]["detail_error"] is None
    t2 = task(env)
    engine.run_task(env.conn, t2)
    assert taskqueue.as_dict(status(env, t2["id"]))["stats"]["jobs_seen"] == 3
    assert env.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_engine_respects_page_cap(env):
    mock_client(env, pages({p: [{"id": p * 10}, {"id": p * 10 + 1}] for p in range(1, 10)}))
    t = task(env, max_pages=9)
    engine.run_task(env.conn, t)
    assert taskqueue.as_dict(status(env, t["id"]))["stats"]["pages"] == 3  # Fake.page_cap


def test_engine_waiting_login_then_resume(env):
    mock_client(env, pages({1: [{"id": 1}]}))
    t = task(env, "fakelogin", city="x")
    engine.run_task(env.conn, t)
    assert status(env, t["id"])["status"] == "waiting_login"
    out = service.login(env.conn, "fakelogin")
    assert out["resumed_tasks"] == 1
    assert status(env, t["id"])["status"] == "pending"
    assert engine.tick(env.conn) is True
    assert status(env, t["id"])["status"] == "done"


def test_engine_blocked_cooldown_and_retry(env):
    mock_client(env, lambda r: httpx.Response(403))
    t = task(env)
    engine.run_task(env.conn, t)
    row = status(env, t["id"])
    assert row["status"] == "pending" and row["attempts"] == 1
    with pytest.raises(egress.NoEgressAvailable):  # the only (implicit direct) exit is cooling down
        egress.lease(env.conn, "fake", "x")
    engine.run_task(env.conn, task(env))  # now no egress: released, attempt not burned
    assert status(env, 2)["status"] == "pending" and status(env, 2)["attempts"] == 0


def test_engine_login_required_invalidates(env):
    env.auth.cookies = {"s": "1"}
    mock_client(env, lambda r: httpx.Response(401))
    t = task(env, "fakelogin", city="x")
    engine.run_task(env.conn, t)
    assert status(env, t["id"])["status"] == "waiting_login"
    assert env.auth.invalidated == ["fakelogin"]


def test_engine_http_error_retries(env):
    mock_client(env, lambda r: httpx.Response(500))
    t = task(env)
    engine.run_task(env.conn, t)
    assert status(env, t["id"])["status"] == "pending"


# ---- MCP in-process ----
def test_mcp_tools_smoke(env, monkeypatch):
    import asyncio

    def call(name, **args):
        return json.loads(asyncio.run(server.mcp.call_tool(name, args))[0].text)

    assert {t["name"] for t in call("provider_list")["result"]} == {"fake", "fakelogin"}
    assert call("crawl_start", provider="fake", keyword="py")["task_id"] == 1
    assert "error" in call("crawl_start", provider="nope", keyword="x")
    assert call("task_list")["result"][0]["status"] == "pending"
    assert call("jobs_get")["result"] == []
    assert call("login", provider="fakelogin")["logged_in"] is True
    assert call("login_status", provider="fakelogin")["logged_in"] is True
    assert call("logout", provider="fakelogin")["logged_in"] is False
    assert "error" in call("set_frequency", provider="fake", interval=0.1)
    assert call("set_frequency", provider="fake", interval=0.1, force=True)["interval"] == 0.1
    assert call("egress_add", url="http://p:1")["url"] == "http://p:1"
    assert call("egress_list")["result"][0]["id"] == "http://p:1"
    assert call("egress_remove", url="http://p:1")["removed"] == 1


def test_egress_masks_password_and_shared_health(env):
    c = env.conn
    egress.add(c, "http://u:secret@h:1")
    assert "secret" not in str(egress.add(c, "http://u:secret@h:1")) + str(egress.list_(c))
    egress.report(c, "http://u:secret@h:1", "fake", ok=False, blocked=True)
    row = egress.list_(c)[0]
    assert row["id"] == "http://u:***@h:1" and row["blocked_until"] > time.time() and row["total_fail"] == 1
    with pytest.raises(KeyError):
        egress.add(c, "http://x:1", provider="nope")
    assert egress.remove(c, row["id"]) == 1 and egress.list_(c) == []


def test_create_rejects_bad_max_pages(env):
    for bad in (0, -1, "3", True):
        with pytest.raises(ValueError):
            taskqueue.create(env.conn, "fake", {"keyword": "k", "max_pages": bad})
