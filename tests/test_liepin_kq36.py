"""Liepin and 康强医疗网 (kq36) tests. Fixtures are SYNTHETIC: hand-built in the shapes the sites answer with."""
import json
import re
from pathlib import Path

import pytest

from djs import auth, cities
from djs.jobs import derive
from djs.normalize import missing
from djs.providers.base import Blocked, Delisted, LoginRequired, ParseError, StructureError
from djs.providers.kq36 import PROBE_URL, WALL, Kq36, js_escape
from djs.providers.liepin import DETAIL_URL, LIST_URL, Liepin
from provider_fakes import client

FIX = Path(__file__).parent / "fixtures"
ID16 = re.compile(r"[0-9a-z]{16}")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    return tmp_path


def load(name: str):
    return json.loads((FIX / name).read_text()) if name.endswith(".json") else (FIX / name).read_bytes()


# ============================================================ liepin
LP_LIST, LP_DETAIL = load("liepin_list.json"), load("liepin_detail.json")
LP_ITEMS = LP_LIST["data"]["data"]["jobCardList"]


def test_liepin_class_attrs_and_login():
    p = Liepin()
    assert (p.name, p.label, p.kind, p.auth, p.city_filter, p.requires_city) == (
        "liepin", "猎聘", "keyword", "optional", "client", False)
    assert (p.page_size, p.first_page, p.page_cap, p.transports, p.min_interval) == (40, 0, 2, ("http",), 2.0)
    assert (p.login.method, p.login.domain, p.login.cookies_any) == ("qr", "liepin.com", ("lt_auth",))
    assert p.city_code("杭州") == "杭州" and p.city_code(None) is None     # no site code: the engine filters by city


def test_liepin_fetch_posts_the_mini_program_request():
    seen = []
    items = Liepin().fetch(client(LP_LIST, seen=seen), {"keyword": "python"}, 1)
    assert [i["job"]["jobId"] for i in items] == [1980641687, "1980641688"]
    assert (seen[0].method, str(seen[0].url)) == ("POST", LIST_URL)
    body = json.loads(seen[0].content)
    assert body["data"]["condition"] == {"keyword": "python", "sortType": "intelligent", "pageSize": 40, "currentPage": 1}
    assert body["data"]["passThroughFrom"] == {"ckId": "", "fkId": "", "skId": "", "scene": "input"}
    assert [body[k] for k in ("version", "version_code", "dev_type", "client_id")] == ["4.4.18", 40418, 1, 80001]


def test_liepin_sends_the_six_gateway_headers_and_keeps_one_device_id_per_install(home):
    seen = []
    http = client(LP_LIST, seen=seen)
    Liepin().fetch(http, {"keyword": "a"}, 0)
    Liepin().fetch(http, {"keyword": "a"}, 1)
    first, second = (r.headers for r in seen)
    for name in ("content-type", "x-fscp-std-info", "x-fscp-trace-id", "x-fscp-bi-stat", "x-client-type", "x-fscp-version"):
        assert first[name]
    assert (first["x-client-type"], first["x-fscp-version"]) == ("wxa", "1.1")
    assert json.loads(first["x-fscp-std-info"]) == {"client_id": 80001, "version": "4.4.18", "version_code": 40418,
                                                    "dev_type": 1, "channel_code": "mp_tx_weixin"}
    device = json.loads(first["x-fscp-bi-stat"])["virtual_device_id"]
    assert ID16.fullmatch(device) and json.loads(second["x-fscp-bi-stat"])["virtual_device_id"] == device
    assert (home / "liepin-device-id").read_text() == device                 # per install, under DJS_HOME
    assert ID16.fullmatch(first["x-fscp-trace-id"]) and first["x-fscp-trace-id"] != second["x-fscp-trace-id"]


def test_liepin_device_id_survives_restarts_and_a_damaged_file_is_replaced(home):
    (home / "liepin-device-id").write_text("abcdefghij012345\n")
    seen = []
    Liepin().fetch(client(LP_LIST, seen=seen), {"keyword": "a"}, 0)
    assert json.loads(seen[0].headers["x-fscp-bi-stat"])["virtual_device_id"] == "abcdefghij012345"
    (home / "liepin-device-id").write_text("short")
    Liepin().fetch(client(LP_LIST, seen=seen), {"keyword": "a"}, 0)
    assert ID16.fullmatch(json.loads(seen[1].headers["x-fscp-bi-stat"])["virtual_device_id"])
    assert ID16.fullmatch((home / "liepin-device-id").read_text())


