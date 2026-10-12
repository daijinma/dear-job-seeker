import json
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from djs import auth, browser, db, egress, providers, service, taskqueue, ui_api, ui_app
from provider_fakes import job


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    for var in ("DJS_JOB58_LUNA_SALT", "DJS_YUPAO_NONCE_KEY", "DJS_YUPAO_SIGN_SALT", "DJS_JOB51_HMAC_KEY"):
        monkeypatch.delenv(var, raising=False)
    return ui_app.Api().call


def add_job(conn, task_ids, job_id=1, provider="nowcoder", **cols):
    cols = {"title": "T", "description": None, **cols}
    conn.execute(f"INSERT INTO jobs(id, provider, source_job_id, {', '.join(cols)}) VALUES (?,?,?,{','.join('?' * len(cols))})",
                 (job_id, provider, f"s{job_id}", *cols.values()))
    conn.executemany("INSERT INTO task_jobs VALUES (?,?)", [(t, job_id) for t in task_ids])


# ---- the page ----
def test_page_is_one_self_contained_document(call):
    html = ui_app.page()
    assert "<title>" in html and "<style>" in html and html.count("<script>") == 1
    assert not re.search(r"app\.(css|js)|<script src|<link|<!-- scripts -->|(src|href)=\"https?:", html)
    ui_dir = Path(ui_app.__file__).with_name("ui")
    assert set(ui_app.SCRIPTS) == {p.name for p in ui_dir.glob("*.js")}   # no script left out, none left over
    where = [html.index((ui_dir / name).read_text(encoding="utf-8")) for name in ui_app.SCRIPTS]
    assert where == sorted(where)   # every script is inlined whole, in the listed order


def test_unexpected_failures_reach_the_window_as_plain_chinese(call, monkeypatch, capsys):
    def boom(conn, a):
        raise RuntimeError("secret internals: sqlite3.OperationalError")
    monkeypatch.setitem(ui_api.ROUTES, "GET /api/about", boom)
    assert call("GET /api/about") == {"error": "出了点问题，请稍后再试"}   # no exception names, no internals
    assert "secret internals" in capsys.readouterr().err   # the detail goes to the terminal that started the app
    assert call("POST /api/login_nope") == {"error": "找不到该功能"}
    assert call("GET /api/login_status") == {"error": "出了点问题，请稍后再试"}   # a missing argument is not a user's problem


def test_the_window_bridge_has_no_save_dialog_any_more():
    assert [m for m in dir(ui_app.Api) if not m.startswith("_")] == ["call"]   # pywebview exposes every public method
    assert "SAVE_DIALOG" not in Path(ui_app.__file__).read_text() and "create_file_dialog" not in Path(ui_app.__file__).read_text()


def test_the_app_starts_the_workers_and_the_login_check_before_it_opens_the_window_and_shuts_down_after(monkeypatch, tmp_path):
    order = []
    window = SimpleNamespace(create_window=lambda *a, **k: order.append(("window", k["js_api"])), start=lambda: order.append("start"))
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "webview", window)
    monkeypatch.setattr(ui_app.envfile, "load", lambda: order.append("env"))
    monkeypatch.setattr(ui_app.engine, "start_worker", lambda: order.append("workers"))
    monkeypatch.setattr(ui_app.service, "start_login_check", lambda: order.append("check"))   # in the background: never held up
    monkeypatch.setattr(ui_app.connect, "sync", lambda keys: order.append("connect") or {})   # never the real config files
    monkeypatch.setattr(ui_app.service, "shutdown", lambda: order.append("shutdown"))
    ui_app.main([])
    assert order[:4] == ["env", "workers", "check", "connect"] and order[5:] == ["start", "shutdown"]
    assert order[4][0] == "window" and isinstance(order[4][1], ui_app.Api)

    def crash():
        raise RuntimeError("the window died")

    order.clear()
    window.start = crash
    with pytest.raises(RuntimeError):
        ui_app.main([])
    assert order[-1] == "shutdown"   # the login windows and workers are stopped on the way out, whatever ended the app


def test_unknown_route(call):
    assert "error" in call("GET /api/nope")
    assert "error" in call("GET /api/jobs") and "error" in call("GET /api/tasks")   # the old UI's routes are gone


# ---- searching ----
def test_crawl_starts_a_completing_task_for_every_site(call):
    assert call("POST /api/crawl", {"keyword": " ", "providers": ["nowcoder"]}) == {"error": "请先填写岗位关键词"}
    assert call("POST /api/crawl", {"keyword": "x", "providers": []}) == {"error": "请至少选择一个网站"}
    body = call("POST /api/crawl", {"keyword": "前端", "city": "杭州", "providers": ["nowcoder", "chinahr"]})
    assert body == {"task_ids": [1, 2], "skipped": []}
    tasks = taskqueue.list_(db.connect())
    assert {t["provider"]: (t["status"], t["payload"]) for t in tasks} == {   # detail 'auto': what is missing is completed
        "nowcoder": ("pending", {"keyword": "前端", "city": "杭州", "detail": "auto"}),
        "chinahr": ("pending", {"keyword": "前端", "city": "杭州", "detail": "auto"})}


