import json
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from djs import auth, db, egress, providers, service, taskqueue, ui_api, ui_app


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.setattr(auth, "load_cookies", lambda p: None)
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


def test_save_csv_writes_the_file_the_dialog_chose(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(SAVE_DIALOG=15))
    chosen = {}
    api = ui_app.Api()
    api._window = SimpleNamespace(create_file_dialog=lambda kind, save_filename: chosen.get("path"))
    assert api.save_csv("岗位.csv", "x") == {}   # the dialog was cancelled: nothing happens, nothing is reported
    chosen["path"] = str(tmp_path / "a.csv")
    assert api.save_csv("岗位.csv", "\ufeff岗位,公司") == {"saved": str(tmp_path / "a.csv")}
    assert (tmp_path / "a.csv").read_bytes() == b"\xef\xbb\xbf" + "岗位,公司".encode()   # the BOM Excel needs for UTF-8
    chosen["path"] = (str(tmp_path / "b.csv"),)   # some platforms answer with a tuple
    assert api.save_csv("岗位.csv", "x") == {"saved": str(tmp_path / "b.csv")}
    chosen["path"] = str(tmp_path / "no-such-folder" / "c.csv")
    assert api.save_csv("岗位.csv", "x") == {"error": "保存失败，请换个位置或文件名再试"}
    assert "FileNotFoundError" in capsys.readouterr().err


def test_unknown_route(call):
    assert "error" in call("GET /api/nope")
    assert "error" in call("GET /api/jobs") and "error" in call("GET /api/tasks")   # the old UI's routes are gone


# ---- searching ----
def test_crawl_starts_quick_mode_tasks_for_every_site(call):
    assert call("POST /api/crawl", {"keyword": " ", "providers": ["nowcoder"]}) == {"error": "请先填写岗位关键词"}
    assert call("POST /api/crawl", {"keyword": "x", "providers": []}) == {"error": "请至少选择一个网站"}
    body = call("POST /api/crawl", {"keyword": "前端", "city": "杭州", "providers": ["nowcoder", "chinahr"]})
    assert body == {"task_ids": [1, 2]}
    tasks = taskqueue.list_(db.connect())
    assert {t["provider"]: (t["status"], t["payload"]) for t in tasks} == {
        "nowcoder": ("pending", {"keyword": "前端", "city": "杭州", "detail": False}),
        "chinahr": ("pending", {"keyword": "前端", "city": "杭州", "detail": False})}


def test_crawl_is_all_or_nothing_and_refuses_in_plain_chinese(call):
    body = call("POST /api/crawl", {"keyword": "x", "providers": ["nowcoder", "job58"]})   # job58: no key, needs a city
    assert body == {"error": "还没配置这个网站需要的密钥"} and taskqueue.list_(db.connect()) == []
    assert call("POST /api/crawl", {"keyword": "x", "providers": ["ganji"]}) == {"error": "这个网站需要先填写城市"}
    assert call("POST /api/crawl", {"keyword": "x", "providers": ["no-such-site"]}) == {"error": "没成功，可以稍后重试"}
    assert taskqueue.list_(db.connect()) == []
    assert call("POST /api/crawl", {"keyword": "x", "providers": ["nowcoder"]}) == {"task_ids": [1]}   # the rollback took the ids back too


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
        add_job(conn, [1], i, "nowcoder", title=f"N{i}", first_seen_at=1000 + i)
    add_job(conn, [2], 100, "chinahr", title="C", first_seen_at=5000)
    first = call("GET /api/search_jobs", {"keyword": "前端", "city": ""})
    assert (len(first["jobs"]), first["total"], first["jobs"][0]["title"]) == (30, 36, "C")
    rest = call("GET /api/search_jobs", {"keyword": "前端", "city": None, "offset": 30})
    assert [j["title"] for j in rest["jobs"]] == ["N6", "N5", "N4", "N3", "N2", "N1"]
    only = call("GET /api/search_jobs", {"keyword": "前端", "provider": "chinahr", "sort": "company"})
    assert [j["title"] for j in only["jobs"]] == ["C"] and only["total"] == 1 and len(only["per_site"]) == 2
    assert call("GET /api/search_jobs", {"keyword": "前端", "sort": "bogus"}) == {"error": "未知的排序方式"}


def test_search_jobs_route_passes_complete_only_through(call):
    call("POST /api/crawl", {"keyword": "前端", "providers": ["nowcoder"]})
    conn = db.connect()
    add_job(conn, [1], 1, title="完整", company="C", city="杭州", salary_text="20-30K", description="D")
    add_job(conn, [1], 2, title="缺薪资", company="C", city="杭州", description="D")
    assert call("GET /api/search_jobs", {"keyword": "前端"})["total"] == 2   # off unless asked for
    assert call("GET /api/search_jobs", {"keyword": "前端", "complete_only": False})["total"] == 2
    page = call("GET /api/search_jobs", {"keyword": "前端", "complete_only": True})
    assert [j["title"] for j in page["jobs"]] == ["完整"] and page["total"] == 1 and page["per_site"][0]["count"] == 1


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
                           ("all egress for nowcoder are cooling down", "被网站暂时限制，稍后再试"),
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
    assert call("POST /api/egress", {"url": "ftp://x"}) == {"error": "代理地址要以 http:// 或 https:// 开头"}
    assert call("POST /api/egress", {"url": "direct"}) == {"error": "代理地址要以 http:// 或 https:// 开头"}


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
    (TimeoutError("login not completed within 180s"), "timeout", "等了三分钟还没有登录成功，请再试一次"),
    (RuntimeError("something odd"), "other", "没成功，可以稍后重试")])
def test_login_failures_come_back_as_plain_chinese(call, monkeypatch, error, kind, shown):
    def failing(p, timeout=180):
        raise error
    monkeypatch.setattr(auth, "login", failing)
    call("POST /api/login", {"provider": "boss"})
    assert wait_login(call) == {"state": "failed", "kind": kind, "error": shown}
    ui_api.login_state.clear()


def test_open_url_only_http(call, monkeypatch):
    opened = []
    monkeypatch.setattr(ui_api.webbrowser, "open", opened.append)
    assert "error" in call("POST /api/open_url", {"url": "file:///etc/passwd"})
    assert "error" in call("POST /api/open_url", {"url": "javascript:alert(1)"})
    assert call("POST /api/open_url", {"url": "https://a.test/x"}) == {"ok": True} and opened == ["https://a.test/x"]
