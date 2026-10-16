"""The tools an MCP client drives: search -> wait_for_search -> jobs_get -> job_detail. Fake platforms behind
httpx.MockTransport, a temp database, no network."""
import asyncio
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import djs
from djs import browser, cli, db, egress, engine, pacer, providers, results, server, service, taskqueue
from djs.providers.base import Login, Provider, get_json, need
from provider_fakes import job, retrying, task

ITEMS = {  # what each fake platform lists for the keyword "py"
    "a": [{"id": 1, "title": "Python 后端", "company": "甲科技有限公司", "pay": "15-25K·14薪", "jd": "使用 Django 开发"},
          {"id": 2, "title": "Java 开发", "company": "乙公司", "pay": "8-12K", "jd": "使用 Spring"}],
    "opt": [{"id": 51, "title": "【急聘】python后端", "company": "甲科技（杭州）有限公司", "pay": "15-25K", "jd": "Django 和 Redis",
             "tags": ["五险一金", "双休"]}],   # the double rest day is in the welfare tags only
    "city": [{"id": 71, "title": "Go 开发", "company": "丙公司", "pay": "20-30K", "jd": "微服务"}],
}


class Site(Provider):
    interval, jitter, min_interval = 0.0, 0.0, 0.0
    page_size, first_page = 3, 1

    def fetch(self, client, payload, page):
        return need(get_json(client.get(f"https://{self.name}.test/list", params={"q": payload["keyword"], "page": page})), "items")

    def job_id(self, job):
        return str(need(job, "id", typ=int))

    def row(self, job, detail):
        return {"title": job["title"], "company": job["company"], "city": "杭州", "salary_text": job["pay"],
                "benefits": job.get("tags"), "description": job["jd"]}

    def meta(self, job, detail):
        return {"source_url": f"https://{self.name}.test/job/{job['id']}", "apply_url": None, "published_at": None,
                "published_at_kind": "unknown"}


def site(name, label, **attrs):
    return type(name, (Site,), {"name": name, "label": label, **attrs})()


def make_providers():
    session = Login(url="https://opt.test/login", method="qr", domain="opt.test", cookies_any=("s",))
    return {p.name: p for p in (
        site("a", "甲站"), site("b", "乙站"),                                   # public; "b" has nothing for "py"
        site("opt", "丙站", auth="optional", login=session),                      # works anonymously
        site("login", "丁站", auth="login", login=session),                       # needs the user's login
        site("window", "戊站", needs_headed=True),                                # opens a browser window
        site("keyed", "己站", requires_env=("DJS_FAKE_TOOLS_KEY",)),              # a key that is not set
        site("city", "庚站", requires_city=True),                                 # nationwide search refused
        site("nocodes", "壬站", city_filter="server"),                            # has no code for any city
        site("bro", "辛站", transports=("browser",)))}                            # needs a browser


