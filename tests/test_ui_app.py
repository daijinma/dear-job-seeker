import json
import threading

import pytest

from djs import auth, db, taskqueue, ui_api, ui_app


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.setattr(auth, "load_cookies", lambda p: None)
    return ui_app.Api().call


def test_index_asset_and_unknown_route(call):
    assert b"<title>" in ui_app.INDEX.read_bytes() and (ui_app.INDEX.parent / "app.js").exists()
    assert "error" in call("GET /api/nope")


def test_crawl_validation_and_tasks(call):
    assert call("POST /api/crawl", {"keyword": " ", "providers": ["nowcoder"]}) == {"error": "请先填写岗位关键词"}
    assert "error" in call("POST /api/crawl", {"keyword": "x", "providers": []})
    body = call("POST /api/crawl", {"keyword": "前端", "city": "杭州", "providers": ["nowcoder", "boss"]})
    assert len(body["task_ids"]) == 2
    assert {t["provider"]: t["status"] for t in taskqueue.list_(db.connect())} == {"nowcoder": "pending", "boss": "pending"}
    assert len(call("GET /api/tasks")["tasks"]) == 2
    assert call("GET /api/jobs", {"limit": 5}) == {"jobs": []}


def test_boss_task_waits_for_login(call):
    from djs import engine
    call("POST /api/crawl", {"keyword": "x", "providers": ["boss"]})
    conn = db.connect()
    engine.run_task(conn, taskqueue.claim(conn))
    assert call("GET /api/tasks")["tasks"][0]["status"] == "waiting_login"


def test_frequency_preset_clamps_and_persists(call):
    assert call("POST /api/frequency_preset", {"preset": "fast"}) == {"preset": "fast"}
    rows = {r["provider"]: r["interval"] for r in db.connect().execute("SELECT * FROM provider_settings")}
    assert rows["nowcoder"] == 0.3 and rows["boss"] == 4.0  # 0.5x, clamped at min_interval
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
    assert "error" in call("POST /api/egress", {"url": "ftp://x"})


def test_login_runs_in_background(call, monkeypatch):
    monkeypatch.setattr(auth, "login", lambda p, timeout=180: {"logged_in": True})
    assert call("POST /api/login", {"provider": "boss"}) == {"state": "running"}
    for _ in range(100):
        state = call("GET /api/login_status", {"provider": "boss"})["login"]["state"]
        if state != "running":
            break
        threading.Event().wait(0.05)
    assert state == "ok"
    ui_api.login_state.clear()


def test_open_url_only_http(call, monkeypatch):
    opened = []
    monkeypatch.setattr(ui_api.webbrowser, "open", opened.append)
    assert "error" in call("POST /api/open_url", {"url": "file:///etc/passwd"})
    assert call("POST /api/open_url", {"url": "https://a.test/x"}) == {"ok": True} and opened == ["https://a.test/x"]
