"""The local half of the crawl-limit tests L1 L3 L5 L6 L7 L8 (docs/plan/v1.4.0/design.md, "Limit tests (T6)").

What each row asks of the real engine, pacer, exit pool and task queue when a site pushes back:
  L1 pace floor       a site that refuses a request that comes too soon: back-off, the platform's cooldown, the cause
                      kept in front of the rest, nothing sent before the cooldown is over, the floor on the interval
  L3 detail budget    a site that refuses the (N+1)-th detail request: the list is kept, one cooldown, no retry storm
  L5 session dies     a login platform loses its session in the middle of a crawl: it waits for a login, and nothing
                      goes out signed out (A24). Parametrised over the registry by `auth`, so a platform added later
                      is covered without touching this file. An `optional` platform with a saved login follows the
                      engine's documented rule instead (engine._login_expired): the dead session is dropped and the
                      NEXT try goes on without it; the run that met the death sends nothing more
  L6 challenge page   HTTP 200 with a slider / captcha page instead of the listing: never "no results", never done
  L7 exits            one dead exit among two, then the only exit dead (F47, F52)
  L8 long run         200 scheduled searches on a fake clock: nothing grows, nothing stays running, cookies run out,
                      a cooldown ends

Everything runs against fake sites behind httpx.MockTransport and the REAL pacing, queue and pool code. The clock is
fake: the modules that read the time or sleep get a Clock, so a thirty-minute cooldown is one function call and nothing
ever really waits. No network, no browser.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time as real_time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from djs import (auth, browser, db, egress, engine, filelock, jobs, pacer, providers, results, server, service, taskqueue,
                 ui_api)
from djs.providers.base import Blocked, Delisted, FetchError, Login, Provider, get_json, need
from provider_fakes import client as canned_client, job as stored_job, stored_jobs

SITE = "https://site.test"
EPS = 1e-3   # the fake clock adds floats the size of a unix time: gaps are exact only to about a microsecond
REAL_PROVIDERS = providers.names()   # the registry as shipped (the fixtures below swap it for fakes)
REAL = {name: providers.get(name) for name in REAL_PROVIDERS}


# ============================================================ the fake world
class Clock:
    """The time of every module that paces, rests or schedules, and its sleep. Time moves only when a test moves it or a
    pacing wait sleeps."""

    def __init__(self):
        self.now = real_time.time()   # coherent with the defaults the database writes itself
        self.slept = 0.0

    def time(self) -> float:
        return self.now

    monotonic = time

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        self.slept += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Hit:
    """One request a site received."""
    at: float
    kind: str            # "list" | "detail"
    n: int               # the page, or the job's id (0 when the request carries neither)
    exit: str            # the exit it came through
    cookie: str | None   # the Cookie header: None = a signed-out request
    status: int = 0      # what the site answered (0: the exit never reached it)


class Site:
    """The network of one platform: `/list?page=N` and `/detail?id=N`. `rule(site, hit)` may answer instead (an
    httpx.Response); `dead` is the set of exits that refuse the connection. Every request is kept in `hits`."""

    def __init__(self, clock: Clock):
        self.clock, self.hits, self.dead, self.rule = clock, [], set(), None
        self.pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}], 3: [{"id": 5}]}   # 5 jobs on 3 pages

    def serve(self, exit_id: str, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        n = next((int(v) for k in ("page", "id") if (v := params.get(k)) and v.isdigit()), 0)
        kind = "detail" if request.url.path.endswith("/detail") else "list"
        hit = Hit(self.clock.now, kind, n, exit_id, request.headers.get("cookie"))
        self.hits.append(hit)
        if exit_id in self.dead:
            raise httpx.ConnectError("connection refused", request=request)
        answer = self.rule(self, hit) if self.rule else None
        if answer is None:
            answer = (httpx.Response(200, json={"items": self.pages.get(n, [])}) if kind == "list"
                      else httpx.Response(200, json={"detail": {"jd": f"jd{n}"}}))
        hit.status = answer.status_code
        return answer

    def of(self, kind: str) -> list[Hit]:
        return [h for h in self.hits if h.kind == kind]

    def gaps(self, hits: list[Hit] | None = None) -> list[float]:
        at = [h.at for h in (self.hits if hits is None else hits)]
        return [b - a for a, b in zip(at, at[1:])]


class Shop(Provider):
    """A public platform: a paged list, then one detail request per job for the description."""
    name, label = "shop", "示例站"
    interval, jitter, min_interval = 2.0, 0.0, 1.0
    page_size, first_page, page_cap = 2, 1, 3

    def fetch(self, client, payload, page):
        return need(get_json(client.get(f"{SITE}/list", params={"page": page})), "items")

    def job_id(self, job):
        return str(need(job, "id", typ=int))

    def detail(self, client, job):
        return need(get_json(client.get(f"{SITE}/detail", params={"id": job["id"]})), "detail", typ=dict)

    def row(self, job, detail):
        return {"title": f"t{job['id']}", "company": "c", "city": "杭州", "salary_text": "10-20K",
                "description": detail and detail["jd"]}


class Member(Shop):
    name, label, auth = "member", "会员站", "login"
    login = Login(url=f"{SITE}/login", method="qr", domain="site.test", cookies_any=("s",))


def variant(base: type[Provider], name: str, **attrs) -> Provider:
    """An instance of `base` under another name and with other class facts."""
    return type(name.title(), (base,), {"name": name, **attrs})()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DJS_HOME", str(tmp_path / "djs"))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def world(home, monkeypatch):
    """Fake platforms behind the real engine: `add(provider)` registers it with a fresh Site. Every http request, and
    every browser page, goes through the platform's Site; pacing, rests and the queue run on `clock`."""
    clock = Clock()
    for module in (pacer, egress, taskqueue, service, engine, auth, filelock):
        monkeypatch.setattr(module, "time", clock)
    provs: dict[str, Provider] = {}
    sites: dict[str, Site] = {}
    browsers: list[str] = []   # the platforms a browser page was opened for

    def get(name):
        if name not in provs:
            raise KeyError(name)
        return provs[name]

    monkeypatch.setattr(providers, "get", get)
    monkeypatch.setattr(providers, "names", lambda: list(provs))
    real_make = egress.make_client

    def make_client(exit_id, prov, interval, jitter, cookies=None, transport=None):
        site = sites[prov.name]
        return real_make(exit_id, prov, interval, jitter, cookies,
                         transport=httpx.MockTransport(lambda request: site.serve(exit_id, request)))

    @contextmanager
    def open_browser(prov, exit_id, interval, jitter):
        """A browser page that acts like a client: the shared profile's cookies, the platform's pace."""
        browsers.append(prov.name)
        jar = auth.load_cookies(prov.name) if prov.login is not None else None
        with egress.make_client(exit_id, prov, interval, jitter, jar) as page:
            yield page

    monkeypatch.setattr(egress, "make_client", make_client)
    monkeypatch.setattr(browser, "open_browser", open_browser)
    monkeypatch.setattr(browser, "ready", lambda: True)
    monkeypatch.setattr(browser, "find_browser", lambda: ("chrome", Path("/apps/chrome")))   # status looks for one

    def add(prov: Provider, site: Site | None = None) -> tuple[Provider, Site]:
        provs[prov.name], sites[prov.name] = prov, site or Site(clock)
        return prov, sites[prov.name]

    conn = db.connect()
    yield SimpleNamespace(clock=clock, conn=conn, add=add, provs=provs, sites=sites, browsers=browsers)
    conn.close()


