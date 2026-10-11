import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from djs import db, egress, engine, pacer, providers, server, service, taskqueue
from djs.providers.base import Blocked, Delisted, LoginRequired, Provider, StructureError, get_json, need


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


class Fake2(Fake):
    name = "fake2"


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

    def logged_in(self, p):
        return self.cookies is not None

    def status(self, p):
        return {"logged_in": self.logged_in(p)}

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


def boom(exc):
    def fn(*args, **kwargs):
        raise exc
    return fn


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


@pytest.fixture
def start_workers(env):
    yield engine.start_worker
    engine.stop_worker()


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


def test_claim_atomic_with_provider_filter(env):
    want = {"fake": [task(env)["id"] for _ in range(15)],
            "fakelogin": [task(env, "fakelogin", city="x")["id"] for _ in range(15)]}
    got, lock = {"fake": [], "fakelogin": []}, threading.Lock()

    def worker(name):
        c = db.connect()
        while (t := taskqueue.claim(c, name)) is not None:
            with lock:
                got[name].append(t["id"])
        c.close()

    ts = [threading.Thread(target=worker, args=(n,)) for n in ("fake", "fakelogin") * 2]  # two threads per platform
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert {n: sorted(ids) for n, ids in got.items()} == want  # each task exactly once, only by its own platform


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
    assert taskqueue.as_dict(row)["stats"] == {"pages": 2, "jobs_new": 3, "jobs_seen": 0, "details_ok": 3,
                                               "details_failed": 0, "complete": 0, "incomplete": 3}
    jobs = {j["source_job_id"]: j for j in service.jobs_get(env.conn, task_id=t["id"], with_detail=True,
                                                            complete_only=False)}
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


# ---- per-platform workers ----
def test_claim_filters_by_provider(env):
    a, b = task(env), task(env, "fakelogin", city="x")
    assert taskqueue.claim(env.conn, "fakelogin")["id"] == b["id"]
    assert taskqueue.claim(env.conn, "fakelogin") is None
    assert taskqueue.claim(env.conn)["id"] == a["id"]


def test_workers_run_platforms_in_parallel(env, start_workers):
    env.provs["fake2"] = Fake2()
    barrier = threading.Barrier(2, timeout=5)  # only passes while both platforms are fetching at the same moment

    def handler(request):
        barrier.wait()
        return httpx.Response(200, json={"items": []})

    mock_client(env, handler)
    ids = [task(env)["id"], task(env, "fake2")["id"]]
    start_workers()
    start_workers()  # idempotent
    assert wait_for(lambda: all(status(env, i)["status"] == "done" for i in ids))
    assert len([t for t in threading.enumerate() if t.name.startswith("djs-worker-")]) == len(env.provs)


def test_tick_can_be_limited_to_one_platform(env):
    t = task(env, "fakelogin", city="x")
    assert engine.tick(env.conn, "fake") is False
    assert status(env, t["id"])["status"] == "pending"
    assert engine.tick(env.conn, "fakelogin") is True


# ---- quick mode and on-demand detail ----
def test_quick_mode_stores_list_items_only(env):
    mock_client(env, pages({1: [{"id": 1}, {"id": 2}]}))
    asked = []
    env.mp.setattr(env.provs["fake"], "detail", lambda client, job: asked.append(job))
    t = task(env, detail=False)
    engine.run_task(env.conn, t)
    stats = taskqueue.as_dict(status(env, t["id"]))["stats"]
    assert asked == [] and stats["jobs_new"] == 2 and stats["details_ok"] == 0
    job = service.jobs_get(env.conn, task_id=t["id"], with_detail=True, complete_only=False)[0]
    assert job["title"] and job["description"] is None and job["detail"] is None and job["detail_error"] is None


def test_job_detail_on_demand_then_cached(env):
    mock_client(env, pages({1: [{"id": 1}]}))
    prov, asked, real_detail = env.provs["fake"], [], env.provs["fake"].detail
    env.mp.setattr(prov, "detail", lambda client, job: asked.append(job["id"]) or real_detail(client, job))
    env.mp.setattr(prov, "row", lambda job, detail: {"title": f"t{job['id']}", "company": "c", "city": None,
                                                    "salary_text": detail and "20-30K",
                                                    "description": detail and detail["jd"]})
    engine.run_task(env.conn, task(env, detail=False))
    job_id = service.jobs_get(env.conn, complete_only=False)[0]["id"]
    env.conn.execute("UPDATE jobs SET city='Hangzhou' WHERE id=?", (job_id,))  # the detail knows no city: keep ours
    got = service.job_detail(env.conn, job_id)
    assert got["description"] == "jd1" and got["salary_text"] == "20-30K" and got["city"] == "Hangzhou"
    assert got["detail"] == {"jd": "jd1"} and got["detail_error"] is None and asked == [1]
    env.mp.setattr(egress, "lease", boom(AssertionError("a stored detail must not touch the network")))
    assert service.job_detail(env.conn, job_id) == got and asked == [1]


def test_job_detail_failures_are_friendly_and_store_nothing(env):
    mock_client(env, pages({1: [{"id": 1}]}))
    engine.run_task(env.conn, task(env, detail=False))
    job_id = service.jobs_get(env.conn, complete_only=False)[0]["id"]
    with pytest.raises(ValueError, match="not found"):
        service.job_detail(env.conn, 999)
    env.mp.setattr(env.provs["fake"], "detail", boom(StructureError("shape")))
    with pytest.raises(ValueError, match=r"could not load the detail.*shape"):
        service.job_detail(env.conn, job_id)
    env.mp.setattr(env.provs["fake"], "detail", boom(Blocked("429")))
    with pytest.raises(ValueError, match="limiting requests"):
        service.job_detail(env.conn, job_id)
    with pytest.raises(egress.NoEgressAvailable):  # a block cools the exit down, same as during a crawl
        egress.lease(env.conn, "fake", "x")
    with pytest.raises(ValueError, match="cooling down"):
        service.job_detail(env.conn, job_id)
    assert service.jobs_get(env.conn, with_detail=True, complete_only=False)[0]["detail"] is None


