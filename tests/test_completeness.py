"""The data contract end to end: a job is final only with title, company, city, a salary range (two numbers) and a
JD; the engine completes records with a detail pass, and everything that reads them knows what is missing and why.
No network: the platforms are fakes behind httpx.MockTransport."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from djs import db, egress, engine, jobs, providers, server, service, taskqueue
from djs.providers.base import Blocked, Delisted, Login, Provider, StructureError, get_json, need
from provider_fakes import job, stored_jobs, task

LIST_JD = "列表里的职位描述"
PAGES = {1: [{"id": 1, "title": "t1", "salary": "15-25K·14薪", "jd": LIST_JD},   # complete from the list alone
            {"id": 2, "title": "t2", "salary": "1-1.5万"},                         # needs the detail for its JD
            {"id": 3, "title": "t3", "salary": "面议"}],                           # the site never gives its salary
         2: [{"id": 4, "title": "t4", "salary": "8千-1.2万"}]}


class Shop(Provider):
    name = "shop"
    interval, jitter, min_interval = 0.0, 0.0, 0.0
    page_size, first_page = 3, 1

    def __init__(self):
        self.asked, self.fail, self.blocked, self.gone = [], set(), set(), set()

    def fetch(self, client, payload, page):
        return need(get_json(client.get("https://shop.test/list", params={"page": page})), "items")

    def job_id(self, job):
        return str(need(job, "id", typ=int))

    def detail(self, client, job):
        self.asked.append(job["id"])
        if job["id"] in self.blocked:
            raise Blocked("429")
        if job["id"] in self.fail:
            raise StructureError("shape")
        if job["id"] in self.gone:
            raise Delisted()
        return {"jd": f"<p>jd{job['id']}</p><p>要求</p>"}

    def row(self, job, detail):
        return {"title": job["title"], "company": "公司", "city": "杭州", "salary_text": job.get("salary"),
                "description": job.get("jd") or (detail or {}).get("jd")}


class Closed(Shop):
    """A platform that needs a login to show salaries."""
    name = "closed"
    auth = "optional"
    login = Login(url="https://shop.test/login", method="qr", domain="shop.test", cookies_any=("s",))


class Picky(Closed):
    """BOSS in miniature: an anonymous visitor's detail never has the salary, so it cannot complete a job without one."""
    name = "picky"

    def detail_can_fill(self, missing, logged_in):
        return logged_in or "salary" not in missing


class Flat(Provider):
    """The list item is the whole job: no detail request exists."""
    name = "flat"
    interval, jitter, min_interval = 0.0, 0.0, 0.0
    page_size, first_page = 3, 1

    def fetch(self, client, payload, page):
        return need(get_json(client.get("https://shop.test/list", params={"page": page})), "items")

    def job_id(self, job):
        return str(need(job, "id", typ=int))

    def row(self, job, detail):
        return {"title": job["title"], "company": "公司", "city": "杭州", "description": job.get("jd")}


