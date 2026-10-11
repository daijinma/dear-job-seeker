import json
import threading
import urllib.error
import urllib.request

import pytest

from djs import auth, db, taskqueue, ui_api, ui_server


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.setattr(auth, "load_cookies", lambda p: None)
    srv = ui_server.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, body=None, headers=None):
        req = urllib.request.Request(base + path, method=method, headers=headers or {},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    yield lambda *a, **k: (lambda s, b: (s, json.loads(b)))(*call(*a, **k)), call
    srv.shutdown()


def test_static_pages(ui):
    _, raw = ui
    for path, marker in (("/", b"<title>"), ("/app.js", b"SITES"), ("/app.css", b"--primary")):
        status, body = raw("GET", path)
        assert status == 200 and marker in body


def test_crawl_validation_and_tasks(ui):
    call, _ = ui
    assert call("POST", "/api/crawl", {"keyword": " ", "providers": ["nowcoder"]}) == (400, {"error": "请先填写岗位关键词"})
    assert call("POST", "/api/crawl", {"keyword": "x", "providers": []})[0] == 400
    status, body = call("POST", "/api/crawl", {"keyword": "前端", "city": "杭州", "providers": ["nowcoder", "boss"]})
    assert status == 200 and len(body["task_ids"]) == 2
    conn = db.connect()
    assert {t["provider"]: t["status"] for t in taskqueue.list_(conn)} == {"nowcoder": "pending", "boss": "pending"}
    assert [p["name"] for p in call("GET", "/api/providers")[1]["providers"] if p.get("logged_in") is False] == ["boss"]
    assert len(call("GET", "/api/tasks")[1]["tasks"]) == 2
    assert call("GET", "/api/jobs?limit=5")[1] == {"jobs": []}


def test_boss_task_waits_for_login(ui):
    from djs import engine
    call, _ = ui
    call("POST", "/api/crawl", {"keyword": "x", "providers": ["boss"]})
    conn = db.connect()
    engine.run_task(conn, taskqueue.claim(conn))
    assert call("GET", "/api/tasks")[1]["tasks"][0]["status"] == "waiting_login"


def test_frequency_preset_clamps_and_persists(ui):
    call, _ = ui
    assert call("POST", "/api/frequency_preset", {"preset": "fast"})[0] == 200
    rows = {r["provider"]: r["interval"] for r in db.connect().execute("SELECT * FROM provider_settings")}
    assert rows["nowcoder"] == 0.3 and rows["iguopin"] == 1.5 and rows["boss"] == 4.0  # 0.5x, clamped at min_interval
    assert call("GET", "/api/providers")[1]["preset"] == "fast"
    call("POST", "/api/frequency_preset", {"preset": "steady"})
    assert all(r["interval"] is None for r in db.connect().execute("SELECT * FROM provider_settings"))
    assert call("POST", "/api/frequency_preset", {"preset": "x"})[0] == 400


def test_egress_masks_password(ui):
    call, _ = ui
    call("POST", "/api/egress", {"url": "http://u:secret@proxy.test:8080"})
    body = call("GET", "/api/egress")[1]
    assert "secret" not in json.dumps(body) and "u:***@proxy.test:8080" in json.dumps(body)
    call("DELETE", "/api/egress", {"url": "http://u:***@proxy.test:8080"})
    assert call("GET", "/api/egress")[1] == {"egress": []}
    assert call("POST", "/api/egress", {"url": "ftp://x"})[0] == 400


def test_login_runs_in_background(ui, monkeypatch):
    call, _ = ui
    monkeypatch.setattr(auth, "login", lambda p, timeout=180: {"logged_in": True})
    call("POST", "/api/crawl", {"keyword": "x", "providers": ["boss"]})
    assert call("POST", "/api/login", {"provider": "boss"})[0] == 200
    for _ in range(100):
        state = call("GET", "/api/login_status?provider=boss")[1]["login"]["state"]
        if state != "running":
            break
        threading.Event().wait(0.05)
    assert state == "ok"
    ui_api.login_state.clear()


def test_rejects_foreign_host_and_origin(ui):
    call, raw = ui
    assert raw("GET", "/api/tasks", headers={"Host": "evil.test"})[0] == 403
    assert raw("POST", "/api/crawl", {"keyword": "x", "providers": ["nowcoder"]}, headers={"Origin": "http://evil.test"})[0] == 403
