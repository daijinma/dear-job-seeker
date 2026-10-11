import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from djs import browser, cli, db, egress, engine, pacer, providers, server, service, taskqueue
from djs.providers.base import Blocked, Delisted, Login, LoginRequired, Provider, StructureError, get_json, need
from provider_fakes import stored_jobs


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
    login = Login(url="https://fake.test/login", method="qr", domain="fake.test", cookies_any=("s",))
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
        return {"logged_in": self.logged_in(p), "state": "logged_in" if self.logged_in(p) else "not_logged_in",
                "expires_at": None}

    def pending_logins(self):
        return []

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


def test_a_running_task_is_taken_over_only_when_its_heartbeat_is_older_than_the_lock_ttl(env):
    assert taskqueue.LOCK_TTL == 180   # the task of a process that died is picked up again within three minutes
    t = task(env)
    taskqueue.claim(env.conn)
    other_process = db.connect()
    assert taskqueue.claim(other_process) is None
    env.conn.execute("UPDATE tasks SET locked_at=? WHERE id=?", (time.time() - taskqueue.LOCK_TTL + 30, t["id"]))
    assert taskqueue.claim(other_process) is None   # a fresh heartbeat: leave it alone
    env.conn.execute("UPDATE tasks SET locked_at=? WHERE id=?", (time.time() - taskqueue.LOCK_TTL - 30, t["id"]))
    assert taskqueue.claim(other_process)["id"] == t["id"]
    other_process.close()


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


@pytest.mark.parametrize("state, error, jobs, outcome, works", [
    ("pending", None, 0, "running", True),                                        # queued, never failed
    ("pending", "", 0, "running", True),
    ("running", None, 0, "running", True),
    ("running", "Blocked: HTTP 403", 0, "running", True),                         # a retry that runs works, whatever failed before
    ("pending", "Blocked: HTTP 403", 0, "retrying", False),                       # failed, waits out a back-off
    ("pending", "all egress for fake are cooling down", 0, "retrying", False),    # waits out the rest of its exits
    ("waiting_login", "login required", 0, "waiting_login", False),
    ("dead", "boom", 0, "failed", False),
    ("done", None, 0, "no_results", False),
    ("done", None, 3, "found", False),
])
def test_one_rule_tells_a_task_that_works_from_one_that_waits_for_its_next_try(state, error, jobs, outcome, works):
    assert taskqueue.outcome(state, jobs, error) == outcome
    assert taskqueue.working(state, error) is works and taskqueue.retrying(state, error) is (outcome == "retrying")
    assert taskqueue.searching([{"status": state, "last_error": error}]) is works
    progress = {"details_total": 4, "details_ok": 1, "details_failed": 1}
    assert taskqueue.summary(state, jobs, progress, error) == {
        "outcome": outcome, **({"completing": {"done": 2, "total": 4}} if works else {})}   # only work has a pass to report


def test_a_task_waits_for_its_next_try_after_a_failure_or_a_rest_and_works_again_once_it_runs_or_is_resumed(env):
    outcomes = lambda: {t["id"]: t["outcome"] for t in taskqueue.list_(env.conn)}   # noqa: E731
    failed, rested, queued = (task(env)["id"] for _ in range(3))
    login = task(env, "fakelogin", city="Hangzhou")["id"]
    assert set(outcomes().values()) == {"running"}                                   # queued: never failed
    taskqueue.retry(env.conn, failed, "Blocked: HTTP 403")                           # a back-off
    taskqueue.release(env.conn, rested, "all egress for fake are cooling down", 60)  # the exits' rest, no attempt burned
    taskqueue.waiting_login(env.conn, login)
    assert outcomes() == {failed: "retrying", rested: "retrying", queued: "running", login: "waiting_login"}
    env.conn.execute("UPDATE tasks SET next_run_at=0 WHERE id=?", (failed,))
    assert taskqueue.claim(env.conn, "fake")["id"] == failed
    assert outcomes()[failed] == "running"                                           # it failed before, it works now
    assert taskqueue.resume_login(env.conn, "fakelogin") == 1
    assert outcomes()[login] == "running" and status(env, login)["last_error"] is None   # due at once, not a retry


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