def test_crawl_starts_the_sites_that_can_and_names_the_ones_that_cannot_in_plain_chinese(call, monkeypatch):
    monkeypatch.setattr(browser, "ready", lambda: False)
    asked = {"keyword": "x", "city": "义乌", "providers": ["nowcoder", "zhaopin", "ganji", "job58", "boss"]}
    body = call("POST /api/crawl", asked)   # a town: nowcoder takes any name, the coded sites do not know it
    assert body["task_ids"] == [1]
    assert [(s["provider"], s["label"], s["reason"]) for s in body["skipped"]] == [
        ("zhaopin", "智联招聘", "不支持“义乌”"), ("ganji", "赶集网", "不支持“义乌”"), ("job58", "58同城", "还没配置密钥"),
        ("boss", "BOSS 直聘", "需要先安装浏览器组件")]
    assert not re.search(r"[A-Za-z]", "".join(s["reason"] for s in body["skipped"]))   # the service's English never reaches the page
    assert [t["provider"] for t in taskqueue.list_(db.connect())] == ["nowcoder"]
    nationwide = call("POST /api/crawl", {**asked, "city": "", "providers": ["zhaopin", "ganji"]})
    assert [(s["provider"], s["reason"]) for s in nationwide["skipped"]] == [("ganji", "需要先填城市")]
    assert nationwide["task_ids"] == [2]


def test_crawl_refuses_in_plain_chinese_when_no_site_can_start(call):
    body = call("POST /api/crawl", {"keyword": "x", "providers": ["job58"]})   # no key, and it needs a city
    assert body == {"error": "还没配置这个网站需要的密钥"} and taskqueue.list_(db.connect()) == []
    assert call("POST /api/crawl", {"keyword": "x", "providers": ["ganji"]}) == {"error": "这个网站需要先填写城市"}
    assert call("POST /api/crawl", {"keyword": "x", "city": "义乌", "providers": ["zhaopin", "ganji"]}) == {
        "error": "这个网站暂时不支持这个城市"}
    assert call("POST /api/crawl", {"keyword": "x", "providers": ["nowcoder", "no-such-site"]}) == {"error": "没成功，可以稍后重试"}
    assert taskqueue.list_(db.connect()) == []   # an unknown site cancels the search: the first task was taken back too
    assert call("POST /api/crawl", {"keyword": "x", "providers": ["nowcoder"]}) == {"task_ids": [1], "skipped": []}   # the rollback took the ids back too


def test_a_city_written_loosely_starts_the_sites_that_know_the_city_it_holds(call):
    body = call("POST /api/crawl", {"keyword": "前端", "city": " 浙江杭州 ", "providers": ["zhaopin", "ganji", "nowcoder"]})
    assert body == {"task_ids": [1, 2, 3], "skipped": []}
    assert {t["payload"]["city"] for t in taskqueue.list_(db.connect())} == {"浙江杭州"}   # the search is told as typed


def test_searches_route_adds_plain_reasons_and_the_task_to_retry(call):
    call("POST /api/crawl", {"keyword": "前端", "city": "杭州", "providers": ["nowcoder", "chinahr"]})
    conn = db.connect()
    taskqueue.dead(conn, 1, "Blocked: HTTP 403")
    [search] = call("GET /api/searches")["searches"]
    sites = {s["provider"]: s for s in search["per_site"]}
    assert (sites["nowcoder"]["status"], sites["nowcoder"]["kind"], sites["nowcoder"]["reason"], sites["nowcoder"]["task_id"]) == (
        "dead", "blocked", "被网站暂时限制，稍后再试", 1)
    assert (sites["chinahr"]["status"], sites["chinahr"]["reason"], sites["chinahr"]["task_id"]) == ("pending", None, 2)
    assert "HTTP 403" not in json.dumps(search, ensure_ascii=False)   # the raw error never reaches the page
    assert call("POST /api/retry", {"task_ids": [1, 2]}) == {"retried": 1}
    assert call("GET /api/searches")["searches"][0]["per_site"][0]["reason"] is None