class FakeAuth:
    def status(self, provider):
        return {"logged_in": False, "state": "not_logged_in", "saved_at": None, "expires_at": None, "method": "qr",
                "unlocks": ""}

    def logged_in(self, provider):
        return False

    def load_cookies(self, provider):
        return None

    def pending_logins(self):
        return []

    def cancel_login(self, provider):
        return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DJS_FAKE_TOOLS_KEY", raising=False)
    provs = make_providers()
    monkeypatch.setattr(providers, "get", lambda n: provs[n] if n in provs else (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(providers, "names", lambda: list(provs))
    for mod in (engine, service):
        monkeypatch.setattr(mod, "auth", FakeAuth())
    monkeypatch.setattr(browser, "ready", lambda: False)
    monkeypatch.setattr(server, "POLL_SECONDS", 0.02)
    real = egress.make_client

    def handler(request):
        name = request.url.host.split(".")[0]
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"items": ITEMS.get(name, [])[(page - 1) * 3: page * 3]})

    monkeypatch.setattr(egress, "make_client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    conn = db.connect()
    yield SimpleNamespace(conn=conn, mp=monkeypatch, provs=provs)
    engine.stop_worker(10)
    conn.close()


def call(name, **args):
    """One MCP tool the way a client calls it: JSON in, JSON out."""
    return json.loads(asyncio.run(server.mcp.call_tool(name, args))[0].text)


# ---- search
def test_search_starts_every_platform_that_is_usable_right_now(env):
    started = call("search", keyword="py")
    assert started["providers"] == ["a", "b", "opt", "nocodes"]   # not: login (needs the user), window, keyed, city, bro (no browser)
    assert (started["keyword"], started["city"], started["task_ids"]) == ("py", None, [1, 2, 3, 4])
    assert started["skipped"] == []
    payload = {"keyword": "py", "max_pages": 1, "detail": "auto"}   # what is missing is completed
    assert [(t["provider"], t["payload"]) for t in taskqueue.list_(env.conn)][::-1] == [
        ("a", payload), ("b", payload), ("opt", payload), ("nocodes", payload)]


def test_a_city_adds_the_platforms_that_need_one_and_drops_those_that_cannot_search_it(env):
    assert call("search", keyword="py", city="杭州")["providers"] == ["a", "b", "opt", "city"]   # "nocodes" has no code for 杭州
    env.mp.setattr(browser, "ready", lambda: True)
    assert call("search", keyword="go", city="杭州")["providers"] == ["a", "b", "opt", "city", "bro"]
    env.mp.setenv("DJS_FAKE_TOOLS_KEY", "x")
    assert call("search", keyword="rust")["providers"] == ["a", "b", "opt", "keyed", "nocodes", "bro"]   # nationwide: no city to refuse


def test_a_named_platform_that_cannot_search_the_city_is_skipped_and_the_others_start(env):
    started = call("search", keyword="py", city="杭州", providers=["a", "nocodes", "city"])
    assert (started["providers"], started["task_ids"]) == (["a", "city"], [1, 2])   # what was started, task for task
    [skipped] = started["skipped"]
    assert (skipped["provider"], skipped["label"]) == ("nocodes", "壬站") and "壬站暂不支持城市「杭州」" in skipped["reason"]
    assert [t["provider"] for t in taskqueue.list_(env.conn)][::-1] == ["a", "city"]   # nothing was queued for the skipped one


def test_every_kind_of_refusal_skips_only_its_platform_and_says_why(env):
    started = call("search", keyword="py", providers=["a", "keyed", "city", "bro", "nocodes"])   # nationwide
    assert (started["providers"], started["task_ids"]) == (["a", "nocodes"], [1, 2])
    reasons = {s["provider"]: s["reason"] for s in started["skipped"]}
    assert list(reasons) == ["keyed", "city", "bro"]   # in the order they were named
    assert "DJS_FAKE_TOOLS_KEY" in reasons["keyed"] and "庚站必须指定城市" in reasons["city"] and "Chrome" in reasons["bro"]
    assert [s["label"] for s in started["skipped"]] == ["己站", "庚站", "辛站"]


def test_a_search_with_nothing_skipped_says_so(env):
    assert call("search", keyword="py", providers=["a"])["skipped"] == []


def test_the_platforms_that_refuse_write_nothing_before_they_refuse(env):
    """The refusals are ValueErrors raised before the INSERT, so the platforms that can start are not held back by
    them: the one transaction holds exactly their inserts."""
    statements = []
    env.conn.set_trace_callback(statements.append)
    started = service.crawl_many(env.conn, ["a", "keyed", "nocodes", "city", "b"], "py", "杭州")
    env.conn.set_trace_callback(None)
    assert started["task_ids"] == [1, 2, 3] and [s["provider"] for s in started["skipped"]] == ["keyed", "nocodes"]
    assert sum(s.startswith("INSERT INTO tasks") for s in statements) == 3
    assert [s for s in statements if s.split()[0] in ("BEGIN", "COMMIT", "ROLLBACK")] == ["BEGIN IMMEDIATE", "COMMIT"]


def test_named_platforms_are_used_as_they_are_even_one_that_opens_a_window(env):
    started = call("search", keyword="py", providers=["window", "a"], max_pages=2)
    assert started["providers"] == ["window", "a"]
    assert [t["payload"]["max_pages"] for t in taskqueue.list_(env.conn)] == [2, 2]


def test_a_search_no_platform_can_take_starts_nowhere_and_says_why_of_each(env):
    error = call("search", keyword="py", providers=["keyed", "city", "bro"])["error"]
    for why in ("己站", "DJS_FAKE_TOOLS_KEY", "庚站: 庚站必须指定城市", "辛站", "Chrome"):
        assert why in error
    assert "壬站暂不支持城市「杭州」" in call("search", keyword="py", city="杭州", providers=["nocodes"])["error"]
    assert taskqueue.list_(env.conn) == []


def test_an_unknown_platform_or_a_missing_keyword_cancels_the_whole_search(env):
    assert "KeyError" in call("search", keyword="py", providers=["a", "nope"])["error"]
    assert "keyword is required" in call("search", keyword="  ")["error"]
    assert taskqueue.list_(env.conn) == []   # the rollback took the first task back too
    env.mp.setattr(providers, "names", lambda: ["login", "window"])
    assert "no platform is usable" in call("search", keyword="py")["error"]


def test_a_worker_committing_while_a_search_starts_does_not_fail_it(env):
    """The tasks of a search are made in one transaction that reads before it writes. A deferred transaction fails at
    once with 'database is locked' when anything commits in between (a worker does, all the time): it must lock first."""
    real = pacer.transport

    def rival(conn, provider, session=False):
        got = real(conn, provider, session)   # the read that opens the transaction's snapshot
        other = sqlite3.connect(db.home() / "djs.db", timeout=0.05)
        try:
            other.execute("INSERT INTO provider_settings(provider, interval) VALUES ('rival', 1)")
            other.commit()
        except sqlite3.OperationalError:   # the write lock is held already, as it should be
            pass
        finally:
            other.close()
        return got

    env.mp.setattr(pacer, "transport", rival)
    assert call("search", keyword="py", providers=["a", "b"])["task_ids"] == [1, 2]


def test_the_search_is_told_by_its_keyword_and_city_without_the_spaces_around_them(env):
    assert call("search", keyword="  py  ", city=" 杭州 ", providers=["a"])["keyword"] == "py"
    status = call("wait_for_search", keyword="py", city="杭州", timeout=0)
    assert (status["keyword"], status["city"], status["status"]) == ("py", "杭州", "running")
    assert "no search for keyword='go'" in call("wait_for_search", keyword="go")["error"]


# ---- outcomes and progress
def test_every_site_of_a_search_says_how_it_turned_out(env):
    ids = {p: taskqueue.create(env.conn, p, {"keyword": "py"}) for p in ("a", "b", "opt", "login", "window")}
    for tid in (ids["a"], ids["b"]):
        env.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    job(env.conn, "a", "x", [ids["a"]], title="found")                 # a finished site that found a job ...
    taskqueue.dead(env.conn, ids["opt"], "Blocked: HTTP 403")           # ... one that failed ...
    taskqueue.waiting_login(env.conn, ids["login"])                      # ... one that needs the user ...
    env.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ids["window"],))   # ... and one still going
    sites = {s["provider"]: s for s in call("search_list")["result"][0]["per_site"]}
    assert {p: s["outcome"] for p, s in sites.items()} == {
        "a": "found", "b": "no_results", "opt": "failed", "login": "waiting_login", "window": "running"}
    assert sites["b"]["jobs"] == 0 and sites["b"]["status"] == "done"   # nothing found is a fact, not a failure
    assert all("completing" not in s for s in sites.values())


def test_a_site_that_is_fetching_details_reports_how_far_it_is(env):
    tid = taskqueue.create(env.conn, "a", {"keyword": "py"})
    env.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    assert "completing" not in call("search_list")["result"][0]["per_site"][0]   # still on the list pages
    taskqueue.progress(env.conn, tid, {"pages": 2, "details_total": 96, "details_ok": 50, "details_failed": 3})
    [site_] = call("search_list")["result"][0]["per_site"]
    assert (site_["outcome"], site_["completing"]) == ("running", {"done": 53, "total": 96})
    [mirrored] = call("task_list")["result"]
    assert (mirrored["outcome"], mirrored["completing"]) == ("running", {"done": 53, "total": 96})
    env.conn.execute("UPDATE tasks SET status='done'")
    [finished] = call("task_list")["result"]
    assert (finished["outcome"], "completing" in finished) == ("no_results", False)   # no jobs were linked to it
    assert "completing" not in call("search_list")["result"][0]["per_site"][0]


def test_task_list_has_an_outcome_for_every_status(env):
    for provider, status in (("a", "pending"), ("a", "running"), ("a", "done"), ("a", "dead"), ("a", "waiting_login")):
        task(env.conn, provider, "py", status=status)
    retrying(env.conn, "a", "py")   # pending as well, but it failed: it waits for its next try
    got = {(t["status"], bool(t["last_error"])): t["outcome"] for t in call("task_list")["result"]}
    assert got == {("pending", False): "running", ("pending", True): "retrying", ("running", False): "running",
                   ("done", False): "no_results", ("dead", False): "failed", ("waiting_login", False): "waiting_login"}
    newest = call("task_list", status="pending", limit=1)["result"][0]
    assert (newest["outcome"], newest["next_run_at"] > time.time()) == ("retrying", True)   # and next_run_at says when


# ---- wait_for_search
def test_wait_for_search_returns_at_once_when_every_site_is_done(env):
    tid = task(env.conn, "a", "py", "杭州")
    job(env.conn, "a", "x", [tid], title="t")
    started = time.monotonic()
    status = call("wait_for_search", keyword="py", city="杭州", timeout=30)
    assert time.monotonic() - started < 2
    assert (status["status"], status["jobs_total"], "timed_out" in status) == ("done", 1, False)
    assert status["per_site"][0]["outcome"] == "found"