@pytest.mark.parametrize("typed, stored", [
    ("http://a:1", "http://a:1"),
    ("127.0.0.1:7890", "http://127.0.0.1:7890"),                 # how Clash and most proxy apps show their address
    ("  http://127.0.0.1:7890 \n", "http://127.0.0.1:7890"),     # pasted with spaces around it
    ("https://u:p@proxy.test:8443", "https://u:p@proxy.test:8443"),
    ("http://proxy.lan", "http://proxy.lan"),                    # no port: the scheme's own
    ("direct", "direct"),
])
def test_egress_takes_a_proxy_the_way_people_type_it(env, typed, stored):
    egress.add(env.conn, typed)
    assert stored in {r["id"] for r in env.conn.execute("SELECT id FROM egress")} or stored == "direct"


@pytest.mark.parametrize("typed", ["socks5://127.0.0.1:1080", "http://127.0.0.1:99999", "http://127.0.0.1:port", "http://", ""])
def test_egress_refuses_what_cannot_work(env, typed):
    with pytest.raises(ValueError):
        egress.add(env.conn, typed)


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
                                               "details_failed": 0, "details_total": 3, "complete": 0, "incomplete": 3}
    jobs = {j["source_job_id"]: j for j in stored_jobs(env.conn, task_id=t["id"], with_detail=True)}
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
    job = stored_jobs(env.conn, task_id=t["id"], with_detail=True)[0]
    assert job["title"] and job["description"] is None and job["detail"] is None and job["detail_error"] is None


def test_job_detail_on_demand_then_cached(env):
    mock_client(env, pages({1: [{"id": 1}]}))
    prov, asked, real_detail = env.provs["fake"], [], env.provs["fake"].detail
    env.mp.setattr(prov, "detail", lambda client, job: asked.append(job["id"]) or real_detail(client, job))
    env.mp.setattr(prov, "row", lambda job, detail: {"title": f"t{job['id']}", "company": "c", "city": None,
                                                    "salary_text": detail and "20-30K",
                                                    "description": detail and detail["jd"]})
    engine.run_task(env.conn, task(env, detail=False))
    job_id = stored_jobs(env.conn)[0]["id"]
    env.conn.execute("UPDATE jobs SET city='Hangzhou' WHERE id=?", (job_id,))  # the detail knows no city: keep ours
    got = service.job_detail(env.conn, job_id)
    assert got["description"] == "jd1" and got["salary_text"] == "20-30K" and got["city"] == "Hangzhou"
    assert "detail" not in got and asked == [1]   # the platform's own JSON is not part of the answer ...
    env.mp.setattr(egress, "lease", boom(AssertionError("a stored detail must not touch the network")))
    assert service.job_detail(env.conn, job_id) == got and asked == [1]
    assert service.job_detail(env.conn, job_id, raw=True) == {**got, "detail": {"jd": "jd1"}}   # ... unless asked for
    assert stored_jobs(env.conn, with_detail=True)[0]["detail"] == {"jd": "jd1"}


def test_job_detail_failures_are_friendly_and_store_nothing(env):
    mock_client(env, pages({1: [{"id": 1}]}))
    engine.run_task(env.conn, task(env, detail=False))
    job_id = stored_jobs(env.conn)[0]["id"]
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
    assert stored_jobs(env.conn, with_detail=True)[0]["detail"] is None


def test_job_detail_on_login_platform_needs_a_cookie(env):
    env.auth.cookies = {"s": "1"}
    mock_client(env, pages({1: [{"id": 1}]}))
    engine.run_task(env.conn, task(env, "fakelogin", city="x", detail=False))
    job_id = stored_jobs(env.conn)[0]["id"]
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
        found = stored_jobs(env.conn, task_id=t["id"])
        assert sorted(j["source_job_id"] for j in found) == ["1", "2"]
    discoverers = {j["task_id"] for j in stored_jobs(env.conn)}
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
                                   "jobs": 0, "complete": 0, "incomplete": 0, "last_error": "login required",
                                   "outcome": "waiting_login"}]
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
    assert call("jobs_get") == {"jobs": [], "total": 0, "returned": 0, "next_offset": None}
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


