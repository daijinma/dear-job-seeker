"""The real page in headless Chromium, talking to the real Api over a shim of the pywebview bridge. Temp database,
fake platforms behind httpx.MockTransport, real per-platform workers: no network, no native window. Skipped when
Playwright's Chromium is not installed."""
from __future__ import annotations

import inspect
import itertools
import json
import re
import threading
import time
from collections import Counter
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from djs import db, egress, engine, providers, results, service, taskqueue, ui_api, ui_app
from djs.auth import LoginCancelled
from djs.providers.base import Blocked, Login, Provider, get_json, need

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect
expect.set_options(timeout=15000)

# pywebview runs every js_api call in its own thread, so a slow call never blocks the page: the shim does the same
# (a thread per call, the page polls for the answer) and lets a test hold a platform request open.
SHIM = """window.pywebview = {api: {
  call: async (route, payload) => {
    const ticket = await window.__start(route, payload);
    for (;;) {
      const done = await window.__take(ticket);
      if (done) return done.value;
      await new Promise((r) => setTimeout(r, 10));
    }
  },
}};"""
KEYWORD, CITY = "前端工程师", "杭州"
JOBS = ".job:not(.skeleton)"
STAMP = "20261011-0930"
SAVED = time.time() - 3 * 86400
NAMELESS = """() => [...document.querySelectorAll('button, a[href], input, select, textarea, [role=switch]')]
  .filter((el) => el.getClientRects().length)
  .filter((el) => !((el.getAttribute('aria-label') || '').trim() || (el.innerText || '').trim()
                    || (el.labels && el.labels[0] ? el.labels[0].innerText.trim() : '')))
  .map((el) => el.outerHTML.slice(0, 100))"""


# ---------------------------------------------------------------- fake platforms
class FakeSite(Provider):
    kind, auth = "keyword", "public"
    interval, jitter, min_interval = 0.0, 0.0, 0.0
    page_size, first_page, page_cap = 20, 1, 3
    detail_hop = False   # True: the list item has no description, a second request per job does

    def fetch(self, client, payload, page):
        r = client.get(f"https://fake.test/{self.name}/list", params={"q": payload["keyword"], "page": page})
        if r.status_code == 403:
            raise Blocked("HTTP 403")
        return need(get_json(r), "items")

    def job_id(self, job):
        return str(need(job, "id", typ=int))

    def meta(self, job, detail):
        return {"source_url": job.get("url") or f"https://fake.test/{self.name}/job/{job['id']}", "apply_url": None,
                "published_at": None, "published_at_kind": "unknown"}

    def row(self, job, detail):
        return {"title": job["title"], "company": job["company"], "city": job.get("city"),
                "salary_text": job.get("salary"), "description": job.get("jd") or (detail or {}).get("jd"),
                **{k: job[k] for k in ("district", "education", "experience", "benefits") if k in job}}

    def detail(self, client, job):
        if self.detail_hop:
            return get_json(client.get(f"https://fake.test/{self.name}/detail", params={"id": job["id"]}))


def site(name, label, **attrs):
    return type(name, (FakeSite,), {"name": name, "label": label, **attrs})()


def session(method, unlocks):
    return Login(url="https://fake.test/login", method=method, domain="fake.test", cookies_any=("s",), unlocks=unlocks)


def make_providers():
    return {p.name: p for p in (
        site("nc", "牛客网"), site("gp", "国聘", detail_hop=True), site("xh", "新华英才"),
        site("zl", "智联招聘", auth="optional", login=session("wechat", "能看到更多岗位")),   # works anonymously, over http
        site("zz", "中智招聘"),
        site("tc", "58同城", requires_city=True),
        site("yp", "鱼泡直聘", requires_env=("DJS_FAKE_KEY",)),
        site("bs", "BOSS 直聘", auth="optional", transports=("browser",), needs_headed=True, login=session("qr", "能看到薪资")),
        site("lg", "登录专用站", auth="login", login=session("password", "不登录无法搜索")))}


class FakeAuth:
    """A session per platform plus a login the test releases by hand (`release`): it succeeds, fails with `error`, or is
    cancelled the way djs.auth ends a login whose window was closed."""

    def __init__(self):
        self.sessions, self.error, self.checks, self.cancels = {}, None, [], 0   # sessions: platform -> logged_in | expired
        self.release, self.started, self.cancelled = threading.Event(), threading.Event(), threading.Event()

    def _state(self, p):
        return self.sessions.get(p, "not_logged_in")

    def load_cookies(self, p):
        return {"s": "1"} if self._state(p) == "logged_in" else None

    def invalidate(self, p):
        self.sessions[p] = "expired"

    def logged_in(self, p):
        return self._state(p) == "logged_in"

    def status(self, p):
        spec = providers.get(p).login
        return {"logged_in": self.logged_in(p), "state": self._state(p), "saved_at": SAVED if p in self.sessions else None,
                "expires_at": None, "method": spec.method, "unlocks": spec.unlocks}

    def check(self, p):
        self.checks.append(p)
        return self.status(p)

    def login(self, p, timeout=180):
        self.started.set()
        for _ in range(3000):   # up to 30 s
            if self.release.is_set() or self.cancelled.is_set():
                break
            time.sleep(0.01)
        if self.cancelled.is_set():
            self.cancelled.clear()
            raise LoginCancelled("已取消登录")
        if self.error:
            raise RuntimeError(self.error)
        self.sessions[p] = "logged_in"
        return self.status(p)

    def cancel_login(self, p):
        self.cancels += 1
        self.cancelled.set()
        return True

    def cancel_all(self, wait=10):
        self.cancelled.set()

    def pending_logins(self):
        return []

    def logout(self, p):
        self.sessions.pop(p, None)


def test_the_fake_auth_covers_everything_the_engine_and_service_ask_of_auth():
    """The page tests stand in for djs.auth; when the engine or service starts using another function of it, fail here
    with its name instead of with a login that never completes."""
    asked = set(re.findall(r"\bauth\.(\w+)", inspect.getsource(engine) + inspect.getsource(service)))
    assert asked and asked <= set(dir(FakeAuth)), asked - set(dir(FakeAuth))


def jobs(name, n, start=1, **extra):
    """Whole jobs by default: a title, a company, a city, a salary and (unless `jd=None`) a description."""
    cities = ["杭州", "杭州", "上海", "杭州·滨江区", "杭州·余杭区"]
    pay = ["15-25K", "20-35K", "22-38K", "25-40K", "200元/天", "18-30K·14薪"]
    return [{"id": start + i, "title": f"{name}前端开发工程师 {start + i}", "company": f"示例科技有限公司 {chr(65 + i % 26)}",
             "city": cities[i % len(cities)], "salary": pay[i % len(pay)], "jd": "岗位职责：负责前端页面开发", **extra} for i in range(n)]