class FakeAuth:
    def __init__(self):
        self.session = False

    def status(self, provider):
        return {"logged_in": self.session}

    def load_cookies(self, provider):
        return {"s": "1"} if self.session else None

    def logged_in(self, provider):
        return self.session


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    provs = {"shop": Shop(), "closed": Closed(), "flat": Flat(), "picky": Picky()}
    monkeypatch.setattr(providers, "get", lambda n: provs[n] if n in provs else (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(providers, "names", lambda: list(provs))
    auth = FakeAuth()
    for mod in (engine, service):
        monkeypatch.setattr(mod, "auth", auth)
    real = egress.make_client

    def handler(request):
        return httpx.Response(200, json={"items": PAGES.get(int(request.url.params["page"]), [])})

    monkeypatch.setattr(egress, "make_client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    conn = db.connect()
    yield SimpleNamespace(conn=conn, provs=provs, shop=provs["shop"], auth=auth, mp=monkeypatch)
    conn.close()


def run(env, provider="shop", **payload):
    tid = taskqueue.create(env.conn, provider, {"keyword": "py", **payload})
    engine.run_task(env.conn, taskqueue.claim(env.conn, provider))
    return taskqueue.get(env.conn, tid)


def stored(env, provider="shop"):
    return {j["source_job_id"]: j for j in stored_jobs(env.conn, provider=provider, with_detail=True)}


# ---- the completion pass
def test_auto_requests_the_detail_only_for_jobs_that_miss_something(env):
    task = run(env)   # no `detail` in the payload = auto
    assert env.shop.asked == [2, 3, 4]   # job 1 had everything in its list item
    assert task["status"] == "done" and task["stats"] == {
        "pages": 2, "jobs_new": 4, "jobs_seen": 0, "details_ok": 3, "details_failed": 0, "details_total": 3,
        "complete": 3, "incomplete": 1}
    got = stored(env)
    assert got["1"]["description"] == LIST_JD and got["1"]["detail"] is None
    assert got["2"]["description"] == "jd2\n要求" and got["2"]["complete"] is True   # HTML became plain text
    assert (got["3"]["missing"], got["3"]["description"]) == (["salary"], "jd3\n要求")
    assert (got["4"]["salary_min"], got["4"]["salary_max"], got["4"]["salary_unit"]) == (8000, 12000, "元/月")


def test_always_requests_every_detail_and_never_none(env):
    run(env, detail="always")
    assert env.shop.asked == [1, 2, 3, 4]
    env.shop.asked.clear()
    env.conn.execute("DELETE FROM jobs")
    task = run(env, detail="never")
    assert env.shop.asked == [] and task["stats"]["details_ok"] == 0
    assert (task["stats"]["complete"], task["stats"]["incomplete"]) == (1, 3)   # only job 1 has its JD in the list


@pytest.mark.parametrize("old,asked", [(True, [1, 2, 3, 4]), (False, [])])
def test_booleans_still_mean_always_and_never(env, old, asked):
    run(env, detail=old)
    assert env.shop.asked == asked


def test_the_list_is_stored_before_the_first_detail_is_asked(env):
    seen = []
    real = env.shop.detail

    def spy(client, job):
        c = db.connect()
        seen.append((c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], taskqueue.get(c, 1)["stats"]))
        c.close()
        return real(client, job)

    env.mp.setattr(env.shop, "detail", spy)
    run(env)
    first_count, first_stats = seen[0]
    assert first_count == 4 and first_stats["jobs_new"] == 4 and first_stats["details_ok"] == 0
    assert (first_stats["complete"], first_stats["incomplete"]) == (1, 3)
    assert seen[-1][1]["details_ok"] == 2   # progress is rewritten after every detail


def test_a_detail_that_fails_is_kept_on_the_job_and_tried_again_by_the_next_task(env):
    env.shop.fail = {2}
    task = run(env)
    assert task["stats"]["details_failed"] == 1 and task["stats"]["incomplete"] == 2
    assert "shape" in stored(env)["2"]["detail_error"] and stored(env)["2"]["complete"] is False
    env.shop.fail, env.shop.asked = set(), []
    run(env)
    assert env.shop.asked == [2]   # only the one that failed: the others hold their detail, complete or not
    assert stored(env)["2"]["complete"] is True and stored(env)["2"]["detail_error"] is None


def test_a_job_that_is_already_complete_or_already_has_its_detail_is_not_asked_again(env):
    run(env)
    env.shop.asked.clear()
    task = run(env)
    assert task["stats"]["jobs_seen"] == 4 and task["stats"]["jobs_new"] == 0
    assert env.shop.asked == []   # 1 and 4 are complete, 2 and 3 already hold their detail
    run(env, detail="always")
    assert env.shop.asked == [1]   # only the one without a stored detail


def test_a_quick_search_is_completed_by_the_next_one(env):
    run(env, detail="never")
    assert [j["complete"] for j in stored(env).values()].count(True) == 1
    task = run(env)
    assert (task["stats"]["jobs_new"], task["stats"]["jobs_seen"], task["stats"]["details_ok"]) == (0, 4, 3)
    assert task["stats"]["complete"] == 3


def test_delisted_jobs_are_not_a_failure_and_stay_incomplete(env):
    env.shop.gone = {2}
    task = run(env)
    assert task["stats"]["details_failed"] == 0 and task["stats"]["details_ok"] == 3
    job = stored(env)["2"]
    assert job["detail_error"] is None and job["missing"] == ["description"]


def test_blocked_in_the_completion_pass_keeps_the_list_and_resumes_where_it_stopped(env):
    env.shop.blocked = {3}
    task = run(env)
    assert (task["status"], task["attempts"]) == ("pending", 1)             # retried later, one attempt used
    assert len(stored(env)) == 4 and stored(env)["2"]["complete"] is True    # the list and job 2's detail are kept
    with pytest.raises(egress.NoEgressAvailable):                            # the exit cools down, as for any block
        egress.lease(env.conn, "shop", "x")
    env.conn.execute("DELETE FROM egress_health")
    env.shop.blocked, env.shop.asked = set(), []
    env.conn.execute("UPDATE tasks SET next_run_at=0")
    engine.run_task(env.conn, taskqueue.claim(env.conn, "shop"))
    assert env.shop.asked == [3, 4]                                          # job 2 is not asked twice
    assert taskqueue.get(env.conn, 1)["status"] == "done" and taskqueue.get(env.conn, 1)["stats"]["complete"] == 3


def test_a_platform_without_a_detail_request_gets_no_completion_pass(env):
    env.mp.setitem(PAGES, 1, [{"id": 1, "title": "t", "jd": "说明"}])
    task = run(env, "flat", detail="always")
    assert env.provs["flat"].has_detail is False and task["stats"]["details_ok"] == 0
    assert stored(env, "flat")["1"]["missing"] == ["salary"]   # the list had everything the site publishes


def test_detail_mode_validation():
    assert [taskqueue.detail_mode(v) for v in ("auto", "always", "never", True, False)] == [
        "auto", "always", "never", "always", "never"]
    for bad in ("sometimes", None, 1, ["auto"]):
        with pytest.raises(ValueError, match="detail"):
            taskqueue.detail_mode(bad)


# ---- the details that cannot complete a job
def test_auto_skips_the_details_that_cannot_complete_a_job_and_always_still_asks(env):
    picky = env.provs["picky"]
    done = run(env, "picky")
    assert picky.asked == [2, 4]   # job 3 lacks a salary, and an anonymous detail has none to give: not worth a request
    assert (done["stats"]["details_total"], done["stats"]["details_ok"], done["stats"]["incomplete"]) == (2, 2, 1)
    env.auth.session = True        # logged in, the same detail does carry the salary
    picky.asked.clear()
    run(env, "picky")
    assert picky.asked == [3]      # and only that one: the others hold their detail already
    env.auth.session = False
    env.conn.execute("DELETE FROM jobs")
    picky.asked.clear()
    run(env, "picky", detail="always")
    assert picky.asked == [1, 2, 3, 4]   # 'always' asks for everything, whatever the platform thinks of it


def test_a_platform_that_says_nothing_about_its_details_keeps_asking_for_every_missing_one(env):
    assert env.shop.detail_can_fill(["salary", "description"], False) is True
    run(env)
    assert env.shop.asked == [2, 3, 4]


def test_a_task_in_its_completion_pass_reports_how_far_it_is(env):
    seen = []
    real = env.shop.detail

    def spy(client, job):
        c = db.connect()
        [t] = taskqueue.list_(c)
        site = service.searches(c)[0]["per_site"][0]
        seen.append(((t["outcome"], t.get("completing")), (site["outcome"], site.get("completing"))))
        c.close()
        return real(client, job)

    env.mp.setattr(env.shop, "detail", spy)
    run(env)
    running = lambda done: (("running", {"done": done, "total": 3}), ("running", {"done": done, "total": 3}))   # noqa: E731
    assert seen == [running(0), running(1), running(2)]
    [t] = taskqueue.list_(env.conn)
    assert (t["outcome"], "completing" in t, t["stats"]["details_total"]) == ("found", False, 3)   # over: no progress to show


# ---- a job whose detail is still to come is being completed, not lacking
def lacking(env, provider="shop", status="running", sid="x", **cols):
    """A job without its description (or whatever `cols` says), seen by a task in `status`."""
    t = task(env.conn, provider, "py", status=status)
    cols = {"title": "t", "company": "c", "city": "杭州", "salary_text": "15-25K", "salary_min": 15000,
            "salary_max": 25000, "salary_unit": "元/月", "complete": 0, "missing": "description", **cols}
    job(env.conn, provider, sid, [t], **cols)
    return t


def reason(env):
    [one] = service.jobs_get(env.conn, complete_only=False)["jobs"]
    return one["incomplete_reason"]


def test_a_running_task_that_has_yet_to_ask_for_the_detail_is_completing_the_job(env):
    t = lacking(env)
    assert reason(env) == "正在补全详情"
    env.conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (t,))   # queued (or waiting to retry) counts too
    assert reason(env) == "正在补全详情"
    env.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (t,))
    assert reason(env) == "平台未提供职位描述"   # finished: the platform really has none