def test_search_jobs_route_pages_filters_and_sorts(call):
    call("POST /api/crawl", {"keyword": "前端", "providers": ["nowcoder", "chinahr"]})
    conn = db.connect()
    for i in range(1, 36):
        add_job(conn, [1], i, "nowcoder", title=f"N{i}", first_seen_at=1000 + i, complete=1)
    add_job(conn, [2], 100, "chinahr", title="C", first_seen_at=5000, complete=1)
    first = call("GET /api/search_jobs", {"keyword": "前端", "city": ""})
    assert (len(first["jobs"]), first["total"]) == (30, 36)
    assert [j["title"] for j in first["jobs"][:3]] == ["N35", "C", "N34"]   # the default order: the sites in turn
    rest = call("GET /api/search_jobs", {"keyword": "前端", "city": None, "offset": 30})
    assert [j["title"] for j in rest["jobs"]] == ["N6", "N5", "N4", "N3", "N2", "N1"]
    only = call("GET /api/search_jobs", {"keyword": "前端", "provider": "chinahr", "sort": "company"})
    assert [j["title"] for j in only["jobs"]] == ["C"] and only["total"] == 1 and len(only["per_site"]) == 2
    assert call("GET /api/search_jobs", {"keyword": "前端", "sort": "new"}) == {"error": "未知的排序方式"}   # newest-first is gone
    assert call("GET /api/search_jobs", {"keyword": "前端", "sort": "bogus"}) == {"error": "未知的排序方式"}
    assert [j["title"] for j in call("GET /api/search_jobs", {"keyword": "前端", "sort": "salary", "limit": 1})["jobs"]] == ["N35"]


def test_search_jobs_route_lists_only_complete_jobs_unless_asked_and_counts_the_rest(call):
    call("POST /api/crawl", {"keyword": "前端", "providers": ["nowcoder"]})
    conn = db.connect()
    add_job(conn, [1], 1, title="完整", complete=1)
    add_job(conn, [1], 2, title="缺薪资", complete=0, missing="salary", salary_text="面议")
    for args in ({}, {"complete_only": True}):
        page = call("GET /api/search_jobs", {"keyword": "前端", **args})
        assert [j["title"] for j in page["jobs"]] == ["完整"] and page["total"] == 1 and page["per_site"][0]["count"] == 1
        assert page["incomplete"] == {"count": 1, "reasons": [{"text": "面议", "count": 1}]}   # what is held back, and why
    everything = call("GET /api/search_jobs", {"keyword": "前端", "complete_only": False})
    assert everything["total"] == 2 and everything["incomplete"]["count"] == 1
    assert {j["title"]: j["incomplete_reason"] for j in everything["jobs"]} == {"完整": None, "缺薪资": "面议"}


def test_search_jobs_route_takes_the_pay_filter_and_folds_the_same_job_on_two_sites(call):
    call("POST /api/crawl", {"keyword": "前端", "city": "杭州", "providers": ["nowcoder", "chinahr"]})
    conn = db.connect()
    same = {"title": "前端开发", "company": "甲科技有限公司", "city": "杭州"}
    job(conn, "nowcoder", "n", [1], **same, district="西湖区", salary_min=15000, salary_max=25000, salary_unit="元/月")
    job(conn, "chinahr", "c", [2], **same, salary_min=700, salary_max=900, salary_unit="元/天")   # 15225-19575 a month
    ask = lambda **args: call("GET /api/search_jobs", {"keyword": "前端", "city": "杭州", **args})   # noqa: E731
    page = ask()
    assert (page["total"], page["all_total"], page["merged"]) == (1, 1, 1)
    [one] = page["jobs"]
    assert (one["provider"], [(o["provider"], o["label"]) for o in one["also_on"]]) == ("nowcoder", [("chinahr", "新华英才")])
    assert [s["count"] for s in page["per_site"]] == [1, 1]   # each site still has its job
    assert ask(min_month_salary=20000)["total"] == 1          # 15-25K can pay 20000 a month; the day rate (19575) cannot
    assert ask(min_month_salary=20000, provider="chinahr")["jobs"] == []
    assert ask(min_month_salary=30000)["jobs"] == [] and ask(min_month_salary=0)["total"] == 1
    assert ask(min_month_salary=None)["total"] == 1


def test_retry_only_touches_dead_and_login_blocked_tasks(call):
    call("POST /api/crawl", {"keyword": "x", "providers": ["nowcoder", "chinahr", "iguopin"]})
    conn = db.connect()
    taskqueue.dead(conn, 1, "boom")
    taskqueue.waiting_login(conn, 2)
    assert call("POST /api/retry", {"task_ids": [1, 2, 3]}) == {"retried": 2}
    assert call("POST /api/retry", {}) == {"retried": 0}
    assert [t["status"] for t in taskqueue.list_(conn)] == ["pending"] * 3