def boot(mp, home):
    """Everything the page talks to: the temp data folder, fake platforms, their fake network and the workers."""
    mp.setenv("DJS_HOME", str(home))
    mp.delenv("DJS_FAKE_KEY", raising=False)
    mp.delenv("DJS_FAKE_KEY2", raising=False)
    provs, auth = make_providers(), FakeAuth()
    mp.setattr(providers, "get", lambda n: provs[n] if n in provs else (_ for _ in ()).throw(KeyError(n)))
    mp.setattr(providers, "names", lambda: list(provs))
    for mod in (engine, service):
        mp.setattr(mod, "auth", auth)
    mp.setattr(ui_api, "downloads", lambda: home / "Downloads")
    mp.setattr(ui_api, "stamp", lambda: STAMP)
    app = SimpleNamespace(provs=provs, auth=auth, home=home, downloads=home / "Downloads", mp=mp, gates={}, blocked=set(),
                          broken=set(), requests=[], calls=Counter(), bridge_down=False, js_errors=[], detail_gates={},
                          data={"nc": {KEYWORD: jobs("牛客", 25, jd="1. 负责前端开发\n2. 熟悉 Vue / React")},
                                "gp": {KEYWORD: jobs("国聘", 60, 100, jd=None)}, "xh": {},
                                "zl": {KEYWORD: jobs("智联", 40, 300)}, "zz": {KEYWORD: jobs("中智", 3, 500)}})

    def handler(request):
        name = request.url.path.split("/")[1]
        app.requests.append(request.url.path)
        if name in app.gates:
            app.gates[name].wait(20)   # a platform the test holds back
        if request.url.path.endswith("/detail") and name in app.detail_gates:
            app.detail_gates[name].wait(20)   # only its detail pages
        if name in app.blocked:
            return httpx.Response(403)
        if request.url.path.endswith("/detail"):
            if name in app.broken:
                return httpx.Response(200, text="<html>not json</html>")   # a page that changed: the detail cannot be read
            return httpx.Response(200, json={"jd": f"岗位职责：\n1. 负责前端页面开发（编号 {request.url.params['id']}）\n2. 与设计、后端协作\n\n任职要求：\n熟悉 Vue / React"})
        items = app.data.get(name, {}).get(request.url.params["q"], [])
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"items": items[(page - 1) * 20: page * 20]})

    real = egress.make_client
    mp.setattr(egress, "make_client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    engine.start_worker()
    return app


def shutdown(app):
    for gate in (*app.gates.values(), *app.detail_gates.values()):
        gate.set()
    app.auth.release.set()
    engine.stop_worker(10)
    ui_api.login_state.clear()


def open_page(browser, app, size=(1280, 860), hash="#/search"):
    page = browser.new_context(viewport={"width": size[0], "height": size[1]}).new_page()
    page.set_default_timeout(15000)
    api, answers, tickets = ui_app.Api(), {}, itertools.count(1)

    def start(route, payload):
        if app.bridge_down:
            raise RuntimeError("the bridge is down")
        app.calls[route] += 1
        ticket = next(tickets)
        threading.Thread(target=lambda: answers.__setitem__(ticket, {"value": api.call(route, payload)}), daemon=True).start()
        return ticket

    page.expose_function("__start", start)
    page.expose_function("__take", lambda ticket: answers.pop(ticket, None))
    page.add_init_script(SHIM)
    page.on("pageerror", lambda e: app.js_errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and app.js_errors.append(m.text))
    page.route("http://djs.test/**", lambda route: route.fulfill(
        body=ui_app.page() if route.request.url == "http://djs.test/" else "", status=200 if route.request.url == "http://djs.test/" else 204,
        content_type="text/html; charset=utf-8"))
    page.goto("http://djs.test/" + hash)
    page.evaluate("POLL_MS = 300")   # a running search is read again every 0.3 s instead of every 3 s
    return page


# ---------------------------------------------------------------- fixtures and helpers
@pytest.fixture(scope="module")
def browser():
    """Playwright's sync API keeps an asyncio loop marked as running on this thread while it is started, which makes
    every asyncio.run() of a later test module fail: so it lives for this module only and is stopped before the next."""
    pw = sync_api.sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch()
        except Exception as e:  # the browser binary is not installed
            pytest.skip(f"Chromium is not available: {type(e).__name__}")
        yield b
        b.close()
    finally:
        pw.stop()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = boot(monkeypatch, tmp_path)
    yield a
    shutdown(a)
    assert a.js_errors == []   # no test may leave an uncaught error or a console error behind


@pytest.fixture
def page(browser, app):
    p = open_page(browser, app)
    yield p
    p.context.close()


def until(page, condition, seconds=10):
    for _ in range(int(seconds * 20)):
        if condition():
            return
        page.wait_for_timeout(50)
    raise AssertionError("condition not met in time")


def start_search(page, keyword=KEYWORD, city=CITY):
    page.get_by_label("岗位或关键词", exact=True).fill(keyword)
    page.get_by_label("城市", exact=True).fill(city)
    page.get_by_label("城市", exact=True).press("Enter")


def newest_card(page):
    return page.locator(".search-card").first


def search_status(keyword=KEYWORD, city=CITY) -> str:
    """How the service rolls the search up: running | retrying | waiting_login | done."""
    conn = db.connect()
    try:
        return service.search_status(conn, keyword, city)["status"]
    finally:
        conn.close()


def finish_search(page, **kw):
    start_search(page, **kw)
    expect(newest_card(page).locator(".pill")).to_have_text("已完成")
    return newest_card(page)


def open_results(page, **kw):
    finish_search(page, **kw)
    page.get_by_role("link", name="我的结果").click()


def show_incomplete(page, on=True):
    switch = page.get_by_role("switch", name="显示信息不全的岗位")
    if (switch.get_attribute("aria-checked") == "true") != on:
        switch.click()
    expect(switch).to_have_attribute("aria-checked", "true" if on else "false")


def hold(app, *names):
    for name in names:
        app.gates[name] = threading.Event()


def release(app, *names):
    for name in names:
        app.gates[name].set()


def exported(page, app):
    """The file the page has just exported (waits until it is written) and its lines as a spreadsheet reads them."""
    until(page, lambda: any(f.stat().st_size for f in app.downloads.glob("*.csv")))
    path, = app.downloads.glob("*.csv")
    return path, path.read_bytes().decode("utf-8").split("\r\n")   # bytes: reading as text would turn \r\n into \n


def only_text(locator):
    return [t.strip() for t in locator.all_inner_texts()]


def nav_logins(page):
    return page.locator('nav a[data-tab="logins"]')


def go_logins(page):
    nav_logins(page).click()
    expect(page.get_by_role("heading", name="平台登录", level=1)).to_be_visible()


def picker_summary(page):
    return page.get_by_role("button", name=re.compile(r"已选 \d+ 个网站 · 修改"))


# ---------------------------------------------------------------- 查找
def test_first_run_search_page(page, app):
    expect(page.get_by_role("heading", name="想找什么工作？")).to_be_visible()
    expect(page.get_by_label("岗位或关键词", exact=True)).to_be_focused()
    expect(page.locator(".first-run")).to_contain_text("还没有查找记录")
    common = page.locator("#sites .chips .chip")
    expect(common).to_have_text(["牛客网", "国聘", "新华英才", "智联招聘", "中智招聘"])   # no key, no city, no window: also an optional-login one
    for chip in common.all():
        expect(chip).to_have_attribute("aria-pressed", "true")   # all selected by default
    expect(page.get_by_role("button", name="更多网站")).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#more")).to_be_hidden()
    expect(picker_summary(page)).to_have_count(0)   # no search yet: the picker is shown in full
    page.get_by_role("button", name="开始查找").click()
    expect(page.locator("#msg")).to_have_text("请先填写岗位关键词")
    assert taskqueue.list_(db.connect()) == []
    page.get_by_role("button", name="Python 开发").click()   # an example chip fills the keyword
    expect(page.get_by_label("岗位或关键词", exact=True)).to_have_value("Python 开发")
    page.get_by_role("button", name="牛客网").click()
    page.get_by_role("button", name="国聘").click()
    page.get_by_role("button", name="新华英才").click()
    page.get_by_role("button", name="智联招聘").click()
    page.get_by_role("button", name="中智招聘").click()
    page.get_by_role("button", name="开始查找").click()
    expect(page.locator("#msg")).to_have_text("58同城需要先填写城市")   # the site that needs a city is ticked too: it alone cannot search the country
    page.get_by_role("button", name="更多网站").click()
    page.locator(".item", has_text="58同城").get_by_role("button", name="58同城").click()
    page.get_by_role("button", name="开始查找").click()
    expect(page.locator("#msg")).to_have_text("请至少选择一个网站")
    assert page.evaluate(NAMELESS) == []


def test_more_sites_are_grouped_explained_and_gated(page, app):
    more = page.get_by_role("button", name="更多网站")
    more.click()
    expect(more).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#more h3")).to_have_text(["要先登录", "会弹出浏览器窗口", "需要填城市", "需要先配置密钥"])
    boss = page.locator(".item", has_text="BOSS 直聘")   # opens a browser window: not among the common sites
    expect(boss).to_contain_text("会弹出一个浏览器窗口；登录后：能看到薪资")
    expect(boss.get_by_role("button", name="去登录")).to_be_visible()   # inside the item, not a loose link next to it
    expect(boss.get_by_role("button", name="BOSS 直聘")).to_have_attribute("aria-pressed", "false")   # works anonymously
    only = page.locator(".item", has_text="登录专用站")
    expect(only).to_have_class(re.compile(r"\boff\b"))
    expect(only).to_contain_text("要先登录才能使用")
    expect(only.get_by_role("button", name="去登录")).to_be_visible()
    expect(only.locator("button[aria-pressed]")).to_have_count(0)   # cannot be ticked before the login
    keyed = page.locator(".item", has_text="鱼泡直聘")
    expect(keyed).to_contain_text("还没配置密钥")
    expect(keyed.locator("button[aria-pressed]")).to_have_count(0)
    assert page.evaluate(NAMELESS) == []

    city_site = page.locator(".item", has_text="58同城")   # needs a city: ticked for you, searched whenever a city is typed
    expect(city_site.get_by_role("button", name="58同城")).to_have_attribute("aria-pressed", "true")
    page.get_by_label("岗位或关键词", exact=True).fill(KEYWORD)
    page.get_by_label("城市", exact=True).fill(CITY)
    page.get_by_role("button", name="开始查找").click()
    expect(page.locator(".search-card")).to_have_count(1)
    assert {t["provider"] for t in taskqueue.list_(db.connect())} == {"nc", "gp", "xh", "zl", "zz", "tc"}

    picker_summary(page).click()   # the picker is one line now; open it again
    keyed.get_by_role("button", name="如何配置").click()   # leads to the keys help in Settings
    expect(page.locator("#keys")).to_be_visible()
    expect(page.locator("#keys")).to_contain_text("DJS_FAKE_KEY")


def groups_of_more(page):
    """[[title, [site names]], ...] of the opened 更多网站."""
    return page.evaluate("""() => [...document.querySelectorAll('#more .group')]
      .map((g) => [g.querySelector('h3').innerText, [...g.querySelectorAll('.item-text b')].map((b) => b.innerText)])""")


def test_the_common_sites_work_as_they_are_and_every_other_site_names_what_it_needs(browser, app):
    app.provs["jb"] = site("jb", "前程无忧", auth="optional", requires_env=("DJS_FAKE_KEY2",), login=session("password", "登录后改用电脑版搜索"))
    engine.start_worker()   # a platform of its own, so it has a worker of its own
    page = open_page(browser, app)
    chips, hints = page.locator("#sites .chips .chip"), page.locator("#sites .chips .pick")
    expect(chips).to_have_text(["牛客网", "国聘", "新华英才", "智联招聘", "中智招聘"])   # public or optional, nothing to configure, no city, no window
    for chip in chips.all():
        expect(chip).to_have_attribute("aria-pressed", "true")   # all of them are selected for the first search
    expect(hints).to_have_count(1)   # only the optional site has something to gain from a login
    expect(hints.get_by_role("button")).to_have_text("智联招聘")
    more = hints.get_by_role("link", name="登录后更全")
    expect(more).to_have_text("登录后更全")
    expect(more).to_have_attribute("href", "#/logins")

    page.get_by_role("button", name="更多网站").click()   # everything else says what it needs, in its own group
    assert groups_of_more(page) == [["要先登录", ["登录专用站"]], ["会弹出浏览器窗口", ["BOSS 直聘"]], ["需要填城市", ["58同城"]],
                                    ["需要先配置密钥", ["鱼泡直聘", "前程无忧"]]]   # keys missing beats optional: not common until configured
    boss = page.locator(".item", has_text="BOSS 直聘")
    expect(boss).to_contain_text("会弹出一个浏览器窗口；登录后：能看到薪资")
    expect(boss.get_by_role("button", name="BOSS 直聘")).to_have_attribute("aria-pressed", "false")   # never ticked for you
    expect(page.locator(".item", has_text="登录专用站")).to_contain_text("要先登录才能使用")
    expect(page.locator(".item", has_text="58同城")).to_contain_text("要先在上面填好城市")
    expect(page.locator(".item", has_text="前程无忧").get_by_role("button")).to_have_text("如何配置")
    more.click()   # the hint leads to the page where the logins are
    expect(page).to_have_url(re.compile(r"#/logins$"))

    app.mp.setenv("DJS_FAKE_KEY2", "x")   # the key is there now: the optional site works as it is, and joins the common ones
    app.auth.sessions.update({"zl": "logged_in", "bs": "logged_in", "lg": "logged_in"})
    page.get_by_role("link", name="查找", exact=True).click()
    page.reload()
    page.evaluate("POLL_MS = 300")
    expect(chips).to_have_text(["牛客网", "国聘", "新华英才", "智联招聘", "中智招聘", "前程无忧"])
    for chip in chips.all():
        expect(chip).to_have_attribute("aria-pressed", "true")
    expect(hints).to_have_count(1)   # 智联招聘 is logged in now: no hint; 前程无忧 is not: it has one
    expect(hints.get_by_role("button")).to_have_text("前程无忧")
    page.get_by_role("button", name="更多网站").click()
    assert groups_of_more(page) == [["要先登录", ["登录专用站"]], ["会弹出浏览器窗口", ["BOSS 直聘"]], ["需要填城市", ["58同城"]],
                                    ["需要先配置密钥", ["鱼泡直聘"]]]
    expect(page.locator(".item", has_text="BOSS 直聘")).to_contain_text("会弹出一个浏览器窗口")
    for name in ("BOSS 直聘", "登录专用站"):   # logged in: they can be ticked now, but they still are not common
        row = page.locator(".item", has_text=name)
        expect(row.locator(".pill")).to_have_text("已登录")
        expect(row.get_by_role("button", name=name)).to_have_attribute("aria-pressed", "false")
    start_search(page)   # the first search uses the common sites, and the one that only needs the city that was typed
    expect(page.locator(".search-card")).to_have_count(1)
    assert {t["provider"] for t in taskqueue.list_(db.connect())} == {"nc", "gp", "xh", "zl", "zz", "tc", "jb"}
    assert page.evaluate(NAMELESS) == []
    page.context.close()


def sites_of(card):
    """The names a search card lists, in its order (a label is the one span of a site without a class)."""
    return card.locator(".dot > span:not([class])")


def test_a_site_that_needs_a_city_is_ticked_and_left_out_quietly_while_no_city_is_typed(page, app):
    city_site = page.locator(".item", has_text="58同城")
    page.get_by_role("button", name="更多网站").click()
    expect(city_site.get_by_role("button", name="58同城")).to_have_attribute("aria-pressed", "true")   # ticked, like the common sites
    page.get_by_label("岗位或关键词", exact=True).fill(KEYWORD)
    for name in ("牛客网", "国聘", "新华英才", "智联招聘", "中智招聘"):
        page.get_by_role("button", name=name).click()
    page.get_by_role("button", name="开始查找").click()   # only the city site is left, and there is no city: that is the one message
    expect(page.locator("#msg")).to_have_text("58同城需要先填写城市")
    assert taskqueue.list_(db.connect()) == []
    page.get_by_role("button", name="牛客网").click()
    page.get_by_role("button", name="开始查找").click()   # another site is picked: no city, no 58同城, no message
    expect(page.locator("#msg")).to_be_empty()
    expect(page.locator(".search-card")).to_have_count(1)
    expect(sites_of(newest_card(page))).to_have_text(["牛客网"])
    assert [t["provider"] for t in taskqueue.list_(db.connect())] == ["nc"]

    page.get_by_label("城市", exact=True).fill(CITY)
    page.get_by_role("button", name="开始查找").click()   # with a city the same picks include it
    expect(page.locator("#newest h3")).to_have_text("前端工程师 · 杭州")
    expect(sites_of(newest_card(page))).to_have_text(["牛客网", "58同城"])
    assert sorted((t["provider"], t["payload"].get("city", "")) for t in taskqueue.list_(db.connect())) == [("nc", ""), ("nc", CITY), ("tc", CITY)]
    expect(page.locator("#msg")).to_be_empty()
    assert page.evaluate(NAMELESS) == []


def test_the_sites_a_search_had_to_leave_out_are_named_once_in_plain_chinese(page, app):
    page.evaluate("""() => { window.said = []; const say = toast; toast = (text, ...rest) => { said.push(text); return say(text, ...rest); }; }""")
    app.provs["tc"].city_filter = app.provs["zz"].city_filter = "server"   # these two have no code for any city
    start_search(page)
    expect(page.locator("#toast")).to_have_text("中智招聘、58同城不支持“杭州”，这次没有找它们")   # one message, the sites grouped by why
    expect(sites_of(newest_card(page))).to_have_text(["牛客网", "国聘", "新华英才", "智联招聘"])   # the card lists what was searched
    expect(newest_card(page).locator(".pill")).to_have_text("已完成")
    app.provs["zz"].city_filter = "none"
    start_search(page, keyword="数据分析")
    expect(page.locator("#toast")).to_have_text("58同城不支持“杭州”，这次没有找它")
    expect(sites_of(newest_card(page))).to_have_text(["牛客网", "国聘", "新华英才", "智联招聘", "中智招聘"])
    start_search(page, keyword="运营", city="")   # nothing is left out: nothing is said
    expect(newest_card(page).locator("h3")).to_have_text("运营 · 全国")
    expect(newest_card(page).locator(".pill")).to_have_text("已完成")
    assert page.evaluate("said") == ["中智招聘、58同城不支持“杭州”，这次没有找它们", "58同城不支持“杭州”，这次没有找它"]


def test_the_note_about_left_out_sites_groups_them_by_the_reason(page, app):
    note = lambda skipped: page.evaluate("skipped => skippedNote(skipped)", skipped)   # noqa: E731
    got = note([{"label": "58同城", "reason": "不支持“三亚”"}, {"label": "赶集网", "reason": "不支持“三亚”"},
                {"label": "鱼泡直聘", "reason": "还没配置密钥"}])
    assert got == "58同城、赶集网不支持“三亚”；鱼泡直聘还没配置密钥，这次没有找它们"
    assert note([{"label": "BOSS 直聘", "reason": "需要先安装浏览器组件"}]) == "BOSS 直聘需要先安装浏览器组件，这次没有找它"


def test_search_shows_progress_then_the_finished_card(page, app):
    hold(app, "zl", "zz")
    start_search(page)
    card = newest_card(page)
    expect(card.locator("h3")).to_have_text("前端工程师 · 杭州")   # on screen the moment the search starts
    expect(card.locator(".pill")).to_have_text("查找中")
    expect(card.locator(".dot.ok")).to_have_count(2)
    expect(card.locator(".dot.run")).to_have_count(2)
    expect(card.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "4")   # nc, gp, xh and 58同城 (the city was typed)
    expect(card.get_by_role("progressbar")).to_have_attribute("aria-valuemax", "6")
    expect(card).to_contain_text("已完成 4 / 6 个网站 · 已找到 85 个岗位")
    nothing = card.locator(".dot", has_text="新华英才")   # zero results are a plain fact, never an alarm
    expect(nothing).to_contain_text("没有找到")
    expect(nothing).not_to_have_class(re.compile(r"warn|bad"))
    assert all(t["payload"]["detail"] == "auto" for t in taskqueue.list_(db.connect()))   # what is missing is completed
    release(app, "zl", "zz")
    expect(card.locator(".pill")).to_have_text("已完成")
    expect(card).to_contain_text("已完成 6 / 6 个网站 · 已找到 128 个岗位")
    expect(card.locator(".dot.run")).to_have_count(0)
    assert sum(p.endswith("/detail") for p in app.requests) == 60   # only the platform whose list lacks the JD was asked for details
    page.get_by_role("link", name="查看结果：前端工程师 · 杭州").click()
    expect(page.locator("#found")).to_have_text("找到 128 个岗位")
    expect(page.locator("#what")).to_have_text("前端工程师 · 杭州")


def test_the_page_polls_only_while_something_runs(page, app):
    hold(app, "zl")
    start_search(page)
    expect(newest_card(page).locator(".pill")).to_have_text("查找中")
    before = app.calls["GET /api/searches"]
    until(page, lambda: app.calls["GET /api/searches"] >= before + 3)   # asked again and again while it runs
    release(app, "zl")
    expect(newest_card(page).locator(".pill")).to_have_text("已完成")
    page.wait_for_timeout(500)
    settled = app.calls["GET /api/searches"]
    page.wait_for_timeout(1200)
    assert app.calls["GET /api/searches"] == settled   # nothing runs: nothing is asked
    page.get_by_role("link", name="设置").click()
    expect(page.get_by_role("heading", name="设置")).to_be_visible()
    assert app.calls["GET /api/searches"] == settled


def test_a_site_that_waits_out_its_cooldown_does_not_keep_the_search_running(page, app):
    """What a real client met: one site's exits rested for half an hour, and its search card said 查找中 all that time."""
    conn = db.connect()
    egress.report(conn, "direct", "zz", ok=False, blocked=True)   # every exit of 中智招聘 rests: its task is released, not run
    conn.close()
    card = finish_search(page)   # 已完成 although the site has not found anything yet
    assert search_status() == "retrying"   # what the service says: nothing works, one site owes a retry. The page: a finished search
    expect(card.locator(".bar.busy")).to_have_count(0)
    expect(card).to_contain_text("已完成 5 / 6 个网站 · 已找到 125 个岗位")
    waiting = card.locator(".dot", has_text="中智招聘")
    expect(waiting.locator(".spin")).to_have_count(0)
    expect(card.locator(".dot.run")).to_have_count(0)
    expect(waiting.locator("svg.ic path")).to_have_attribute("d", "M12 7v5l3 2")   # the clock: it waits
    expect(card.locator(".note.warn")).to_have_text("中智招聘 · 这个网站刚才连续出错，正在歇一会儿，稍后会自动重试")
    settled = app.calls["GET /api/searches"]
    page.wait_for_timeout(1200)
    assert app.calls["GET /api/searches"] == settled   # nothing works: nothing is asked
    page.get_by_role("link", name="查看结果：前端工程师 · 杭州").click()
    expect(page.locator("#found")).to_have_text("找到 125 个岗位")
    expect(page.locator(".notice")).to_have_count(0)   # no 还在查找中 banner, no spinner
    expect(page.locator("#notice .note.warn")).to_have_text("中智招聘 · 这个网站刚才连续出错，正在歇一会儿，稍后会自动重试")


def test_a_search_whose_every_site_waits_for_its_next_try_says_so(page, app):
    """Nothing found yet and every site owes a retry: the card says 稍后重试, not 已完成 with nothing in it."""
    conn = db.connect()
    for name in providers.names():
        egress.report(conn, "direct", name, ok=False, blocked=True)   # every exit of every site rests
    conn.close()
    start_search(page)
    expect(newest_card(page).locator(".pill")).to_have_text("稍后重试")
    assert search_status() == "retrying"   # not "done": the card says 稍后重试 and, like a finished search, is not polled
    settled = app.calls["GET /api/searches"]
    page.wait_for_timeout(1200)
    assert app.calls["GET /api/searches"] == settled


def test_a_retrying_site_says_later_once(page, app):
    """No network: "网络不通，稍后再试，稍后会自动重试" said "later" twice."""
    note = page.evaluate("siteNote({provider: 'zz', label: '中智招聘', outcome: 'retrying', reason: '网络不通，稍后再试'})")
    assert note["text"] == "网络不通，稍后会自动重试"


def test_typed_text_and_chosen_sites_survive_a_tab_change(page, app):
    page.get_by_label("岗位或关键词", exact=True).fill("数据分析")
    page.get_by_label("城市", exact=True).fill("上海")
    page.get_by_role("button", name="国聘").click()
    page.get_by_role("link", name="设置").click()
    expect(page.get_by_role("heading", name="设置")).to_be_visible()
    page.get_by_role("link", name="查找", exact=True).click()
    expect(page.get_by_label("岗位或关键词", exact=True)).to_have_value("数据分析")
    expect(page.get_by_label("城市", exact=True)).to_have_value("上海")
    expect(page.get_by_role("button", name="国聘")).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_role("button", name="牛客网")).to_have_attribute("aria-pressed", "true")


