"""Boss + auth tests. No network, no browser. Fixtures under tests/fixtures/boss_*.json are SYNTHETIC
(hand-built from the Go mapper's expected shape, not recorded from Boss)."""
import builtins
import json
import os
import stat
import time
from pathlib import Path

import httpx
import pytest

from djs import auth
from djs.providers.base import Blocked, LoginRequired, ParseError, StructureError
from djs.providers.boss import Boss

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def reply(body: dict, status: int = 200):
    return lambda request: httpx.Response(status, json=body)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    return tmp_path


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# ------------------------------------------------------------------ auth store

def test_save_load_perms_and_no_leftover_tmp():
    auth.save_cookies("boss", {"wt2": "SECRET", "zp_at": "x"})
    d = auth.profile_dir("boss")
    assert mode(d) == 0o700 and mode(d / "cookies.json") == 0o600
    assert not (d / "cookies.json.tmp").exists()
    assert auth.load_cookies("boss") == {"wt2": "SECRET", "zp_at": "x"}


def test_load_missing_or_corrupt_is_none():
    assert auth.load_cookies("boss") is None
    auth.save_cookies("boss", {"wt2": "a"})
    (auth.profile_dir("boss") / "cookies.json").write_text("{not json")
    assert auth.load_cookies("boss") is None


def test_expiry_heuristic():
    auth.save_cookies("boss", {"wt2": "a"})
    f = auth.profile_dir("boss") / "cookies.json"
    state = json.loads(f.read_text())
    state["saved_at"] = time.time() - 8 * 86400
    f.write_text(json.dumps(state))
    assert auth.load_cookies("boss") is None
    assert auth.status("boss")["logged_in"] is False


def test_invalidate_keeps_file_but_stops_use():
    auth.save_cookies("boss", {"wt2": "a"})
    auth.invalidate("boss")
    assert auth.load_cookies("boss") is None
    assert mode(auth.profile_dir("boss") / "cookies.json") == 0o600
    auth.invalidate("nosuch-file-yet")  # no-op, no error


def test_logout_removes_profile():
    auth.save_cookies("boss", {"wt2": "a"})
    auth.logout("boss")
    assert not auth.profile_dir("boss").exists()
    auth.logout("boss")  # idempotent


def test_status_never_contains_cookie_values():
    auth.save_cookies("boss", {"wt2": "TOPSECRET"})
    st = auth.status("boss")
    assert st["logged_in"] is True and st["saved_at"]
    assert "TOPSECRET" not in json.dumps(st)


# ------------------------------------------------------------------ login flow

def test_login_without_playwright_gives_hint(monkeypatch):
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(RuntimeError, match="playwright install chromium"):
        auth.login("boss")


def test_auth_cookies_requires_auth_cookie_on_domain():
    cfg = auth.LOGIN["boss"]
    ck = lambda n, d: {"name": n, "value": "v", "domain": d}
    assert auth._auth_cookies([ck("wt2", ".zhipin.com"), ck("a", ".zhipin.com")], cfg) == {"wt2": "v", "a": "v"}
    assert auth._auth_cookies([ck("a", ".zhipin.com")], cfg) == {}
    assert auth._auth_cookies([ck("wt2", ".other.com")], cfg) == {}


def test_wait_for_login_times_out():
    class Ctx:
        def cookies(self):
            return []
    with pytest.raises(TimeoutError):
        auth._wait_for_login(Ctx(), auth.LOGIN["boss"], timeout=0.05)


# ------------------------------------------------------------------ provider

def test_class_attrs_match_contract():
    b = Boss()
    assert (b.name, b.kind, b.auth, b.requires_city) == ("boss", "keyword", "login", False)
    assert (b.interval, b.min_interval, b.page_size, b.first_page, b.page_cap) == (6.0, 4.0, 15, 1, 5)


def test_fetch_sends_query_city_and_headers():
    seen = {}

    def handler(request):
        seen["req"] = request
        return httpx.Response(200, json=fixture("boss_list.json"))

    jobs = Boss().fetch(client_for(handler), {"keyword": "python", "city": "上海"}, 2)
    q = seen["req"].url.params
    assert (q["query"], q["city"], q["page"], q["pageSize"]) == ("python", "101020100", "2", "15")
    assert seen["req"].headers["referer"] == "https://www.zhipin.com/"
    assert [j["encryptJobId"] for j in jobs] == ["synthJobAAA1", "synthJobBBB2"]