def start(w, name="shop", keyword="py", city="杭州", max_pages=3) -> int:
    """One search on one platform: the id of its task."""
    return service.search(w.conn, keyword, city, sites=[name], max_pages=max_pages)["task_ids"][0]


def task(w, tid: int) -> dict:
    return taskqueue.get(w.conn, tid)


def next_due(w) -> float | None:
    return w.conn.execute("SELECT MIN(next_run_at) FROM tasks WHERE status='pending'").fetchone()[0]


def wait_until(w, done, limit: int = 600, each=lambda: None) -> None:
    """Run what is due; while nothing is, let the clock jump to the next moment something can run. `each` looks at the
    world after every step."""
    for _ in range(limit):
        if done():
            return
        if not engine.tick(w.conn):
            due = next_due(w)
            assert due is not None, "nothing is left to run and the condition does not hold"
            w.clock.now = max(w.clock.now, due) + 0.01
        each()
    raise AssertionError("did not settle")


def shown(w, keyword="py", city="杭州") -> list[dict]:
    """The jobs of a search the way the tool shows them, whole or not (`missing`, and why in `incomplete_reason`)."""
    return service.jobs_get(w.conn, keyword, city, complete_only=False, limit=100)["jobs"]


def health(w, exit_id: str = "direct", provider: str = "shop") -> dict:
    return dict(w.conn.execute("SELECT * FROM egress_health WHERE egress_id=? AND provider=?",
                               (exit_id, provider)).fetchone())


def refusal(status: int, retry_after: int | None = None) -> httpx.Response:
    return httpx.Response(status, headers={"Retry-After": str(retry_after)} if retry_after else {})


def log_in(prov: Provider) -> None:
    """A saved session, with the cookies the platform's own success rule names."""
    names = (*prov.login.cookies_any, *prov.login.cookies_all) or ("sid",)
    auth.save_cookies(prov.name, dict.fromkeys(names, "1"))


# ============================================================ L1: the pace floor
def faster_than(seconds: float, status: int = 429, retry_after: int | None = None):
    """A site that refuses a request coming less than `seconds` after the one before it (whatever that one was)."""
    def rule(site: Site, hit: Hit):
        if len(site.hits) > 1 and hit.at - site.hits[-2].at < seconds - EPS:
            return refusal(status, retry_after)
    return rule


@pytest.mark.parametrize("name", REAL_PROVIDERS)
def test_l1_every_platform_has_a_floor_and_no_default_or_preset_goes_below_it(home, name):
    prov, conn = REAL[name], db.connect()
    assert 0 < prov.min_interval <= prov.interval
    assert pacer.effective(conn, name)[0] == prov.interval
    with pytest.raises(ValueError, match="force=True"):
        pacer.set_frequency(conn, name, interval=prov.min_interval / 2)
    with pytest.raises(ValueError, match="force=True"):
        service.set_frequency(conn, name, interval=prov.min_interval - 0.01)   # the tool's way in is the same
    assert pacer.effective(conn, name)[0] == prov.interval   # a refused value is not kept
    assert pacer.set_frequency(conn, name, interval=prov.min_interval)["interval"] == prov.min_interval   # the floor itself
    for key in ui_api.PRESETS:   # the app's speed choices clamp at the floor and never need force
        ui_api.apply_preset(conn, key)
        assert pacer.effective(conn, name)[0] >= prov.min_interval, (name, key)
    # below the floor is only possible on purpose
    assert pacer.set_frequency(conn, name, interval=prov.min_interval / 2, force=True)["interval"] == prov.min_interval / 2
    conn.close()


def test_l1_at_the_default_pace_every_gap_holds_and_the_site_never_refuses(world):
    prov, site = world.add(variant(Shop, "shop", jitter=0.5))
    site.rule = faster_than(prov.interval)
    tid = start(world)
    wait_until(world, lambda: task(world, tid)["status"] == "done")
    assert len(site.hits) == 3 + 5 and {h.status for h in site.hits} == {200}   # 3 list pages, then 5 details
    assert all(prov.interval - EPS <= g <= prov.interval + prov.jitter + EPS for g in site.gaps()), site.gaps()
    assert health(world)["total_fail"] == 0 and health(world)["blocked_until"] == 0
    assert world.clock.slept >= 7 * prov.interval   # the pace was waited for (on the fake clock), not skipped