# ---------------------------------------------------------------- the page plan of 查找
def test_the_newest_search_sits_under_the_search_box_in_view_and_the_top_bar_stays(browser, app):
    for i in range(7):   # enough earlier searches that the page scrolls
        service.crawl_start(db.connect(), "nc", keyword=f"更早的查找 {i}")
    page = open_page(browser, app)
    start_search(page)
    card = newest_card(page)
    expect(card).to_be_visible()
    expect(card.locator("h3")).to_have_text("前端工程师 · 杭州")   # the newest, above the earlier ones
    assert page.locator("#newest .search-card").count() == 1 and page.locator("#recent .search-card").count() == 5   # the page reads the 6 newest
    box, bar = card.bounding_box(), page.locator("#kw").bounding_box()
    assert box["y"] > bar["y"] and box["y"] + box["height"] <= 860   # right under the search box, whole, without scrolling
    assert page.evaluate("scrollY") == 0
    page.evaluate("scrollTo(0, document.body.scrollHeight)")
    assert page.evaluate("scrollY") > 50   # the page is long ...
    assert page.locator(".top").bounding_box()["y"] == 0   # ... and the top bar stays where it is
    expect(nav_logins(page)).to_be_in_viewport()
    page.context.close()


def test_the_site_picker_is_one_line_once_a_search_exists_and_opens_on_a_click(page, app):
    expect(page.locator("#sites .chips")).to_be_visible()   # first time: all of it
    finish_search(page)
    summary = picker_summary(page)
    expect(summary).to_have_text("已选 6 个网站 · 修改")   # the five common sites and the one that needs a city
    expect(summary).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#sites .chips")).to_be_hidden()
    summary.click()
    expect(summary).to_have_attribute("aria-expanded", "true")
    page.get_by_role("button", name="国聘").click()
    expect(summary).to_have_text("已选 5 个网站 · 修改")   # the line follows the picks
    page.get_by_role("link", name="设置").click()   # it is remembered across pages, like the picks
    page.get_by_role("link", name="查找", exact=True).click()
    expect(picker_summary(page)).to_have_text("已选 5 个网站 · 修改")
    assert page.evaluate(NAMELESS) == []


def test_the_recent_searches_below_list_the_older_ones_only(page, app):
    expect(page.locator(".first-run")).to_be_visible()
    finish_search(page)
    expect(page.locator("#recent")).to_be_empty()   # one search: it is the card under the box, nothing else to list
    finish_search(page, keyword="数据分析")
    expect(page.locator("#newest h3")).to_have_text("数据分析 · 杭州")
    expect(page.locator("#recent h2")).to_have_text("更早的查找")
    expect(page.locator("#recent .search-card h3")).to_have_text(["前端工程师 · 杭州"])