def test_fetch_without_city_is_nationwide():
    seen = {}

    def handler(request):
        seen["q"] = request.url.params
        return httpx.Response(200, json=fixture("boss_empty.json"))

    assert Boss().fetch(client_for(handler), {"keyword": "x"}, 1) == []
    assert seen["q"]["city"] == "100010000"


def test_unknown_city_is_structure_error():
    with pytest.raises(StructureError, match="unknown Boss city"):
        Boss().fetch(client_for(reply(fixture("boss_list.json"))), {"keyword": "x", "city": "火星"}, 1)


def test_code_37_is_blocked_not_login():
    with pytest.raises(Blocked) as ei:
        Boss().fetch(client_for(reply(fixture("boss_code37.json"))), {"keyword": "x"}, 1)
    assert not isinstance(ei.value, LoginRequired)


def test_not_logged_in_message_is_login_required():
    body = {"code": 7, "message": "您还未登录"}
    with pytest.raises(LoginRequired):
        Boss().fetch(client_for(reply(body)), {"keyword": "x"}, 1)


@pytest.mark.parametrize("code", [5, 1010, -1])
def test_other_codes_are_blocked_not_login(code):
    with pytest.raises(Blocked) as ei:
        Boss().fetch(client_for(reply({"code": code, "message": "busy"})), {"keyword": "x"}, 1)
    assert not isinstance(ei.value, LoginRequired)


def test_http_403_blocked_and_401_login():
    with pytest.raises(Blocked):
        Boss().fetch(client_for(reply({}, 403)), {"keyword": "x"}, 1)
    with pytest.raises(LoginRequired):
        Boss().fetch(client_for(reply({}, 401)), {"keyword": "x"}, 1)


def test_structure_and_parse_errors():
    for body in ({"code": 0}, {"code": 0, "zpData": {}}, {"code": 0, "zpData": {"jobList": None}}):
        with pytest.raises(StructureError):
            Boss().fetch(client_for(reply(body)), {"keyword": "x"}, 1)
    with pytest.raises(ParseError):
        Boss().fetch(client_for(lambda r: httpx.Response(200, text="<html>")), {"keyword": "x"}, 1)


def test_job_id_missing_raises():
    assert Boss().job_id(fixture("boss_list.json")["zpData"]["jobList"][0]) == "synthJobAAA1"
    with pytest.raises(StructureError):
        Boss().job_id({"jobName": "x"})


def test_detail_uses_security_id():
    seen = {}

    def handler(request):
        seen["q"] = request.url.params
        return httpx.Response(200, json=fixture("boss_detail.json"))

    job = fixture("boss_list.json")["zpData"]["jobList"][0]
    d = Boss().detail(client_for(handler), job)
    assert seen["q"]["securityId"] == "synthSecAAA1"
    assert d["jobInfo"]["postDescription"] == "Synthetic JD text."


def test_detail_errors():
    job = fixture("boss_list.json")["zpData"]["jobList"][0]
    with pytest.raises(StructureError):
        Boss().detail(client_for(reply(fixture("boss_detail.json"))), {"encryptJobId": "x"})
    with pytest.raises(Blocked):
        Boss().detail(client_for(reply(fixture("boss_code37.json"))), job)


def test_meta_and_row():
    b = Boss()
    job = fixture("boss_list.json")["zpData"]["jobList"][0]
    m = b.meta(job, None)
    assert m["source_url"] == "https://www.zhipin.com/job_detail/synthJobAAA1.html"
    assert m["published_at"] is None and m["published_at_kind"] == "unknown"
    assert b.row(job, fixture("boss_detail.json")["zpData"]) == {
        "title": "Python Engineer", "company": "Example Co", "city": "北京",
        "salary_text": "20-30K", "description": "Synthetic JD text."}
    assert b.row(job, None)["description"] is None


def test_probe_payload_city_is_known():
    from djs.providers.boss import city_code
    assert city_code(Boss().probe_payload()["city"]) != "100010000"
