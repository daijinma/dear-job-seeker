"""The tools an MCP client drives: search -> wait_for_search -> jobs_get -> job_detail. Fake platforms behind
httpx.MockTransport, a temp database, no network."""
import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from djs import browser, cli, db, egress, engine, pacer, providers, server, service, taskqueue
from djs.providers.base import Login, Provider, get_json, need
from provider_fakes import job, task

ITEMS = {  # what each fake platform lists for the keyword "py"
    "a": [{"id": 1, "title": "Python 后端", "company": "甲科技有限公司", "pay": "15-25K·14薪", "jd": "使用 Django 开发"},
          {"id": 2, "title": "Java 开发", "company": "乙公司", "pay": "8-12K", "jd": "使用 Spring"}],
    "opt": [{"id": 51, "title": "【急聘】python后端", "company": "甲科技（杭州）有限公司", "pay": "15-25K", "jd": "Django 和 Redis"}],
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
                "description": job["jd"]}

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
    assert "DJS_FAKE_TOOLS_KEY" in reasons["keyed"] and "庚站必须指定城市" in reasons["city"] and "Playwright" in reasons["bro"]
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
    for why in ("己站", "DJS_FAKE_TOOLS_KEY", "庚站: 庚站必须指定城市", "辛站", "Playwright"):
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
    got = {t["status"]: t["outcome"] for t in call("task_list")["result"]}
    assert got == {"pending": "running", "running": "running", "done": "no_results", "dead": "failed",
                   "waiting_login": "waiting_login"}


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
                        "salary_month_max", "salary_unit", "salary_months", "education", "experience", "source_url",
                        "missing", "also_on", "description_snippet"}
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
    for word in ("no_results", "waiting_login", "salary_month_min", "min_month_salary", "next_offset"):
        assert word in text
    assert "call status first" in text   # and where to look when it does not go as described
    assert 'title="Python 后端"' in text and 'text="Django"' in text   # the role goes in the title, a skill in the text


def test_the_new_and_changed_tools_come_with_an_example_each():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("search", "wait_for_search", "jobs_get", "job_detail", "task_list", "search_list", "status"):
        assert "Example:" in tools[name].description, name
    props = tools["jobs_get"].inputSchema["properties"]
    assert (props["limit"]["default"], props["fields"]["default"], props["dedupe"]["default"],
            props["complete_only"]["default"]) == (20, "summary", True, True)
    assert tools["search"].inputSchema["required"] == ["keyword"] and tools["search"].inputSchema["properties"]["max_pages"]["default"] == 1
    assert tools["wait_for_search"].inputSchema["properties"]["timeout"]["default"] == 120


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
    assert set(got) == {"version", "data_dir", "workers", "queue", "browser", "logins", "platforms"}
    assert got["data_dir"] == str(db.home()) and got["version"]
    assert list(got["workers"]) == list(got["platforms"]) == list(env.provs)   # one entry per platform, in registry order
    assert set(got["browser"]) == {"executable", "hint", "playwright", "cdp_url_set", "login_windows", "profiles_in_use"}
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


def test_status_says_which_browser_a_login_window_would_use_or_why_there_is_none(env, chrome):
    browser_state = call("status")["browser"]
    assert browser_state["executable"] == {"kind": "chrome", "path": str(Path("/apps/chrome"))} and browser_state["hint"] is None
    assert (browser_state["playwright"], browser_state["cdp_url_set"]) == (False, False)   # the fixture says Playwright is not there
    env.mp.setenv("DJS_CDP_URL", "http://127.0.0.1:9222")
    env.mp.setattr(browser, "find_browser", lambda: None)
    browser_state = call("status")["browser"]
    assert browser_state["executable"] is None and "No browser found" in browser_state["hint"] and browser_state["cdp_url_set"] is True

    def playwright_missing():
        raise browser.BrowserUnavailable(browser.INSTALL_HINT)

    env.mp.setattr(browser, "find_browser", playwright_missing)
    got = call("status")["browser"]   # no Chrome or Edge, and nobody to ask about Chromium: still an answer, not an error
    assert got["executable"] is None and got["hint"] == browser.INSTALL_HINT


def test_status_shows_the_open_login_windows_and_the_profiles_in_use(env, chrome):
    env.mp.setattr(service.auth, "pending_logins", lambda: [{"provider": "opt", "since": 1234.5}])
    with browser.profile_lock("opt"):   # a login window, or a crawl in a browser, holds it
        got = call("status")["browser"]
        assert got["login_windows"] == [{"provider": "opt", "since": 1234.5}] and got["profiles_in_use"] == ["opt"]
    assert call("status")["browser"]["profiles_in_use"] == []   # released again (the folder stays, nobody holds it)


def test_djs_call_status_prints_the_same_json(env, chrome, capsys):
    assert cli.main(["call", "status"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"version", "data_dir", "workers", "queue", "browser", "logins", "platforms"}