@pytest.mark.parametrize("status, retry_after", [(429, None), (429, 3600), (403, None)])
def test_l1_below_the_pace_a_refusal_starts_the_cooldown_and_nothing_goes_out_until_it_ends(world, status, retry_after):
    prov, site = world.add(Shop())
    site.rule = faster_than(2.0, status, retry_after)   # the site wants 2 s between requests
    with pytest.raises(ValueError, match="force=True"):
        pacer.set_frequency(world.conn, "shop", interval=0.5)   # 0.5 is below the platform's floor of 1.0
    pacer.set_frequency(world.conn, "shop", interval=0.5, force=True)
    tid = start(world)
    cooldown = max(egress.BLOCK_COOLDOWN, retry_after or 0)

    # --- the first run: page 1 is served, page 2 comes 0.5 s later and is refused
    assert engine.tick(world.conn)
    assert [h.status for h in site.hits] == [200, status]
    refused = site.hits[-1]
    assert refused.at - site.hits[0].at == pytest.approx(0.5, abs=EPS)
    row = task(world, tid)
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, f"Blocked: HTTP {status}")
    delay = row["next_run_at"] - refused.at
    assert delay >= max(2, retry_after or 0) - EPS   # backed off, and never earlier than the site asked
    assert retry_after or delay <= 2.5 + EPS
    assert health(world)["blocked_until"] == pytest.approx(refused.at + cooldown, abs=EPS)
    assert len(stored_jobs(world.conn, tid)) == 2   # page 1 is kept
    seen = service.status(world.conn)
    assert seen["platforms"]["shop"]["cooling_down"] == [{"exit": "direct", "until": pytest.approx(refused.at + cooldown, abs=EPS)}]
    assert seen["platforms"]["shop"]["last_error"] == {"task_id": tid, "status": "retrying", "kind": "blocked",
                                                       "reason": "被网站暂时限制，稍后再试", "retry_at": row["next_run_at"]}
    assert [(e["id"], e["cooling_down"]) for e in seen["exits"]] == [("direct", True)]

    # --- the task comes due again and again during the rest (not at all when the site said when to come back): it
    # asks for nothing, and the cause stays in front (F47)
    asked, claimed = len(site.hits), 0
    while world.clock.now + 61 < health(world)["blocked_until"]:
        world.clock.advance(61)
        if engine.tick(world.conn):   # claimed, and every exit is resting: no request
            claimed += 1
            row = task(world, tid)
            assert row["attempts"] == 1 and row["status"] == "pending"   # a rest does not burn an attempt
            assert row["last_error"].startswith(f"Blocked: HTTP {status}") and "cooling down" in row["last_error"]
            assert row["last_error"].count(engine.REST_MARK) == 1   # the rest is not stacked on itself
            assert results.classify(row["last_error"])[0] == "blocked"   # read as a block, not as "the site errs"
        assert len(site.hits) == asked
    assert (claimed > 0) == (retry_after is None)

    # --- after the cooldown, at a pace the site tolerates, the same task completes
    pacer.set_frequency(world.conn, "shop", interval=2.0)
    world.clock.now = max(health(world)["blocked_until"], task(world, tid)["next_run_at"]) + 1
    assert engine.tick(world.conn)
    resumed = site.hits[asked:]
    assert resumed[0].at - refused.at >= cooldown   # the first request after the refusal waited out the whole cooldown
    assert all(g >= 2.0 - EPS for g in site.gaps(resumed))
    assert {h.status for h in resumed} == {200}
    assert task(world, tid)["status"] == "done" and len(stored_jobs(world.conn, tid)) == 5
    assert health(world)["total_fail"] == 1   # one refusal in all


def test_l1_a_pace_that_stays_too_fast_costs_one_refused_request_per_cooldown_until_the_task_ends(world):
    """Nothing in the engine slows a pace the user forced below what the site tolerates: every try meets the wall again,
    but only ever once per cooldown, and after 8 tries the task is over (failed, with the cause)."""
    prov, site = world.add(Shop())
    site.rule = faster_than(2.0)
    pacer.set_frequency(world.conn, "shop", interval=0.5, force=True)
    tid = start(world)
    wait_until(world, lambda: task(world, tid)["status"] == "dead", limit=3000)
    refused = [h for h in site.hits if h.status == 429]
    assert len(refused) == taskqueue.MAX_ATTEMPTS and len(site.hits) == 2 * taskqueue.MAX_ATTEMPTS   # 1 served, 1 refused, each try
    assert all(b.at - a.at >= egress.BLOCK_COOLDOWN for a, b in zip(refused, refused[1:]))
    row = task(world, tid)
    assert (row["status"], row["last_error"]) == ("dead", "Blocked: HTTP 429")
    assert service.search_status(world.conn, "py", "杭州")["per_site"][0]["outcome"] == "failed"


# ============================================================ L3: the detail budget
def details_budget(allowed: int, status: int = 403, retry_after: int | None = None, window: float = 1200.0):
    """A site that serves `allowed` detail requests per `window` seconds (from the first of them) and refuses the rest;
    list pages are always served."""
    state = {"count": 0, "since": None}

    def rule(site: Site, hit: Hit):
        if hit.kind != "detail":
            return None
        if state["since"] is not None and hit.at - state["since"] >= window:
            state.update(count=0, since=None)
        state["since"] = hit.at if state["since"] is None else state["since"]
        state["count"] += 1
        return refusal(status, retry_after) if state["count"] > allowed else None
    return rule