# ---------------------------------------------------------------- 我的结果
def test_results_filter_sort_load_more_and_export(page, app):
    finish_search(page)
    page.get_by_role("link", name="我的结果").click()
    expect(page.locator(JOBS)).to_have_count(30)
    expect(page.locator("#more")).to_contain_text("已显示 30 / 128 个")
    chips = page.locator("#filters .chip")
    expect(chips).to_have_text(["全部 128", "牛客网 25", "国聘 60", "智联招聘 40", "中智招聘 3"])   # no chip for the site with 0 jobs
    expect(page.locator("#incomplete")).to_be_empty()   # every job is whole: nothing to say

    page.locator("#filters").get_by_role("button", name="国聘").click()
    expect(page.locator("#filters").get_by_role("button", name="国聘")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#more")).to_contain_text("已显示 30 / 60 个")
    expect(page.locator(".job .tags .pill:not(.gray)")).to_have_text(["国聘"] * 30)

    page.get_by_role("button", name="导出表格").click()   # every job of the filter, not only the 30 on screen
    expect(page.locator("#toast")).to_contain_text("已导出到「下载」文件夹")
    csv_file, lines = exported(page, app)
    assert csv_file.name == f"找岗位-前端工程师-杭州-{STAMP}.csv"
    assert lines[0].startswith("\ufeff")   # the BOM a spreadsheet needs to read Chinese
    assert lines[0] == ('\ufeff"岗位","公司","薪资","城市","网站","链接","区","学历","经验","福利","最低薪资","最高薪资","薪资单位","发薪月数","缺少的信息"')
    assert len(lines) == 1 + 60 and all(line.startswith('"国聘前端开发工程师 ') and ',"国聘",' in line for line in lines[1:])
    assert {line.split(",")[5] for line in lines[1:]} == {f'"https://fake.test/gp/job/{i}"' for i in range(100, 160)}
    by_title = {line.split(",")[0]: line for line in lines[1:]}   # the salary as words, then the figures behind it
    assert by_title['"国聘前端开发工程师 100"'] == ('"国聘前端开发工程师 100","示例科技有限公司 A","1.5-2.5万/月","杭州","国聘",'
                                             '"https://fake.test/gp/job/100","","","","","15000","25000","元/月","",""')
    assert by_title['"国聘前端开发工程师 105"'] == ('"国聘前端开发工程师 105","示例科技有限公司 F","1.8-3万/月 · 14薪","杭州","国聘",'
                                             '"https://fake.test/gp/job/105","","","","","18000","30000","元/月","14",""')
    assert by_title['"国聘前端开发工程师 104"'].split(",")[2] == '"200元/天"' and by_title['"国聘前端开发工程师 104"'].split(",")[12] == '"元/天"'

    page.get_by_role("button", name="加载更多").click()
    expect(page.locator(JOBS)).to_have_count(60)
    expect(page.get_by_role("button", name="加载更多")).to_have_count(0)
    expect(page.locator("#more")).to_contain_text("已显示 60 / 60 个")

    page.get_by_role("button", name="公司", exact=True).click()
    expect(page.locator(JOBS).first.locator(".sub")).to_have_text("示例科技有限公司 A")
    page.locator("#filters").get_by_role("button", name="全部").click()
    csv_file.unlink()
    page.get_by_role("button", name="导出表格").click()
    assert len(exported(page, app)[1]) == 1 + 128


def test_the_default_order_gives_each_site_a_turn_and_the_salary_sort_puts_the_best_paid_first(page, app):
    mixed = lambda name, pays: [{"id": 800 + i + (50 if name == "智联" else 0), "title": f"{name} {i}", "company": f"{name}公司 {i}",
                                 "city": "杭州", "salary": p, "jd": "说明"} for i, p in enumerate(pays)]
    app.data["nc"]["交错"] = mixed("牛客", ["10-15K", "20-30K", "8-12K"])
    app.data["zl"]["交错"] = mixed("智联", ["300元/天", "25-40K", "15-20K"])
    open_results(page, keyword="交错")
    sites = page.locator(".job .tags .pill:not(.gray)")
    expect(page.locator("#tools .chip")).to_have_text(["综合", "薪资从高到低", "公司"])
    expect(page.get_by_role("button", name="综合")).to_have_attribute("aria-pressed", "true")
    expect(sites).to_have_text(["牛客网", "智联招聘"] * 3)   # the sites take turns instead of one of them taking the top

    page.get_by_role("button", name="薪资从高到低").click()
    expect(page.locator(".job .salary")).to_have_text(["2.5-4万/月", "2-3万/月", "1.5-2万/月", "1-1.5万/月", "0.8-1.2万/月", "300元/天"])   # a day's pay counts as 21.75 days
    page.get_by_role("button", name="公司", exact=True).click()
    expect(page.locator(JOBS).first.locator(".sub")).to_have_text("智联公司 0")
    page.get_by_role("button", name="综合").click()
    expect(sites).to_have_text(["牛客网", "智联招聘"] * 3)


def test_the_default_order_puts_the_titles_holding_the_keywords_first(page, app):
    pick = lambda i, title: {"id": 3000 + i, "title": title, "company": f"示例{i}有限公司", "city": "杭州", "salary": "15-25K", "jd": "说明"}   # noqa: E731
    app.data["nc"]["Python 后端"] = [pick(0, "风电结构仿真分析工程师"), pick(1, "Python后端开发"), pick(2, "后端运维")]
    app.data["zl"]["Python 后端"] = [pick(3, "软件测试工程师"), pick(4, "后端 python 工程师")]
    open_results(page, keyword="Python 后端")
    expect(page.locator(JOBS)).to_have_count(5)
    titles = only_text(page.locator(JOBS).locator("h3"))
    # both words first (the sites take turns), then one word, then none: the newest card of a site is not the first any more
    assert titles == ["Python后端开发", "后端 python 工程师", "后端运维", "风电结构仿真分析工程师", "软件测试工程师"]
    page.get_by_role("button", name="公司", exact=True).click()   # the other orders do not read the keyword
    expect(page.locator(JOBS).first.locator(".sub")).to_have_text("示例0有限公司")
    page.get_by_role("button", name="综合").click()
    expect(page.locator(JOBS).first.locator("h3")).to_have_text("Python后端开发")


@pytest.mark.parametrize("job, label", [
    ({"salary_min": 15000, "salary_max": 25000, "salary_unit": "元/月", "salary_months": 14}, "1.5-2.5万/月 · 14薪"),
    ({"salary_min": 15000, "salary_max": 25000, "salary_unit": "元/月", "salary_months": None}, "1.5-2.5万/月"),
    ({"salary_min": 8000, "salary_max": 12000, "salary_unit": "元/月"}, "0.8-1.2万/月"),
    ({"salary_min": 12500, "salary_max": 20000, "salary_unit": "元/月"}, "1.25-2万/月"),
    ({"salary_min": 3000, "salary_max": 5000, "salary_unit": "元/月"}, "3000-5000元/月"),
    ({"salary_min": 4000, "salary_max": 4000, "salary_unit": "元/月"}, "4000元/月"),
    ({"salary_min": 200, "salary_max": 400, "salary_unit": "元/天"}, "200-400元/天"),
    ({"salary_min": 50, "salary_max": 80, "salary_unit": "元/时"}, "50-80元/时"),
    ({"salary_min": 200000, "salary_max": 300000, "salary_unit": "元/年"}, "20-30万/年"),
    ({"salary_min": 80000, "salary_max": 90000, "salary_unit": "元/年"}, "8-9万/年"),
    ({"salary_min": None, "salary_max": None, "salary_text": "面议"}, "面议"),
    ({"salary_min": None, "salary_max": None, "salary_text": None}, ""),
    ({"salary_min": 10000, "salary_max": 20000, "salary_text": "10-20K", "salary_unit": "元/月"}, "1-2万/月")])   # the figures win over the words
def test_the_salary_is_written_from_its_figures_and_falls_back_to_the_sites_words(page, job, label):
    assert page.evaluate("j => salaryLabel(j)", job) == label


PAYS = "薪资"


def paid_jobs(app):
    """A search whose jobs are paid per month, per day and per year (no two share a company, so none is a duplicate)."""
    pay = [("低薪", "8-12K"), ("日薪", "700元/天"), ("中薪", "15-25K"), ("高薪", "30-45K"), ("年薪", "20-30万/年")]   # 700 a day is 15225 a month
    app.data["nc"][PAYS] = [{"id": 1000 + i, "title": f"牛客{name}", "company": f"示例{name}有限公司", "city": "杭州", "salary": text,
                             "jd": "说明"} for i, (name, text) in enumerate(pay)] + [
        {"id": 1099, "title": "牛客无薪", "company": "示例无薪有限公司", "city": "杭州", "salary": None, "jd": "说明"}]   # held back: no salary


def shown_titles(page):
    return sorted(only_text(page.locator(JOBS).locator("h3")))


def test_the_salary_chips_filter_by_the_pay_per_month_whatever_the_unit(page, app):
    paid_jobs(app)
    open_results(page, keyword=PAYS)
    chips = page.locator("#salary .chip")
    expect(chips).to_have_text(["不限", "5千+", "1万+", "1.5万+", "2万+", "3万+"])
    expect(page.get_by_role("group", name="按月薪筛选")).to_be_visible()
    expect(page.get_by_role("button", name="不限", exact=True)).to_have_attribute("aria-pressed", "true")
    assert shown_titles(page) == sorted(["牛客低薪", "牛客日薪", "牛客中薪", "牛客年薪", "牛客高薪"])

    page.get_by_role("button", name="1.5万+").click()   # a day's pay counts 21.75 days and a year's a twelfth: 700 a day is 15225
    expect(page.get_by_role("button", name="1.5万+")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_role("button", name="不限", exact=True)).to_have_attribute("aria-pressed", "false")
    expect(page.locator(JOBS)).to_have_count(4)
    assert shown_titles(page) == sorted(["牛客日薪", "牛客中薪", "牛客高薪", "牛客年薪"])
    expect(page.locator("#more")).to_contain_text("已显示 4 / 4 个")
    expect(page.locator("#found")).to_have_text("找到 4 个岗位")
    page.get_by_role("button", name="2万+").click()   # 15-25K, 20-30万 a year (25000 a month at most) and 30-45K can reach 20000
    expect(page.locator(JOBS)).to_have_count(3)
    assert shown_titles(page) == sorted(["牛客中薪", "牛客高薪", "牛客年薪"])
    page.get_by_role("button", name="3万+").click()
    expect(page.locator(JOBS)).to_have_count(1)
    assert shown_titles(page) == ["牛客高薪"]
    page.get_by_role("button", name="导出表格").click()   # the export follows the filter
    assert len(exported(page, app)[1]) == 1 + 1
    page.get_by_role("button", name="不限", exact=True).click()
    expect(page.locator(JOBS)).to_have_count(5)
    assert page.evaluate(NAMELESS) == []


def test_a_salary_filter_that_leaves_nothing_says_so_and_keeps_the_way_back(page, app):
    app.data["nc"]["没钱"] = [{"id": 1500 + i, "title": f"低薪岗位 {i}", "company": f"示例低{i}有限公司", "city": "杭州", "salary": "3-5K", "jd": "说明"}
                              for i in range(3)]
    open_results(page, keyword="没钱")
    expect(page.locator(JOBS)).to_have_count(3)
    page.get_by_role("button", name="1万+").click()
    expect(page.get_by_role("heading", name="没有符合这个月薪的岗位")).to_be_visible()
    expect(page.get_by_role("heading", name="这次没有找到岗位")).to_have_count(0)   # the search did find jobs
    expect(page.locator("#salary .chip")).to_have_count(6)                          # the row stays: the filter can be taken off
    expect(page.get_by_role("button", name="导出表格")).to_have_count(0)
    page.get_by_role("button", name="不限月薪").click()
    expect(page.locator(JOBS)).to_have_count(3)
    assert page.evaluate(NAMELESS) == []


def test_the_salary_row_is_not_there_when_nothing_was_found(page, app):
    finish_search(page, keyword="运营", city="")
    page.get_by_role("link", name="查看结果：运营 · 全国").click()
    expect(page.get_by_role("heading", name="这次没有找到岗位")).to_be_visible()
    expect(page.locator("#salary")).to_be_empty()


# ---- the same job on several sites
def twin_jobs(app):
    """One job posted on 牛客网 (with its education) and 智联招聘, and one that only 智联招聘 has."""
    same = {"title": "全栈工程师", "company": "示例科技有限公司", "city": "杭州", "salary": "20-30K", "jd": "负责全栈开发"}
    app.data["nc"]["重复"] = [{**same, "id": 2000, "education": "本科"}]
    app.data["zl"]["重复"] = [{**same, "id": 2300, "title": "【急聘】全栈工程师"}, {**same, "id": 2301, "title": "测试工程师", "company": "另一家公司"}]


def test_the_same_job_on_two_sites_is_one_card_with_a_badge_and_a_way_to_the_other_site(page, app, monkeypatch):
    opened = []
    monkeypatch.setattr(ui_api.webbrowser, "open", opened.append)
    twin_jobs(app)
    open_results(page, keyword="重复")
    expect(page.locator(JOBS)).to_have_count(2)                       # three records, two jobs
    expect(page.locator("#found")).to_have_text("找到 2 个岗位")
    expect(page.locator("#what")).to_have_text("重复 · 杭州 · 已合并 1 个重复岗位")
    expect(page.locator("#filters .chip")).to_have_text(["全部 2", "牛客网 1", "智联招聘 2"])   # a site's chip counts its own jobs
    twin = page.locator(JOBS, has_text="全栈工程师")
    expect(twin.locator(".tags .pill:not(.gray)")).to_have_text("牛客网")        # the record with more of its columns stands for the job
    expect(twin.locator(".tags .pill.gray", has_text="也在")).to_have_text("也在 智联招聘")
    expect(page.locator(JOBS, has_text="测试工程师").locator(".tags .pill.gray", has_text="也在")).to_have_count(0)

    twin.get_by_role("button").click()
    drawer = page.locator("#drawer")
    expect(drawer.locator(".tags .pill.gray", has_text="也在")).to_have_text("也在 智联招聘")
    expect(drawer.get_by_role("heading", name="也在这些网站")).to_be_visible()
    drawer.get_by_role("button", name="在智联招聘打开").click()
    until(page, lambda: opened == ["https://fake.test/zl/job/2300"])
    drawer.get_by_role("button", name="打开原页面").click()
    until(page, lambda: opened[-1] == "https://fake.test/nc/job/2000")   # the drawer's own button reads the same data
    assert page.evaluate(NAMELESS) == []
    page.keyboard.press("Escape")

    page.locator("#filters").get_by_role("button", name="智联招聘").click()   # that site's own page: both of its jobs, nothing folded in
    expect(page.locator(JOBS)).to_have_count(2)
    expect(page.locator("#list .tags .pill.gray", has_text="也在")).to_have_count(0)
    expect(page.locator("#what")).to_have_text("重复 · 杭州 · 已合并 1 个重复岗位")   # the search as a whole still folded one record
    expect(page.locator("#more")).to_contain_text("已显示 2 / 2 个")                 # and the chip said 2: clicking it lists 2


def test_the_search_card_the_strip_and_the_heading_count_the_same_jobs(page, app):
    twin_jobs(app)   # three records, two jobs: the site numbers add up to more than the jobs there are
    card = finish_search(page, keyword="重复")
    expect(card).to_contain_text("已找到 2 个岗位")
    expect(card.locator(".dot", has_text="牛客网")).to_have_text("牛客网1")           # a site's own number stays its own
    expect(card.locator(".dot", has_text="智联招聘")).to_have_text("智联招聘2")
    page.get_by_role("link", name="我的结果").click()
    expect(page.locator("#found")).to_have_text("找到 2 个岗位")
    expect(page.locator("#strip a.chip.on")).to_have_text("重复 · 杭州 · 2 个")
    expect(page.locator("#filters .chip")).to_have_text(["全部 2", "牛客网 1", "智联招聘 2"])
    page.get_by_role("link", name="查找", exact=True).click()
    expect(newest_card(page)).to_contain_text("已找到 2 个岗位")


def test_a_job_that_is_only_on_one_site_has_no_badge_and_no_list_of_sites(page, app):
    open_results(page, keyword=KEYWORD)
    expect(page.locator("#list .tags .pill.gray", has_text="也在")).to_have_count(0)
    expect(page.locator("#what")).to_have_text("前端工程师 · 杭州")   # nothing merged: nothing to say
    page.locator(JOBS).first.get_by_role("button").click()
    expect(page.locator("#drawer-title")).to_be_visible()
    expect(page.get_by_role("heading", name="也在这些网站")).to_have_count(0)


def test_results_follow_a_running_search(page, app):
    hold(app, "zl", "zz")
    start_search(page)
    expect(page.locator(".search-card .dot.ok")).to_have_count(2)
    page.get_by_role("link", name="查看结果：前端工程师 · 杭州").click()
    notice = page.locator(".notice")
    expect(notice).to_contain_text("还在查找中，已找到 85 个，结果会自动更新")
    expect(page.locator("#filters .chip")).to_have_text(["全部 85", "牛客网 25", "国聘 60"])
    release(app, "zl", "zz")
    expect(notice).to_have_count(0)   # the banner goes by itself ...
    expect(page.locator("#found")).to_have_text("找到 128 个岗位")   # ... and the numbers arrived without a click
    expect(page.locator("#filters .chip")).to_have_text(["全部 128", "牛客网 25", "国聘 60", "智联招聘 40", "中智招聘 3"])


# layout size: the bounding box of a turning square is bigger than the square
SPIN_SIZES = "() => [...document.querySelectorAll('.spin')].filter((el) => el.getClientRects().length).map((el) => [el.offsetWidth, el.offsetHeight])"


def test_a_spinner_keeps_its_own_size_wherever_it_turns(page, app):
    hold(app, "zl", "zz")
    start_search(page)
    expect(newest_card(page).locator(".dot.run")).to_have_count(2)
    sizes = page.evaluate(SPIN_SIZES)
    assert len(sizes) == 3 and all(size == [14, 14] for size in sizes)   # the card's pill and its two running sites
    page.get_by_role("link", name="查看结果：前端工程师 · 杭州").click()
    expect(page.locator(".notice .spin")).to_be_visible()
    assert page.evaluate(SPIN_SIZES) == [[14, 14]]   # a rule meant for the banner's text once stretched the spinner across the page
    assert page.locator(".notice > span:not(.spin)").bounding_box()["width"] > 200   # the text still takes the room that is left
    release(app, "zl", "zz")
    expect(page.locator(".notice")).to_have_count(0)


def test_the_notice_of_a_search_that_has_found_nothing_yet_keeps_its_spinner_small_too(page, app):
    hold(app, "nc", "gp", "xh", "zl", "zz")
    start_search(page)
    page.get_by_role("link", name="我的结果").click()
    expect(page.locator(".skeleton")).to_have_count(6)
    expect(page.locator(".notice .spin")).to_be_visible()
    assert page.evaluate(SPIN_SIZES) == [[14, 14]]
    release(app, "nc", "gp", "xh", "zl", "zz")


def test_results_with_nothing_yet_show_loading_cards(page, app):
    hold(app, "nc", "gp", "xh", "zl", "zz")
    start_search(page)
    page.get_by_role("link", name="我的结果").click()
    expect(page.locator(".skeleton")).to_have_count(6)
    expect(page.locator(".notice")).to_contain_text("已找到 0 个")
    expect(page.get_by_role("button", name="导出表格")).to_have_count(0)   # nothing to export yet
    release(app, "nc", "gp", "xh", "zl", "zz")
    expect(page.locator(JOBS).first).to_be_visible()
    expect(page.locator(".skeleton")).to_have_count(0)


def test_empty_results_are_neutral_and_problems_offer_the_right_button(page, app):
    finish_search(page, keyword="运营", city="")
    page.get_by_role("link", name="查看结果：运营 · 全国").click()
    expect(page.get_by_role("heading", name="这次没有找到岗位")).to_be_visible()
    expect(page.locator(".tips li")).to_have_text(
        ["放宽关键词，比如把“高级前端工程师”改成“前端”", "去掉城市，看看全国的结果", "换几个网站再找一次"])
    notes = page.locator(".empty .note")
    expect(notes).to_have_count(5)
    for note in notes.all():
        expect(note).to_have_class(re.compile(r"\bnone\b"))   # zero results from a site: a fact, not a problem
        expect(note).to_contain_text("这个网站没有找到")
    expect(page.get_by_role("button", name="导出表格")).to_have_count(0)
    expect(page.locator(".seg")).to_have_count(0)
    expect(page.locator("#incomplete")).to_be_empty()

    conn = db.connect()
    task = lambda provider: conn.execute("SELECT id FROM tasks WHERE provider=? ORDER BY id DESC", (provider,)).fetchone()[0]
    taskqueue.dead(conn, task("zl"), "Blocked: HTTP 403")
    taskqueue.dead(conn, task("zz"), "missing environment variable(s): DJS_X")
    service.crawl_start(conn, "lg", keyword="运营")   # a login-only site, nobody is logged in
    page.get_by_role("link", name="查找", exact=True).click()
    page.get_by_role("link", name="查看结果：运营 · 全国").click()
    expect(page.locator(".empty .note.none")).to_have_count(3)
    blocked = page.locator(".empty .note.bad", has_text="智联招聘")
    expect(blocked).to_contain_text("被网站暂时限制，稍后再试")
    expect(blocked.get_by_role("button", name="重试")).to_be_visible()
    keys = page.locator(".empty .note.bad", has_text="中智招聘")
    expect(keys).to_contain_text("还没配置这个网站需要的密钥")
    expect(keys.get_by_role("button")).to_have_text("如何配置")   # retrying cannot fix a missing key
    login = page.locator(".empty .note.warn", has_text="登录专用站")
    expect(login).to_contain_text("需要先登录")
    expect(login.get_by_role("button", name="去登录")).to_be_visible()

    blocked.get_by_role("button", name="重试").click()
    expect(page.locator("#toast")).to_have_text("已重新开始查找")
    expect(page.locator(".empty .note.none")).to_have_count(4)   # the retried site ran again and found nothing
    expect(page.locator(".empty .note.bad", has_text="智联招聘")).to_have_count(0)
    keys.get_by_role("button", name="如何配置").click()
    expect(page.locator("#keys")).to_be_visible()
    page.go_back()
    page.locator(".empty .note.warn", has_text="登录专用站").get_by_role("button", name="去登录").click()
    expect(page.locator("#modal .state.waiting")).to_be_visible()


def test_a_failed_load_offers_a_retry(page, app, monkeypatch):
    finish_search(page)
    working = ui_api.ROUTES["GET /api/search_jobs"]

    def broken(conn, a):
        raise ValueError("读取失败")
    monkeypatch.setitem(ui_api.ROUTES, "GET /api/search_jobs", broken)
    page.get_by_role("link", name="我的结果").click()
    expect(page.get_by_role("heading", name="没能读取结果")).to_be_visible()
    expect(page.locator("#list")).to_contain_text("读取失败")
    monkeypatch.setitem(ui_api.ROUTES, "GET /api/search_jobs", working)
    page.get_by_role("button", name="重试").click()
    expect(page.locator(JOBS)).to_have_count(30)


def test_a_page_that_cannot_open_offers_a_retry(page, app, monkeypatch):
    working = ui_api.ROUTES["GET /api/providers"]

    def broken(conn, a):
        raise ValueError("读取失败")
    monkeypatch.setitem(ui_api.ROUTES, "GET /api/providers", broken)
    page.get_by_role("link", name="设置").click()
    expect(page.get_by_role("heading", name="暂时打不开这一页")).to_be_visible()
    expect(page.locator("#view")).to_contain_text("读取失败")
    monkeypatch.setitem(ui_api.ROUTES, "GET /api/providers", working)
    page.get_by_role("button", name="重试").click()
    expect(page.get_by_role("heading", name="设置")).to_be_visible()   # the page the address asked for, not the old one
    expect(page.get_by_role("heading", name="暂时打不开这一页")).to_have_count(0)


def test_the_strip_switches_between_searches_and_scrolls(page, app):
    finish_search(page)
    for i in range(12):
        service.crawl_start(db.connect(), "nc", keyword=f"另一个很长的查找关键词 {i}", detail="never")
    page.get_by_role("link", name="我的结果").click()
    strip = page.locator("#strip")
    expect(strip.locator("a.chip")).to_have_count(13)
    assert page.evaluate("document.querySelector('#strip').scrollWidth > document.querySelector('#strip').clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")   # the strip scrolls, the page does not
    strip.get_by_role("link", name="前端工程师 · 杭州 · 128 个").scroll_into_view_if_needed()
    strip.get_by_role("link", name="前端工程师 · 杭州 · 128 个").click()
    expect(page.locator("#what")).to_have_text("前端工程师 · 杭州")
    expect(strip.locator("a.chip.on")).to_have_text("前端工程师 · 杭州 · 128 个")
    expect(strip.locator("a.chip.on")).to_have_attribute("aria-current", "true")
    page.get_by_role("link", name="另一个很长的查找关键词 3 · 全国 · 0 个").click()
    expect(page.locator("#what")).to_have_text("另一个很长的查找关键词 3 · 全国")
    expect(page).to_have_url(re.compile(r"#/results\?k=.*3&c=$"))


# ---------------------------------------------------------------- the data contract in the pages
INCOMPLETE = "缺信息"


def lacking(app):
    """A search whose jobs lack something, for every reason the pages explain."""
    whole = lambda name, start, n: [{"id": start + i, "title": f"{name}完整 {i}", "company": f"{name}公司", "city": "杭州",
                                     "salary": "15-25K", "jd": "说明"} for i in range(n)]
    bare = lambda name, start, pays: [{"id": start + i, "title": f"{name}缺薪资 {i}", "company": f"{name}公司", "city": "杭州",
                                       "salary": p, "jd": "说明"} for i, p in enumerate(pays)]
    app.data["nc"][INCOMPLETE] = whole("牛客", 900, 4)                                  # whole from the list
    app.data["gp"][INCOMPLETE] = [{**j, "jd": None} for j in whole("国聘", 920, 2)]       # whole after its detail request
    app.data["zl"][INCOMPLETE] = bare("智联", 930, ["面议", "面议", None, None, None])      # a login would show the salary
    app.data["xh"][INCOMPLETE] = bare("新华", 950, [None, None, None])                    # this site never publishes one


def test_the_results_list_whole_jobs_and_say_how_many_are_held_back_and_why(page, app):
    lacking(app)
    open_results(page, keyword=INCOMPLETE)
    expect(page.locator("#found")).to_have_text("找到 6 个岗位")   # nc 4 + gp 2: whole, one of them after its detail request
    expect(page.locator(JOBS)).to_have_count(6)
    expect(page.locator(".miss")).to_have_count(0)
    quiet = page.locator("#incomplete")
    expect(quiet).to_contain_text("另有 8 条信息不全（原因：平台未提供薪资 3 · 需登录才能看到薪资 3 · 面议 2）")
    switch = page.get_by_role("switch", name="显示信息不全的岗位")
    expect(switch).to_have_attribute("aria-checked", "false")
    chips = page.locator("#filters .chip")
    expect(chips).to_have_text(["全部 6", "牛客网 4", "国聘 2", "新华英才 0", "智联招聘 0"])   # a site with only held-back jobs keeps its chip
    expect(page.locator(".notes .note")).to_have_text([
        re.compile("新华英才 · 找到 3 个岗位，但信息都不全（平台未提供薪资）"), re.compile("智联招聘 · 找到 5 个岗位，但信息都不全$")])   # a shared reason is said, differing ones are not made up
    assert page.evaluate(NAMELESS) == []

    show_incomplete(page)
    expect(page.locator(JOBS)).to_have_count(14)
    expect(page.locator("#found")).to_have_text("找到 14 个岗位")
    expect(quiet).to_contain_text("其中 8 条信息不全（原因：平台未提供薪资 3 · 需登录才能看到薪资 3 · 面议 2）")
    expect(page.locator(".miss")).to_have_count(8)
    expect(page.locator(JOBS, has_text="智联缺薪资 0").locator(".miss")).to_have_text("信息不全：面议")
    expect(page.locator(JOBS, has_text="智联缺薪资 2").locator(".miss")).to_have_text("信息不全：需登录才能看到薪资")
    expect(page.locator(JOBS, has_text="新华缺薪资 1").locator(".miss")).to_have_text("信息不全：平台未提供薪资")
    expect(page.locator(JOBS, has_text="新华缺薪资 1").locator(".salary.none")).to_have_text("薪资未公开")
    expect(page.locator(JOBS, has_text="牛客完整 0").locator(".salary")).to_have_text("1.5-2.5万/月")
    expect(page.locator("#filters .chip")).to_have_text(["全部 14", "牛客网 4", "国聘 2", "新华英才 3", "智联招聘 5"])
    expect(page.locator(JOBS, has_text="智联缺薪资 0").locator(".salary")).to_have_text("面议")   # the site's own word when it gave no figures

    show_incomplete(page, on=False)   # and back
    expect(page.locator(JOBS)).to_have_count(6)
    page.locator("#filters").get_by_role("button", name="智联招聘").click()   # a site that has nothing whole to show
    expect(page.get_by_role("heading", name="找到的岗位信息都不全")).to_be_visible()
    expect(page.locator("#list")).to_contain_text("共有 5 个岗位缺少薪资等必要信息，默认先不显示")
    expect(quiet).to_contain_text("另有 5 条信息不全（原因：需登录才能看到薪资 3 · 面议 2）")
    show_incomplete(page)
    expect(page.locator(JOBS)).to_have_count(5)


def test_the_export_follows_the_view_and_says_what_each_incomplete_job_lacks(page, app):
    lacking(app)
    open_results(page, keyword=INCOMPLETE)
    page.get_by_role("button", name="导出表格").click()
    csv_file, lines = exported(page, app)
    assert len(lines) == 1 + 6   # the whole jobs only, like the list
    csv_file.unlink()
    show_incomplete(page)
    page.get_by_role("button", name="导出表格").click()
    lines = exported(page, app)[1]
    assert len(lines) == 1 + 14
    cells = lambda title: next(line for line in lines if title in line).split(",")
    mianyi, never = cells("智联缺薪资 0"), cells("新华缺薪资 1")
    assert (mianyi[2], mianyi[10:13], mianyi[14]) == ('"面议"', ['""'] * 3, '"面议"')   # the site's words, no figures, and why
    assert (never[2], never[14]) == ('""', '"平台未提供薪资"')


def test_a_search_with_nothing_whole_says_so_instead_of_looking_broken(page, app):
    lacking(app)
    app.data["nc"][INCOMPLETE] = []
    app.data["gp"][INCOMPLETE] = []
    open_results(page, keyword=INCOMPLETE)
    expect(page.get_by_role("heading", name="找到的岗位信息都不全")).to_be_visible()
    expect(page.locator("#list")).to_contain_text("共有 8 个岗位缺少薪资等必要信息，默认先不显示")
    expect(page.locator("#list")).to_contain_text("新华英才 · 找到 3 个岗位，但信息都不全（平台未提供薪资）")
    expect(page.get_by_role("heading", name="这次没有找到岗位")).to_have_count(0)
    expect(page.get_by_role("button", name="导出表格")).to_have_count(0)
    show_incomplete(page)
    expect(page.locator(JOBS)).to_have_count(8)
    expect(page.get_by_role("button", name="导出表格")).to_be_visible()


def test_the_search_card_counts_the_whole_jobs_per_site_and_explains_a_site_that_has_none(page, app):
    lacking(app)
    finish_search(page, keyword=INCOMPLETE)
    card = newest_card(page)
    expect(card).to_contain_text("已完成 6 / 6 个网站 · 已找到 6 个岗位")
    dot = lambda name: card.locator(".dot", has_text=name)
    expect(dot("牛客网")).to_have_text("牛客网4")
    expect(dot("国聘")).to_have_text("国聘2")
    expect(dot("智联招聘")).to_have_text("智联招聘0 / 5")   # whole jobs, and how many it found in all
    expect(dot("新华英才")).to_have_text("新华英才0 / 3")
    expect(dot("中智招聘")).to_contain_text("没有找到")
    expect(card.locator(".notes .note")).to_have_text([
        re.compile("新华英才 · 找到 3 个岗位，但信息都不全（平台未提供薪资）"), re.compile("智联招聘 · 找到 5 个岗位，但信息都不全$")])
    expect(page.locator("#recent")).to_be_empty()


def test_a_search_that_is_still_completing_shows_the_whole_jobs_growing(page, app):
    app.detail_gates["gp"] = threading.Event()   # its list is stored, but not one detail request has been answered
    app.data["gp"][INCOMPLETE] = jobs("国聘", 4, 700, jd=None)
    start_search(page, keyword=INCOMPLETE)
    dot = newest_card(page).locator(".dot", has_text="国聘")
    expect(dot).to_have_text("国聘0 / 4")
    app.detail_gates["gp"].set()
    expect(dot).to_have_text("国聘4")


def test_jobs_that_wait_for_their_detail_say_so_instead_of_blaming_the_platform(page, app):
    app.detail_gates["gp"] = threading.Event()   # the list is stored, but no detail request has been answered yet
    app.data["gp"]["补全中"] = jobs("国聘", 4, 700, jd=None)
    start_search(page, keyword="补全中")
    page.get_by_role("link", name="查看结果：补全中 · 杭州").click()
    expect(page.locator("#incomplete")).to_contain_text("另有 4 条信息不全（原因：正在补全详情 4）")
    show_incomplete(page)
    expect(page.locator(JOBS)).to_have_count(4)
    expect(page.locator("#list .miss")).to_have_text(["信息不全：正在补全详情"] * 4)
    page.locator(JOBS).first.get_by_role("button").click()
    expect(page.locator("#drawer .miss")).to_have_text("信息不全：正在补全详情")
    page.keyboard.press("Escape")
    app.detail_gates["gp"].set()   # the details arrive: the jobs are whole, nothing is held back any more
    expect(page.locator("#list .miss")).to_have_count(0)
    expect(page.locator("#incomplete")).to_be_empty()
    expect(page.locator(JOBS)).to_have_count(4)


def test_a_job_that_lacks_something_says_so_in_the_drawer_too(page, app):
    lacking(app)
    open_results(page, keyword=INCOMPLETE)
    show_incomplete(page)
    page.locator(JOBS, has_text="智联缺薪资 2").get_by_role("button").click()
    expect(page.locator("#drawer .miss")).to_have_text("信息不全：需登录才能看到薪资")
    expect(page.locator("#drawer .salary")).to_have_text("薪资未公开")
    expect(page.locator("#drawer-text")).to_have_text("说明")


# ---------------------------------------------------------------- the detail drawer
def test_drawer_stored_and_lazy_description(page, app, monkeypatch):
    opened = []
    monkeypatch.setattr(ui_api.webbrowser, "open", opened.append)
    app.broken.add("gp")   # the platform's detail pages cannot be read while the search runs ...
    finish_search(page)
    page.get_by_role("link", name="我的结果").click()
    flt = page.locator("#filters")
    expect(flt.get_by_role("button", name="国聘")).to_contain_text("国聘 0")   # ... so none of its jobs is whole yet

    flt.get_by_role("button", name="牛客网").click()   # the description came with the list: shown at once, from the database
    expect(page.locator("#more")).to_contain_text("已显示 25 / 25 个")
    first = page.locator(JOBS).first
    title = first.locator("h3").inner_text()
    first.get_by_role("button").click()
    drawer = page.locator("#drawer")
    expect(drawer.get_by_role("dialog")).to_be_visible()
    expect(page.locator("#drawer-title")).to_have_text(title)
    expect(drawer).to_contain_text("示例科技有限公司")
    assert page.locator("#drawer-text").inner_text() == "1. 负责前端开发\n2. 熟悉 Vue / React"
    assert page.evaluate(NAMELESS) == []
    page.get_by_role("button", name="打开原页面").click()
    until(page, lambda: opened)
    assert opened[0].startswith("https://fake.test/nc/job/")
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    expect(first.get_by_role("button")).to_be_focused()   # focus goes back to the card that opened it
    first.get_by_role("button").click()
    expect(drawer).to_be_visible()
    page.mouse.click(40, 400)   # the dim area closes it too
    expect(drawer).to_be_hidden()

    flt.get_by_role("button", name="全部").click()
    show_incomplete(page)   # no description yet: the platform is asked once, with a visible wait
    flt.get_by_role("button", name="国聘").click()
    expect(page.locator("#more")).to_contain_text("已显示 30 / 60 个")
    app.broken.clear()
    hold(app, "gp")
    page.locator(JOBS).first.get_by_role("button").click()
    expect(page.locator("#drawer-status")).to_contain_text("正在到网站上获取这个岗位的详细介绍")
    expect(page.locator("#drawer-text")).to_be_hidden()
    release(app, "gp")
    expect(page.locator("#drawer-text")).to_contain_text("负责前端页面开发")
    expect(page.locator("#drawer-status")).to_be_empty()
    asked = sum(p.endswith("/detail") for p in app.requests)
    page.keyboard.press("Escape")
    page.locator(JOBS).first.get_by_role("button").click()   # the second time it comes from memory
    expect(page.locator("#drawer-text")).to_contain_text("负责前端页面开发")
    assert asked == sum(p.endswith("/detail") for p in app.requests)

    app.broken.add("gp")   # a failing platform: plain Chinese, and a retry that works once it is back
    page.keyboard.press("Escape")
    page.locator(JOBS).nth(1).get_by_role("button").click()
    expect(page.locator("#drawer-status")).to_contain_text("网站页面变了，暂时读不出来")
    app.broken.clear()
    page.get_by_role("button", name="重试").click()
    expect(page.locator("#drawer-text")).to_contain_text("负责前端页面开发")
    expect(page.locator("#drawer-status")).to_be_empty()


WITH_TAGS = {"id": 700, "title": "牛客福利岗", "company": "示例科技有限公司 A", "city": "杭州", "salary": "15-25K", "jd": "负责记账",
             "benefits": ["五险一金", "周末双休", "五险一金", "餐补"]}
WITHOUT_TAGS = {"id": 701, "title": "牛客无福利岗", "company": "示例科技有限公司 B", "city": "杭州", "salary": "12-18K", "jd": "负责出纳"}


def test_the_drawer_and_the_export_carry_the_welfare_tags_a_site_lists(page, app):
    app.data["nc"]["福利"] = [WITH_TAGS, WITHOUT_TAGS]
    open_results(page, keyword="福利")
    expect(page.locator(JOBS)).to_have_count(2)
    expect(page.locator(".job .benefits")).to_have_count(0)           # the cards stay as they were: the tags are for the drawer
    page.locator(JOBS, has_text="牛客福利岗").get_by_role("button").click()
    expect(page.locator("#drawer .benefits")).to_have_text("福利：五险一金、周末双休、餐补")   # each once, in the site's order
    assert page.evaluate(NAMELESS) == []
    page.keyboard.press("Escape")
    page.locator(JOBS, has_text="牛客无福利岗").get_by_role("button").click()
    expect(page.locator("#drawer-title")).to_have_text("牛客无福利岗")
    expect(page.locator("#drawer .benefits")).to_have_count(0)        # a job that lists none has no empty line
    page.keyboard.press("Escape")
    page.get_by_role("button", name="导出表格").click()
    lines = exported(page, app)[1]
    head = lines[0].lstrip("\ufeff").split(",")
    assert head[head.index('"经验"') + 1] == '"福利"'
    cell = lambda title: next(line for line in lines[1:] if f'"{title}"' in line).split(",")[head.index('"福利"')]   # noqa: E731
    assert (cell("牛客福利岗"), cell("牛客无福利岗")) == ('"五险一金、周末双休、餐补"', '""')


def test_the_drawers_close_button_is_labelled_reachable_and_takes_the_focus(page, app):
    open_results(page)
    first = page.locator(JOBS).first.get_by_role("button")
    first.click()
    close = page.locator("#drawer").get_by_role("button", name="关闭")
    expect(close).to_be_visible()
    expect(close).to_be_focused()   # keyboard focus moves into the drawer when it opens
    box = close.bounding_box()
    assert box["width"] >= 60 and box["height"] >= 28 and box["y"] < 100   # a real button at the top, not a speck
    page.keyboard.press("Enter")   # reachable without the mouse ...
    expect(page.locator("#drawer")).to_be_hidden()
    expect(first).to_be_focused()
    first.click()
    page.locator("#drawer").get_by_role("button", name="关闭").click()   # ... and with it
    expect(page.locator("#drawer")).to_be_hidden()
    assert page.evaluate(NAMELESS) == []


def test_keyboard_stays_inside_an_open_layer(page, app):
    finish_search(page)
    page.get_by_role("link", name="我的结果").click()
    page.locator(JOBS).first.get_by_role("button").click()
    inside = lambda layer: page.evaluate(f"document.activeElement === document.body || !!document.activeElement.closest('#{layer}')")
    for _ in range(10):   # Tab never reaches the page behind the drawer
        page.keyboard.press("Tab")
        assert inside("drawer")
    page.keyboard.press("Escape")
    page.locator("#filters").get_by_role("button", name="牛客网").focus()   # and the page is reachable again
    expect(page.locator("#filters").get_by_role("button", name="牛客网")).to_be_focused()
    go_logins(page)
    page.get_by_role("button", name="登录", exact=True).first.click()
    expect(page.locator("#modal .state.waiting")).to_be_visible()
    for _ in range(10):
        page.keyboard.press("Tab")
        assert inside("modal")


def test_a_job_without_any_text_says_so(page, app):
    app.data["gp"]["没有介绍"] = jobs("国聘", 1, 700, jd=None)
    app.provs["gp"].detail = lambda client, job: None   # the platform has nothing more to say
    finish_search(page, keyword="没有介绍")
    page.get_by_role("link", name="我的结果").click()
    show_incomplete(page)   # without a description the job is not whole
    page.locator(JOBS).first.get_by_role("button").click()
    expect(page.locator("#drawer-status")).to_contain_text("这个岗位没有提供更详细的介绍")
    expect(page.locator("#drawer-text")).to_be_hidden()
    expect(page.get_by_role("button", name="打开原页面")).to_be_visible()


# ---------------------------------------------------------------- nothing in a job may run
EVIL = {"id": 900, "title": '<img src=x onerror="window.__pwned=1">', "company": '"><script>window.__pwned=2</script>',
        "city": "<svg onload=window.__pwned=3>", "salary": "<b onmouseover=window.__pwned=4>1</b>",
        "jd": '<img src=x onerror="window.__pwned=5"><script>window.__pwned=6</script>危险<br>第二行',
        "url": "javascript:window.__pwned=7", "benefits": ['<img src=x onerror="window.__pwned=10">', "<b onmouseover=window.__pwned=11>双休</b>"]}
FORMULA = {"id": 901, "title": '=HYPERLINK("http://evil.test","x")', "company": "+cmd", "city": "@x", "salary": "-1",
           "benefits": ["+SUM(1+1)"]}


def test_job_text_never_becomes_markup_or_script(page, app, monkeypatch):
    opened = []
    monkeypatch.setattr(ui_api.webbrowser, "open", opened.append)
    assert results.plain_text(EVIL["jd"]) == "危险\n第二行"
    app.data["nc"]["安全"] = [EVIL, FORMULA]
    finish_search(page, keyword="安全")
    page.get_by_role("link", name="我的结果").click()
    show_incomplete(page)   # their salary has no figures, so they are not whole: shown on request, with the site's own words
    cards = page.locator(JOBS)
    expect(cards).to_have_count(2)
    evil = cards.filter(has_text="onerror")
    expect(evil.locator("h3")).to_have_text('<img src=x onerror="window.__pwned=1">')   # shown as text, not parsed
    expect(evil).to_contain_text('"><script>window.__pwned=2</script>')
    expect(evil).to_contain_text("<svg onload=window.__pwned=3>")
    expect(evil).to_contain_text("<b onmouseover=window.__pwned=4>1</b>")
    evil.get_by_role("button").click()
    expect(page.locator("#drawer-text")).to_be_visible()
    assert page.locator("#drawer-text").inner_text() == "危险\n第二行"   # the tags are gone, the lines are kept
    expect(page.locator("#drawer .benefits")).to_have_text('福利：<img src=x onerror="window.__pwned=10">、<b onmouseover=window.__pwned=11>双休</b>')
    page.locator("#drawer-text").hover()
    page.locator("#drawer .benefits").hover()
    page.get_by_role("button", name="打开原页面").click()   # a javascript: link is refused by the bridge
    expect(page.locator("#toast")).to_have_text("只能打开网页链接")
    assert opened == []
    assert page.evaluate("window.__pwned") is None
    for selector in ("img", "iframe", "[onerror]", "[onload]", "[onmouseover]", "#view script", "#drawer script"):
        assert page.locator(selector).count() == 0, selector
    assert page.locator("script").count() == 1   # only the app's own

    page.keyboard.press("Escape")   # the spreadsheet formula guard in the export
    page.get_by_role("button", name="导出表格").click()
    csv = "\r\n".join(exported(page, app)[1])
    assert '"\'=HYPERLINK(""http://evil.test"",""x"")"' in csv and '"\'+cmd"' in csv and '"\'@x"' in csv
    assert '"\'+SUM(1+1)"' in csv   # the welfare tags are a cell like any other


def test_typed_keywords_are_text_too(page, app):
    evil = '<img src=x onerror="window.__pwned=8"><script>window.__pwned=9</script>'
    start_search(page, keyword=evil, city="")
    expect(newest_card(page).locator("h3")).to_have_text(evil + " · 全国")
    page.get_by_role("link", name="我的结果").click()
    expect(page.locator("#what")).to_have_text(evil + " · 全国")
    expect(page.locator("#strip a.chip.on")).to_contain_text(evil)
    assert page.evaluate("window.__pwned") is None and page.locator("img").count() == 0 and page.locator("script").count() == 1


# ---------------------------------------------------------------- exporting
def test_the_export_goes_to_the_downloads_folder_and_offers_to_show_it_there(page, app, monkeypatch):
    launched = []
    monkeypatch.setattr(ui_api, "subprocess", SimpleNamespace(Popen=launched.append))
    open_results(page)
    export = page.get_by_role("button", name="导出表格")
    export.click()
    toast = page.locator("#toast")
    expect(toast).to_contain_text("已导出到「下载」文件夹")
    expect(export).to_be_enabled()   # not busy for ever
    path = app.downloads / f"找岗位-前端工程师-杭州-{STAMP}.csv"
    assert path.is_file() and path.read_bytes().startswith(b"\xef\xbb\xbf")
    toast.get_by_role("button", name=ui_api.REVEAL_LABEL).click()   # the button of the toast shows the file in the file manager
    until(page, lambda: launched)
    assert str(path) in launched[0] or str(path.parent) in launched[0] or f"/select,{path}" in launched[0]
    export.click()   # a second export in the same minute never overwrites the first
    until(page, lambda: (app.downloads / f"找岗位-前端工程师-杭州-{STAMP}-2.csv").is_file())
    assert path.is_file()
    assert page.evaluate("typeof window.pywebview.api.save_csv") == "undefined"   # no save dialog is involved any more


def test_an_unwritable_downloads_folder_is_explained_not_a_busy_button(page, app):
    open_results(page)
    app.downloads.parent.mkdir(exist_ok=True)
    app.downloads.write_text("a file where the folder should be")
    export = page.get_by_role("button", name="导出表格")
    export.click()
    expect(page.locator("#toast")).to_have_text("没能保存到「下载」文件夹，请检查它的权限")
    expect(export).to_be_enabled()


# ---------------------------------------------------------------- 设置
def test_settings_speed_logins_link_advanced_and_about(page, app, monkeypatch):
    launched = []
    monkeypatch.setattr(ui_api, "subprocess", SimpleNamespace(Popen=launched.append))
    page.get_by_role("link", name="设置").click()
    expect(page.get_by_role("heading", name="设置")).to_be_visible()
    expect(page.get_by_role("button", name="稳妥")).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="较快").click()
    expect(page.locator("#toast")).to_have_text("已切换为“较快”")
    expect(page.get_by_role("button", name="较快")).to_have_attribute("aria-pressed", "true")
    assert json.loads((app.home / "ui_prefs.json").read_text())["preset"] == "fast"

    card = page.locator(".settings .card", has=page.get_by_role("heading", name="平台登录"))   # the accounts live on their own page
    expect(card.locator(".item, .pill")).to_have_count(0)   # no second list of platforms here
    card.get_by_role("link", name="去平台登录").click()
    expect(page).to_have_url(re.compile(r"#/logins$"))
    expect(page.get_by_role("heading", name="平台登录", level=1)).to_be_visible()
    page.get_by_role("link", name="设置").click()

    expect(page.locator("#advanced")).to_be_hidden()   # advanced is collapsed until asked for
    page.get_by_role("button", name="高级设置").click()
    expect(page.locator("#advanced")).to_be_visible()
    expect(page.get_by_role("switch", name="使用代理服务器")).to_have_attribute("aria-checked", "false")
    keys = page.locator("#keys")
    expect(keys).to_contain_text("有些网站需要你自己的密钥才能查找")
    expect(keys.locator(".item", has_text="鱼泡直聘")).to_contain_text("DJS_FAKE_KEY")
    expect(keys.locator(".item", has_text="鱼泡直聘").locator(".pill")).to_have_text("未配置")
    keys.get_by_role("button", name="打开配置文件夹").click()
    until(page, lambda: launched)
    assert launched[0][1:] == [str(app.home)]

    app.mp.setenv("DJS_FAKE_KEY", "topsecretvalue")   # configured: the names stay, the values never reach the page
    page.reload()
    page.evaluate("POLL_MS = 300")
    page.get_by_role("button", name="高级设置").click()
    expect(page.locator("#keys .item", has_text="鱼泡直聘").locator(".pill")).to_have_text("已配置")
    assert "topsecretvalue" not in page.content()

    about = page.locator(".card", has=page.get_by_role("heading", name="关于"))
    expect(about).to_contain_text(str(app.home))
    expect(about).to_contain_text("版本")
    assert page.evaluate(NAMELESS) == []