def test_the_reason_stays_plain_when_the_detail_is_not_coming(env):
    t = lacking(env)
    env.conn.execute("UPDATE tasks SET payload=json_set(payload, '$.detail', 'never') WHERE id=?", (t,))
    assert reason(env) == "平台未提供职位描述"                                  # the task is told not to ask
    env.conn.executescript("DELETE FROM jobs; DELETE FROM task_jobs")
    lacking(env, detail_error="StructureError: shape")
    assert reason(env) == "详情获取失败"                                        # asked, and it failed
    env.conn.executescript("DELETE FROM jobs; DELETE FROM task_jobs")
    lacking(env, "flat")
    assert reason(env) == "平台未提供职位描述"                                  # the platform has no detail request at all
    env.conn.executescript("DELETE FROM jobs; DELETE FROM task_jobs")
    lacking(env, detail="{}")
    assert reason(env) == "平台未提供职位描述"                                  # asked already, and it did not have one


def test_the_other_reasons_keep_their_words_while_the_detail_is_on_its_way(env):
    lacking(env, missing="salary,description", salary_text="面议", salary_min=None, salary_max=None)
    assert reason(env) == "面议；正在补全详情"   # the site's own word for the salary is a fact, not something to wait for
    env.conn.executescript("DELETE FROM jobs; DELETE FROM task_jobs")
    lacking(env, "closed", missing="salary,description", salary_text=None, salary_min=None, salary_max=None)
    assert reason(env) == "需登录才能看到薪资；正在补全详情"   # logged out, the salary needs the login; the JD may still arrive
    env.auth.session = True
    assert reason(env) == "正在补全详情"                      # one phrase, however many elements wait for the detail


