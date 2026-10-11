"""Boss + auth tests. No network, no browser. Fixtures under tests/fixtures/boss_*.json are SYNTHETIC
(hand-built from the Go mapper's expected shape, not recorded from Boss)."""
import builtins
import json
import stat
import time
from pathlib import Path

import httpx
import pytest

from djs import auth, cities
from djs.jobs import derive
from djs.normalize import missing
from djs.providers.base import Blocked, LoginRequired, ParseError, StructureError
from djs.providers.boss import STOKEN, Boss
from provider_fakes import FakeBrowser

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


# ------------------------------------------------------------------ provider

def test_class_attrs_match_contract():
    b = Boss()
    assert (b.name, b.kind, b.auth, b.requires_city, b.city_filter) == ("boss", "keyword", "optional", False, "server")
    assert (b.interval, b.min_interval, b.page_size, b.first_page, b.page_cap) == (8.0, 6.0, 15, 1, 5)
    assert (b.transports, b.needs_headed, b.login_transport) == (("browser",), True, None)   # plain http gets code 37


def test_fetch_sends_query_city_and_headers():
    seen = {}

    def handler(request):
        seen["req"] = request
        return httpx.Response(200, json=fixture("boss_list.json"))

    jobs = Boss().fetch(client_for(handler), {"keyword": "python", "city": "上海"}, 2)
    q = seen["req"].url.params
    assert (q["query"], q["city"], q["page"], q["pageSize"], q["scene"]) == ("python", "101020100", "2", "15", "1")
    assert seen["req"].headers["x-requested-with"] == "XMLHttpRequest"
    assert "referer" not in seen["req"].headers and "origin" not in seen["req"].headers   # the page adds its own
    assert [j["encryptJobId"] for j in jobs] == ["synthJobAAA1", "synthJobBBB2"]


def test_fetch_without_city_is_nationwide():
    seen = {}

    def handler(request):
        seen["q"] = request.url.params
        return httpx.Response(200, json=fixture("boss_empty.json"))

    assert Boss().fetch(client_for(handler), {"keyword": "x"}, 1) == []
    assert seen["q"]["city"] == "100010000"


def test_unknown_city_is_refused_not_nationwide():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=fixture("boss_list.json"))

    with pytest.raises(ValueError, match="BOSS 直聘暂不支持城市「火星」"):
        Boss().fetch(client_for(handler), {"keyword": "x", "city": "火星"}, 1)
    assert seen == []


def test_city_name_suffix_is_normalized():
    seen = {}

    def handler(request):
        seen["q"] = request.url.params
        return httpx.Response(200, json=fixture("boss_empty.json"))

    Boss().fetch(client_for(handler), {"keyword": "x", "city": "杭州市"}, 1)
    assert seen["q"]["city"] == "101210100"


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
    assert (seen["q"]["securityId"], seen["q"]["lid"]) == ("synthSecAAA1", "synthLid1")
    assert d["jobInfo"]["postDescription"] == "Synthetic JD text."
    Boss().detail(client_for(handler), {k: v for k, v in job.items() if k != "lid"})
    assert "lid" not in seen["q"]


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
        "title": "Python Engineer", "company": "Example Co", "city": "北京", "district": "海淀区",
        "education": "本科", "experience": "3-5年", "salary_text": "20-30K", "description": "Synthetic JD text."}
    assert b.row(job, None)["description"] is None


def test_salary_is_empty_until_logged_in_and_the_detail_may_fill_it():
    b = Boss()
    anonymous = fixture("boss_list.json")["zpData"]["jobList"][1]       # salaryDesc: "" (not logged in)
    assert b.row(anonymous, None)["salary_text"] is None
    detail = {"jobInfo": {"salaryDesc": "25-35K", "degreeName": "硕士", "experienceName": "1-3年"}}
    r = b.row(anonymous, detail)
    assert (r["salary_text"], r["education"], r["experience"]) == ("25-35K", "硕士", "1-3年")


def test_probe_payload_city_is_known():
    assert cities.code("boss", Boss().probe_payload()["city"]) not in (None, "100010000")


# ---- the browser side: warmup, anonymous vs logged-in rows

SEARCH_PAGE = "https://www.zhipin.com/web/geek/jobs"


def visit_calls(url):
    """What one fresh visit asks of the browser, in order."""
    return [("clear_cookie", STOKEN), ("goto", url), ("page.settle", "networkidle"), ("wait_cookie", STOKEN),
            ("eval", "() => location.href")]