def test_settings_proxy_list(page, app):
    page.get_by_role("link", name="设置").click()
    page.get_by_role("button", name="高级设置").click()
    switch = page.get_by_role("switch", name="使用代理服务器")
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "true")
    page.get_by_label("代理服务器地址").fill("ftp://nope")
    page.get_by_label("代理服务器地址").press("Enter")
    expect(page.locator("#toast")).to_have_text("代理地址的格式不对，例如 http://127.0.0.1:7890")
    page.get_by_label("代理服务器地址").fill("http://u:pw@127.0.0.1:9")
    page.get_by_role("button", name="添加").click()
    row = page.locator(".item", has_text="127.0.0.1:9")
    expect(row).to_contain_text("http://u:***@127.0.0.1:9")   # the password is never shown
    assert "pw@" not in page.content()
    expect(switch).to_be_disabled()   # it stays on while a proxy is in use
    row.get_by_role("button", name="删除").click()
    expect(row).to_have_count(0)
    expect(switch).to_be_enabled()


# ---------------------------------------------------------------- 平台登录
def steps(*texts):
    """The dialog's numbered steps: the last one is always the same promise that the window closes by itself."""
    every = [*texts, "登录成功后，窗口会自动关闭，这里也会提示你"]
    return [re.compile(rf"^{i + 1}\s*{re.escape(t)}$") for i, t in enumerate(every)]