def test_liepin_device_id_is_not_lost_when_the_user_logs_out():
    seen = []
    http = client(LP_LIST, seen=seen)
    Liepin().fetch(http, {"keyword": "a"}, 0)
    auth.save_cookies("liepin", {"lt_auth": "x"})
    auth.logout("liepin")                                    # removes the whole profile directory of the platform
    Liepin().fetch(http, {"keyword": "a"}, 0)
    assert len({json.loads(r.headers["x-fscp-bi-stat"])["virtual_device_id"] for r in seen}) == 1


def test_liepin_a_missing_job_card_list_is_how_zero_results_look():
    assert Liepin().fetch(client({"flag": 1, "data": {"data": {"compCard": {}}}}), {"keyword": "x"}, 0) == []


@pytest.mark.parametrize("body", [{"flag": 0, "data": {"data": {}}}, {"flag": 1}, {"flag": 1, "data": {"data": None}},
                                  {"flag": 1, "data": {"data": {"jobCardList": "x"}}},
                                  {"flag": 1, "data": {"data": {"jobCardList": None}}}])
def test_liepin_anything_else_is_a_structure_error(body):
    with pytest.raises(StructureError, match="liepin|jobCardList|data"):
        Liepin().fetch(client(body), {"keyword": "x"}, 0)


def test_liepin_http_blocks_and_login_walls():
    with pytest.raises(Blocked):
        Liepin().fetch(client({}, status=429), {"keyword": "x"}, 0)
    with pytest.raises(LoginRequired):
        Liepin().fetch(client({}, status=401), {"keyword": "x"}, 0)
    with pytest.raises(StructureError, match="version=4.4.18"):
        Liepin().fetch(client({"flag": 0}), {"keyword": "x"}, 0)


def test_liepin_job_id_comes_from_the_job_object_with_the_top_level_as_fallback():
    p = Liepin()
    assert p.job_id(LP_ITEMS[0]) == "1980641687" and p.job_id(LP_ITEMS[1]) == "1980641688"
    assert p.job_id({"jobId": 7}) == "7"
    for bad in ({}, {"job": {"title": "x"}}, {"job": {"jobId": None}}):
        with pytest.raises(StructureError):
            p.job_id(bad)


def test_liepin_detail_goes_to_the_api_ac_host_with_the_numeric_id_and_the_jobs_kind():
    seen = []
    d = Liepin().detail(client(LP_DETAIL, seen=seen), LP_ITEMS[0])
    assert str(seen[0].url) == DETAIL_URL and DETAIL_URL.startswith("https://api-ac.liepin.com/")   # not api-c
    assert json.loads(seen[0].content)["data"] == {"jobId": 1980641687, "jobKind": "1"}
    assert "x-fscp-std-info" in seen[0].headers
    assert d["jd"].startswith("岗位职责：") and (d["workArea"], d["education"], d["experience"]) == ("杭州-西湖区", "本科", "3-5年")
    Liepin().detail(client(LP_DETAIL, seen=seen), LP_ITEMS[1])                        # no jobKind in the list: 2
    assert json.loads(seen[1].content)["data"] == {"jobId": 1980641688, "jobKind": "2"}


def test_liepin_detail_failures():
    p, item = Liepin(), LP_ITEMS[0]
    with pytest.raises(Delisted):
        p.detail(client({"flag": 1, "data": {"expired": True, "jobDuty": ""}}), item)
    for body in ({"flag": 1, "data": {"jobDuty": "  \x00 "}}, {"flag": 1, "data": {"jobDuty": None}}):
        with pytest.raises(ParseError):
            p.detail(client(body), item)
    for body in ({"flag": 0}, {"flag": 1}, {"flag": 1, "data": []}):
        with pytest.raises(StructureError):
            p.detail(client(body), item)
    assert p.detail(client({"flag": 1, "data": {"jobDuty": "a\x00b"}}), item)["jd"] == "ab"
    with pytest.raises(StructureError, match="numeric"):
        p.detail(client(LP_DETAIL), {"job": {"jobId": "abc"}})