def test_a_detail_that_cannot_complete_the_job_is_not_on_its_way(env):
    lacking(env, "picky", missing="salary,description", salary_text=None, salary_min=None, salary_max=None)
    assert reason(env) == "需登录才能看到薪资；平台未提供职位描述"   # anonymous: the engine will not ask (Provider.detail_can_fill)
    env.auth.session = True
    assert reason(env) == "正在补全详情"


def test_reasons_cope_with_a_platform_that_no_longer_exists(env):
    job_row = {"provider": "gone", "missing": ["description"], "salary_text": None, "detail_error": None, "detail_due": 1}
    assert service.reasons(job_row, lambda provider: False) == ["平台未提供职位描述"]
    assert service.reasons({**job_row, "provider": "shop"}, lambda provider: False) == ["正在补全详情"]
    assert service.reasons({**job_row, "missing": ""}, lambda provider: False) == []


def test_the_results_page_says_the_same(env):
    from djs import results
    lacking(env)
    lacking(env, sid="y", missing="salary,description", title="u")   # two elements wait for the detail: still one reason
    page = results.search_jobs(env.conn, "py", None, complete_only=False)
    assert page["incomplete"] == {"count": 2, "reasons": [{"text": "正在补全详情", "count": 2}]}
    assert {j["incomplete_reason"] for j in page["jobs"]} == {"正在补全详情"}
    assert all("detail_due" not in j for j in page["jobs"])


# ---- derivation
def test_figures_the_provider_read_from_fields_beat_the_text(env):
    prov = env.shop
    env.mp.setattr(prov, "row", lambda job, detail: {
        "salary_text": "面议", "salary_min": 9000, "salary_max": 12000, "salary_unit": "元/月", "salary_months": 13})
    got = jobs.derive(prov, {"id": 9}, None)
    assert (got["salary_min"], got["salary_max"], got["salary_months"]) == (9000, 12000, 13)
    env.mp.setattr(prov, "row", lambda job, detail: {"salary_text": "1-2万", "salary_min": 9000})   # half a range
    got = jobs.derive(prov, {"id": 9}, None)
    assert (got["salary_min"], got["salary_max"], got["salary_unit"]) == (10000, 20000, "元/月")