def login_cards(page):
    return page.locator(".login-card")


def card_of(page, label):
    return page.locator(".login-card", has=page.get_by_role("heading", name=label))


def badge(page):
    return page.locator("#login-badge")


def test_the_nav_has_a_fourth_item_with_a_badge_counting_the_platforms_that_need_a_login(page, app):
    nav = page.get_by_role("navigation", name="主导航").get_by_role("link")
    expect(nav).to_have_text(["查找", "我的结果", "设置", re.compile(r"^平台登录\s*3$")])
    expect(badge(page)).to_have_text("3")   # 智联招聘, BOSS 直聘 and 登录专用站 have a login and none is logged in
    expect(nav_logins(page)).to_have_attribute("aria-label", "平台登录，还有 3 个没登录")
    assert page.evaluate("getComputedStyle(document.querySelector('#login-badge')).backgroundColor") == "rgb(179, 95, 0)"   # orange
    app.auth.sessions.update({"zl": "logged_in", "bs": "expired"})   # an expired login still needs doing
    page.get_by_role("link", name="设置").click()
    expect(badge(page)).to_have_text("2")   # the badge is on every page
    expect(nav_logins(page)).to_have_attribute("aria-label", "平台登录，还有 2 个没登录")
    app.auth.sessions.update({"bs": "logged_in", "lg": "logged_in"})
    page.get_by_role("link", name="查找", exact=True).click()
    expect(badge(page)).to_be_hidden()   # nothing left to do: no badge at all
    expect(nav_logins(page)).not_to_have_attribute("aria-label", re.compile("."))