def test_warmup_does_nothing_because_every_call_loads_its_own_page():
    page = FakeBrowser()
    Boss().warmup(page, {"keyword": "python"})
    assert page.calls == []


def test_fetch_loads_a_fresh_search_page_with_a_new_token_right_before_the_api_call():
    """Boss blanks an automated page seconds after load, so each API call gets a freshly loaded page."""
    page = FakeBrowser(body=fixture("boss_list.json"), href="https://www.zhipin.com/web/geek/jobs?query=python")
    Boss().fetch(page, {"keyword": "python", "city": "上海"}, 1)
    assert page.calls == visit_calls(f"{SEARCH_PAGE}?query=python&city=101020100")


def test_detail_loads_the_plain_search_page_first():
    job = fixture("boss_list.json")["zpData"]["jobList"][0]
    page = FakeBrowser(body=fixture("boss_detail.json"), href="https://www.zhipin.com/job_detail/x.html")
    Boss().detail(page, job)
    assert page.calls == visit_calls(SEARCH_PAGE)


def test_a_missing_token_is_not_fatal_and_an_unknown_city_is_refused():
    Boss().fetch(FakeBrowser(body=fixture("boss_list.json"), cookie_timeout=True), {"keyword": "x"}, 1)
    with pytest.raises(ValueError, match="暂不支持城市「火星」"):
        Boss().fetch(FakeBrowser(), {"keyword": "x", "city": "火星"}, 1)


def test_the_security_check_page_blocks_but_the_url_left_after_passing_it_does_not():
    with pytest.raises(Blocked, match="security check"):
        Boss().fetch(FakeBrowser(href="https://www.zhipin.com/web/common/_security_check.html?x=1"), {"keyword": "x"}, 1)
    passed = "https://www.zhipin.com/web/geek/jobs?query=x&city=101210100&_security_check=1_1791707741840"
    assert Boss().fetch(FakeBrowser(body=fixture("boss_list.json"), href=passed), {"keyword": "x"}, 1)


@pytest.mark.parametrize("shown", ["", " ", "-", "--", "-K", "-k", "\ue0a1-\ue0a2K", "\ue000", "\uf8ff1K"])
def test_a_salary_that_is_not_readable_counts_as_missing(shown):
    job = fixture("boss_list.json")["zpData"]["jobList"][0]
    assert Boss().row({**job, "salaryDesc": shown}, None)["salary_text"] is None
    assert Boss().row({**job, "salaryDesc": ""}, {"jobInfo": {"salaryDesc": shown}})["salary_text"] is None


def test_the_readable_salary_of_the_two_sources_wins_and_the_detail_goes_first():
    job = fixture("boss_list.json")["zpData"]["jobList"][0]                        # list: 20-30K
    assert Boss().row(job, {"jobInfo": {"salaryDesc": "25-35K"}})["salary_text"] == "25-35K"
    assert Boss().row(job, {"jobInfo": {"salaryDesc": "\ue001K"}})["salary_text"] == "20-30K"   # glyphs: fall back


def test_anonymous_jobs_lack_only_the_salary_and_logged_in_ones_are_complete():
    b = Boss()
    listed = fixture("boss_list.json")["zpData"]["jobList"]
    detail = fixture("boss_detail.json")["zpData"]
    anonymous = derive(b, {**listed[0], "salaryDesc": ""}, {"jobInfo": {**detail["jobInfo"], "salaryDesc": ""}})
    assert missing(anonymous) == ["salary"] and anonymous["description"] == "Synthetic JD text."
    full = derive(b, listed[0], detail)
    assert missing(full) == [] and (full["salary_min"], full["salary_max"], full["salary_unit"]) == (20000, 30000, "元/月")
    assert (full["city"], full["district"], full["education"], full["experience"]) == ("北京", "海淀区", "本科", "3-5年")


def test_an_anonymous_detail_cannot_complete_a_job_that_lacks_a_salary():
    b = Boss()
    # anonymous: the detail has everything but the salary, so a job without one stays incomplete whatever is fetched
    assert b.detail_can_fill(["salary"], False) is False
    assert b.detail_can_fill(["salary", "description"], False) is False
    assert b.detail_can_fill(["description"], False) is True    # a job that lacks only its JD is worth the request
    assert b.detail_can_fill([], False) is True
    # logged in, the detail carries the salary as well
    assert b.detail_can_fill(["salary"], True) is True
    assert b.detail_can_fill(["salary", "description"], True) is True