@pytest.mark.parametrize("status, retry_after", [(403, None), (429, 7200)])
def test_l3_the_refused_detail_ends_the_run_keeps_the_list_cools_down_once_and_carries_on_later(world, status, retry_after):
    prov, site = world.add(Shop())
    site.rule = details_budget(3, status, retry_after)
    tid = start(world)
    cooldown = max(egress.BLOCK_COOLDOWN, retry_after or 0)

    assert engine.tick(world.conn)
    # 3 list pages, the 3 details the budget allows, and the 4th, refused: the run stops there, no request after it
    assert [(h.kind, h.status) for h in site.hits] == [("list", 200)] * 3 + [("detail", 200)] * 3 + [("detail", status)]
    refused = site.hits[-1]
    row = task(world, tid)
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, f"Blocked: HTTP {status}")   # a retry time, not a loop
    assert row["next_run_at"] > world.clock.now
    assert taskqueue.outcome(row["status"], 5, row["last_error"]) == "retrying"
    # the list it already has is kept; the jobs without a detail are incomplete and say so
    assert row["stats"]["pages"] == 3 and row["stats"]["jobs_new"] == 5
    assert (row["stats"]["details_total"], row["stats"]["details_ok"]) == (5, 3)
    jobs = shown(world)   # as jobs_get shows them: 3 whole, 2 that lack the description and say a retry is on its way
    assert len(jobs) == 5 and sorted(bool(j["missing"]) for j in jobs) == [False, False, False, True, True]
    for job in (j for j in jobs if j["missing"]):
        assert job["missing"] == ["description"] and job["incomplete_reason"] == "正在补全详情"
    # the refused job and the one it kept the pass from asking carry the refusal; the whole ones carry none
    assert sorted((j["source_job_id"], (j["detail_error"] or "")[:9]) for j in stored_jobs(world.conn, tid)) == [
        ("1", ""), ("2", ""), ("3", ""), ("4", "Blocked: "), ("5", "Blocked: ")]
    found = service.search_status(world.conn, "py", "杭州")
    assert found["status"] == "retrying" and found["per_site"][0]["outcome"] == "retrying"
    assert (found["per_site"][0]["jobs"], found["per_site"][0]["complete"]) == (5, 3)
    assert found["per_site"][0]["reason"] == "被网站暂时限制，稍后再试" and found["per_site"][0]["retry_at"] == row["next_run_at"]

    # --- the whole rest: the platform asks for nothing and cools down once
    asked = len(site.hits)
    while world.clock.now + 61 < health(world)["blocked_until"]:
        world.clock.advance(61)
        engine.tick(world.conn)   # due now and then, or not at all when the site said when to come back
        assert len(site.hits) == asked   # nothing after the refusal, in a cooldown of half an hour or more
    assert (task(world, tid)["attempts"], health(world)["total_fail"]) == (1, 1)

    # --- afterwards the budget is back: the list again, and a detail only for the jobs that still lack one
    world.clock.now = max(health(world)["blocked_until"], task(world, tid)["next_run_at"]) + 1
    assert engine.tick(world.conn)
    after = site.hits[asked:]
    assert after[0].at - refused.at >= cooldown
    assert [h.n for h in after if h.kind == "detail"] == [4, 5] and [h.n for h in after if h.kind == "list"] == [1, 2, 3]
    assert {h.status for h in after} == {200}
    row = task(world, tid)
    assert row["status"] == "done" and all(j["complete"] for j in stored_jobs(world.conn, tid))
    assert all(j["detail_error"] is None for j in stored_jobs(world.conn, tid))   # the refusal is gone with the detail
    assert health(world)["total_fail"] == 1   # still one refusal in all


def test_l3_a_site_that_never_gives_more_ends_the_task_after_one_refusal_per_cooldown(world):
    prov, site = world.add(Shop())
    site.rule = details_budget(3, window=10 ** 9)   # the 3 details of the first run are all it will ever give
    tid = start(world)
    wait_until(world, lambda: task(world, tid)["status"] == "dead", limit=3000)
    refused = [h for h in site.hits if h.status == 403]
    assert len(refused) == taskqueue.MAX_ATTEMPTS   # one refused request per attempt, nothing in between
    assert all(b.at - a.at >= egress.BLOCK_COOLDOWN for a, b in zip(refused, refused[1:]))   # a cooldown apart
    row = task(world, tid)
    assert (row["status"], row["last_error"]) == ("dead", "Blocked: HTTP 403")
    assert taskqueue.outcome(row["status"], 5, row["last_error"]) == "failed"
    jobs = stored_jobs(world.conn, tid)
    assert len(jobs) == 5 and sum(j["complete"] for j in jobs) == 3   # the list and the 3 details it got are kept


def test_l3_when_the_task_gives_up_the_jobs_without_a_detail_do_not_blame_the_platform(world):
    prov, site = world.add(Shop())
    site.rule = details_budget(3, window=10 ** 9)
    tid = start(world)
    wait_until(world, lambda: task(world, tid)["status"] == "dead", limit=3000)
    lacking = [j for j in shown(world) if j["missing"]]
    assert len(lacking) == 2
    assert [j["incomplete_reason"] for j in lacking] == ["网站拒绝了详情请求"] * 2   # the site refused, not "the platform has none"
    page = results.search_jobs(world.conn, "py", "杭州")   # the results page says the same, once
    assert page["incomplete"] == {"count": 2, "reasons": [{"text": "网站拒绝了详情请求", "count": 2}]}


def test_l3_refusing_details_marks_only_the_jobs_that_still_lack_one(world):
    """A detail can arrive between the request that was refused and the mark (a job opened in the app meanwhile)."""
    bare = stored_job(world.conn, "shop", "1", [], complete=0, missing="description")
    whole = stored_job(world.conn, "shop", "2", [], detail='{"jd": "x"}')
    jobs.refuse_details(world.conn, [bare, whole], Blocked("HTTP 403"))
    marks = dict(world.conn.execute("SELECT id, detail_error FROM jobs").fetchall())
    assert marks == {bare: "Blocked: HTTP 403", whole: None}


# ============================================================ L5: the session dies in the middle of a crawl
def stand_in(real: Provider) -> Provider:
    """A platform with the real one's name, auth, login, transports and facts, and the fake site's list and detail."""
    class Stand(type(real)):
        requires_env = ()
        interval = jitter = min_interval = 0.0
        page_size, first_page, page_cap = 2, 1, None
        requires_city, city_filter, home = False, "none", ""

        def warmup(self, client, payload):
            pass

        fetch, job_id, detail, row = Shop.fetch, Shop.job_id, Shop.detail, Shop.row

        def meta(self, job, detail):
            return Provider.meta(self, job, detail)

        def detail_can_fill(self, missing, logged_in):
            return True
    Stand.__name__ = f"Stand{type(real).__name__}"
    return Stand()


def _with_a_login():
    found = []
    for name in REAL_PROVIDERS:
        prov = REAL[name]
        if prov.auth == "login" or (prov.auth == "optional" and prov.login is not None):
            found.append(pytest.param(name, id=f"{prov.auth}-{name}"))
    return found


def dies(kind: str, n: int):
    """A site whose session ends right before it answers request (kind, n): from then on a request that carries the
    session cookie is told to log in again. A request without one is served as ever."""
    def rule(site: Site, hit: Hit):
        if (hit.kind, hit.n) == (kind, n):
            site.alive = False
        if hit.cookie and not getattr(site, "alive", True):
            return httpx.Response(401)
    return rule