# ---- the lock of a running task: heartbeat, hand-back on a stop, no late writes ----
def test_progress_refreshes_the_lock_of_a_running_task_and_only_of_a_running_one(env):
    t = task(env)
    taskqueue.progress(env.conn, t["id"], {"pages": 1})   # still queued: the stats are kept, no lock appears
    row = status(env, t["id"])
    assert row["locked_at"] is None and json.loads(row["stats"]) == {"pages": 1}
    taskqueue.claim(env.conn)
    env.conn.execute("UPDATE tasks SET locked_at=? WHERE id=?", (time.time() - 100, t["id"]))
    taskqueue.progress(env.conn, t["id"], {"pages": 2})
    assert status(env, t["id"])["locked_at"] == pytest.approx(time.time(), abs=2)
    taskqueue.done(env.conn, t["id"], {"pages": 2})
    taskqueue.progress(env.conn, t["id"], {"pages": 3})
    assert status(env, t["id"])["locked_at"] is None   # a finished task keeps no lock


def test_every_page_and_every_detail_of_a_crawl_is_a_heartbeat(env):
    other_process, seen = db.connect(), []

    def lock():
        return other_process.execute("SELECT locked_at FROM tasks WHERE id=1").fetchone()[0]

    def handler(request):
        page = int(request.url.params["page"])
        if page > 1:
            seen.append(lock())   # what the pages before it reported
        items = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}], 3: [{"id": 5}]}
        return httpx.Response(200, json={"items": items[page]})

    mock_client(env, handler)
    prov = env.provs["fake"]
    real_detail = prov.detail
    env.mp.setattr(prov, "detail", lambda client, job: seen.append(lock()) or real_detail(client, job))
    task(env)
    claimed = taskqueue.claim(env.conn)
    engine.run_task(env.conn, claimed)
    assert len(seen) == 2 + 5   # the requests of pages 2 and 3, then the five details
    beats = [claimed["locked_at"], *seen]
    assert beats == sorted(beats) and len(set(beats)) == len(beats)   # every report moved the lock on
    assert status(env, 1)["status"] == "done" and status(env, 1)["locked_at"] is None
    other_process.close()


def test_a_handed_back_task_keeps_its_stats_and_attempts_and_is_claimable_at_once(env):
    t = task(env)
    holder = taskqueue.Claim(taskqueue.claim(env.conn))
    taskqueue.progress(env.conn, holder, {"pages": 2})
    taskqueue.hand_back(env.conn, holder)
    row = status(env, t["id"])
    assert (row["status"], row["locked_at"], row["attempts"], row["last_error"]) == ("pending", None, 0, None)
    assert json.loads(row["stats"]) == {"pages": 2}
    assert taskqueue.claim(env.conn)["id"] == t["id"]   # due at once, not after a backoff or the lock


@pytest.mark.parametrize("how", ["handed back", "went silent"])
def test_the_old_holders_late_writes_never_touch_a_task_another_claim_took(env, how):
    t = task(env)
    old = taskqueue.Claim(taskqueue.claim(env.conn))
    if how == "handed back":
        taskqueue.hand_back(env.conn, old)
    else:   # its process stopped writing for longer than the lock lasts
        env.conn.execute("UPDATE tasks SET locked_at=? WHERE id=?", (time.time() - taskqueue.LOCK_TTL - 5, t["id"]))
    other_process = db.connect()
    new = taskqueue.Claim(taskqueue.claim(other_process))
    assert new.id == t["id"]
    before = dict(status(env, t["id"]))
    with pytest.raises(taskqueue.Lost):
        taskqueue.progress(env.conn, old, {"pages": 99})
    with pytest.raises(taskqueue.Lost):
        taskqueue.beat(env.conn, old)
    taskqueue.done(env.conn, old, {"pages": 99})
    taskqueue.dead(env.conn, old, "late")
    taskqueue.waiting_login(env.conn, old)
    taskqueue.release(env.conn, old, "late", 0)
    taskqueue.retry(env.conn, old, "late")
    taskqueue.hand_back(env.conn, old)
    assert dict(status(env, t["id"])) == before   # still running, still the new holder's lock
    taskqueue.progress(other_process, new, {"pages": 1})   # and the new holder writes as ever
    taskqueue.done(other_process, new, {"pages": 1})
    assert status(env, t["id"])["status"] == "done"
    other_process.close()


def test_a_claim_ends_with_the_write_that_lets_go_of_the_task(env):
    t = task(env)
    holder = taskqueue.Claim(taskqueue.claim(env.conn))
    taskqueue.done(env.conn, holder, {"pages": 1})
    taskqueue.retry(env.conn, holder, "late")   # would have burned an attempt and queued it again
    with pytest.raises(taskqueue.Lost):
        taskqueue.beat(env.conn, holder)
    row = status(env, t["id"])
    assert (row["status"], row["attempts"], row["last_error"]) == ("done", 0, None)