def test_job_detail_on_login_platform_needs_a_cookie(env):
    env.auth.cookies = {"s": "1"}
    mock_client(env, pages({1: [{"id": 1}]}))
    engine.run_task(env.conn, task(env, "fakelogin", city="x", detail=False))
    job_id = service.jobs_get(env.conn, complete_only=False)[0]["id"]
    env.mp.setattr(env.provs["fakelogin"], "detail", boom(LoginRequired("401")))
    with pytest.raises(ValueError, match="login required"):  # cookie expired on the server
        service.job_detail(env.conn, job_id)
    assert env.auth.invalidated == ["fakelogin"]
    with pytest.raises(ValueError, match="login required"):  # no cookie at all: nothing is attempted
        service.job_detail(env.conn, job_id)


def test_crawl_start_passes_and_validates_detail(env):
    tid = service.crawl_start(env.conn, "fake", keyword="x", detail=False)["task_id"]
    assert taskqueue.get(env.conn, tid)["payload"] == {"keyword": "x", "detail": False}   # the old boolean still works
    default = service.crawl_start(env.conn, "fake", keyword="x")["task_id"]
    assert taskqueue.get(env.conn, default)["payload"]["detail"] == "auto"
    for mode in ("always", "never"):
        assert service.crawl_start(env.conn, "fake", keyword="x", detail=mode)["task_id"]
    with pytest.raises(ValueError, match="detail"):
        service.crawl_start(env.conn, "fake", keyword="x", detail="no")


# ---- search results ----
def test_repeat_search_gets_its_own_results(env):
    mock_client(env, pages({1: [{"id": 1}, {"id": 2}]}))
    first, again = task(env), task(env)
    engine.run_task(env.conn, first)
    engine.run_task(env.conn, again)
    assert taskqueue.as_dict(status(env, again["id"]))["stats"]["jobs_new"] == 0
    for t in (first, again):
        found = service.jobs_get(env.conn, task_id=t["id"], complete_only=False)
        assert sorted(j["source_job_id"] for j in found) == ["1", "2"]
    discoverers = {j["task_id"] for j in service.jobs_get(env.conn, complete_only=False)}
    assert discoverers == {first["id"]}  # still "first discovered by"
    assert [t["jobs_found"] for t in taskqueue.list_(env.conn)] == [2, 2]


def test_progress_is_written_after_each_page(env):
    t = task(env)
    seen = []

    def handler(request):
        page = int(request.url.params["page"])
        if page == 2:  # page 1 has been fully stored by now
            c = db.connect()
            seen.append(taskqueue.get(c, t["id"])["stats"])
            c.close()
        return httpx.Response(200, json={"items": {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}[page]})

    mock_client(env, handler)
    engine.run_task(env.conn, t)
    assert seen == [{"pages": 1, "jobs_new": 2, "jobs_seen": 0, "details_ok": 0, "details_failed": 0,
                     "complete": 0, "incomplete": 2}]   # the list is stored first; the detail pass comes after it
    assert taskqueue.get(env.conn, t["id"])["stats"]["pages"] == 2


def test_searches_group_tasks_by_keyword_and_city(env):
    env.provs["fake2"] = Fake2()
    mock_client(env, pages({1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}))
    py = [task(env), task(env, "fake2"), task(env)]  # the last one repeats the search on fake
    for t in py:
        engine.run_task(env.conn, t)
    go = task(env, keyword="go", city="Hangzhou")  # still queued
    rust = task(env, "fakelogin", keyword="rust", city="Hangzhou")
    engine.run_task(env.conn, rust)  # no cookie -> waiting_login
    got = service.searches(env.conn)
    assert [(s["keyword"], s["city"], s["status"]) for s in got] == [
        ("rust", "Hangzhou", "waiting_login"), ("go", "Hangzhou", "running"), ("py", None, "done")]
    assert got[2]["task_ids"] == [t["id"] for t in py] and got[2]["jobs_total"] == 6  # 3 + 3, repeat not counted twice
    assert [(s["provider"], s["label"], s["status"], s["jobs"]) for s in got[2]["per_site"]] == [
        ("fake", "fake", "done", 3), ("fake2", "fake2", "done", 3)]
    assert got[1]["task_ids"] == [go["id"]] and got[1]["per_site"][0]["status"] == "pending"
    assert got[0]["per_site"] == [{"provider": "fakelogin", "label": "fakelogin", "status": "waiting_login",
                                   "jobs": 0, "complete": 0, "incomplete": 0, "last_error": "login required"}]
    assert [s["keyword"] for s in service.searches(env.conn, limit=1)] == ["rust"]


# ---- MCP in-process ----
def test_mcp_tools_smoke(env, monkeypatch):
    import asyncio

    def call(name, **args):
        return json.loads(asyncio.run(server.mcp.call_tool(name, args))[0].text)

    assert {t["name"] for t in call("provider_list")["result"]} == {"fake", "fakelogin"}
    assert call("crawl_start", provider="fake", keyword="py")["task_id"] == 1
    assert "error" in call("crawl_start", provider="nope", keyword="x")
    assert call("crawl_start", provider="fake", keyword="py", detail=False)["task_id"] == 2
    assert call("task_list")["result"][0]["status"] == "pending"
    assert call("jobs_get")["result"] == []
    assert "error" in call("job_detail", job_id=99)
    assert [(s["keyword"], s["task_ids"]) for s in call("search_list")["result"]] == [("py", [1, 2])]
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