def test_l5_the_registry_has_platforms_with_a_login_to_cover():
    assert {p.auth for p in REAL.values()} >= {"login", "optional", "public"}
    assert len(_with_a_login()) >= 2


@pytest.mark.parametrize("dies_before", [("list", 2), ("detail", 1)], ids=["before-page-2", "before-the-first-detail"])
@pytest.mark.parametrize("name", _with_a_login())
def test_l5_a_dead_session_ends_the_run_and_nothing_goes_out_signed_out(world, name, dies_before):
    prov, site = world.add(stand_in(REAL[name]))
    site.rule = dies(*dies_before)
    log_in(prov)
    tid = start(world, name)
    answered = {("list", 2): 1, ("detail", 1): 3}[dies_before]   # the requests served before the session died
    kept = {("list", 2): 2, ("detail", 1): 5}[dies_before]       # the jobs of the pages it did get

    assert engine.tick(world.conn)
    # the run went the way the platform's own facts say: a browser page where its transport is a browser, http elsewhere
    assert (name in world.browsers) == (pacer.transport(world.conn, name, True) == "browser")
    refused = [h for h in site.hits if h.status == 401]
    assert len(refused) == 1 and site.hits[-1] is refused[0] and len(site.hits) == answered + 1   # nothing after the 401
    assert all(h.cookie for h in site.hits)   # every request so far carried the session
    assert auth.status(name)["state"] == "expired"   # the session is dropped ...
    assert world.conn.execute("SELECT COUNT(*) FROM egress_health WHERE blocked_until > 0").fetchone()[0] == 0   # ... not blamed on the exit
    assert len(stored_jobs(world.conn, tid)) == kept   # what it already had is kept
    assert all(j["detail_error"] is None for j in stored_jobs(world.conn, tid))   # a dead session is not "the site refused"
    row = task(world, tid)

    if prov.auth == "login":
        # A24: a login platform is never crawled signed out. The task waits for the user and stops asking.
        assert (row["status"], row["attempts"], row["last_error"]) == ("waiting_login", 0, "login required")
        assert service.search_status(world.conn, "py", "杭州")["status"] == "waiting_login"
        assert engine.tick(world.conn) is False   # nothing left to run, however long we wait
        world.clock.advance(7 * 86400)
        assert engine.tick(world.conn) is False
        # the user presses retry without logging in: the task goes straight back to waiting, with no request at all
        assert taskqueue.requeue(world.conn, [tid]) == 1
        assert engine.tick(world.conn) is True and task(world, tid)["status"] == "waiting_login"
        # a job opened in the app does not reach the site signed out either
        job = world.conn.execute("SELECT id FROM jobs WHERE detail IS NULL ORDER BY id").fetchone()["id"]
        with pytest.raises(ValueError, match="login required"):
            service.job_detail(world.conn, job)
        assert len(site.hits) == answered + 1 and all(h.cookie for h in site.hits)
        # once the user logs in again the task resumes and completes
        site.alive, site.rule = True, None
        log_in(prov)
        assert taskqueue.resume_login(world.conn, name) == 1
        wait_until(world, lambda: task(world, tid)["status"] == "done")
        assert all(h.cookie for h in site.hits) and len(stored_jobs(world.conn, tid)) == 5
    else:
        # an optional platform works signed out (decision in engine._login_expired and test_login_engine): the dead session
        # is dropped, the run ends at once, and the NEXT try goes on without it
        assert (row["status"], row["attempts"]) == ("pending", 1)
        assert len(site.hits) == answered + 1   # the dying run sent nothing signed out
        world.clock.advance(60)
        wait_until(world, lambda: task(world, tid)["status"] == "done")
        later = site.hits[answered + 1:]
        assert later and all(h.cookie is None for h in later)   # a fresh run, without the session


def test_l5_the_probe_of_a_login_that_flips_marks_it_expired_and_the_engine_follows(world, monkeypatch):
    """The saved session still looks fine until the platform's own probe says otherwise (service.login_check)."""
    probed = Login(url=f"{SITE}/login", method="qr", domain="site.test", cookies_any=("s",), probe_url=f"{SITE}/me",
                   probe_fail_text="请先登录")
    prov, site = world.add(variant(Member, "member", login=probed))
    log_in(prov)
    answer = {"body": "ok"}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, text=answer["body"], request=httpx.Request("GET", url)))
    assert service.login_check(world.conn, "member")["state"] == "logged_in"
    answer["body"] = "请先登录"   # the platform ends the session
    assert service.login_check(world.conn, "member")["state"] == "expired"
    tid = start(world, "member")
    assert engine.tick(world.conn)
    assert task(world, tid)["status"] == "waiting_login" and site.hits == []   # not one request


# ============================================================ L6: the challenge page
VERIFY_PAGE = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>安全验证</title></head><body>"
               "<div id='nc_1_wrapper' class='nc-container'>请按住滑块，拖动到最右边</div>"
               "<script src='//g.alicdn.com/sd/nch5/index.js'></script></body></html>")
VISIT_PAGE = ("<html><head><title>访问验证</title></head><body><div class='verify-wrap'>"
              "<p>您的访问行为异常，请完成验证后继续访问</p><button>点击验证</button></div></body></html>")
CAPTCHA_ONLY = ("<html><head></head><body><script src='https://turing.captcha.qcloud.com/TCaptcha.js'></script>"
                "<script>new TencentCaptcha('190000000', function(){}).show()</script></body></html>")
CHALLENGES = {"verification": VERIFY_PAGE, "visit-verification": VISIT_PAGE, "empty-with-captcha-script": CAPTCHA_ONLY}
# what a platform that does not read HTML is told by a JSON answer with a business code of its own
JSON_REFUSAL = '{"code": 9999, "message": "请先完成安全验证", "data": null}'
KEYS = {"DJS_JOB51_HMAC_KEY": "k", "DJS_YUPAO_NONCE_KEY": "k", "DJS_YUPAO_SIGN_SALT": "s", "DJS_JOB58_LUNA_SALT": "s"}


@pytest.fixture
def keys(monkeypatch):
    for k, v in KEYS.items():
        monkeypatch.setenv(k, v)