# ---- the drawer's text ----
def test_job_detail_serves_a_stored_description_without_the_network_as_plain_text(call, monkeypatch):
    call("POST /api/crawl", {"keyword": "x", "providers": ["nowcoder"]})
    add_job(db.connect(), [1], description='岗位职责：<br>1. 开发<img src=x onerror="window.__pwned=1"><script>x()</script>')

    def no_network(*args):
        raise AssertionError("a stored description must not touch the network")
    monkeypatch.setattr(egress, "lease", no_network)
    assert call("POST /api/job_detail", {"job_id": 1}) == {"job": {"id": 1, "description": "岗位职责：\n1. 开发"}}


def test_job_detail_fetches_once_when_nothing_is_stored_and_speaks_chinese_on_failure(call, monkeypatch):
    add_job(db.connect(), [], 7)
    monkeypatch.setattr(service, "job_detail", lambda conn, job_id: {"description": "<p>远程拿到</p>"})
    assert call("POST /api/job_detail", {"job_id": 7}) == {"job": {"id": 7, "description": "远程拿到"}}

    def failing(message):
        def fn(conn, job_id):
            raise ValueError(message)
        return fn
    for message, shown in (("nowcoder is limiting requests right now; try again later", "被网站暂时限制，稍后再试"),
                           ("all egress for nowcoder are cooling down", "这个网站刚才连续出错，正在歇一会儿"),   # cause unknown here: no blame
                           ("nowcoder: login required; log in and try again", "需要先登录"),
                           ("nowcoder: could not load the detail (ConnectError: x)", "网络不通，稍后再试"),
                           ("job 99 not found", "没成功，可以稍后重试")):
        monkeypatch.setattr(service, "job_detail", failing(message))
        assert call("POST /api/job_detail", {"job_id": 7}) == {"error": shown}


# ---- settings ----
def test_providers_route_names_the_keys_but_never_their_values(call, monkeypatch):
    monkeypatch.setenv("DJS_YUPAO_NONCE_KEY", "sekret-value-123")
    body = call("GET /api/providers")
    rows = {p["name"]: p for p in body["providers"]}
    assert rows["yupao"]["requires_env"] == ["DJS_YUPAO_NONCE_KEY", "DJS_YUPAO_SIGN_SALT"] and rows["yupao"]["configured"] is False
    assert rows["nowcoder"]["requires_env"] == [] and rows["nowcoder"]["configured"] is True
    assert {"label", "auth", "requires_city", "configured", "missing_env"} <= set(rows["nowcoder"])
    assert "sekret-value-123" not in json.dumps(body)


def test_providers_route_carries_the_login_overview_once_for_the_whole_page(call):
    body = call("GET /api/providers")
    logins = {x["provider"]: x for x in body["logins"]}
    assert set(logins) == {p["name"] for p in body["providers"] if "logged_in" in p} == {"boss", "zhaopin", "job51", "liepin", "kq36"}
    assert (logins["boss"]["state"], logins["boss"]["logged_in"], logins["boss"]["method"]) == ("not_logged_in", False, "qr")
    assert logins["boss"]["unlocks"] and logins["boss"]["label"] == "BOSS 直聘" and logins["kq36"]["auth"] == "login"
    assert [p["transport"] for p in body["providers"] if p["name"] == "zhaopin"] == ["http"]   # logged out: its first transport
    headed = {p["name"]: p["needs_headed"] for p in body["providers"]}   # the page lists the sites that open a window apart
    assert headed["boss"] is True and headed["zhaopin"] is False and all(isinstance(v, bool) for v in headed.values())


def test_about_and_open_home(call, tmp_path, monkeypatch):
    about = call("GET /api/about")
    assert about["home"] == str(tmp_path) and isinstance(about["version"], str) and about["version"]
    launched = []
    monkeypatch.setattr(ui_api, "subprocess", SimpleNamespace(Popen=launched.append))
    assert call("POST /api/open_home") == {"ok": True}
    assert launched[0][0] in ("open", "explorer", "xdg-open") and launched[0][1:] == [str(tmp_path)]

    def no_file_manager(cmd):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(ui_api, "subprocess", SimpleNamespace(Popen=no_file_manager))
    assert call("POST /api/open_home") == {"ok": True}   # a machine without one is not an error


def test_frequency_preset_clamps_and_persists(call):
    assert call("POST /api/frequency_preset", {"preset": "fast"}) == {"preset": "fast"}
    rows = {r["provider"]: r["interval"] for r in db.connect().execute("SELECT * FROM provider_settings")}
    for name in providers.names():   # 0.5x of the default, never below the platform's floor
        p = providers.get(name)
        assert rows[name] == round(max(p.interval * 0.5, p.min_interval), 2)
    assert call("GET /api/providers")["preset"] == "fast"
    call("POST /api/frequency_preset", {"preset": "steady"})
    assert all(r["interval"] is None for r in db.connect().execute("SELECT * FROM provider_settings"))
    assert "error" in call("POST /api/frequency_preset", {"preset": "x"})