def test_wait_for_search_gives_the_status_so_far_when_the_timeout_hits(env):
    task(env.conn, "a", "py", status="running")
    started = time.monotonic()
    status = call("wait_for_search", keyword="py", timeout=0.3)
    assert 0.25 < time.monotonic() - started < 3
    assert (status["status"], status["timed_out"], status["per_site"][0]["outcome"]) == ("running", True, "running")


def test_a_search_waiting_for_the_user_is_not_waited_on(env):
    tid = task(env.conn, "login", "py", status="waiting_login")
    status = call("wait_for_search", keyword="py", timeout=30)
    assert (status["status"], status["per_site"][0]["outcome"], tid) == ("waiting_login", "waiting_login", 1)


def test_a_search_is_done_when_every_site_finished_whether_it_found_nothing_or_failed(env):
    job(env.conn, "a", "x", [task(env.conn, "a", "py")], title="t")   # found
    task(env.conn, "b", "py")                                          # finished, found nothing
    task(env.conn, "opt", "py", status="dead")                         # failed
    status = call("wait_for_search", keyword="py", timeout=30)
    assert (status["status"], "timed_out" in status) == ("done", False)
    assert {s["provider"]: s["outcome"] for s in status["per_site"]} == {"a": "found", "b": "no_results", "opt": "failed"}
    assert call("search_list")["result"][0]["status"] == "done"


def test_a_search_whose_every_site_waits_for_its_next_try_is_retrying_not_done_and_not_waited_on(env):
    """What a real client met with every exit pointing at a dead proxy: each site failed and waited for its next try,
    and the search said "done", which reads as a success. It is retrying; the call still returns at once."""
    for name in ("a", "b"):
        retrying(env.conn, name, "py", "杭州", error="ConnectError: [Errno 61] Connection refused")
    started = time.monotonic()
    status = call("wait_for_search", keyword="py", city="杭州", timeout=30)
    assert time.monotonic() - started < 2   # "retrying" is not "running": nothing is waited for
    assert (status["status"], "timed_out" in status, status["jobs_total"]) == ("retrying", False, 0)
    assert {s["provider"]: (s["outcome"], s["reason"]) for s in status["per_site"]} == {
        "a": ("retrying", "网络不通，稍后再试"), "b": ("retrying", "网络不通，稍后再试")}
    assert call("search_list")["result"][0]["status"] == "retrying"


@pytest.fixture
def waiting(env):
    """Search ('py', '杭州'): "a" is done and found a job, "b" failed and waits for its next try."""
    a = task(env.conn, "a", "py", "杭州")
    job(env.conn, "a", "x", [a], title="found")
    return SimpleNamespace(a=a, b=retrying(env.conn, "b", "py", "杭州"))


def test_wait_for_search_does_not_wait_for_a_site_that_only_waits_for_its_next_try(env, waiting):
    started = time.monotonic()
    status = call("wait_for_search", keyword="py", city="杭州", timeout=30)
    assert time.monotonic() - started < 2   # the retry is seconds away at best; nobody waits for it
    assert (status["status"], "timed_out" in status, status["jobs_total"]) == ("retrying", False, 1)   # "b" is unfinished: not done
    sites = {s["provider"]: s for s in status["per_site"]}
    assert sites["a"]["outcome"] == "found" and not {"reason", "retry_at"} & set(sites["a"])
    assert (sites["b"]["outcome"], sites["b"]["status"], sites["b"]["reason"]) == ("retrying", "pending", "被网站暂时限制，稍后再试")
    assert sites["b"]["retry_at"] == taskqueue.get(env.conn, waiting.b)["next_run_at"]   # when it tries again
    assert sites["b"]["retry_at"] > time.time() and "completing" not in sites["b"]
    assert call("search_list")["result"][0]["status"] == "retrying"


def test_jobs_get_and_the_results_page_say_searching_only_while_some_site_works(env, waiting):
    assert call("jobs_get", keyword="py", city="杭州")["searching"] is False
    assert results.search_jobs(env.conn, "py", "杭州")["searching"] is False
    env.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (waiting.b,))   # its retry begins: searching again
    assert call("jobs_get", keyword="py", city="杭州")["searching"] is True
    assert results.search_jobs(env.conn, "py", "杭州")["searching"] is True
    assert call("search_list")["result"][0]["status"] == "running"


def test_a_queued_site_that_never_failed_keeps_the_search_running_beside_one_that_waits(env, waiting):
    queued = task(env.conn, "opt", "py", "杭州", status="pending")
    status = call("wait_for_search", keyword="py", city="杭州", timeout=0.3)
    assert (status["status"], status["timed_out"]) == ("running", True)
    assert {s["provider"]: s["outcome"] for s in status["per_site"]} == {"a": "found", "b": "retrying", "opt": "running"}
    assert call("jobs_get", keyword="py", city="杭州")["searching"] is True
    assert results.search_jobs(env.conn, "py", "杭州")["searching"] is True
    env.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (queued,))
    assert call("wait_for_search", keyword="py", city="杭州", timeout=30)["status"] == "retrying"   # "b" still owes its retry


def test_a_site_whose_exits_all_rest_is_retrying_and_a_search_does_not_wait_for_the_cooldown(env):
    """What a real client met: one site waited out its exits' cooldown, minutes to half an hour, and the search said
    running for all of it. The site tries again by itself; the call returns as soon as the others are done, and says
    retrying (not done: that site has not finished)."""
    egress.report(env.conn, "direct", "b", ok=False, blocked=True)   # every exit of "b" rests for 30 minutes
    engine.start_worker()
    call("search", keyword="py", city="杭州", providers=["a", "b"])
    status = call("wait_for_search", keyword="py", city="杭州", timeout=30)   # its retry is a minute away: it would time out
    assert (status["status"], "timed_out" in status) == ("retrying", False)
    sites = {s["provider"]: s for s in status["per_site"]}
    assert (sites["a"]["outcome"], sites["a"]["jobs"]) == ("found", 2)
    assert (sites["b"]["outcome"], sites["b"]["reason"]) == ("retrying", "这个网站刚才连续出错，正在歇一会儿")
    assert sites["b"]["last_error"] == "all egress for b are cooling down"
    assert sites["b"]["retry_at"] == pytest.approx(time.time() + engine.NO_EGRESS_DELAY, abs=20)
    assert call("jobs_get", keyword="py", city="杭州")["searching"] is False


def test_wait_for_search_polls_without_blocking_the_event_loop_and_returns_when_the_search_ends(env):
    tid = task(env.conn, "a", "py", status="running")
    threading.Timer(0.3, lambda: db.connect().execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))).start()

    async def go():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1
        ticking = asyncio.create_task(ticker())
        status = await server.wait_for_search("py", None, 30)
        ticking.cancel()
        return status, ticks

    status, ticks = asyncio.run(go())
    assert (status["status"], "timed_out" in status) == ("done", False)
    assert ticks > 20   # the loop kept running while the database was polled (it would stand still for ~0.3 s otherwise)