def fetch_once(name: str, body: str, status: int = 200):
    prov = REAL[name]
    payload = dict(prov.probe_payload())
    return prov.fetch(canned_client(text=body, status=status), payload, prov.first_page)


@pytest.mark.parametrize("shape", list(CHALLENGES))
@pytest.mark.parametrize("name", REAL_PROVIDERS)
def test_l6_a_challenge_page_is_an_error_never_an_empty_list(home, keys, name, shape):
    with pytest.raises(FetchError) as caught:   # an empty list would be "no jobs": it must raise
        fetch_once(name, CHALLENGES[shape])
    assert str(caught.value) and not isinstance(caught.value, Delisted)   # and say something


@pytest.mark.parametrize("name", REAL_PROVIDERS)
def test_l6_an_empty_answer_and_a_business_refusal_are_errors_too(home, keys, name):
    for body in ("", JSON_REFUSAL):
        with pytest.raises(FetchError):
            fetch_once(name, body)


@pytest.mark.parametrize("shape", list(CHALLENGES))
@pytest.mark.parametrize("name", REAL_PROVIDERS)
def test_l6_a_challenge_page_is_recognised_as_a_block(home, keys, name, shape):
    """A Blocked, not a StructureError: a cooldown of the exit and a retry much later (8 tries in 3.5 hours, where a page
    that changed is tried 8 times in about 14 minutes), and the user reads "被网站暂时限制", not "网站页面变了"."""
    with pytest.raises(Blocked) as caught:
        fetch_once(name, CHALLENGES[shape])
    assert results.classify(f"Blocked: {caught.value}")[0] == "blocked"


@pytest.mark.parametrize("name", REAL_PROVIDERS)
def test_l6_through_the_engine_a_challenge_page_never_reads_as_no_results_and_ends_failed_with_a_reason(
        world, keys, name):
    real = type(REAL[name])()
    prov, site = world.add(real)
    site.rule = lambda s, hit: httpx.Response(200, text=VERIFY_PAGE)
    if prov.login is not None:
        log_in(prov)   # a login platform is only visited with a session
    probe = prov.probe_payload()
    tid = start(world, name, probe["keyword"], probe.get("city", ""), max_pages=1)
    seen = []

    def each():
        found = service.search_status(world.conn, probe["keyword"], probe.get("city", ""))["per_site"][0]
        seen.append(found["outcome"])
        assert found["outcome"] in ("running", "retrying", "failed"), found   # never found, no_results or waiting_login
        assert task(world, tid)["status"] != "done"
        if found["outcome"] == "retrying":
            assert found["reason"] and found["retry_at"] > world.clock.now - 1   # a stated reason and when it tries again
    wait_until(world, lambda: task(world, tid)["status"] == "dead", limit=2500, each=each)
    row = task(world, tid)
    assert taskqueue.outcome(row["status"], 0, row["last_error"]) == "failed"
    kind, reason = results.classify(row["last_error"])
    assert row["last_error"] and kind != "other" and reason   # the reason is stated in words, not "没成功"
    assert "retrying" in seen   # it went through its retries before it gave up
    assert site.hits and all(h.status == 200 for h in site.hits)
    assert row["stats"] is None and world.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0   # nothing stored


def test_l6_a_challenge_in_the_middle_of_the_list_is_not_the_last_page(world):
    """Page 1 is full, page 2 is a challenge page: the loop must not take it for the end of the list."""
    prov, site = world.add(Shop())
    site.rule = lambda s, hit: httpx.Response(200, text=VISIT_PAGE) if (hit.kind, hit.n) == ("list", 2) else None
    tid = start(world)
    assert engine.tick(world.conn)
    row = task(world, tid)
    assert row["status"] == "pending" and row["last_error"].startswith("Blocked")   # a block, retried after a cooldown
    assert len(stored_jobs(world.conn, tid)) == 2   # what page 1 gave is kept
    assert service.search_status(world.conn, "py", "杭州")["per_site"][0]["outcome"] == "retrying"
    assert health(world)["blocked_until"] > world.clock.now


@pytest.mark.parametrize("shape", list(CHALLENGES))
def test_l6_a_challenge_page_on_a_detail_ends_the_detail_pass(world, shape):
    prov, site = world.add(Shop())
    site.rule = lambda s, hit: httpx.Response(200, text=CHALLENGES[shape]) if hit.kind == "detail" else None
    tid = start(world)
    assert engine.tick(world.conn)
    assert len(site.of("detail")) == 1   # the first refusal ends the pass
    row = task(world, tid)
    assert (row["status"], row["attempts"], row["last_error"].startswith("Blocked")) == ("pending", 1, True)   # retried later
    assert health(world)["blocked_until"] > world.clock.now and len(site.hits) == 3 + 1   # one cooldown, nothing after it
    assert len(stored_jobs(world.conn, tid)) == 5   # the list is kept; its jobs lack the detail and say a retry is coming
    assert {j["incomplete_reason"] for j in shown(world) if j["missing"]} == {"正在补全详情"}


def test_l6_a_detail_that_cannot_be_read_is_that_jobs_problem_not_the_passs(world):
    """The other side of the same rule: one odd answer is kept on its job and the pass goes on."""
    prov, site = world.add(Shop())
    site.rule = lambda s, hit: httpx.Response(200, json={"detail": "gone"}) if (hit.kind, hit.n) == ("detail", 2) else None
    tid = start(world)
    assert engine.tick(world.conn)
    row = task(world, tid)
    assert row["status"] == "done" and (row["stats"]["details_ok"], row["stats"]["details_failed"]) == (4, 1)
    assert [j["incomplete_reason"] for j in shown(world) if j["missing"]] == ["详情获取失败"]
    assert health(world)["total_fail"] == 0   # and the exit is not blamed