def test_egress_masks_password(call):
    call("POST /api/egress", {"url": "http://u:secret@proxy.test:8080"})
    body = call("GET /api/egress")
    assert "secret" not in json.dumps(body) and "u:***@proxy.test:8080" in json.dumps(body)
    call("DELETE /api/egress", {"url": "http://u:***@proxy.test:8080"})
    assert call("GET /api/egress") == {"egress": []}
    bad = {"error": "代理地址的格式不对，例如 http://127.0.0.1:7890"}
    for typed in ("ftp://x", "direct", "http://127.0.0.1:99999", "socks5://127.0.0.1:1080"):
        assert call("POST /api/egress", {"url": typed}) == bad
    assert call("POST /api/egress", {"url": " 127.0.0.1:7890 "}) == {"url": "http://127.0.0.1:7890", "provider": "*"}


# ---- exporting a table ----
@pytest.fixture
def downloads(tmp_path, monkeypatch):
    folder = tmp_path / "home" / "Downloads"   # does not exist yet: the route makes it
    monkeypatch.setattr(ui_api, "downloads", lambda: folder)
    monkeypatch.setattr(ui_api, "stamp", lambda: "20261011-0930")
    return folder


def test_export_writes_the_table_into_the_downloads_folder_under_a_fresh_name(call, downloads):
    text = '\ufeff岗位,公司\r\n"前端","甲"'
    got = call("POST /api/export_csv", {"keyword": "前端工程师", "city": "杭州", "text": text})
    assert got == {"path": str(downloads / "找岗位-前端工程师-杭州-20261011-0930.csv"), "reveal": ui_api.REVEAL_LABEL}
    assert Path(got["path"]).read_bytes() == b"\xef\xbb\xbf" + '岗位,公司\r\n"前端","甲"'.encode()   # the BOM Excel needs, the CSV's own \r\n
    again = call("POST /api/export_csv", {"keyword": "前端工程师", "city": "杭州", "text": "x"})
    assert Path(again["path"]).name == "找岗位-前端工程师-杭州-20261011-0930-2.csv"   # never over an older file
    assert Path(got["path"]).read_text(encoding="utf-8").startswith("\ufeff岗位")   # which is untouched
    nationwide = call("POST /api/export_csv", {"keyword": "a/b:c*?" + "长" * 60, "city": "", "text": "x"})
    assert Path(nationwide["path"]).name == "找岗位-a_b_c_" + "长" * 34 + "-全国-20261011-0930.csv"   # nothing a file system refuses, not too long


def test_export_refuses_nothing_to_write_and_says_so_when_the_folder_cannot_be_written(call, downloads):
    assert call("POST /api/export_csv", {"keyword": "x", "text": ""}) == {"error": "没有可以导出的内容"}
    assert call("POST /api/export_csv", {"keyword": "x"}) == {"error": "没有可以导出的内容"}
    downloads.parent.mkdir(parents=True)
    downloads.write_text("a file where the folder should be")
    assert call("POST /api/export_csv", {"keyword": "x", "text": "x"}) == {"error": "没能保存到「下载」文件夹，请检查它的权限"}


@pytest.mark.parametrize("platform, command", [
    ("darwin", lambda p: ["open", "-R", str(p)]),
    ("win32", lambda p: ["explorer", f"/select,{p}"]),
    ("linux", lambda p: ["xdg-open", str(p.parent)])])
def test_reveal_shows_the_exported_file_in_the_systems_file_manager(call, downloads, monkeypatch, platform, command):
    path = Path(call("POST /api/export_csv", {"keyword": "x", "text": "x"})["path"])
    launched = []
    monkeypatch.setattr(ui_api, "subprocess", SimpleNamespace(Popen=launched.append))
    monkeypatch.setattr(ui_api.sys, "platform", platform)
    assert call("POST /api/reveal", {"path": str(path)}) == {"ok": True}
    assert launched == [command(path)]