def test_a_new_salary_replaces_the_stored_one_as_a_whole(env):
    run(env, detail="never")
    pk = env.conn.execute("SELECT id FROM jobs WHERE source_job_id='1'").fetchone()[0]
    assert env.conn.execute("SELECT salary_months FROM jobs WHERE id=?", (pk,)).fetchone()[0] == 14
    jobs.rederive(env.conn, pk, env.shop, {"id": 1, "title": "t1", "salary": "1-2万"}, None)   # no months any more
    row = env.conn.execute("SELECT salary_min, salary_max, salary_months FROM jobs WHERE id=?", (pk,)).fetchone()
    assert tuple(row) == (10000, 20000, None)
    jobs.rederive(env.conn, pk, env.shop, {"id": 1, "title": "t1", "salary": "面议"}, None)     # unknown now: kept
    assert env.conn.execute("SELECT salary_min FROM jobs WHERE id=?", (pk,)).fetchone()[0] == 10000


def test_job_detail_derives_the_new_fields_and_the_verdict(env):
    run(env, detail="never")
    job_id = stored(env)["2"]["id"]
    assert stored(env)["2"]["missing"] == ["description"]
    got = service.job_detail(env.conn, job_id)
    assert got["description"] == "jd2\n要求" and got["missing"] == [] and got["complete"] is True
    assert (got["salary_min"], got["salary_max"], got["incomplete_reason"]) == (10000, 15000, None)
    assert env.shop.asked == [2]
    env.mp.setitem(PAGES, 1, [{"id": 1, "title": "t", "jd": "说明"}])
    run(env, "flat")
    flat_id = stored(env, "flat")["1"]["id"]
    env.mp.setattr(egress, "lease", lambda *a: pytest.fail("a list-only platform has nothing to fetch"))
    assert service.job_detail(env.conn, flat_id)["description"] == "说明"


# ---- what readers see
def test_jobs_get_returns_complete_jobs_unless_asked_for_the_rest(env):
    task = run(env)
    complete = service.jobs_get(env.conn, task_id=1)["jobs"]
    assert sorted(j["title"] for j in complete) == ["t1", "t2", "t4"]
    assert all(j["missing"] == [] and "incomplete_reason" not in j for j in complete)
    everything = {j["title"]: j for j in service.jobs_get(env.conn, task_id=1, complete_only=False)["jobs"]}
    assert sorted(everything) == ["t1", "t2", "t3", "t4"]
    assert everything["t3"]["missing"] == ["salary"] and everything["t3"]["incomplete_reason"] == "面议"
    assert task["stats"]["incomplete"] == 1
    one = everything["t1"]
    assert (one["salary_month_min"], one["salary_month_max"], one["salary_unit"], one["salary_months"]) == (15000, 25000, "元/月", 14)
    assert set(one) >= {"district", "education", "experience", "salary_text", "source_url"}
    raw = stored(env)["1"]   # the whole row keeps the figures as the site gave them
    assert (raw["salary_min"], raw["salary_max"], raw["salary_unit"], raw["salary_months"]) == (15000, 25000, "元/月", 14)
    assert set(raw) >= {"published_at", "source_job_id", "task_id", "detail_error"}


def test_search_list_counts_complete_and_incomplete_per_site(env):
    run(env)
    run(env, "closed")
    site = {s["provider"]: s for s in service.searches(env.conn)[0]["per_site"]}
    assert (site["shop"]["jobs"], site["shop"]["complete"], site["shop"]["incomplete"]) == (4, 3, 1)
    assert site["closed"]["jobs"] == site["closed"]["complete"] + site["closed"]["incomplete"] == 4


JOB = {"provider": "shop", "salary_text": None, "detail_error": None}