# ---- pacing waits are signs of life too ----
def test_pacing_waits_give_signs_of_life_also_while_queued_behind_another_holder(env, monkeypatch):
    assert pacer.BEAT_SECONDS * 3 <= taskqueue.LOCK_TTL   # several chances to be seen alive per lock
    monkeypatch.setattr(pacer, "BEAT_SECONDS", 0.05)
    pacer.wait("fake", "direct", 0, 0)   # stamps the time of a request
    beats = []
    begun = time.monotonic()
    with pacer.heartbeat(lambda: beats.append(1)):
        pacer.wait("fake", "direct", 0.4, 0)
    assert time.monotonic() - begun >= 0.38 and len(beats) >= 5   # the wait itself is unchanged, with a beat per slice
    holder = threading.Thread(target=pacer.wait, args=("fake", "direct", 0.5, 0))
    holder.start()
    time.sleep(0.1)   # it has the turn and sleeps with the lock held
    beats.clear()
    with pacer.heartbeat(lambda: beats.append(1)):
        pacer.wait("fake", "direct", 0, 0)
    holder.join()
    assert len(beats) >= 4


def test_a_lost_task_ends_the_wait_that_notices_it(env):
    def lost():
        raise taskqueue.Lost("handed back")

    with pytest.raises(taskqueue.Lost), pacer.heartbeat(lost):
        pacer.wait("fake", "direct", 0, 0)
    pacer.wait("fake", "direct", 0, 0)   # the pace lock was let go on the way out


def test_a_pacing_wait_longer_than_the_lock_ttl_keeps_the_task_alive(env, monkeypatch):
    monkeypatch.setattr(taskqueue, "LOCK_TTL", 1.0)
    monkeypatch.setattr(pacer, "BEAT_SECONDS", 0.1)
    pacer.set_frequency(env.conn, "fake", interval=1.6, force=True)   # the wait before page 2 outlasts the lock
    stolen, polling = [], threading.Event()

    def poll():   # the other processes: whatever they claim while the first one waits for its turn is stolen
        conn = db.connect()
        while not polling.is_set():
            if (row := taskqueue.claim(conn)) is not None:
                stolen.append(row["id"])
            time.sleep(0.02)
        conn.close()

    poller = threading.Thread(target=poll)

    def handler(request):
        page = int(request.url.params["page"])
        if page == 1:
            poller.start()   # the task is running by now
        return httpx.Response(200, json={"items": {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}[page]})

    mock_client(env, handler)
    task(env)
    try:
        assert engine.tick(env.conn) is True
    finally:
        polling.set()
        if poller.ident:
            poller.join()
    assert stolen == [] and status(env, 1)["status"] == "done"
    assert pacer._hook.beat is None   # the worker's hook does not outlive its task


# ---- a stop hands the tasks back ----
@pytest.fixture
def chrome(env):
    """A machine with Chrome, so that status does not look for a browser."""
    env.mp.setattr(browser, "find_browser", lambda: ("chrome", Path("/apps/chrome")))


def stuck_crawl(env, start_workers):
    """Queue a crawl on 'fake' and start the workers; returns once the worker is inside the request for page 2, which
    does not answer until `gate` is set."""
    gate, entered = threading.Event(), threading.Event()
    items = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}

    def handler(request):
        page = int(request.url.params["page"])
        if page == 2 and not gate.is_set():
            entered.set()
            assert gate.wait(10)
        return httpx.Response(200, json={"items": items[page]})

    mock_client(env, handler)
    tid = task(env)["id"]
    start_workers()
    assert entered.wait(10)
    return SimpleNamespace(tid=tid, gate=gate)