def test_reveal_refuses_anything_that_was_not_exported(call, downloads, tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(ui_api, "subprocess", SimpleNamespace(Popen=launched.append))
    elsewhere = tmp_path / "secret.txt"
    elsewhere.write_text("x")
    downloads.mkdir(parents=True)
    for bad in (str(elsewhere), str(downloads / "no-such.csv"), str(downloads), str(downloads / ".." / "secret.txt"), "", None):
        assert call("POST /api/reveal", {"path": bad}) == {"error": "找不到这个文件"}, bad
    assert launched == []


# ---- login ----
def wait_login(call, provider="boss"):
    for _ in range(200):
        login = call("GET /api/login_status", {"provider": provider})["login"]
        if login["state"] != "running":
            return login
        threading.Event().wait(0.05)


def test_login_runs_in_background(call, monkeypatch):
    monkeypatch.setattr(auth, "login", lambda p, timeout=180: {"logged_in": True})
    assert call("POST /api/login", {"provider": "boss"}) == {"state": "running"}
    assert wait_login(call) == {"state": "ok"}
    ui_api.login_state.clear()


@pytest.mark.parametrize("error, kind, shown", [
    (RuntimeError("Playwright/Chromium missing. Run: pip install playwright && playwright install chromium"),
     "browser", "需要先安装浏览器组件"),
    (TimeoutError("180 秒内没有登录成功，请再试一次"), "timeout", "等了三分钟还没有登录成功，请再试一次"),
    (RuntimeError("登录窗口被关闭了，请重新登录"), "closed", "登录窗口被关掉了，请再试一次"),
    (RuntimeError("正在使用浏览器，请稍后再试"), "busy", "浏览器正忙"),
    (RuntimeError("something odd"), "other", "没成功，可以稍后重试")])
def test_login_failures_come_back_as_plain_chinese(call, monkeypatch, error, kind, shown):
    def failing(p, timeout=180):
        raise error
    monkeypatch.setattr(auth, "login", failing)
    call("POST /api/login", {"provider": "boss"})
    assert wait_login(call) == {"state": "failed", "kind": kind, "error": shown}
    ui_api.login_state.clear()


def test_cancel_closes_the_waiting_login_and_forgets_the_dialogs_state(call, monkeypatch):
    started, stop = threading.Event(), threading.Event()

    def waiting(provider, timeout=180):
        started.set()
        stop.wait(5)
        raise auth.LoginCancelled("已取消登录")

    monkeypatch.setattr(auth, "login", waiting)
    monkeypatch.setattr(auth, "cancel_login", lambda p: stop.set() or True)   # what really closes the window
    assert call("POST /api/login", {"provider": "boss"}) == {"state": "running"}
    assert started.wait(5)
    assert call("POST /api/login_cancel", {"provider": "boss"}) == {"state": "cancelled"}
    assert stop.is_set()
    threading.Event().wait(0.3)   # the login thread has ended with its cancellation by now
    assert call("GET /api/login_status", {"provider": "boss"})["login"] == {}   # nothing left to show: no failure either
    ui_api.login_state.clear()


def test_a_cancelled_login_never_overwrites_the_state_of_the_next_one(call, monkeypatch):
    release, runs = threading.Event(), []

    def login(provider, timeout=180):
        runs.append(provider)
        if len(runs) == 1:   # the first one is still winding down when the user starts again
            release.wait(5)
            raise auth.LoginCancelled("已取消登录")
        return {"logged_in": True}

    monkeypatch.setattr(auth, "login", login)
    monkeypatch.setattr(auth, "cancel_login", lambda p: True)
    call("POST /api/login", {"provider": "boss"})
    call("POST /api/login_cancel", {"provider": "boss"})
    call("POST /api/login", {"provider": "boss"})
    assert wait_login(call) == {"state": "ok"}
    release.set()
    threading.Event().wait(0.3)
    assert call("GET /api/login_status", {"provider": "boss"})["login"] == {"state": "ok"}
    ui_api.login_state.clear()


def test_login_check_asks_the_platform_and_reads_a_failure_to_ask_as_bad_network(call, monkeypatch):
    wall = providers.get("kq36").login.probe_fail_text
    cookies = {"usercookies": "x"}
    auth.save_cookies("kq36", cookies)
    answer = lambda text: httpx.Response(200, text=text, request=httpx.Request("GET", "https://www.kq36.com/"))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: answer("职位列表"))
    assert call("POST /api/login_check", {"provider": "kq36"})["state"] == "logged_in"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: answer(wall))
    got = call("POST /api/login_check", {"provider": "kq36"})
    assert (got["logged_in"], got["state"]) == (False, "expired")

    def down(*a, **k):
        raise httpx.ConnectError("refused")
    auth.save_cookies("kq36", cookies)
    monkeypatch.setattr(httpx, "get", down)
    assert call("POST /api/login_check", {"provider": "kq36"}) == {"error": "网络不通，稍后再试"}
    assert auth.status("kq36")["state"] == "logged_in"   # not being able to ask changes nothing


def test_open_url_only_http(call, monkeypatch):
    opened = []
    monkeypatch.setattr(ui_api.webbrowser, "open", opened.append)
    assert "error" in call("POST /api/open_url", {"url": "file:///etc/passwd"})
    assert "error" in call("POST /api/open_url", {"url": "javascript:alert(1)"})
    assert call("POST /api/open_url", {"url": "https://a.test/x"}) == {"ok": True} and opened == ["https://a.test/x"]