def test_incomplete_reason_says_why_in_a_few_words(env):
    never = lambda provider: False  # noqa: E731
    assert service.incomplete_reason({**JOB, "missing": []}) is None
    assert service.incomplete_reason({**JOB, "missing": "", "salary_text": "面议"}) is None
    assert service.incomplete_reason({**JOB, "missing": ["salary"], "salary_text": "面议"}) == "面议"
    assert service.incomplete_reason({**JOB, "missing": ["salary"]}, never) == "平台未提供薪资"   # a public platform
    assert service.incomplete_reason({**JOB, "provider": "closed", "missing": ["salary"]}, never) == "需登录才能看到薪资"
    assert service.incomplete_reason({**JOB, "provider": "closed", "missing": ["salary"]}, lambda p: True) == "平台未提供薪资"
    assert service.incomplete_reason({**JOB, "missing": ["description"], "detail_error": "boom"}) == "详情获取失败"
    assert service.incomplete_reason({**JOB, "missing": ["description"]}) == "平台未提供职位描述"
    assert service.incomplete_reason({**JOB, "missing": "title,company,city"}) == "平台未提供职位名；平台未提供公司；平台未提供城市"
    assert service.incomplete_reason({**JOB, "provider": "gone", "missing": ["salary"]}, never) == "平台未提供薪资"


def test_the_reason_follows_the_login_state(env):
    run(env, "closed")
    reasons = lambda: {j["incomplete_reason"] for j in service.jobs_get(env.conn, complete_only=False)["jobs"] if j["missing"]}  # noqa: E731
    assert reasons() == {"面议"}
    env.conn.execute("UPDATE jobs SET salary_text=''")
    assert reasons() == {"需登录才能看到薪资"}
    env.auth.session = True
    assert reasons() == {"平台未提供薪资"}


def test_mcp_tools_take_the_new_parameters(env):
    def call(name, **args):
        return json.loads(asyncio.run(server.mcp.call_tool(name, args))[0].text)

    assert call("crawl_start", provider="shop", keyword="py", detail="always")["task_id"] == 1
    assert call("crawl_start", provider="shop", keyword="py", detail=False)["task_id"] == 2
    assert "error" in call("crawl_start", provider="shop", keyword="py", detail="sometimes")
    while (task := taskqueue.claim(env.conn, "shop")) is not None:
        engine.run_task(env.conn, task)
    assert sorted(j["title"] for j in call("jobs_get")["jobs"]) == ["t1", "t2", "t4"]
    assert call("jobs_get", complete_only=False)["total"] == 4
    schemas = {t.name: t.inputSchema["properties"] for t in asyncio.run(server.mcp.list_tools())}
    assert schemas["jobs_get"]["complete_only"]["default"] is True
    assert {"string", "boolean"} <= {opt["type"] for opt in schemas["crawl_start"]["detail"]["anyOf"]}


# ---- schema v3 on a database an earlier version wrote
V2_JOBS = """
CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'keyword',
  payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, locked_at REAL,
  next_run_at REAL NOT NULL DEFAULT 0, last_error TEXT, stats TEXT,
  created_at REAL NOT NULL DEFAULT (strftime('%s','now')));
CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, source_job_id TEXT NOT NULL,
  task_id INTEGER REFERENCES tasks(id), title TEXT, company TEXT, city TEXT, salary_text TEXT, description TEXT,
  source_url TEXT, published_at TEXT, published_at_kind TEXT, payload TEXT, detail TEXT, detail_error TEXT,
  first_seen_at REAL NOT NULL DEFAULT (strftime('%s','now')), last_seen_at REAL NOT NULL DEFAULT (strftime('%s','now')),
  UNIQUE(provider, source_job_id));
CREATE TABLE task_jobs (task_id INTEGER NOT NULL REFERENCES tasks(id), job_id INTEGER NOT NULL REFERENCES jobs(id),
  PRIMARY KEY (task_id, job_id));
CREATE TABLE provider_settings (provider TEXT PRIMARY KEY, interval REAL, jitter REAL, max_pages INTEGER,
  transport TEXT);
PRAGMA user_version = 2;
"""
NOWCODER = {"id": 7, "jobName": "算法", "recommendInternCompany": {"companyName": "甲"}, "jobCityList": ["杭州"],
            "salaryType": 2, "salaryMin": 20, "salaryMax": 30, "salaryMonth": 14,
            "ext": json.dumps({"requirements": "<p>熟悉算法</p>"})}