def test_liepin_rows_are_complete_once_the_detail_is_in():
    p = Liepin()
    listed = derive(p, LP_ITEMS[0], None)
    assert missing(listed) == ["description"]            # the list has everything but the JD
    assert (listed["salary_min"], listed["salary_max"], listed["salary_unit"], listed["salary_months"]) == (
        20000, 40000, "元/月", 15)
    assert (listed["title"], listed["company"], listed["city"], listed["district"]) == ("Python开发工程师", "示例科技", "杭州", "西湖区")
    assert (listed["education"], listed["experience"]) == ("本科", "3-5年")
    assert (listed["source_url"], listed["published_at"], listed["published_at_kind"]) == (
        "https://www.liepin.com/job/1980641687.shtml", "2026-09-16T12:00:00+08:00", "refresh")
    done = derive(p, LP_ITEMS[0], p.detail(client(LP_DETAIL), LP_ITEMS[0]))
    assert missing(done) == [] and "熟悉 Python" in done["description"]


def test_liepin_district_education_and_negotiable_pay_come_out_right():
    p = Liepin()
    plain = derive(p, LP_ITEMS[1], None)                 # dq "上海": a city without a district; salary 面议
    assert (plain["city"], plain["district"]) == ("上海", None)
    assert missing(plain) == ["salary", "description"] and plain["published_at"] is None
    assert plain["published_at_kind"] == "unknown"
    filled = derive(p, LP_ITEMS[1], {"jd": "x", "workArea": "上海-长宁区", "education": "x", "experience": "x"})
    assert (filled["district"], filled["education"], filled["experience"]) == ("长宁区", "学历不限", "经验不限")


# ============================================================ kq36
KQ_LIST, KQ_DETAIL = load("kq36_list.html"), load("kq36_detail.html")
WALL_PAGE = f"<script>alert('{WALL}');history.back()</script>".encode()


def test_kq36_class_attrs_and_login():
    p = Kq36()
    assert (p.name, p.label, p.kind, p.auth, p.city_filter, p.requires_city, p.transports) == (
        "kq36", "康强医疗网", "keyword", "login", "server", False, ("http",))
    assert (p.page_size, p.first_page, p.page_cap, p.min_interval) == (15, 1, None, 1.0)
    assert (p.login.method, p.login.domain, p.login.cookies_any, p.login.url) == (
        "password", "kq36.com", ("usercookies",), "https://www.kq36.com/")
    assert (p.login.probe_url, p.login.probe_fail_text, p.login.unlocks) == (PROBE_URL, WALL, "不登录无法搜索")


@pytest.mark.parametrize("text, expected", [
    ("护士", "%u62A4%u58EB"), ("a b", "a%20b"), ("AZaz09@*_+-./", "AZaz09@*_+-./"), ("c++", "c++"),
    ("é", "%E9"), ("python 开发", "python%20%u5F00%u53D1"), ("😀", "%uD83D%uDE00"), ("&=#%", "%26%3D%23%25"), ("", "")])
def test_js_escape_matches_javascripts_escape(text, expected):
    assert js_escape(text) == expected


def test_kq36_search_url_is_what_the_sites_own_search_box_builds():
    seen = []
    http = client(content=KQ_LIST, seen=seen)
    Kq36().fetch(http, {"keyword": "护士", "city": "北京"}, 2)
    Kq36().fetch(http, {"keyword": "python 开发"}, 1)
    assert str(seen[0].url) == "https://www.kq36.com/job_list.asp?S_Type=0&keyword=%u62A4%u58EB&provinceid=15&cityid=146&page=2"
    assert str(seen[1].url) == "https://www.kq36.com/job_list.asp?S_Type=0&keyword=python%20%u5F00%u53D1&page=1"
    assert seen[0].headers["referer"] == "https://www.kq36.com/"
    with pytest.raises(ValueError, match="康强医疗网暂不支持城市「火星」"):
        Kq36().fetch(client(content=KQ_LIST), {"keyword": "x", "city": "火星"}, 1)


def test_kq36_sends_the_saved_cookie_raw():
    seen = []
    Kq36().fetch(client(content=KQ_LIST, seen=seen, cookies={"usercookies": "a%5Fb%2Bc=="}), {"keyword": "x"}, 1)
    assert seen[0].headers["cookie"] == "usercookies=a%5Fb%2Bc=="               # %5F stays %5F


def test_kq36_the_login_wall_is_login_required_on_the_list_and_on_the_detail():
    with pytest.raises(LoginRequired, match="login wall"):
        Kq36().fetch(client(content=WALL_PAGE), {"keyword": "x"}, 1)
    with pytest.raises(LoginRequired):
        Kq36().detail(client(content=WALL_PAGE), {"id": "9000001"})