def test_wait_for_search_never_blocks_longer_than_the_cap(env):
    env.mp.setattr(server, "MAX_WAIT", 0.2)
    task(env.conn, "a", "py", status="running")
    started = time.monotonic()
    assert call("wait_for_search", keyword="py", timeout=9999)["timed_out"] is True
    assert time.monotonic() - started < 3


# ---- the whole flow, with workers
def test_search_wait_and_read_end_to_end(env):
    engine.start_worker()
    started = call("search", keyword="py", city="杭州")
    assert started.get("providers") == ["a", "b", "opt", "city"], started
    status = call("wait_for_search", keyword="py", city="杭州", timeout=30)
    assert status["status"] == "done" and "timed_out" not in status
    assert {s["provider"]: (s["outcome"], s["jobs"]) for s in status["per_site"]} == {
        "a": ("found", 2), "b": ("no_results", 0), "opt": ("found", 1), "city": ("found", 1)}
    assert status["jobs_total"] == 4
    got = call("jobs_get", keyword="py", city="杭州", min_month_salary=15000, text="django")
    assert got["searching"] is False and got["total"] == got["returned"] == 1   # "a"'s and "opt"'s Python jobs are one
    [one] = got["jobs"]
    assert one["company"].startswith("甲科技") and (one["salary_month_min"], one["salary_month_max"]) == (15000, 25000)
    assert one["provider"] in ("a", "opt") and [o["provider"] for o in one["also_on"]] == [
        "opt" if one["provider"] == "a" else "a"]   # whichever stands for the job names the other
    detail = call("job_detail", job_id=one["id"])
    assert detail["description"] and detail["salary_month_min"] == 15000


def test_a_condition_that_only_the_welfare_tags_state_is_found_by_text_and_shown_in_every_view(env):
    engine.start_worker()
    call("search", keyword="py", city="杭州")
    assert call("wait_for_search", keyword="py", city="杭州", timeout=30)["status"] == "done"
    got = call("jobs_get", keyword="py", city="杭州", text="双休")
    [one] = got["jobs"]
    assert (got["total"], one["provider"], one["benefits"]) == (1, "opt", "五险一金、双休")
    assert "双休" not in one["title"] + one["description_snippet"]                  # it is in the tags and nowhere else
    assert call("jobs_get", keyword="py", city="杭州", text="双休", fields="full")["jobs"][0]["benefits"] == "五险一金、双休"
    assert call("jobs_get", keyword="py", city="杭州", title="双休")["jobs"] == []   # a title is not searched in the tags
    assert call("jobs_get", keyword="py", city="杭州", text="django 双休")["total"] == 1   # tags and description together
    assert call("jobs_get", keyword="py", city="杭州", text="五险")["total"] == 1
    others = call("jobs_get", keyword="py", city="杭州", complete_only=False, limit=100)["jobs"]
    assert [j["benefits"] for j in others if j["title"] in ("Java 开发", "Go 开发")] == [None, None]   # none stated: NULL
    detail = call("job_detail", job_id=one["id"])
    assert detail["benefits"] == "五险一金、双休" and [o["provider"] for o in detail["also_on"]] == ["a"]


# ---- jobs_get
@pytest.fixture
def stocked(env):
    """Search ('py', '杭州') with eleven jobs on two platforms, a duplicate pair among them."""
    a, b = task(env.conn, "a", "py", "杭州"), task(env.conn, "b", "py", "杭州")
    for i in range(8):
        job(env.conn, "a", f"a{i}", [a], seen=100 + i, title=f"工程师 {i}", company=f"公司{i}", city="杭州",
            salary_min=10000 + 1000 * i, salary_max=15000 + 1000 * i, salary_unit="元/月",
            description=f"第 {i} 个岗位。" + "负责后端开发，" * 200 + (" Django" if i % 2 else ""))
    job(env.conn, "b", "b0", [b], seen=300, title="Python 工程师", company="甲科技有限公司", city="杭州", salary_min=300,
        salary_max=400, salary_unit="元/天", description="日薪岗位 django")
    job(env.conn, "a", "a-dup", [a], seen=200, title="python工程师", company="甲科技（杭州）有限公司", city="杭州",
        salary_min=300, salary_max=400, salary_unit="元/天", description="同一个岗位 Django", district="西湖区")
    job(env.conn, "a", "a-bad", [a], seen=50, title="缺薪资", company="丁公司", city="杭州", complete=0, missing="salary",
        salary_text="面议")
    return SimpleNamespace(a=a, b=b)


def test_jobs_get_returns_short_summaries_and_one_row_per_job(stocked):
    got = call("jobs_get", keyword="py", city="杭州")
    assert (got["total"], got["returned"], got["next_offset"], got["searching"]) == (9, 9, None, False)   # 10 complete records, one pair merged
    row = next(j for j in got["jobs"] if j["title"] == "python工程师")   # the record with more columns filled stands for the pair
    assert set(row) == {"id", "provider", "label", "title", "company", "city", "district", "salary_text", "salary_month_min",
                        "salary_month_max", "salary_unit", "salary_months", "education", "experience", "benefits",
                        "source_url", "missing", "also_on", "description_snippet"}
    assert (row["label"], row["district"], row["salary_unit"]) == ("甲站", "西湖区", "元/天")
    assert (row["salary_month_min"], row["salary_month_max"]) == (6525, 8700)   # a day is 21.75 working days
    assert [(o["provider"], o["label"]) for o in row["also_on"]] == [("b", "乙站")] and row["also_on"][0]["source_url"]
    assert row["missing"] == [] and "incomplete_reason" not in row
    assert all(len(j["description_snippet"]) <= 120 and "\n" not in j["description_snippet"] for j in got["jobs"])
    assert any(j["description_snippet"].endswith("…") for j in got["jobs"])     # a long text is cut, and says so
    assert "Django" in next(j for j in got["jobs"] if j["title"] == "python工程师")["description_snippet"]   # a short one is whole


def test_fields_full_swaps_the_snippet_for_the_whole_description(stocked):
    summary = call("jobs_get", keyword="py", city="杭州", text="第 3 个")["jobs"][0]
    full = call("jobs_get", keyword="py", city="杭州", text="第 3 个", fields="full")["jobs"][0]
    assert "description" not in summary and "description_snippet" not in full
    assert full["description"].startswith("第 3 个岗位。") and len(full["description"]) > 300
    assert {k: v for k, v in full.items() if k != "description"} == {k: v for k, v in summary.items() if k != "description_snippet"}
    assert "fields must be" in call("jobs_get", fields="everything")["error"]