def test_stop_worker_hands_back_a_task_its_worker_is_stuck_in_and_a_new_worker_resumes_it(env, chrome, start_workers):
    held = stuck_crawl(env, start_workers)
    got = service.status(env.conn)
    assert got["queue"]["running"] == 1 and got["workers"]["fake"]["running"]["task_id"] == held.tid
    begun = time.monotonic()
    engine.stop_worker(0.3)   # the worker is inside a request and cannot leave
    assert time.monotonic() - begun < 3
    row = status(env, held.tid)
    assert (row["status"], row["locked_at"], row["attempts"], row["last_error"]) == ("pending", None, 0, None)
    assert taskqueue.as_dict(row)["stats"]["pages"] == 1   # what the worker had done stays
    got = service.status(env.conn)
    assert got["queue"]["running"] == 0 and got["queue"]["pending"] == 1 and got["workers"]["fake"]["running"] is None
    held.gate.set()   # the old worker wakes up: every write it still makes is refused
    engine._threads["fake"].join(5)
    assert not engine._threads["fake"].is_alive() and engine._held == {}
    assert status(env, held.tid)["status"] == "pending"   # its late finish did not mark the handed-back task done
    start_workers()   # a new worker takes it up again
    assert wait_for(lambda: status(env, held.tid)["status"] == "done")
    assert status(env, held.tid)["attempts"] == 0


@pytest.mark.parametrize("how", ["handed back by a stop", "lock ran out"])
def test_a_worker_that_wakes_up_late_leaves_the_next_holders_task_alone(env, start_workers, how):
    held = stuck_crawl(env, start_workers)
    if how == "handed back by a stop":
        engine.stop_worker(0.2)
    else:   # nothing was written to the task for longer than the lock lasts
        env.conn.execute("UPDATE tasks SET locked_at=? WHERE id=?", (time.time() - taskqueue.LOCK_TTL - 5, held.tid))
    other_process = db.connect()
    assert taskqueue.claim(other_process)["id"] == held.tid   # up for grabs again, and someone takes it
    before = dict(status(env, held.tid))
    held.gate.set()
    assert wait_for(lambda: engine._held == {})   # the old worker's run is over: its next write was refused
    assert dict(status(env, held.tid)) == before   # the late page, progress and finish changed nothing
    other_process.close()


def test_workers_notice_a_stop_between_requests_and_hand_their_task_back_themselves(env, start_workers):
    mock_client(env, pages({1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}], 3: [{"id": 5}, {"id": 6}]}))
    prov, asked, second_detail = env.provs["fake"], [], threading.Event()
    real_detail = prov.detail

    def slow_detail(client, job):
        asked.append(job["id"])
        if len(asked) == 2:
            second_detail.set()
        time.sleep(0.2)
        return real_detail(client, job)

    env.mp.setattr(prov, "detail", slow_detail)
    tid = task(env)["id"]
    start_workers()
    assert second_detail.wait(10)
    begun = time.monotonic()
    engine.stop_worker(5)
    assert time.monotonic() - begun < 1.5   # it did not wait for the rest of the pass
    row = status(env, tid)
    assert (row["status"], row["locked_at"], row["attempts"], row["last_error"]) == ("pending", None, 0, None)
    stats = taskqueue.as_dict(row)["stats"]
    assert stats["details_total"] == 6 and 2 <= stats["details_ok"] < 6   # it left between two details
    assert not any(engine.workers().values())
    start_workers()
    assert wait_for(lambda: status(env, tid)["status"] == "done")
    assert sorted(asked) == [1, 2, 3, 4, 5, 6]   # over both runs each detail was requested once


def test_stop_worker_does_not_open_the_database_when_it_holds_nothing(env, monkeypatch):
    monkeypatch.setattr(db, "connect", boom(AssertionError("nothing to hand back")))
    engine.stop_worker(0)


def test_djs_work_that_times_out_hands_its_task_back_and_the_next_one_resumes_it(env, capsys):
    slow = [True]

    def handler(request):
        if slow[0]:
            time.sleep(0.5)
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"items": [{"id": page * 10}, {"id": page * 10 + 1}]})

    mock_client(env, handler)
    tid = task(env)["id"]
    assert cli.main(["work", "--until-idle", "--timeout", "0.6"]) == 2
    assert "timeout: 1 task(s) still active" in capsys.readouterr().err
    row = status(env, tid)
    assert (row["status"], row["locked_at"], row["attempts"]) == ("pending", None, 0)   # not left for the lock to expire
    assert taskqueue.as_dict(row)["stats"]["pages"] >= 1
    slow[0] = False
    assert cli.main(["work", "--until-idle", "--timeout", "30"]) == 0   # the next start takes it up at once
    assert f"task {tid} fake done" in capsys.readouterr().out