# ---- connecting the AI assistants on this computer
@pytest.fixture
def user(call, tmp_path, monkeypatch):
    """A home folder of its own, so no real assistant's config is read or written."""
    folder = tmp_path / "user"
    folder.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(folder))
    monkeypatch.setenv("APPDATA", str(folder / "AppData" / "Roaming"))
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/app/bin/python")
    return folder


def states(body):
    return {t["key"]: t["state"] for t in body["targets"] if t["state"] != "off"}


def saved(tmp_path):
    return json.loads((tmp_path / "ui_prefs.json").read_text())


def test_connect_lists_every_assistant_found_or_not_and_the_ones_to_paste_by_hand(call, user):
    (user / ".cursor").mkdir()
    body = call("GET /api/connect")
    assert set(body) == {"command", "asked", "config", "targets", "guided"}
    assert (body["command"], body["asked"]) == ("/opt/app/bin/djs", False)
    assert json.loads(body["config"]) == {"mcpServers": {"dear-job-seeker": {"command": "/opt/app/bin/djs"}}}
    assert len(body["targets"]) == 12   # found or not
    assert {t["key"] for t in body["targets"] if t["detected"]} == {"cursor"}
    assert [t for t in body["targets"] if t["key"] == "cursor"] == [
        {"key": "cursor", "label": "Cursor", "group": "intl", "detected": True, "state": "off", "path": "~/.cursor/mcp.json"}]
    assert {t["group"] for t in body["targets"]} == {"cn", "intl"} and states(body) == {}
    assert [(g["key"], g["label"]) for g in body["guided"]] == [
        ("qoder", "Qoder"), ("trae", "Trae / Trae CN"), ("lingma", "通义灵码"), ("cherry-studio", "Cherry Studio")]
    assert all(set(g) == {"key", "label", "where"} and g["where"] for g in body["guided"])


def test_the_switch_writes_and_removes_our_entry_and_remembers_it(call, user, tmp_path):
    (user / ".cursor").mkdir()
    (user / ".qwen").mkdir()
    body = call("POST /api/connect", {"key": "cursor", "on": True})
    assert states(body) == {"cursor": "connected"} and body == call("GET /api/connect")   # the same shape as the page's read
    assert json.loads((user / ".cursor" / "mcp.json").read_text())["mcpServers"]["dear-job-seeker"]["command"] == "/opt/app/bin/djs"
    assert saved(tmp_path)["connect"] == {"on": ["cursor"], "asked": False}
    call("POST /api/connect", {"key": "qwen-code", "on": True})
    assert saved(tmp_path)["connect"]["on"] == ["cursor", "qwen-code"]
    body = call("POST /api/connect", {"key": "cursor", "on": False})
    assert states(body) == {"qwen-code": "connected"} and saved(tmp_path)["connect"]["on"] == ["qwen-code"]
    assert json.loads((user / ".cursor" / "mcp.json").read_text()) == {"mcpServers": {}}
    assert call("POST /api/connect", {"key": "qwen-code", "on": True}) == call("GET /api/connect")   # twice: no change, no error


def test_the_switch_says_in_chinese_when_it_cannot(call, user, tmp_path):
    assert call("POST /api/connect", {"key": "kiro", "on": True}) == {"error": "没有检测到 Kiro"}   # not on this computer
    assert call("POST /api/connect", {"key": "nope", "on": True}) == {"error": "未知的应用"}
    assert call("POST /api/connect", {"on": True}) == {"error": "未知的应用"}
    assert not (user / ".kiro").exists() and not (tmp_path / "ui_prefs.json").exists()
    (user / ".cursor").mkdir()
    config = user / ".cursor" / "mcp.json"
    config.write_text("// mine\n{}")
    wrong = {"error": f"配置文件读不懂（可能带有注释）：{config}，请改用“复制配置”手动添加"}
    assert call("POST /api/connect", {"key": "cursor", "on": True}) == wrong
    assert config.read_text() == "// mine\n{}" and not (tmp_path / "ui_prefs.json").exists()   # not switched on


def test_switching_off_forgets_the_assistant_even_when_its_file_cannot_be_edited(call, user, tmp_path):
    (user / ".cursor").mkdir()
    call("POST /api/connect", {"key": "cursor", "on": True})
    config = user / ".cursor" / "mcp.json"
    config.write_text("// since edited by hand\n{}")
    assert "读不懂" in call("POST /api/connect", {"key": "cursor", "on": False})["error"]
    assert saved(tmp_path)["connect"]["on"] == []   # it is not refreshed at the next start any more
    assert config.read_text() == "// since edited by hand\n{}"