def test_the_default_summary_is_much_smaller_than_the_full_text(stocked):
    summary, full = (len(json.dumps(call("jobs_get", keyword="py", city="杭州", limit=100, fields=f), ensure_ascii=False))
                     for f in ("summary", "full"))
    assert summary * 3 < full   # the point of the summary: a few dozen jobs fit in a client's context


PLATFORM_JSON = {"jobInfo": {"share": {"url": "https://share.test/x?t=abc"}, "hr": {"name": "招聘者", "avatar": "https://img.test/a.png"},
                             "trace": ["x" * 200] * 20}}   # what a platform's detail response carries besides the job


def test_job_detail_is_the_jobs_get_full_row_and_the_platforms_json_only_on_request(stocked, env):
    full = {j["title"]: j for j in call("jobs_get", keyword="py", city="杭州", complete_only=False, fields="full", limit=100)["jobs"]}
    env.conn.execute("UPDATE jobs SET detail=?", (json.dumps(PLATFORM_JSON, ensure_ascii=False),))
    for title in ("python工程师", "工程师 3", "缺薪资"):   # a job merged with another site's, a plain one, an incomplete one
        row = full[title]
        assert call("job_detail", job_id=row["id"]) == row                  # exactly what jobs_get returns with fields='full'
        assert {"description", "benefits", "missing", "also_on"} <= set(row)
    assert full["缺薪资"]["incomplete_reason"] == "面议" and "incomplete_reason" not in full["工程师 3"]
    job_id = full["python工程师"]["id"]
    clean, raw = call("job_detail", job_id=job_id), call("job_detail", job_id=job_id, raw=True)
    assert "detail" not in clean and "https://share.test" not in json.dumps(clean) and "招聘者" not in json.dumps(clean)
    assert raw == {**clean, "detail": PLATFORM_JSON}                         # raw adds the platform's JSON, as `detail`, and nothing else
    assert len(json.dumps(raw, ensure_ascii=False)) > 3 * len(json.dumps(clean, ensure_ascii=False))
    assert [(o["provider"], o["label"]) for o in clean["also_on"]] == [("b", "乙站")] and clean["also_on"][0]["source_url"]


def test_job_detail_carries_the_welfare_tags_and_the_whole_description(env):
    t = task(env.conn, "a", "py")
    pk = job(env.conn, "a", "x", [t], title="会计", company="甲", city="宁波", description="第一行\n第二行", benefits="五险一金、双休")
    got = call("job_detail", job_id=pk)
    assert (got["benefits"], got["description"], got["missing"], got["also_on"]) == ("五险一金、双休", "第一行\n第二行", [], [])
    none = call("job_detail", job_id=job(env.conn, "a", "y", [t], title="出纳", company="乙", city="宁波"))
    assert none["benefits"] is None and "detail" not in none


def test_jobs_get_pages_with_limit_and_next_offset(stocked):
    seen, offset = [], 0
    while offset is not None:
        page = call("jobs_get", keyword="py", city="杭州", limit=4, offset=offset)
        assert page["total"] == 9 and page["returned"] == len(page["jobs"])
        seen += [j["id"] for j in page["jobs"]]
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 9
    assert [call("jobs_get", limit=0)["returned"], call("jobs_get", limit=500)["returned"]] == [1, 9]   # clamped to 1..100
    assert call("jobs_get", keyword="py", city="杭州", limit=4, offset=8)["next_offset"] is None
    assert call("jobs_get", keyword="py", city="杭州", offset=99) == {
        "jobs": [], "total": 9, "returned": 0, "next_offset": None, "searching": False}


def test_jobs_get_filters_by_pay_text_city_platform_and_task(stocked):
    titles = lambda **kw: sorted(j["title"] for j in call("jobs_get", limit=100, **kw)["jobs"])   # noqa: E731
    assert titles(keyword="py", city="杭州", min_month_salary=16000) == [f"工程师 {i}" for i in range(1, 8)]   # 11-16K and up
    assert titles(keyword="py", city="杭州", floor_month_salary=16000) == ["工程师 6", "工程师 7"]   # the LOWEST pay is 16K or more
    assert titles(keyword="py", city="杭州", floor_month_salary=7000) == [f"工程师 {i}" for i in range(8)]   # not the 6525-8700 job
    assert titles(keyword="py", city="杭州", floor_month_salary=6525, max_month_salary=7000) == ["python工程师"]   # 6525 itself counts
    assert titles(keyword="py", city="杭州", max_month_salary=7000) == ["python工程师"]   # 6525 a month
    assert titles(keyword="py", city="杭州", min_month_salary=8000, max_month_salary=10500) == ["python工程师", "工程师 0"]
    assert titles(keyword="py", city="杭州", text="django 后端") == ["工程师 1", "工程师 3", "工程师 5", "工程师 7"]
    assert titles(provider="b") == ["Python 工程师"]
    assert titles(task_id=stocked.b) == ["Python 工程师"]
    assert titles(city="上海") == []
    assert len(titles()) == 9   # no scope at all: everything stored (complete jobs, each once)


def test_title_asks_for_the_role_and_text_for_skills_that_may_sit_anywhere(stocked):
    titles = lambda **kw: sorted(j["title"] for j in call("jobs_get", keyword="py", city="杭州", limit=100, **kw)["jobs"])   # noqa: E731
    assert len(titles(text="后端")) == 8 and titles(title="后端") == []     # the work is in eight descriptions, in no title
    assert titles(text="django") == ["python工程师", "工程师 1", "工程师 3", "工程师 5", "工程师 7"] and titles(title="django") == []
    assert titles(title="python") == ["python工程师"]                       # "Python 工程师" on the other site is the same job
    assert titles(title="PYTHON 工程师") == ["python工程师"]                 # every word, any case
    assert titles(title="工程师", text="django") == ["python工程师", "工程师 1", "工程师 3", "工程师 5", "工程师 7"]
    assert titles(title="工程师", text="django", min_month_salary=16000) == ["工程师 1", "工程师 3", "工程师 5", "工程师 7"]   # the filters stack
    assert "title takes at most" in call("jobs_get", title=" ".join("w" * 11))["error"]
    assert call("jobs_get", keyword="py", city="杭州", title="工程师", limit=1)["total"] == 9   # all the engineers; paging counts the filter


def test_incomplete_jobs_come_on_request_with_what_is_missing_and_why(stocked):
    got = call("jobs_get", keyword="py", city="杭州", complete_only=False, limit=100)
    row = next(j for j in got["jobs"] if j["title"] == "缺薪资")
    assert (got["total"], row["missing"], row["incomplete_reason"]) == (10, ["salary"], "面议")
    assert (row["salary_month_min"], row["salary_month_max"]) == (None, None)
    assert call("jobs_get", keyword="py", city="杭州", min_month_salary=0, complete_only=False)["total"] == 9   # a salary filter needs a salary


def test_dedupe_false_lists_every_record(stocked):
    got = call("jobs_get", keyword="py", city="杭州", dedupe=False, limit=100)
    assert got["total"] == 10 and all(j["also_on"] == [] for j in got["jobs"])