def test_kq36_list_cards_are_read_from_the_real_markup():
    cards = Kq36().fetch(client(content=KQ_LIST), {"keyword": "x"}, 1)
    assert [c["id"] for c in cards] == ["9000001", "9000002", "9000003"]
    assert cards[0] == {"id": "9000001", "title": "示例护士", "salary": "4K～5K", "region": "北京·北京市·密云区",
                        "experience": "1年以上", "education": "本科", "company": "示例中医医院", "published": "2026-07-13"}
    assert (cards[1]["published"], cards[2]["published"]) == ("2026-12-05", "2026-07-01")


def test_kq36_rows_are_complete_once_the_detail_is_in():
    p = Kq36()
    cards = p.fetch(client(content=KQ_LIST), {"keyword": "x"}, 1)
    listed = derive(p, cards[0], None)
    assert missing(listed) == ["description"]
    assert (listed["title"], listed["company"], listed["city"], listed["district"]) == ("示例护士", "示例中医医院", "北京", "密云区")
    assert (listed["salary_min"], listed["salary_max"], listed["salary_unit"]) == (4000, 5000, "元/月")
    assert (listed["education"], listed["experience"]) == ("本科", "1年以上")
    detail = p.detail(client(content=KQ_DETAIL), cards[0])
    done = derive(p, cards[0], detail)
    assert missing(done) == [] and done["description"] == "岗位职责：负责病区护理工作。 任职要求：护士执业证，\n有责任心。"
    assert (cards[1]["region"], derive(p, cards[1], None)["city"]) == ("湖北·孝感·孝南区", "孝感")
    assert missing(derive(p, cards[1], detail)) == ["salary"]              # 面议 has no numbers
    third = derive(p, cards[2], detail)
    assert (third["city"], third["district"], third["salary_min"], third["salary_max"]) == ("杭州", "西湖区", 10000, 15000)


def test_kq36_detail_reads_the_job_block_and_not_the_company_intro():
    seen = []
    d = Kq36().detail(client(content=KQ_DETAIL, seen=seen), {"id": "9000001"})
    assert str(seen[0].url) == "https://www.kq36.com/job/9000001" and "简介" not in d["description"]
    with pytest.raises(ParseError, match="9000001"):
        Kq36().detail(client(content=b"<html><div class='job-sec-text fold-text'>x</div></html>"), {"id": "9000001"})
    with pytest.raises(Blocked):
        Kq36().detail(client(content=b"", status=403), {"id": "9000001"})


def test_kq36_zero_cards_is_empty_only_on_a_real_list_page():
    empty = b'<html><input name="keyword"  id="keyword" value=""><ul class="position-job-list"></ul></html>'
    assert Kq36().fetch(client(content=empty), {"keyword": "zzz"}, 1) == []
    with pytest.raises(StructureError, match="no job cards"):
        Kq36().fetch(client(content=b"<html>captcha</html>"), {"keyword": "x"}, 1)


def test_kq36_cards_that_cannot_be_read_are_never_dropped_silently():
    broken = KQ_LIST.replace(b'<a href="/job/9000002"', b'<a href="/jobs/9000002"')        # a card without an id link
    with pytest.raises(ParseError, match="/job/<id>"):
        Kq36().fetch(client(content=broken), {"keyword": "x"}, 1)
    truncated = KQ_LIST.replace(b"</li>\n\n\t\t<li", b"</li><li", 1).replace(b"\t\t\t</div>\n\t\t</li>", b"", 1)
    with pytest.raises(ParseError, match="job cards on the page"):
        Kq36().fetch(client(content=truncated), {"keyword": "x"}, 1)


def test_kq36_meta_keeps_the_date_but_not_a_claim_it_is_the_original():
    p = Kq36()
    card = p.fetch(client(content=KQ_LIST), {"keyword": "x"}, 1)[0]
    assert p.meta(card, None) == {"source_url": "https://www.kq36.com/job/9000001", "apply_url": None,
                                  "published_at": "2026-07-13T00:00:00+08:00", "published_at_kind": "unknown"}
    assert p.meta({}, None)["source_url"] is None and p.meta({}, None)["published_at"] is None


def test_kq36_city_codes_are_province_city_pairs_from_the_sites_own_dropdown():
    assert [cities.code("kq36", c) for c in ("北京市", "上海", "杭州", "西安", "深圳", "宁波", "济南", "青岛")] == [
        "15-146", "8-88", "19-195", "26-261", "10-100", "19-196", "18-178", "18-179"]
    assert cities.code("kq36", "火星") is None
    assert cities.supported("kq36") == list(cities.GB)                       # every canonical city has one
    assert cities.code("kq36", Kq36().probe_payload()["city"])
    assert cities.name_of("kq36", "19-195") == "杭州"