@pytest.mark.parametrize("status", [404, 410])
def test_l6_a_detail_that_answers_404_is_that_jobs_problem_too(world, status):
    """A posting that was taken down: that job says so, the jobs after it are still asked, the task is done."""
    prov, site = world.add(Shop())
    site.rule = lambda s, hit: httpx.Response(status) if (hit.kind, hit.n) == ("detail", 2) else None
    tid = start(world)
    assert engine.tick(world.conn)
    row = task(world, tid)
    assert row["status"] == "done" and (row["stats"]["details_ok"], row["stats"]["details_failed"]) == (4, 1)
    assert [h.n for h in site.of("detail")] == [1, 2, 3, 4, 5]   # not one detail was left unasked
    assert [j["incomplete_reason"] for j in shown(world) if j["missing"]] == ["职位已下架"]
    assert results.search_jobs(world.conn, "py", "杭州")["incomplete"] == {
        "count": 1, "reasons": [{"text": "职位已下架", "count": 1}]}
    assert health(world)["total_fail"] == 0 and health(world)["blocked_until"] == 0   # and the exit is not blamed
    gone = next(j["id"] for j in stored_jobs(world.conn, tid) if j["detail_error"])
    with pytest.raises(ValueError, match=rf"posting is gone \(HTTP {status}\)"):   # a job opened in the app says so too
        service.job_detail(world.conn, gone)


@pytest.mark.parametrize("status", [500, 502])
def test_l6_a_detail_that_fails_at_the_server_still_ends_the_pass_and_retries_the_task(world, status):
    """Only a posting that is gone is one job's problem: the other HTTP errors keep their handling."""
    prov, site = world.add(Shop())
    site.rule = lambda s, hit: httpx.Response(status) if (hit.kind, hit.n) == ("detail", 2) else None
    tid = start(world)
    assert engine.tick(world.conn)
    row = task(world, tid)
    assert (row["status"], row["attempts"]) == ("pending", 1) and row["last_error"].startswith("HTTPStatusError")
    assert [h.n for h in site.of("detail")] == [1, 2]


# ============================================================ L7: the exits
DEAD, LIVE = "http://dead.test:1", "http://live.test:1"


def landing(w, tid: int) -> str:
    """The exit task `tid` goes out through while both are usable."""
    return egress.lease(w.conn, "shop", str(tid))


def with_exits(w):
    prov, site = w.add(Shop(), None)
    site.rule = None
    egress.add(w.conn, DEAD)
    egress.add(w.conn, LIVE)
    site.dead = {DEAD}
    return prov, site


def exit_row(w, url: str) -> dict:
    return next(e for e in service.status(w.conn)["exits"] if e["id"] == url)


def tasks_on(w, exit_url: str, count: int, first: int) -> list[int]:
    """Start searches until `count` of them are bound for `exit_url` (the others go to the other exit); their ids."""
    mine, n = [], first
    while len(mine) < count:
        tid = start(w, keyword=f"kw{n}")
        mine += [tid] if landing(w, tid) == exit_url else []
        n += 1
    return mine


def test_l7_a_dead_exit_is_rested_the_other_serves_and_the_dead_one_is_tried_again_after_its_rest(world):
    prov, site = with_exits(world)
    ids = [start(world, keyword=f"kw{i}") for i in range(6)]
    assert {landing(world, t) for t in ids} == {DEAD, LIVE}   # both exits carry part of the load

    wait_until(world, lambda: all(task(world, t)["status"] == "done" for t in ids))
    on_dead = [h for h in site.hits if h.exit == DEAD]
    assert len(on_dead) == egress.FAIL_STREAK_LIMIT and all(h.status == 0 for h in on_dead)   # 3 refusals, then it rested
    assert all(h.status == 200 for h in site.hits if h.exit == LIVE) and any(h.exit == LIVE for h in site.hits)
    assert all(task(world, t)["attempts"] <= 1 for t in ids)   # nobody burned more than the one failed attempt
    dead, live = exit_row(world, DEAD), exit_row(world, LIVE)
    assert (dead["cooling_down"], dead["total_fail"], dead["total_ok"]) == (True, 3, 0)   # status shows both exits
    assert (live["cooling_down"], live["total_fail"]) == (False, 0) and live["total_ok"] >= 6
    assert dead["blocked_until"] == pytest.approx(world.clock.now + egress.FAIL_COOLDOWN, abs=60)
    # while it rests nothing is sent to it
    mine = tasks_on(world, LIVE, 4, 100)
    wait_until(world, lambda: all(task(world, t)["status"] == "done" for t in mine))
    assert len([h for h in site.hits if h.exit == DEAD]) == 3

    # after its rest the dead exit is tried again: three more refusals, and it rests again
    world.clock.advance(egress.FAIL_COOLDOWN + 1)
    assert exit_row(world, DEAD)["cooling_down"] is False
    mine = tasks_on(world, DEAD, 3, 200)
    wait_until(world, lambda: all(task(world, t)["status"] == "done" for t in mine))
    assert len([h for h in site.hits if h.exit == DEAD]) == 3 + 3
    assert exit_row(world, DEAD)["cooling_down"] is True

    # and once its network is back it takes the work again
    site.dead.clear()
    world.clock.advance(egress.FAIL_COOLDOWN + 1)
    mine = tasks_on(world, DEAD, 2, 300)
    wait_until(world, lambda: all(task(world, t)["status"] == "done" for t in mine))
    assert exit_row(world, DEAD)["total_ok"] >= 2 and health(world, DEAD)["fail_streak"] == 0


def test_l7_with_the_only_exit_dead_the_search_says_retrying_with_the_cause_and_recovers(world):
    prov, site = world.add(Shop())
    egress.add(world.conn, DEAD)
    site.dead = {DEAD}
    tid = start(world)
    wait_until(world, lambda: "cooling down" in (task(world, tid)["last_error"] or ""))   # the exit rests after 3 refusals
    assert len(site.hits) == egress.FAIL_STREAK_LIMIT
    row = task(world, tid)
    # F47: the cause stays in front; the rest is only what follows
    assert row["status"] == "pending" and row["last_error"].startswith("ConnectError: connection refused")
    assert row["last_error"].count(engine.REST_MARK) == 1 and row["last_error"].endswith("all egress for shop are cooling down")
    # F52: the search is not done and not a success: it retries, and the reason names the network
    found = service.search_status(world.conn, "py", "杭州")
    assert found["status"] == "retrying"
    assert (found["per_site"][0]["outcome"], found["per_site"][0]["reason"]) == ("retrying", "网络不通，稍后再试")
    assert found["per_site"][0]["retry_at"] == row["next_run_at"] > world.clock.now
    waited = asyncio.run(server.wait_for_search("py", "杭州", timeout=5))
    assert waited["status"] == "retrying" and "timed_out" not in waited   # it does not wait for a site that retries later
    seen = service.status(world.conn)
    assert [(e["id"], e["cooling_down"], e["total_fail"]) for e in seen["exits"]] == [(DEAD, True, 3)]
    assert seen["platforms"]["shop"]["last_error"]["kind"] == "network" and seen["platforms"]["shop"]["last_error"]["status"] == "retrying"
    assert seen["platforms"]["shop"]["cooling_down"][0]["exit"] == DEAD
    # the rest ends, the network is back: nothing needs the user
    rest_ends = health(world, DEAD)["blocked_until"]
    while world.clock.now + 61 < rest_ends:
        world.clock.advance(61)
        engine.tick(world.conn)
    assert len(site.hits) == egress.FAIL_STREAK_LIMIT   # nothing was sent while the only exit rested
    site.dead.clear()
    wait_until(world, lambda: task(world, tid)["status"] == "done")
    assert service.search_status(world.conn, "py", "杭州")["status"] == "done"
    assert service.search_status(world.conn, "py", "杭州")["per_site"][0]["outcome"] == "found"