def test_jobs_get_says_whether_the_search_is_still_running_and_refuses_a_search_that_never_ran(stocked, env):
    env.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (stocked.a,))
    assert call("jobs_get", keyword="py", city="杭州")["searching"] is True
    assert "searching" not in call("jobs_get")   # no search named: nothing to wait for
    assert "no search for keyword='rust'" in call("jobs_get", keyword="rust", city="杭州")["error"]
    assert "no search for keyword='py', city=None" in call("jobs_get", keyword="py")["error"]   # the nationwide one never ran


def test_jobs_get_is_newest_first_by_default(env):
    t = task(env.conn, "a", "py")
    ids = [job(env.conn, "a", f"j{i}", [t], seen=100 + i // 2, title=f"j{i}") for i in range(6)]   # two a second
    assert [j["id"] for j in call("jobs_get", limit=100)["jobs"]] == ids[::-1]


# ---- what the server tells a client
def test_the_server_teaches_the_recommended_flow():
    text = server.mcp.instructions
    steps = [text.index(name) for name in ("search(", "wait_for_search(", "jobs_get(", "job_detail(")]
    assert steps == sorted(steps)   # in the order a client should use them
    for word in ("no_results", "waiting_login", "retrying", "retry_at", "salary_month_min", "min_month_salary", "next_offset"):
        assert word in text
    flat = " ".join(text.split())
    assert "does not wait for it" in flat   # a site that tries again later does not hold the call
    for line in ("- running: a site still works", "- retrying: no site works and at least one failed and waits for its next try",
                 "- waiting_login: a site needs the user", "- done: every site finished"):
        assert line in flat   # the four values of a search's status, one line each
    assert "If every site is retrying, check status() for the exits and the reason before searching again" in flat
    assert "call status first" in text and "exits" in text   # and where to look when it does not go as described
    assert 'title="会计"' in text and 'text="双休"' in text            # the role goes in the title, a condition in the text
    assert "floor_month_salary=8000" in text and "benefits" in text    # a hard pay floor has its own filter; the tags are searched
    assert 'update.state == "ready"' in text   # a new version in the app is passed on to the user, who installs it there
    assert "a new version waits in the app: it installs from the app, not from here" in flat
    assert text.index('update.state == "ready"') > text.index("call status first")   # at the end of the status paragraph


def test_the_new_and_changed_tools_come_with_an_example_each():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("search", "wait_for_search", "jobs_get", "job_detail", "task_list", "search_list", "status"):
        assert "Example:" in tools[name].description, name
    for name in ("wait_for_search", "task_list", "search_list", "status"):   # where a site's retrying shows
        assert "retrying" in tools[name].description, name
    assert "is not waited for" in tools["wait_for_search"].description
    for name in ("wait_for_search", "search_list"):   # the values of a search's status
        assert all(value in tools[name].description for value in ("running", "retrying", "waiting_login", "done")), name
    assert "check status() for the exits and the reason before searching again" in " ".join(tools["wait_for_search"].description.split())
    assert "`exits`" in tools["status"].description and "'direct'" in tools["status"].description
    props = tools["jobs_get"].inputSchema["properties"]
    assert (props["limit"]["default"], props["fields"]["default"], props["dedupe"]["default"],
            props["complete_only"]["default"]) == (20, "summary", True, True)
    assert {"integer", "null"} == {opt["type"] for opt in props["floor_month_salary"]["anyOf"]} and props["floor_month_salary"]["default"] is None
    assert tools["job_detail"].inputSchema["properties"]["raw"]["default"] is False
    assert tools["job_detail"].inputSchema["required"] == ["job_id"]
    assert tools["search"].inputSchema["required"] == ["keyword"] and tools["search"].inputSchema["properties"]["max_pages"]["default"] == 1
    assert tools["wait_for_search"].inputSchema["properties"]["timeout"]["default"] == 120
    assert "login_cancel ends it early" in " ".join(tools["login"].description.split())   # a login does not have to run out
    assert tools["login_cancel"].inputSchema["required"] == ["provider"]
    assert "Tasks keep waiting for a login" in tools["login_cancel"].description
    assert "`update`" in tools["status"].description and "not from here" in tools["status"].description


def test_the_two_pay_filters_are_told_apart_plainly_with_an_example_each():
    text = " ".join({t.name: t for t in asyncio.run(server.mcp.list_tools())}["jobs_get"].description.split())
    assert "floor_month_salary (the job's LOWEST pay is at least this: floor_month_salary=8000 keeps 8k-12k and drops 6k-9k" in text
    assert "min_month_salary (the job's pay range REACHES this: min_month_salary=8000 keeps 6k-9k as well as 8k-12k" in text
    assert "welfare tags `benefits`" in text and 'text="双休"' in text and 'title="会计"' in text   # what text searches, and an example
    assert 'Example: jobs_get(keyword="会计", city="宁波", title="会计", text="双休", floor_month_salary=8000, limit=10)' in text
    detail = " ".join({t.name: t for t in asyncio.run(server.mcp.list_tools())}["job_detail"].description.split())
    assert "not the platform's own JSON; raw=true adds that as `detail`" in detail


# ---- djs call
@pytest.fixture
def run(env, capsys):
    def go(*argv):
        code = cli.main(["call", *argv])
        return code, json.loads(capsys.readouterr().out)
    return go


def test_djs_call_runs_search_wait_for_search_and_jobs_get(env, run):
    code, started = run("search", '{"keyword": "py", "city": "杭州", "providers": ["a"]}')
    assert (code, started["task_ids"]) == (0, [1])
    code, status = run("wait_for_search", '{"keyword": "py", "city": "杭州", "timeout": 0.1}')   # nobody works the queue here
    assert (code, status["status"], status["timed_out"]) == (0, "running", True)
    engine.start_worker()
    code, status = run("wait_for_search", '{"keyword": "py", "city": "杭州", "timeout": 30}')
    assert (code, status["status"], status["per_site"][0]["outcome"]) == (0, "done", "found")
    code, got = run("jobs_get", '{"keyword": "py", "city": "杭州", "min_month_salary": 15000}')
    assert code == 0 and [j["title"] for j in got["jobs"]] == ["Python 后端"]
    code, err = run("wait_for_search", '{"keyword": "nothing"}')
    assert code == 1 and "no search" in err["error"]


# ---- status: what is going on, in one call
@pytest.fixture
def chrome(env):
    """A machine with Chrome, so the answer does not depend on the one running the tests."""
    env.mp.setattr(browser, "find_browser", lambda: ("chrome", Path("/apps/chrome")))


def test_status_has_the_shape_clients_rely_on_and_needs_no_network_and_no_time(env, chrome, monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("status must not touch the network"))
    begun = time.monotonic()
    got = call("status")
    assert time.monotonic() - begun < 1
    assert set(got) == {"version", "app", "update", "data_dir", "workers", "queue", "exits", "browser", "logins", "platforms"}
    assert set(got["app"]) == {"version", "frozen", "path"} and set(got["update"]) == {"current", "latest", "state", "note"}
    assert got["data_dir"] == str(db.home()) and got["version"]
    assert list(got["workers"]) == list(got["platforms"]) == list(env.provs)   # one entry per platform, in registry order
    assert set(got["browser"]) == {"executable", "hint", "cdp_url_set", "login_windows", "profiles_in_use"}
    assert got["logins"]["opt"] == {"state": "not_logged_in", "expires_at": None}   # the platforms that have a login
    assert set(got["logins"]) == {"opt", "login"}
    assert got["queue"] == {"pending": 0, "running": 0, "waiting_login": 0, "done": 0, "dead": 0}


def test_status_shows_the_task_a_platform_runs_with_its_progress_and_whether_this_process_works(env, chrome):
    running = task(env.conn, "a", "py", "杭州", status="running")
    stats = {"pages": 2, "details_total": 5, "details_ok": 2, "details_failed": 1}
    env.conn.execute("UPDATE tasks SET locked_at=?, stats=? WHERE id=?", (time.time() - 5, json.dumps(stats), running))
    task(env.conn, "b", "py")   # done
    waiting = task(env.conn, "opt", "py", status="pending")
    got = call("status")
    assert got["workers"]["a"]["running"] == {"task_id": running, "keyword": "py", "city": "杭州",
                                              "since": pytest.approx(time.time() - 5, abs=5), "pages": 2,
                                              "completing": {"done": 3, "total": 5}}
    assert got["workers"]["b"]["running"] is None and waiting   # only a running task counts
    assert got["queue"] == {"pending": 1, "running": 1, "waiting_login": 0, "done": 1, "dead": 0}
    assert {w["alive_in_this_process"] for w in got["workers"].values()} == {False}   # nobody started workers in this process
    engine.start_worker()
    assert {w["alive_in_this_process"] for w in call("status")["workers"].values()} == {True}


def test_status_lists_the_tasks_that_wait_for_their_next_try_beside_the_one_a_platform_runs(env, chrome):
    running = task(env.conn, "a", "py", "杭州", status="running")
    first, second = retrying(env.conn, "a", "go", error="ConnectError: refused"), retrying(env.conn, "a", "rust")
    task(env.conn, "b", "py", status="pending")   # queued, never failed: it waits for its turn, not for a retry
    workers = call("status")["workers"]
    due = lambda tid: taskqueue.get(env.conn, tid)["next_run_at"]   # noqa: E731
    assert workers["a"]["running"]["task_id"] == running   # the running task stays as it was
    assert workers["a"]["retrying"] == [{"task_id": first, "reason": "网络不通，稍后再试", "retry_at": due(first)},
                                        {"task_id": second, "reason": "被网站暂时限制，稍后再试", "retry_at": due(second)}]
    assert all(w["retrying"] == [] for name, w in workers.items() if name != "a")


def test_status_tells_in_plain_words_why_the_newest_task_of_a_platform_failed_or_waits(env, chrome):
    old = task(env.conn, "b", "py", status="dead")
    task(env.conn, "b", "py2", status="done")   # the newest task of "b" is fine: its old failure is history
    dead = task(env.conn, "a", "py", status="dead")
    env.conn.execute("UPDATE tasks SET last_error='Blocked: HTTP 403' WHERE id=?", (dead,))
    waiting = task(env.conn, "opt", "py", status="waiting_login")
    env.conn.execute("UPDATE tasks SET last_error='login required' WHERE id=?", (waiting,))
    task(env.conn, "login", "py", status="dead")   # dead without a recorded error: still reported
    platforms = call("status")["platforms"]
    assert old and platforms["b"]["last_error"] is None and platforms["city"]["last_error"] is None
    assert platforms["a"]["last_error"] == {"task_id": dead, "status": "dead", "kind": "blocked", "reason": "被网站暂时限制，稍后再试"}
    assert platforms["opt"]["last_error"] == {"task_id": waiting, "status": "waiting_login", "kind": "login", "reason": "需要先登录"}
    assert platforms["login"]["last_error"]["kind"] == "other"
    assert call("status")["queue"]["dead"] == 3 and call("status")["queue"]["waiting_login"] == 1


def test_status_tells_why_a_platform_whose_newest_task_waits_for_its_next_try_is_not_getting_anywhere(env, chrome):
    """What a real client met with the network down: every task failed and waited for its retry, and last_error was
    null on every platform, so the cause showed in wait_for_search only."""
    refused = retrying(env.conn, "a", "py", error="ConnectError: [Errno 61] Connection refused")
    rested = retrying(env.conn, "b", "py", error="ConnectError: refused | then: all egress for b are cooling down")
    retrying(env.conn, "opt", "py")                          # an old retry ...
    task(env.conn, "opt", "py2")                             # ... behind a task that finished: history
    running = retrying(env.conn, "login", "py")
    env.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (running,))   # its retry runs: it works now
    task(env.conn, "window", "py", status="pending")        # queued, never failed: it waits for its turn only
    platforms = call("status")["platforms"]
    due = lambda tid: taskqueue.get(env.conn, tid)["next_run_at"]   # noqa: E731
    assert platforms["a"]["last_error"] == {"task_id": refused, "status": "retrying", "kind": "network",
                                            "reason": "网络不通，稍后再试", "retry_at": due(refused)}
    assert platforms["b"]["last_error"] == {"task_id": rested, "status": "retrying", "kind": "network",
                                            "reason": "网络不通，稍后再试", "retry_at": due(rested)}
    assert platforms["a"]["last_error"]["retry_at"] > time.time()   # unix time, in the future
    assert [platforms[name]["last_error"] for name in ("opt", "login", "window", "bro")] == [None] * 4   # "bro" never ran


def test_status_lists_missing_keys_by_name_and_never_shows_a_value(env, chrome):
    platforms = call("status")["platforms"]
    assert platforms["keyed"]["configured"] is False and platforms["keyed"]["missing_env"] == ["DJS_FAKE_TOOLS_KEY"]
    assert platforms["a"]["configured"] is True and platforms["a"]["missing_env"] == []
    env.mp.setenv("DJS_FAKE_TOOLS_KEY", "s3cret-value")
    got = call("status")
    assert got["platforms"]["keyed"]["configured"] is True and "s3cret-value" not in json.dumps(got)


def test_status_names_the_exits_that_are_cooling_down_without_their_passwords(env, chrome):
    egress.report(env.conn, "http://u:pw@proxy.test:8080", "a", ok=False, blocked=True)   # 30 minutes
    egress.report(env.conn, "direct", "b", ok=True)   # healthy: not listed
    got = call("status")
    [cooling] = got["platforms"]["a"]["cooling_down"]
    assert cooling["exit"] == "http://u:***@proxy.test:8080" and cooling["until"] == pytest.approx(time.time() + 1800, abs=60)
    assert got["platforms"]["b"]["cooling_down"] == [] and "pw@" not in json.dumps(got)


def test_status_lists_the_exits_as_egress_list_shows_them_and_whether_they_cool_down(env, chrome, monkeypatch):
    """What a real client could not see: that every request went out through a proxy, and that the proxy was dead."""
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("status must not touch the network"))
    half_hour = pytest.approx(time.time() + 1800, abs=60)

    def exit_(id_, provider, **kw):
        return {"id": id_, "provider": provider, "enabled": 1, "blocked_until": 0, "total_ok": 0, "total_fail": 0,
                "cooling_down": False, **kw}

    assert call("status")["exits"] == [exit_("direct", "*")]                # none configured: every request goes direct
    egress.add(env.conn, "http://u:pw@proxy.test:8080")                     # shared by every platform
    egress.add(env.conn, "http://other.test:3128", provider="a")            # dedicated to one
    egress.report(env.conn, "http://u:pw@proxy.test:8080", "b", ok=False, blocked=True)   # it rests for one platform ...
    egress.report(env.conn, "http://u:pw@proxy.test:8080", "a", ok=False)                 # ... and failed for another
    egress.report(env.conn, "http://other.test:3128", "a", ok=True)
    got = call("status")
    assert got["exits"] == [exit_("http://other.test:3128", "a", total_ok=1),
                            exit_("http://u:***@proxy.test:8080", "*", total_fail=2, blocked_until=half_hour, cooling_down=True)]
    assert "pw@" not in json.dumps(got)                                     # the password is masked, as in egress_list
    assert [{k: v for k, v in e.items() if k != "cooling_down"} for e in got["exits"]] == call("egress_list")["result"]
    egress.remove(env.conn, "http://u:***@proxy.test:8080")                 # the masked form, as shown
    egress.remove(env.conn, "http://other.test:3128")
    assert call("status")["exits"] == [exit_("direct", "*")]                # nothing configured: direct again
    egress.report(env.conn, "direct", "a", ok=False, blocked=True)          # and direct rests and counts like any exit
    rested = {"total_fail": 1, "blocked_until": half_hour, "cooling_down": True}
    assert call("status")["exits"] == [exit_("direct", "*", **rested)]
    egress.add(env.conn, "direct", provider="a")                            # configured on purpose: listed with its scope
    assert call("status")["exits"] == [exit_("direct", "a", **rested)]


