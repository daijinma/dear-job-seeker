"""The real page in headless Chromium, talking to the real Api over a shim of the pywebview bridge. Temp database,
fake platforms behind httpx.MockTransport, real per-platform workers: no network, no native window. Skipped when
Playwright's Chromium is not installed."""
from __future__ import annotations

import inspect
import itertools
import json
import re
import threading
from collections import Counter
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from djs import db, egress, engine, providers, results, service, taskqueue, ui_api, ui_app
from djs.providers.base import Blocked, Provider, get_json, need

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
  save_csv: (name, text) => window.__save_csv(name, text),
}};"""
KEYWORD, CITY = "前端工程师", "杭州"
JOBS = ".job:not(.skeleton)"
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
                "salary_text": job.get("salary"), "description": job.get("jd") or (detail or {}).get("jd")}

    def detail(self, client, job):
        if self.detail_hop:
            return get_json(client.get(f"https://fake.test/{self.name}/detail", params={"id": job["id"]}))


def site(name, label, **attrs):
    return type(name, (FakeSite,), {"name": name, "label": label, **attrs})()


def make_providers():
    return {p.name: p for p in (
        site("nc", "牛客网"), site("gp", "国聘", detail_hop=True), site("xh", "新华英才"), site("zl", "智联招聘"),
        site("zz", "中智招聘"),
        site("tc", "58同城", requires_city=True),
        site("yp", "鱼泡直聘", requires_env=("DJS_FAKE_KEY",)),
        site("bs", "BOSS 直聘", auth="optional"),
        site("lg", "登录专用站", auth="login"))}


class FakeAuth:
    """Cookie store plus a login the test releases by hand (`release`), which succeeds or fails with `error`."""

    def __init__(self):
        self.cookies, self.error = None, None
        self.release, self.started = threading.Event(), threading.Event()

    def load_cookies(self, p):
        return self.cookies

    def invalidate(self, p):
        self.cookies = None

    def logged_in(self, p):
        return self.cookies is not None

    def status(self, p):
        return {"logged_in": self.logged_in(p)}

    def login(self, p, timeout=180):
        self.started.set()
        self.release.wait(30)
        if self.error:
            raise RuntimeError(self.error)
        self.cookies = {"s": "1"}
        return {"logged_in": True}

    def logout(self, p):
        self.cookies = None


def test_the_fake_auth_covers_everything_the_engine_and_service_ask_of_auth():
    """The page tests stand in for djs.auth; when the engine or service starts using another function of it, fail here
    with its name instead of with a login that never completes."""
    asked = set(re.findall(r"\bauth\.(\w+)", inspect.getsource(engine) + inspect.getsource(service)))
    assert asked and asked <= set(dir(FakeAuth)), asked - set(dir(FakeAuth))


def jobs(name, n, start=1, **extra):
    cities = ["杭州", "杭州", "上海", "杭州·滨江区", "杭州·余杭区"]
    pay = ["15-25K", "20-35K", None, "25-40K", "200元/天", "18-30K·14薪"]
    return [{"id": start + i, "title": f"{name}前端开发工程师 {start + i}", "company": f"示例科技有限公司 {chr(65 + i % 26)}",
             "city": cities[i % len(cities)], "salary": pay[i % len(pay)], **extra} for i in range(n)]


def boot(mp, home):
    """Everything the page talks to: the temp data folder, fake platforms, their fake network and the workers."""
    mp.setenv("DJS_HOME", str(home))
    mp.delenv("DJS_FAKE_KEY", raising=False)
    provs, auth = make_providers(), FakeAuth()
    mp.setattr(providers, "get", lambda n: provs[n] if n in provs else (_ for _ in ()).throw(KeyError(n)))
    mp.setattr(providers, "names", lambda: list(provs))
    for mod in (engine, service):
        mp.setattr(mod, "auth", auth)
    app = SimpleNamespace(provs=provs, auth=auth, home=home, mp=mp, gates={}, blocked=set(), broken=set(), saved=[],
                          requests=[], calls=Counter(), bridge_down=False, js_errors=[],
                          data={"nc": {KEYWORD: jobs("牛客", 25, jd="1. 负责前端开发\n2. 熟悉 Vue / React")},
                                "gp": {KEYWORD: jobs("国聘", 60, 100)}, "xh": {},
                                "zl": {KEYWORD: jobs("智联", 40, 300)}, "zz": {KEYWORD: jobs("中智", 3, 500)}})

    def handler(request):
        name = request.url.path.split("/")[1]
        app.requests.append(request.url.path)
        if name in app.gates:
            app.gates[name].wait(20)   # a platform the test holds back
        if name in app.blocked:
            return httpx.Response(403)
        if request.url.path.endswith("/detail"):
            if name in app.broken:
                return httpx.Response(500)
            return httpx.Response(200, json={"jd": f"岗位职责：\n1. 负责前端页面开发（编号 {request.url.params['id']}）\n2. 与设计、后端协作\n\n任职要求：\n熟悉 Vue / React"})
        items = app.data.get(name, {}).get(request.url.params["q"], [])
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"items": items[(page - 1) * 20: page * 20]})

    real = egress.make_client
    mp.setattr(egress, "make_client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    engine.start_worker()
    return app


def shutdown(app):
    for gate in app.gates.values():
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
    page.expose_function("__save_csv", lambda name, text: app.saved.append((name, text)) or {"saved": f"/Users/me/{name}"})
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


def finish_search(page, **kw):
    start_search(page, **kw)
    expect(newest_card(page).locator(".pill")).to_have_text("已完成")
    return newest_card(page)


def hold(app, *names):
    for name in names:
        app.gates[name] = threading.Event()


def release(app, *names):
    for name in names:
        app.gates[name].set()


def only_text(locator):
    return [t.strip() for t in locator.all_inner_texts()]


# ---------------------------------------------------------------- 查找
def test_first_run_search_page(page, app):
    expect(page.get_by_role("heading", name="想找什么工作？")).to_be_visible()
    expect(page.get_by_label("岗位或关键词", exact=True)).to_be_focused()
    expect(page.locator(".first-run")).to_contain_text("还没有查找记录")
    common = page.locator("#sites .chips .chip")
    expect(common).to_have_text(["牛客网", "国聘", "新华英才", "智联招聘", "中智招聘"])   # public, no key, no city needed
    for chip in common.all():
        expect(chip).to_have_attribute("aria-pressed", "true")   # all selected by default
    expect(page.get_by_role("button", name="更多网站")).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#more")).to_be_hidden()
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
    expect(page.locator("#msg")).to_have_text("请至少选择一个网站")
    assert page.evaluate(NAMELESS) == []


def test_more_sites_are_grouped_explained_and_gated(page, app):
    more = page.get_by_role("button", name="更多网站")
    more.click()
    expect(more).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#more h3")).to_have_text(["登录后看得更全", "需要填城市", "需要先配置密钥"])
    boss = page.locator(".item", has_text="BOSS 直聘")
    expect(boss).to_contain_text("会弹出一个浏览器窗口；登录后能看到薪资")
    expect(boss.get_by_role("button", name="去登录")).to_be_visible()   # inside the item, not a loose link next to it
    expect(boss.get_by_role("button", name="BOSS 直聘")).to_have_attribute("aria-pressed", "false")   # works anonymously
    only = page.locator(".item", has_text="登录专用站")
    expect(only).to_have_class(re.compile(r"\boff\b"))
    expect(only.get_by_role("button", name="去登录")).to_be_visible()
    expect(only.locator("button[aria-pressed]")).to_have_count(0)   # cannot be ticked before the login
    keyed = page.locator(".item", has_text="鱼泡直聘")
    expect(keyed).to_contain_text("还没配置密钥")
    expect(keyed.locator("button[aria-pressed]")).to_have_count(0)
    assert page.evaluate(NAMELESS) == []

    city_site = page.locator(".item", has_text="58同城")   # needs a city: refuses without one, starts with one
    city_site.get_by_role("button", name="58同城").click()
    expect(city_site.get_by_role("button", name="58同城")).to_have_attribute("aria-pressed", "true")
    page.get_by_label("岗位或关键词", exact=True).fill(KEYWORD)
    page.get_by_role("button", name="开始查找").click()
    expect(page.locator("#msg")).to_have_text("58同城需要先填写城市")
    assert taskqueue.list_(db.connect()) == []
    page.get_by_label("城市", exact=True).fill(CITY)
    page.get_by_role("button", name="开始查找").click()
    expect(page.locator(".search-card")).to_have_count(1)
    assert {t["provider"] for t in taskqueue.list_(db.connect())} == {"nc", "gp", "xh", "zl", "zz", "tc"}

    keyed.get_by_role("button", name="如何配置").click()   # leads to the keys help in Settings
    expect(page.locator("#keys")).to_be_visible()
    expect(page.locator("#keys")).to_contain_text("DJS_FAKE_KEY")


def test_search_shows_progress_then_the_finished_card(page, app):
    hold(app, "zl", "zz")
    start_search(page)
    card = newest_card(page)
    expect(card.locator("h3")).to_have_text("前端工程师 · 杭州")   # on screen the moment the search starts
    expect(card.locator(".pill")).to_have_text("查找中")
    expect(card.locator(".dot.ok")).to_have_count(2)
    expect(card.locator(".dot.run")).to_have_count(2)
    expect(card.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "3")
    expect(card.get_by_role("progressbar")).to_have_attribute("aria-valuemax", "5")
    expect(card).to_contain_text("已完成 3 / 5 个网站 · 已找到 85 个岗位")
    nothing = card.locator(".dot", has_text="新华英才")   # zero results are a plain fact, never an alarm
    expect(nothing).to_contain_text("没有找到")
    expect(nothing).not_to_have_class(re.compile(r"warn|bad"))
    assert all(t["payload"]["detail"] is False for t in taskqueue.list_(db.connect()))   # quick mode
    release(app, "zl", "zz")
    expect(card.locator(".pill")).to_have_text("已完成")
    expect(card).to_contain_text("已完成 5 / 5 个网站 · 已找到 128 个岗位")
    expect(card.locator(".dot.run")).to_have_count(0)
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


# ---------------------------------------------------------------- 我的结果
def test_results_filter_sort_load_more_and_export(page, app):
    finish_search(page)
    page.get_by_role("link", name="我的结果").click()
    expect(page.locator(JOBS)).to_have_count(30)
    expect(page.locator("#more")).to_contain_text("已显示 30 / 128 个")
    chips = page.locator("#filters .chip")
    expect(chips).to_have_text(["全部 128", "牛客网 25", "国聘 60", "智联招聘 40", "中智招聘 3"])   # no chip for the site with 0

    page.locator("#filters").get_by_role("button", name="国聘").click()
    expect(page.locator("#filters").get_by_role("button", name="国聘")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#more")).to_contain_text("已显示 30 / 60 个")
    expect(page.locator(".job .tags .pill:not(.gray)")).to_have_text(["国聘"] * 30)

    page.get_by_role("button", name="导出表格").click()   # every job of the filter, not only the 30 on screen
    expect(page.locator("#toast")).to_contain_text("已保存到：")
    (name, text), = app.saved
    lines = text.split("\r\n")
    assert name == "前端工程师-杭州.csv" and lines[0] == '\ufeff"岗位","公司","薪资","城市","网站","链接"'
    assert len(lines) == 1 + 60 and all(line.startswith('"国聘前端开发工程师 ') and ',"国聘",' in line for line in lines[1:])
    assert {line.rsplit(",", 1)[1] for line in lines[1:]} == {f'"https://fake.test/gp/job/{i}"' for i in range(100, 160)}
    by_title = {line.split(",")[0]: line for line in lines[1:]}   # the salary column: the platform's wording, or empty
    assert by_title['"国聘前端开发工程师 100"'] == '"国聘前端开发工程师 100","示例科技有限公司 A","15-25K","杭州","国聘","https://fake.test/gp/job/100"'
    assert by_title['"国聘前端开发工程师 102"'] == '"国聘前端开发工程师 102","示例科技有限公司 C","","上海","国聘","https://fake.test/gp/job/102"'
    expect(page.locator(".job .salary.none").first).to_have_text("薪资未公开")   # and on the cards
    expect(page.locator(".job .salary:not(.none)").first).to_have_text(re.compile(r"\d"))

    page.get_by_role("button", name="加载更多").click()
    expect(page.locator(JOBS)).to_have_count(60)
    expect(page.get_by_role("button", name="加载更多")).to_have_count(0)
    expect(page.locator("#more")).to_contain_text("已显示 60 / 60 个")

    page.get_by_role("button", name="公司", exact=True).click()
    expect(page.locator(JOBS).first.locator(".sub")).to_have_text("示例科技有限公司 A")
    page.locator("#filters").get_by_role("button", name="全部").click()
    app.saved.clear()
    page.get_by_role("button", name="导出表格").click()
    until(page, lambda: app.saved)
    assert len(app.saved[0][1].split("\r\n")) == 1 + 128


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

    conn = db.connect()
    task = lambda provider: conn.execute("SELECT id FROM tasks WHERE provider=? ORDER BY id DESC", (provider,)).fetchone()[0]
    taskqueue.dead(conn, task("zl"), "Blocked: HTTP 403")
    taskqueue.dead(conn, task("zz"), "missing environment variable(s): DJS_X")
    service.crawl_start(conn, "lg", keyword="运营", detail=False)   # a login-only site, nobody is logged in
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
        service.crawl_start(db.connect(), "nc", keyword=f"另一个很长的查找关键词 {i}", detail=False)
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


# ---------------------------------------------------------------- the detail drawer
def test_drawer_stored_and_lazy_description(page, app, monkeypatch):
    opened = []
    monkeypatch.setattr(ui_api.webbrowser, "open", opened.append)
    finish_search(page)
    page.get_by_role("link", name="我的结果").click()
    flt = page.locator("#filters")

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

    flt.get_by_role("button", name="国聘").click()   # no description yet: the platform is asked once, with a visible wait
    expect(page.locator("#more")).to_contain_text("已显示 30 / 60 个")
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
    assert asked == 1 and sum(p.endswith("/detail") for p in app.requests) == 1

    app.broken.add("gp")   # a failing platform: plain Chinese, and a retry that works once it is back
    page.keyboard.press("Escape")
    page.locator(JOBS).nth(1).get_by_role("button").click()
    expect(page.locator("#drawer-status")).to_contain_text("没成功，可以稍后重试")
    app.broken.clear()
    page.get_by_role("button", name="重试").click()
    expect(page.locator("#drawer-text")).to_contain_text("负责前端页面开发")
    expect(page.locator("#drawer-status")).to_be_empty()


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
    page.get_by_role("link", name="设置").click()
    page.get_by_role("button", name="去登录").first.click()
    expect(page.locator("#modal .state.waiting")).to_be_visible()
    for _ in range(10):
        page.keyboard.press("Tab")
        assert inside("modal")


def test_a_job_without_any_text_says_so(page, app):
    app.data["gp"]["没有介绍"] = jobs("国聘", 1, 700)
    app.provs["gp"].detail = lambda client, job: None   # the platform has nothing more to say
    finish_search(page, keyword="没有介绍")
    page.get_by_role("link", name="我的结果").click()
    page.locator(JOBS).first.get_by_role("button").click()
    expect(page.locator("#drawer-status")).to_contain_text("这个岗位没有提供更详细的介绍")
    expect(page.locator("#drawer-text")).to_be_hidden()
    expect(page.get_by_role("button", name="打开原页面")).to_be_visible()


# ---------------------------------------------------------------- nothing in a job may run
EVIL = {"id": 900, "title": '<img src=x onerror="window.__pwned=1">', "company": '"><script>window.__pwned=2</script>',
        "city": "<svg onload=window.__pwned=3>", "salary": "<b onmouseover=window.__pwned=4>1</b>",
        "jd": '<img src=x onerror="window.__pwned=5"><script>window.__pwned=6</script>危险<br>第二行',
        "url": "javascript:window.__pwned=7"}
FORMULA = {"id": 901, "title": '=HYPERLINK("http://evil.test","x")', "company": "+cmd", "city": "@x", "salary": "-1"}


def test_job_text_never_becomes_markup_or_script(page, app, monkeypatch):
    opened = []
    monkeypatch.setattr(ui_api.webbrowser, "open", opened.append)
    assert results.plain_text(EVIL["jd"]) == "危险\n第二行"
    app.data["nc"]["安全"] = [EVIL, FORMULA]
    finish_search(page, keyword="安全")
    page.get_by_role("link", name="我的结果").click()
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
    page.locator("#drawer-text").hover()
    page.get_by_role("button", name="打开原页面").click()   # a javascript: link is refused by the bridge
    expect(page.locator("#toast")).to_have_text("只能打开网页链接")
    assert opened == []
    assert page.evaluate("window.__pwned") is None
    for selector in ("img", "iframe", "[onerror]", "[onload]", "[onmouseover]", "#view script", "#drawer script"):
        assert page.locator(selector).count() == 0, selector
    assert page.locator("script").count() == 1   # only the app's own

    page.keyboard.press("Escape")   # the spreadsheet formula guard in the export
    page.get_by_role("button", name="导出表格").click()
    until(page, lambda: app.saved)
    csv = app.saved[0][1]
    assert '"\'=HYPERLINK(""http://evil.test"",""x"")"' in csv and '"\'+cmd"' in csv and '"\'@x"' in csv and '"\'-1"' in csv


def test_typed_keywords_are_text_too(page, app):
    evil = '<img src=x onerror="window.__pwned=8"><script>window.__pwned=9</script>'
    start_search(page, keyword=evil, city="")
    expect(newest_card(page).locator("h3")).to_have_text(evil + " · 全国")
    page.get_by_role("link", name="我的结果").click()
    expect(page.locator("#what")).to_have_text(evil + " · 全国")
    expect(page.locator("#strip a.chip.on")).to_contain_text(evil)
    assert page.evaluate("window.__pwned") is None and page.locator("img").count() == 0 and page.locator("script").count() == 1


# ---------------------------------------------------------------- 设置
def test_settings_speed_accounts_advanced_and_about(page, app, monkeypatch):
    launched = []
    monkeypatch.setattr(ui_api, "subprocess", SimpleNamespace(Popen=launched.append))
    page.get_by_role("link", name="设置").click()
    expect(page.get_by_role("heading", name="设置")).to_be_visible()
    expect(page.get_by_role("button", name="稳妥")).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="较快").click()
    expect(page.locator("#toast")).to_have_text("已切换为“较快”")
    expect(page.get_by_role("button", name="较快")).to_have_attribute("aria-pressed", "true")
    assert json.loads((app.home / "ui_prefs.json").read_text())["preset"] == "fast"

    accounts = page.locator(".item", has_text="BOSS 直聘")   # accounts are generated from the sites that can use a login
    expect(accounts).to_contain_text("登录后能看到薪资")
    expect(accounts.locator(".pill")).to_have_text("未登录")
    expect(page.locator(".item", has_text="登录专用站")).to_contain_text("要先登录才能使用")
    app.auth.cookies = {"s": "1"}
    page.reload()
    page.evaluate("POLL_MS = 300")
    expect(accounts.locator(".pill")).to_have_text("已登录")
    accounts.get_by_role("button", name="退出登录").click()
    expect(page.locator("#toast")).to_have_text("已退出登录")
    expect(accounts.locator(".pill")).to_have_text("未登录")

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
    expect(page.locator("#toast")).to_have_text("代理地址要以 http:// 或 https:// 开头")
    page.get_by_label("代理服务器地址").fill("http://u:pw@127.0.0.1:9")
    page.get_by_role("button", name="添加").click()
    row = page.locator(".item", has_text="127.0.0.1:9")
    expect(row).to_contain_text("http://u:***@127.0.0.1:9")   # the password is never shown
    assert "pw@" not in page.content()
    expect(switch).to_be_disabled()   # it stays on while a proxy is in use
    row.get_by_role("button", name="删除").click()
    expect(row).to_have_count(0)
    expect(switch).to_be_enabled()


# ---------------------------------------------------------------- the login dialog
def test_login_dialog_waiting_cancel_success(page, app):
    page.get_by_role("link", name="设置").click()
    only = page.locator(".item", has_text="登录专用站")
    only.get_by_role("button", name="去登录").click()
    modal = page.locator("#modal")
    expect(modal.get_by_role("dialog")).to_be_visible()
    expect(modal.locator("h2")).to_have_text("登录 登录专用站")
    expect(modal.locator(".state.waiting")).to_be_visible()
    expect(modal.locator("#count")).to_have_text(re.compile(r"^[0-3]:\d\d$"))
    expect(modal).to_contain_text("账号信息不会上传到任何地方")
    assert page.evaluate(NAMELESS) == []
    until(page, lambda: app.auth.started.is_set())
    modal.get_by_role("button", name="取消").click()
    expect(modal).to_be_hidden()
    only.get_by_role("button", name="去登录").click()   # the login behind it is still running: the dialog attaches to it
    expect(modal.locator(".state.waiting")).to_be_visible()
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()
    only.get_by_role("button", name="去登录").click()
    app.auth.release.set()
    expect(modal.locator(".state.ok")).to_be_visible()
    expect(modal).to_contain_text("登录成功")
    expect(page.locator("#toast")).to_have_text("已登录 登录专用站")
    expect(modal).to_be_hidden()   # it closes itself
    expect(only.locator(".pill")).to_have_text("已登录")   # and the page behind it already knows


def test_login_dialog_failures_say_why_and_retry(page, app):
    app.auth.error = "login not completed within 180s"
    page.get_by_role("link", name="设置").click()
    only = page.locator(".item", has_text="登录专用站")
    only.get_by_role("button", name="去登录").click()
    app.auth.release.set()
    modal = page.locator("#modal")
    expect(modal.locator(".state.failed")).to_contain_text("登录没有成功")
    expect(modal).to_contain_text("等了三分钟还没有登录成功，请再试一次")
    expect(modal).not_to_contain_text("playwright install")
    app.auth.error = "Playwright/Chromium missing. Run: pip install playwright && playwright install chromium (Error)"
    app.auth.release.clear()
    modal.get_by_role("button", name="重试").click()
    expect(modal.locator(".state.waiting")).to_be_visible()
    app.auth.release.set()
    expect(modal.locator(".state.failed")).to_contain_text("需要先安装浏览器组件")
    expect(modal).to_contain_text("playwright install chromium")   # the one thing the user has to do
    assert page.evaluate(NAMELESS) == []
    modal.get_by_role("button", name="关闭").click()
    expect(modal).to_be_hidden()
    expect(only.locator(".pill")).to_have_text("未登录")


def test_a_site_waiting_for_login_can_log_in_from_its_search_card(page, app):
    service.crawl_start(db.connect(), "lg", keyword="运营", detail=False)
    page.get_by_role("link", name="设置").click()
    page.get_by_role("link", name="查找", exact=True).click()
    card = newest_card(page)
    expect(card.locator(".pill")).to_have_text("等你登录")
    note = card.locator(".note.warn")
    expect(note).to_contain_text("登录专用站 · 需要先登录")
    note.get_by_role("button", name="去登录").click()
    app.auth.release.set()
    expect(page.locator("#toast")).to_have_text("已登录 登录专用站")
    expect(card.locator(".pill")).to_have_text("已完成", timeout=20000)   # the waiting task resumed by itself


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
    page.get_by_role("link", name="我的结果").click()
    expect(page.locator(JOBS)).to_have_count(30)
    assert names() == []
    page.locator(JOBS).first.get_by_role("button").click()
    expect(page.locator("#drawer-title")).to_be_visible()
    assert names() == []
    page.keyboard.press("Escape")
    page.get_by_role("link", name="设置").click()
    page.get_by_role("button", name="高级设置").click()
    page.get_by_role("switch", name="使用代理服务器").click()
    assert names() == []


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
    for hash in ("#/search", results_hash, "#/settings"):
        page = open_page(browser, app, size, hash)
        if hash == "#/search":
            page.get_by_role("button", name="更多网站").click()
        if hash == "#/settings":
            page.get_by_role("button", name="高级设置").click()
        if hash == results_hash:
            expect(page.locator(JOBS)).to_have_count(30)
            assert page.evaluate("getComputedStyle(document.querySelector('.jobs')).gridTemplateColumns.split(' ').length") == columns
            page.locator("#filters").get_by_role("button", name="牛客网").click()
            expect(page.locator("#more")).to_contain_text("已显示 26 / 26 个")
            page.locator(JOBS).filter(has_text="超长岗位名称").get_by_role("button").click()   # the drawer must not overflow either
            expect(page.locator("#drawer-text")).to_contain_text("说明说明")
            assert page.evaluate("document.querySelector('.drawer').scrollWidth <= document.querySelector('.drawer').clientWidth")
            page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (size, hash)
        page.context.close()