# ============================================================ L8: a long run, compressed
def open_files() -> int | None:
    for path in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(path):
            return len(os.listdir(path))
    return None   # Windows: no cheap way, the thread and row checks still hold


STEP = 61   # seconds of the fake clock between two searches: a little over a minute, so that a minute-long rest is over
ROUNDS = 200


def test_l8_two_hundred_scheduled_searches_leave_nothing_behind_cookies_expire_and_a_cooldown_ends(world):
    alpha, a_site = world.add(variant(Shop, "alpha"))
    beta, b_site = world.add(variant(Shop, "beta"))
    member, m_site = world.add(variant(Member, "member"))
    for prov in (alpha, beta, member):
        pacer.set_frequency(world.conn, prov.name, interval=0, jitter=0, force=True)
    started = world.clock.now
    auth.save_cookies("member", {"s": "1"}, {"s": started + 120.5 * STEP})   # the session ends in the middle of the run
    state = SimpleNamespace(round=0)
    refused_window = range(50, 78)
    b_site.rule = lambda s, hit: refusal(403) if state.round in refused_window else None

    def running() -> int:
        return world.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]

    searches, warm = [], None
    for state.round in range(ROUNDS):
        world.clock.advance(STEP)
        made = service.search(world.conn, f"job{state.round}", "杭州", sites=["alpha", "beta", "member"], max_pages=3)
        searches.append(dict(zip(("alpha", "beta", "member"), made["task_ids"])))
        for _ in range(500):
            if not engine.tick(world.conn):
                break
        else:
            raise AssertionError("the queue never settled")
        assert running() == 0, f"a task was left running in round {state.round}"
        if state.round == 20:
            warm = SimpleNamespace(threads=threading.active_count(), files=open_files())
        if state.round == 79:
            newest = task(world, searches[79]["beta"])
            assert newest["status"] == "pending" and newest["last_error"]   # still resting: retrying, not lost
        if state.round == 85:
            assert task(world, searches[85]["beta"])["status"] == "done"   # the cooldown ended: the platform is back

    # nothing grew
    assert threading.active_count() <= warm.threads
    if warm.files is not None:
        assert open_files() <= warm.files + 2
    # beta: one refusal in the window, one cooldown, then every search that waited for it completed
    assert [h.status for h in b_site.hits].count(403) == 1
    assert health(world, "direct", "beta")["total_fail"] == 1
    assert all(task(world, ids["beta"])["status"] == "done" for ids in searches)
    assert max(task(world, ids["beta"])["attempts"] for ids in searches) <= 1
    # alpha never noticed
    assert all(task(world, ids["alpha"])["status"] == "done" for ids in searches) and len(a_site.of("list")) == 3 * ROUNDS
    # member: done while the session lived, waiting for a login after it ran out, and not a request signed out
    got = [task(world, ids["member"])["status"] for ids in searches]
    assert got.count("done") == 120 and got.count("waiting_login") == ROUNDS - 120
    assert got == ["done"] * 120 + ["waiting_login"] * (ROUNDS - 120)
    assert all(h.cookie for h in m_site.hits) and m_site.hits[-1].at < started + 121 * STEP
    assert auth.load_cookies("member") is None and auth.status("member")["state"] == "expired"
    assert service.search_status(world.conn, "job199", "杭州")["status"] == "waiting_login"
    # the user logs in again: every waiting task completes
    log_in(member)
    assert taskqueue.resume_login(world.conn, "member") == ROUNDS - 120
    for _ in range(500):
        if not engine.tick(world.conn):
            break
    assert all(task(world, ids["member"])["status"] == "done" for ids in searches)
    assert world.conn.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done')").fetchone()[0] == 0


def test_l8_the_workers_start_and_stop_cleanly_round_after_round(world, monkeypatch):
    monkeypatch.setattr(engine, "POLL_SECONDS", 0.005)
    monkeypatch.setattr(engine, "ERROR_BACKOFF", 0.005)
    for name in ("alpha", "beta"):
        prov, _ = world.add(variant(Shop, name))
        pacer.set_frequency(world.conn, name, interval=0, jitter=0, force=True)
    ours = ("djs-worker-alpha", "djs-worker-beta")
    workers = lambda: sorted(t.name for t in threading.enumerate() if t.name in ours)   # noqa: E731
    baseline = threading.active_count()
    engine._threads.clear()
    assert workers() == []
    try:
        for n in range(4):
            engine.start_worker()
            engine.start_worker()   # idempotent
            assert workers() == list(ours)   # one thread per platform, never more
            ids = service.search(world.conn, f"kw{n}", "杭州", sites=["alpha", "beta"], max_pages=3)["task_ids"]
            end = real_time.monotonic() + 15
            while real_time.monotonic() < end and not all(task(world, t)["status"] == "done" for t in ids):
                real_time.sleep(0.01)
            assert all(task(world, t)["status"] == "done" for t in ids)
            engine.stop_worker(5)
            assert workers() == [] and threading.active_count() <= baseline
            engine._threads.clear()
        assert world.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0] == 0
    finally:
        engine.stop_worker(5)
        engine._threads.clear()