def test_status_says_which_browser_a_login_window_would_use_or_why_there_is_none(env, chrome):
    browser_state = call("status")["browser"]
    assert browser_state["executable"] == {"kind": "chrome", "path": str(Path("/apps/chrome"))} and browser_state["hint"] is None
    assert browser_state["cdp_url_set"] is False
    env.mp.setenv("DJS_CDP_URL", "http://127.0.0.1:9222")
    env.mp.setattr(browser, "find_browser", lambda: None)
    browser_state = call("status")["browser"]
    assert browser_state["executable"] is None and browser_state["hint"] == browser.no_browser_hint()
    assert "Chrome" in browser_state["hint"] and browser_state["cdp_url_set"] is True


def test_status_shows_the_open_login_windows_and_the_profiles_in_use(env, chrome):
    env.mp.setattr(service.auth, "pending_logins", lambda: [{"provider": "opt", "since": 1234.5}])
    with browser.profile_lock("opt"):   # a login window, or a crawl in a browser, holds it
        got = call("status")["browser"]
        assert got["login_windows"] == [{"provider": "opt", "since": 1234.5}] and got["profiles_in_use"] == ["opt"]
    assert call("status")["browser"]["profiles_in_use"] == []   # released again (the folder stays, nobody holds it)


def test_djs_call_status_prints_the_same_json(env, chrome, capsys):
    assert cli.main(["call", "status"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"version", "app", "update", "data_dir", "workers", "queue", "exits",
                                                        "browser", "logins", "platforms"}


def test_djs_call_runs_login_cancel_like_any_other_tool(env, capsys):
    assert cli.main(["call", "login_cancel", '{"provider": "opt"}']) == 0   # no window is open: nothing to stop
    assert json.loads(capsys.readouterr().out) == {"provider": "opt", "cancelled": False}


# ---- status: the updater and the running program
@pytest.fixture
def updater(monkeypatch):
    """What the updater (djs.update) answers to `status`: the dict it returns, which a test may change."""
    from djs import update
    answer = {"current": "1.2.6", "latest": None, "state": "none", "note": "", "install_on_quit": False}
    monkeypatch.setattr(update, "current", lambda: dict(answer))
    return answer


def test_status_says_what_the_updater_says_without_what_only_the_app_needs(env, chrome, updater):
    assert call("status")["update"] == {"current": "1.2.6", "latest": None, "state": "none", "note": ""}
    updater.update(latest="1.4.0", state="ready", note="1.4.0 已下载，可以安装", install_on_quit=True)
    assert call("status")["update"] == {"current": "1.2.6", "latest": "1.4.0", "state": "ready", "note": "1.4.0 已下载，可以安装"}


@pytest.mark.parametrize("broken", ["raises", "lacks a key", "is not importable"])
def test_status_still_answers_when_the_updater_cannot(env, chrome, updater, monkeypatch, broken):
    def fail():
        raise RuntimeError("no state yet")

    if broken == "raises":
        monkeypatch.setattr(djs.update, "current", fail)
    elif broken == "lacks a key":
        updater.pop("state")
    else:
        monkeypatch.setitem(sys.modules, "djs.update", None)
        monkeypatch.delattr(djs, "update")
    assert call("status")["update"] == {"current": service.app_version(), "latest": None, "state": "none", "note": ""}


def test_status_reads_the_real_updater_the_way_it_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))   # the updater keeps its state in the data folder: never the real one
    from djs import update   # the real one: the contract between the two is the four fields
    answer = update.current()
    assert {"current", "latest", "state", "note"} <= set(answer) and answer["state"] in ("none", "downloading", "ready", "failed")
    assert service._update() == {key: answer[key] for key in ("current", "latest", "state", "note")}


def test_status_tells_which_program_answers_and_where_it_lives(env, chrome, tmp_path, monkeypatch):
    here = call("status")["app"]   # a source checkout: the folder that holds the package
    assert here == {"version": service.app_version() or "dev", "frozen": False, "path": str(Path(service.__file__).resolve().parent.parent)}
    bundle = tmp_path / "找岗位助手.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "djs").touch()
    folder = tmp_path / "找岗位助手"
    folder.mkdir()
    (folder / "找岗位助手.exe").touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for exe, where in ((macos / "djs", bundle), (folder / "找岗位助手.exe", folder)):   # the bundle, or the install folder
        monkeypatch.setattr(sys, "executable", str(exe))
        assert call("status")["app"] == {"version": service.app_version() or "dev", "frozen": True, "path": str(where.resolve())}