def test_the_login_page_lists_every_platform_with_its_state_what_it_gives_and_how_to_log_in(page, app):
    app.auth.sessions.update({"zl": "logged_in", "bs": "expired"})
    go_logins(page)
    expect(nav_logins(page)).to_have_attribute("aria-current", "page")
    expect(page.get_by_role("heading", name="平台登录", level=1)).to_be_visible()
    expect(page.locator("#view")).to_contain_text("账号信息不会上传到任何地方")
    expect(login_cards(page).get_by_role("heading")).to_have_text(["智联招聘", "BOSS 直聘", "登录专用站"])   # exactly the ones with a login

    zl, bs, lg = card_of(page, "智联招聘"), card_of(page, "BOSS 直聘"), card_of(page, "登录专用站")
    expect(zl.locator(".pill")).to_have_text("已登录")
    expect(zl.locator(".pill")).to_have_class(re.compile(r"\bok\b"))
    expect(zl).to_contain_text("登录后：能看到更多岗位")
    expect(zl).to_contain_text("登录方式：用微信扫码")
    expect(zl).to_contain_text("上次登录：3 天前")
    expect(zl.get_by_role("button")).to_have_text(["重新检测", "退出登录"])
    expect(bs.locator(".pill")).to_have_text("已过期")
    expect(bs).to_contain_text("登录后：能看到薪资")
    expect(bs).to_contain_text("登录方式：用 BOSS 直聘 App 扫码")
    expect(bs.get_by_role("button")).to_have_text(["登录"])
    expect(lg.locator(".pill")).to_have_text("未登录")
    expect(lg).to_contain_text("登录后：不登录无法搜索")
    expect(lg).to_contain_text("登录方式：输入账号密码")
    expect(lg).not_to_contain_text("上次登录")   # never logged in
    expect(lg.get_by_role("button")).to_have_text(["登录"])
    for state_pill in (bs.locator(".pill"), lg.locator(".pill")):
        expect(state_pill).to_have_class(re.compile(r"\bwarn\b"))
    assert page.evaluate(NAMELESS) == []


def test_the_login_methods_are_told_in_plain_words(page, app):
    spec = lambda name, method: setattr(app.provs[name], "login", session(method, "x"))
    spec("zl", "sms")
    spec("bs", "wechat")
    go_logins(page)
    expect(card_of(page, "智联招聘")).to_contain_text("登录方式：短信验证码")
    expect(card_of(page, "BOSS 直聘")).to_contain_text("登录方式：用微信扫码")


def test_what_a_login_gives_never_says_it_twice(page, app):
    app.provs["zl"].login = session("wechat", "登录后改用电脑版搜索，职位描述随列表带出")   # a text that starts with 登录后 itself
    page.reload()
    page.evaluate("POLL_MS = 300")
    page.get_by_role("button", name="更多网站").click()
    expect(page.locator(".item", has_text="BOSS 直聘")).to_contain_text("登录后：能看到薪资")
    go_logins(page)
    expect(card_of(page, "智联招聘")).to_contain_text("登录后：改用电脑版搜索，职位描述随列表带出")
    expect(card_of(page, "智联招聘")).not_to_contain_text("登录后：登录后")
    expect(card_of(page, "BOSS 直聘")).to_contain_text("登录后：能看到薪资")


def test_there_is_a_plain_page_when_no_platform_has_a_login(page, app):
    for p in app.provs.values():
        p.login = None
    go_logins(page)
    expect(page.get_by_role("heading", name="现在没有需要登录的网站")).to_be_visible()
    expect(badge(page)).to_be_hidden()


def test_the_search_page_invites_logins_until_every_one_is_done(page, app):
    hint = page.locator("#login-hint")
    expect(hint).to_contain_text("登录 3 个平台后，结果更全")
    link = hint.get_by_role("link", name="去登录")
    link.click()
    expect(page).to_have_url(re.compile(r"#/logins$"))
    app.auth.sessions.update({"zl": "logged_in", "bs": "logged_in"})
    page.get_by_role("link", name="查找", exact=True).click()
    expect(hint).to_contain_text("登录 1 个平台后，结果更全")
    app.auth.sessions["lg"] = "logged_in"
    page.get_by_role("link", name="设置").click()
    page.get_by_role("link", name="查找", exact=True).click()
    expect(page.locator("#sites")).to_be_visible()
    expect(hint).to_be_empty()   # all done: no nagging
    assert page.evaluate(NAMELESS) == []