def test_connect_many_connects_the_ticked_ones_found_here_and_names_the_ones_that_fail(call, user, tmp_path):
    for folder in (".cursor", ".gemini", ".qwen"):
        (user / folder).mkdir()
    (user / ".qwen" / "settings.json").write_text("{ // not json")
    keys = ["cursor", "qwen-code", "gemini-cli", "kiro", "nope", "cursor"]   # Kiro is not here, nope is not an assistant
    body = call("POST /api/connect_many", {"keys": keys})
    assert body["failed"] == [{"label": "Qwen Code", "error": f"配置文件读不懂（可能带有注释）：{user / '.qwen' / 'settings.json'}，请改用“复制配置”手动添加"}]
    assert {k: v for k, v in body.items() if k != "failed"} == call("GET /api/connect")   # the same shape plus `failed`
    assert states(body) == {"gemini-cli": "connected", "cursor": "connected", "qwen-code": "unreadable"}
    assert saved(tmp_path)["connect"] == {"on": ["cursor", "gemini-cli"], "asked": True}   # in the order they went on
    assert not (user / ".kiro").exists()
    assert call("POST /api/connect_many", {"keys": ["cursor"]})["failed"] == []   # again: nothing to do, nothing wrong
    assert saved(tmp_path)["connect"] == {"on": ["cursor", "gemini-cli"], "asked": True}   # in the order they went on


def test_connect_many_with_nothing_ticked_still_marks_the_card_as_answered(call, user, tmp_path):
    assert call("POST /api/connect_many", {"keys": []})["failed"] == [] and call("GET /api/connect")["asked"] is True
    assert saved(tmp_path)["connect"] == {"on": [], "asked": True}


def test_connect_asked_only_marks_it_asked(call, user, tmp_path):
    (user / ".cursor").mkdir()
    call("POST /api/connect", {"key": "cursor", "on": True})
    body = call("POST /api/connect_asked")
    assert body["asked"] is True and body == call("GET /api/connect") and "failed" not in body
    assert saved(tmp_path)["connect"] == {"on": ["cursor"], "asked": True}   # nothing was connected or disconnected by it
    (user / ".gemini").mkdir()
    assert states(call("POST /api/connect_asked")) == {"cursor": "connected"}   # a newly found assistant is not connected unasked


def test_the_connection_prefs_and_the_speed_preset_share_one_file_without_losing_each_other(call, user, tmp_path):
    call("POST /api/frequency_preset", {"preset": "fast"})
    call("POST /api/connect_asked")
    assert saved(tmp_path) == {"preset": "fast", "connect": {"on": [], "asked": True}}
    call("POST /api/frequency_preset", {"preset": "steady"})
    assert saved(tmp_path) == {"preset": "steady", "connect": {"on": [], "asked": True}}
    (tmp_path / "ui_prefs.json").write_text(json.dumps({"preset": "fast", "connect": {"on": ["cursor", "gone-app"]}}))
    assert ui_api.connect_prefs() == {"on": ["cursor"], "asked": False}   # an app that is no longer known is dropped


def test_the_app_refreshes_the_connections_the_user_switched_on_before_the_window_opens(call, user, tmp_path, monkeypatch, capsys):
    for folder in (".cursor", ".qwen"):
        (user / folder).mkdir()
    call("POST /api/connect", {"key": "cursor", "on": True})
    (user / ".qwen" / "settings.json").write_text(json.dumps({"mcpServers": {"dear-job-seeker": {"command": "/old/djs"}}}))   # not switched on
    monkeypatch.setattr(sys, "executable", "/new/place/python")   # the app moved
    started = []
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=lambda *a, **k: None, start=lambda: started.append(1)))
    monkeypatch.setattr(ui_app.engine, "start_worker", lambda: None)
    monkeypatch.setattr(ui_app.service, "start_login_check", lambda: None)
    monkeypatch.setattr(ui_app.service, "shutdown", lambda: None)
    ui_app.main([])
    entry = lambda name: json.loads((user / name / ("mcp.json" if name == ".cursor" else "settings.json")).read_text())["mcpServers"]["dear-job-seeker"]   # noqa: E731
    assert entry(".cursor")["command"] == "/new/place/djs" and entry(".qwen") == {"command": "/old/djs"} and started == [1]

    def fail(keys):
        raise RuntimeError("secret internals")
    monkeypatch.setattr(ui_app.connect, "sync", fail)
    ui_app.main([])
    assert started == [1, 1] and "secret internals" in capsys.readouterr().err   # the window opens anyway; the detail is for the terminal
    monkeypatch.setattr(ui_app.connect, "sync", lambda keys: {"cursor": "没能写入配置文件"})
    ui_app.main([])
    assert started == [1, 1, 1] and "cursor" in capsys.readouterr().err   # the ones that failed are named there, and nothing more