CHINAHR = {"jobId": "c1", "jobName": "销售", "comName": "乙", "workPlace": "南京-鼓楼区", "salary": "6k-9k元/月"}


def write_v2(home):
    old = sqlite3.connect(home / "djs.db")
    old.executescript(V2_JOBS)
    rows = [("nowcoder", "7", NOWCODER, None, "旧的<br>描述"),                       # HTML description from before
            ("chinahr", "c1", CHINAHR, {"jobDesc": "<p>负责销售</p>"}, None),         # its JD is only in the detail
            ("gone", "x", {"anything": 1}, None, None),                              # a platform that no longer exists
            ("nowcoder", "no-payload", None, None, None)]
    for provider, sid, payload, detail, description in rows:
        old.execute("INSERT INTO jobs(provider, source_job_id, title, company, city, salary_text, description, payload,"
                    " detail) VALUES (?,?,?,?,?,?,?,?,?)",
                    (provider, sid, "标题", "公司", "杭州", "10-20K", description, payload and json.dumps(payload),
                     detail and json.dumps(detail)))
    old.commit()
    old.close()


def columns(conn):
    return [tuple(r)[1:] for r in conn.execute("PRAGMA table_info(jobs)")]


def test_v2_database_is_upgraded_and_every_stored_job_is_derived(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    write_v2(tmp_path)
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    got = {r["source_job_id"]: r for r in conn.execute("SELECT * FROM jobs")}
    nc = got["7"]
    assert (nc["salary_min"], nc["salary_max"], nc["salary_unit"], nc["salary_months"]) == (20000, 30000, "元/月", 14)
    assert (nc["description"], nc["missing"], nc["complete"]) == ("熟悉算法", "", 1)   # re-derived, HTML gone
    ch = got["c1"]   # JD from the stored detail, salary from the text, district split off the place
    assert (ch["description"], ch["salary_min"], ch["salary_max"], ch["city"], ch["district"]) == (
        "负责销售", 6000, 9000, "南京", "鼓楼区")
    assert (ch["missing"], ch["complete"], ch["education"]) == ("", 1, None)
    gone, bare = got["x"], got["no-payload"]   # nothing to derive from: judged by what is stored, no salary figures
    assert (gone["missing"], gone["complete"], bare["missing"]) == ("salary,description", 0, "salary,description")
    assert gone["description"] is None and gone["salary_text"] == "10-20K"
    # v4: the salary per month and the duplicate fingerprint are derived for what was already stored
    assert (nc["salary_month_min"], nc["salary_month_max"], nc["fingerprint"]) == (20000, 30000, "甲|算法|杭州")
    assert (ch["salary_month_min"], ch["salary_month_max"], ch["fingerprint"]) == (6000, 9000, "乙|销售|南京")
    assert (gone["salary_month_min"], gone["salary_month_max"], gone["fingerprint"]) == (None, None, None)
    assert "jobs_fingerprint" in {r["name"] for r in conn.execute("PRAGMA index_list(jobs)")}


def test_the_upgrade_is_repeatable_and_matches_a_fresh_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path / "old"))
    (tmp_path / "old").mkdir()
    write_v2(tmp_path / "old")
    upgraded = db.connect()
    before = [tuple(r) for r in upgraded.execute("SELECT * FROM jobs ORDER BY id")]
    upgraded.execute("PRAGMA user_version = 2")   # as if another process were upgrading right now
    db.connect().close()
    assert [tuple(r) for r in upgraded.execute("SELECT * FROM jobs ORDER BY id")] == before
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 4
    monkeypatch.setenv("DJS_HOME", str(tmp_path / "new"))
    fresh = db.connect()
    assert sorted(columns(fresh)) == sorted(columns(upgraded))   # same names, types, NOT NULL and defaults


def test_a_failed_upgrade_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    write_v2(tmp_path)
    monkeypatch.setattr(jobs, "refresh", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    monkeypatch.setattr(db, "_initialized", set())
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        db.connect()
    raw = sqlite3.connect(tmp_path / "djs.db")
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2   # still version 2, so the next start tries again
    assert "salary_min" not in [r[1] for r in raw.execute("PRAGMA table_info(jobs)")]