def test_check_again_asks_and_says_what_it_found(page, app):
    app.auth.sessions["zl"] = "logged_in"
    go_logins(page)
    card = card_of(page, "智联招聘")
    card.get_by_role("button", name="重新检测").click()
    expect(page.locator("#toast")).to_have_text("登录状态正常")
    assert app.auth.checks == ["zl"]
    app.auth.sessions["zl"] = "expired"   # the platform has rejected it meanwhile
    page.get_by_role("link", name="设置").click()
    go_logins(page)
    expect(card.locator(".pill")).to_have_text("已过期")
    app.auth.sessions["zl"] = "logged_in"
    page.reload()
    page.evaluate("POLL_MS = 300")
    app.auth.check = lambda p: (_ for _ in ()).throw(httpx.ConnectError("offline"))   # no network
    card.get_by_role("button", name="重新检测").click()
    expect(page.locator("#toast")).to_have_text("网络不通，稍后再试")
    expect(card.get_by_role("button", name="重新检测")).to_be_enabled()
    expect(card.locator(".pill")).to_have_text("已登录")   # not being able to ask changes nothing


def test_check_again_that_finds_a_dead_login_says_so_and_offers_the_login(page, app):
    app.auth.sessions["zl"] = "logged_in"
    go_logins(page)
    card = card_of(page, "智联招聘")
    app.auth.check = lambda p: app.auth.sessions.update({p: "expired"}) or app.auth.status(p)
    card.get_by_role("button", name="重新检测").click()
    expect(page.locator("#toast")).to_have_text("登录已经失效了，请重新登录")
    expect(card.locator(".pill")).to_have_text("已过期")
    expect(card.get_by_role("button")).to_have_text(["登录"])
    expect(badge(page)).to_have_text("3")


def test_log_out_from_the_login_page(page, app):
    app.auth.sessions["bs"] = "logged_in"
    go_logins(page)
    expect(badge(page)).to_have_text("2")
    card_of(page, "BOSS 直聘").get_by_role("button", name="退出登录").click()
    expect(page.locator("#toast")).to_have_text("已退出登录")
    expect(card_of(page, "BOSS 直聘").locator(".pill")).to_have_text("未登录")
    expect(badge(page)).to_have_text("3")


# ---------------------------------------------------------------- the login dialog
def test_login_dialog_waiting_cancel_success(page, app):
    go_logins(page)
    only = card_of(page, "登录专用站")
    only.get_by_role("button", name="登录").click()
    modal = page.locator("#modal")
    expect(modal.get_by_role("dialog")).to_be_visible()
    expect(modal.locator("h2")).to_have_text("登录 登录专用站")
    expect(modal.locator(".state.waiting")).to_be_visible()
    assert page.evaluate(SPIN_SIZES) == [[32, 32]]   # the dialog's own, larger spinner
    expect(modal.locator(".steps li")).to_have_text(steps("在弹出的窗口里找到登录框", "输入你的账号和密码，完成登录"))
    expect(modal.locator("#count")).to_have_text(re.compile(r"^[0-3]:\d\d$"))
    expect(modal).to_contain_text("账号信息不会上传到任何地方")
    assert page.evaluate(NAMELESS) == []
    until(page, lambda: app.auth.started.is_set())
    modal.get_by_role("button", name="取消").click()   # cancel: the dialog closes AND the window on the screen is told to close
    expect(modal).to_be_hidden()
    until(page, lambda: app.auth.cancels == 1)
    until(page, lambda: not app.auth.cancelled.is_set())   # the login thread ended by being cancelled
    assert only.locator(".pill").inner_text() == "未登录" and ui_api.login_state == {}   # no failure left behind either
    app.auth.started.clear()
    only.get_by_role("button", name="登录").click()   # and a new login starts fresh
    expect(modal.locator(".state.waiting")).to_be_visible()
    until(page, lambda: app.auth.started.is_set())
    page.keyboard.press("Escape")   # Escape is the same as cancel
    expect(modal).to_be_hidden()
    until(page, lambda: app.auth.cancels == 2)
    until(page, lambda: not app.auth.cancelled.is_set())
    app.auth.started.clear()
    only.get_by_role("button", name="登录").click()
    app.auth.release.set()
    expect(modal.locator(".state.ok")).to_be_visible()
    expect(modal).to_contain_text("登录成功")
    expect(page.locator("#toast")).to_have_text("已登录 登录专用站")
    expect(modal).to_be_hidden()   # it closes itself
    expect(only.locator(".pill")).to_have_text("已登录")   # and the page behind it already knows
    expect(badge(page)).to_have_text("2")


def test_every_method_gets_its_own_steps_in_the_dialog(page, app):
    go_logins(page)
    for label, texts in (("BOSS 直聘", ["在弹出的窗口里找到二维码", "打开手机上的BOSS 直聘 App，扫一扫并确认登录"]),
                         ("智联招聘", ["在弹出的窗口里找到二维码", "打开手机上的微信，扫一扫并确认登录"])):
        app.auth.started.clear()
        card_of(page, label).get_by_role("button", name="登录").click()
        expect(page.locator("#modal h2")).to_have_text(f"登录 {label}")
        expect(page.locator("#modal .steps li")).to_have_text(steps(*texts))
        until(page, lambda: app.auth.started.is_set())
        page.locator("#modal").get_by_role("button", name="取消").click()
        until(page, lambda: not app.auth.cancelled.is_set())


def test_login_dialog_failures_say_why_and_retry(page, app):
    app.auth.error = "180 秒内没有登录成功，请再试一次"
    go_logins(page)
    only = card_of(page, "登录专用站")
    only.get_by_role("button", name="登录").click()
    app.auth.release.set()
    modal = page.locator("#modal")
    expect(modal.locator(".state.failed")).to_contain_text("登录没有成功")
    expect(modal).to_contain_text("等了三分钟还没有登录成功，请再试一次")
    expect(modal).not_to_contain_text("Chrome")
    app.auth.error = "打不开浏览器，没法登录。No browser found. Install Google Chrome or Microsoft Edge"
    app.auth.release.clear()
    modal.get_by_role("button", name="重试").click()
    expect(modal.locator(".state.waiting")).to_be_visible()
    app.auth.release.set()
    expect(modal.locator(".state.failed")).to_contain_text("需要先安装浏览器组件")
    expect(modal).to_contain_text("请先安装 Google Chrome 或 Microsoft Edge 浏览器，再重新登录")   # the one thing the user has to do
    assert page.evaluate(NAMELESS) == []
    app.auth.error = "正在使用浏览器，请稍后再试"
    app.auth.release.clear()
    modal.get_by_role("button", name="重试").click()
    app.auth.release.set()
    expect(modal.locator(".state.failed")).to_contain_text("浏览器正忙")
    expect(modal).to_contain_text("这个网站正在查找，等它结束后再点“重试”")
    app.auth.error = "登录窗口被关闭了，请重新登录"
    app.auth.release.clear()
    modal.get_by_role("button", name="重试").click()
    app.auth.release.set()
    expect(modal.locator(".state.failed")).to_contain_text("登录窗口被关掉了，请再试一次")
    modal.get_by_role("button", name="关闭").click()
    expect(modal).to_be_hidden()
    expect(only.locator(".pill")).to_have_text("未登录")
    assert app.auth.cancels == 0   # closing a finished dialog cancels nothing


def test_a_site_waiting_for_login_can_log_in_from_its_search_card(page, app):
    service.crawl_start(db.connect(), "lg", keyword="运营")
    page.get_by_role("link", name="设置").click()
    page.get_by_role("link", name="查找", exact=True).click()
    card = newest_card(page)
    expect(card.locator(".pill")).to_have_text("等你登录")
    note = card.locator(".note.warn")
    expect(note).to_contain_text("登录专用站 · 需要先登录")
    expect(badge(page)).to_have_text("3")
    note.get_by_role("button", name="去登录").click()
    app.auth.release.set()
    expect(page.locator("#toast")).to_have_text("已登录 登录专用站")
    expect(card.locator(".pill")).to_have_text("已完成", timeout=20000)   # the waiting task resumed by itself
    expect(badge(page)).to_have_text("2")   # the search page already shows the new login


def test_the_reasons_follow_the_login_state(page, app):
    lacking(app)
    open_results(page, keyword=INCOMPLETE)
    expect(page.locator("#incomplete")).to_contain_text("需登录才能看到薪资 3")
    app.auth.sessions["zl"] = "logged_in"   # logged in now: the same jobs lack the salary for another reason
    page.get_by_role("link", name="查找", exact=True).click()
    page.get_by_role("link", name="查看结果：缺信息 · 杭州").click()
    expect(page.locator("#incomplete")).to_contain_text("平台未提供薪资 6")
    expect(page.locator("#incomplete")).not_to_contain_text("需登录才能看到薪资")


# ---------------------------------------------------------------- the bridge, accessibility, size
def test_a_dead_bridge_shows_a_banner_not_a_blank_page(browser, app):
    app.bridge_down = True
    page = open_page(browser, app)
    expect(page.locator("#banner")).to_contain_text("和程序的连接断开了")
    app.bridge_down = False
    page.get_by_role("link", name="设置").click()
    expect(page.get_by_role("heading", name="设置")).to_be_visible()
    expect(page.locator("#banner")).to_be_hidden()
    page.context.close()


def test_every_control_has_a_name_in_every_state(page, app):
    names = lambda: page.evaluate(NAMELESS)
    page.get_by_role("button", name="更多网站").click()
    assert names() == []
    finish_search(page)
    assert names() == []
    picker_summary(page).click()
    assert names() == []
    page.get_by_role("link", name="我的结果").click()
    expect(page.locator(JOBS)).to_have_count(30)
    assert names() == []
    page.locator(JOBS).first.get_by_role("button").click()
    expect(page.locator("#drawer-title")).to_be_visible()
    assert names() == []
    page.keyboard.press("Escape")
    go_logins(page)
    assert names() == []
    page.get_by_role("link", name="设置").click()
    page.get_by_role("button", name="高级设置").click()
    page.get_by_role("switch", name="使用代理服务器").click()
    assert names() == []


@pytest.mark.parametrize("size", [(960, 640), (1280, 860), (700, 800)])
def test_the_search_cards_button_stays_in_view_even_when_the_card_is_tall(browser, app, size):
    lacking(app)   # two sites found jobs that all lack something: the newest card carries two notes under its sites
    page = open_page(browser, app, size)
    finish_search(page, keyword=INCOMPLETE)
    card = newest_card(page)
    expect(card.locator(".notes .note")).to_have_count(2)
    link = card.get_by_role("link", name=re.compile("查看结果"))
    box = link.bounding_box()
    assert box["y"] + box["height"] <= size[1] and page.evaluate("scrollY") == 0   # on the first screen, nothing cut off
    expect(link).to_be_in_viewport(ratio=1)
    assert box["x"] >= 0 and box["x"] + box["width"] <= size[0]
    link.click()
    expect(page.locator("#what")).to_have_text("缺信息 · 杭州")
    page.context.close()


@pytest.mark.parametrize("size", [(960, 640), (1280, 860)])
def test_the_search_card_is_whole_on_the_first_screen_while_it_has_no_notes(browser, app, size):
    page = open_page(browser, app, size)
    finish_search(page)
    box = newest_card(page).bounding_box()
    assert box["y"] + box["height"] <= size[1] and page.evaluate("scrollY") == 0
    page.context.close()


@pytest.mark.parametrize("size, columns", [((960, 640), 2), ((1280, 860), 3), ((1440, 900), 3), ((700, 800), 1)])
def test_no_horizontal_scroll_and_the_grid_follows_the_width(browser, app, size, columns):
    long_word = {"id": 800, "title": "超长岗位名称" * 12 + "A" * 80, "company": "超长公司名" * 10 + "B" * 60, "city": "杭州" * 30,
                 "salary": "1" * 50, "jd": "说明" * 100 + "C" * 150}   # text without a place to wrap must not widen the page
    app.data["nc"][KEYWORD].insert(0, long_word)
    first = open_page(browser, app)
    finish_search(first, keyword=KEYWORD)
    finish_search(first, keyword="很长的查找关键词" * 8 + "D" * 60)
    first.context.close()
    results_hash = "#/results?" + urlencode({"k": KEYWORD, "c": CITY})
    for hash in ("#/search", results_hash, "#/settings", "#/logins"):
        page = open_page(browser, app, size, hash)
        if hash == "#/search":
            picker_summary(page).click()
            page.get_by_role("button", name="更多网站").click()
        if hash == "#/settings":
            page.get_by_role("button", name="高级设置").click()
        if hash == results_hash:
            expect(page.locator(JOBS)).to_have_count(30)
            assert page.evaluate("getComputedStyle(document.querySelector('.jobs')).gridTemplateColumns.split(' ').length") == columns
            page.locator("#filters").get_by_role("button", name="牛客网").click()
            show_incomplete(page)   # the long salary has no figures: it is only listed on request
            expect(page.locator("#more")).to_contain_text("已显示 26 / 26 个")
            page.locator(JOBS).filter(has_text="超长岗位名称").get_by_role("button").click()   # the drawer must not overflow either
            expect(page.locator("#drawer-text")).to_contain_text("说明说明")
            assert page.evaluate("document.querySelector('.drawer').scrollWidth <= document.querySelector('.drawer').clientWidth")
            page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (size, hash)
        page.context.close()
