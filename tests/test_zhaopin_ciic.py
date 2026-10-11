"""Zhaopin / Ciic tests. All fixtures are SYNTHETIC (built from documented shapes)."""
import json
from pathlib import Path

import httpx
import pytest

from djs.jobs import derive
from djs.normalize import missing
from djs.providers.base import Blocked, LoginRequired, ParseError, StructureError
from djs.providers.ciic import Ciic
from djs.providers.zhaopin import PC_URL, Zhaopin
from provider_fakes import FakeBrowser

SEARCH = {"keyword": "算法"}
JOB = {"_synthetic": True, "number": "CC1", "name": "算法工程师", "companyName": "甲公司",
       "workCity": "北京", "cityDistrict": "海淀", "education": "本科", "workingExp": "3-5年", "salary60": "20K-40K",
       "publishTime": "2026-09-10 12:30:00"}
DETAIL = {"_synthetic": True, "position": {"desc": {"description": "写算法"}}}
EMBEDDED = {"position": {"desc": {"description": "列表里就有的职位描述"}}}   # the list item's own copy of the job module


def client(body=None, status=200, text=None, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(status, text=text) if text is not None else httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def li(uuid="ab12", title="前端开发", company="乙公司", address="广东|广州", salary="10-20K", education="本科"):
    return (f'<li class="position-info-item" uuid="{uuid}"><div class="job-name ellipsis">'
            f'<a href="x" title="{title}">{title}</a></div>'
            f'<span class="job-salary ellipsis" title="{salary}">{salary}</span>'
            f'<span class="job-address ellipsis" title="{address}">x</span>'
            f'<span class="job-education ellipsis" title="{education}">{education}</span>'
            f'<h3><a title="{company}">{company}</a></h3></li>')


def page(rows=""):
    return f"<title>职位列表-中智招聘网</title><div class='main-inner-container'>{rows}</div>"


def test_class_attrs():
    z, c = Zhaopin(), Ciic()
    assert (z.name, z.label, z.kind, z.auth, z.interval, z.min_interval, z.page_size, z.first_page,
            z.page_cap, z.requires_city) == ("zhaopin", "智联招聘", "keyword", "optional", 3.0, 1.5, 20, 1, 3, False)
    assert (c.name, c.label, c.kind, c.auth, c.interval, c.min_interval, c.page_size, c.first_page,
            c.page_cap, c.requires_city) == ("ciic", "中智招聘", "keyword", "public", 3.0, 1.5, 20, 1, 20, False)
    assert z.note and c.note


# ---- zhaopin ----
def test_zhaopin_happy_and_headers():
    seen = []
    body = {"statusCode": 200, "data": {"list": [JOB]}}
    jobs = Zhaopin().fetch(client(body, seen=seen), {**SEARCH, "city": "北京"}, 1)
    assert jobs == [JOB]
    assert seen[0].headers["x-zp-version"] and seen[0].url.params["S_SOU_WORK_CITY"] == "530"


def test_zhaopin_no_city_sends_no_city_slice():
    seen = []
    Zhaopin().fetch(client({"statusCode": 200, "data": {"list": []}}, seen=seen), SEARCH, 1)
    assert "S_SOU_WORK_CITY" not in seen[0].url.params


def test_zhaopin_empty_list_is_valid():
    assert Zhaopin().fetch(client({"statusCode": 200, "data": {"list": []}}), SEARCH, 1) == []


@pytest.mark.parametrize("status", [403, 429])
def test_zhaopin_http_block(status):
    with pytest.raises(Blocked):
        Zhaopin().fetch(client({}, status=status), SEARCH, 1)


def test_zhaopin_business_block_and_structure_break():
    with pytest.raises(Blocked):
        Zhaopin().fetch(client({"statusCode": 211}), SEARCH, 1)
    with pytest.raises(StructureError):
        Zhaopin().fetch(client({"statusCode": 500}), SEARCH, 1)
    with pytest.raises(StructureError):
        Zhaopin().fetch(client({"statusCode": 200, "data": {}}), SEARCH, 1)
    with pytest.raises(ValueError, match="智联招聘暂不支持城市「火星」"):
        Zhaopin().fetch(client({"statusCode": 200, "data": {"list": []}}), {**SEARCH, "city": "火星"}, 1)


def test_zhaopin_job_id_detail_meta_row():
    z = Zhaopin()
    assert z.job_id(JOB) == "CC1"
    with pytest.raises(StructureError):
        z.job_id({"name": "x"})
    d = z.detail(client({"code": "200", "data": DETAIL}), JOB)
    assert d == DETAIL
    with pytest.raises(Blocked):
        z.detail(client({"code": 210}), JOB)
    m = z.meta(JOB, d)
    assert m["source_url"].endswith("/CC1.htm") and m["published_at"] == "2026-09-10T12:30:00+08:00"
    assert m["published_at_kind"] == "refresh"
    assert z.meta({"number": "A"}, None)["published_at_kind"] == "unknown"
    assert z.row(JOB, d) == {"title": "算法工程师", "company": "甲公司", "city": "北京", "district": "海淀",
                             "education": "本科", "experience": "3-5年", "benefits": [], "salary_text": "20K-40K",
                             "description": "写算法"}
    assert z.row(JOB, None)["description"] is None


def test_zhaopin_list_item_already_carries_the_jd():
    z = Zhaopin()
    assert z.row({**JOB, "jobDetailData": EMBEDDED}, None)["description"] == "列表里就有的职位描述"
    assert z.row({**JOB, "jobDetailData": EMBEDDED}, DETAIL)["description"] == "列表里就有的职位描述"   # list copy first
    empty = {"position": {"desc": {"description": ""}}}
    assert z.row({**JOB, "jobDetailData": empty}, DETAIL)["description"] == "写算法"   # the detail is the fallback


def keywords(*values):
    """`jobKeyword` as the list item carries it: the poster's welfare tags among empty placeholders."""
    return {"keywords": [{"itemValue": v} for v in values]}


def test_zhaopin_benefits_are_the_job_keywords_the_poster_picked():
    z = Zhaopin()
    item = {**JOB, "jobKeyword": keywords("", "", "五险一金", "", "周末双休", "节日慰问", "五险一金")}   # a real item's shape
    assert z.row(item, None)["benefits"] == ["五险一金", "周末双休", "节日慰问", "五险一金"]
    assert derive(z, item, None)["benefits"] == "五险一金、周末双休、节日慰问"        # each once, in the site's order
    assert derive(z, {**JOB, "jobKeyword": keywords("", "")}, None)["benefits"] is None
    for odd in (None, {}, {"keywords": None}, {"keywords": "五险一金"}, {"keywords": [None, 3, {"x": 1}]}, "x"):
        assert z.row({**JOB, "jobKeyword": odd}, None)["benefits"] == []   # whatever else a site sends is no tag


def test_zhaopin_benefits_take_the_fuller_of_the_items_and_the_details_keywords():
    z = Zhaopin()
    in_detail = lambda *values: {"position": {"other": {"jobKeyword": keywords(*values)}}}   # noqa: E731
    item = {**JOB, "jobKeyword": keywords("五险一金", "")}
    assert z.row(item, in_detail("", "五险一金", "周末双休"))["benefits"] == ["五险一金", "周末双休"]   # the detail names more
    assert z.row(item, in_detail("社保"))["benefits"] == ["五险一金"]                                  # the item names more
    assert z.row(JOB, in_detail("社保公积金"))["benefits"] == ["社保公积金"]                             # only the detail names any
    assert z.row(item, DETAIL)["benefits"] == ["五险一金"]                                             # a detail without the field


def test_zhaopin_rows_from_cgate_are_complete():
    z = Zhaopin()
    done = derive(z, {**JOB, "jobDetailData": EMBEDDED}, None)
    assert missing(done) == [] and (done["salary_min"], done["salary_max"]) == (20000, 40000)
    assert missing(derive(z, JOB, None)) == ["description"]      # the detail hop is what completes it


# ---- zhaopin, logged in: the PC search in a browser ----
LOGIN_COOKIES = {"at": "AT-SECRET", "rt": "RT-SECRET", "smidV2": "fp"}
PC_BODY = {"S_SOU_FULL_INDEX": "护士", "S_SOU_WORK_CITY": "653", "pageIndex": 2, "pageSize": 20,
           "eventScenario": "pcSearchedSouSearch", "anonymous": 0, "platform": 13, "version": "0.0.0",
           "at": "AT-SECRET", "rt": "RT-SECRET"}


def pc(name: str) -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def test_zhaopin_declares_the_two_transports_and_the_login():
    z = Zhaopin()
    assert (z.transports, z.login_transport, z.home) == (("http", "browser"), "browser", "https://www.zhaopin.com/")
    assert (z.login.method, z.login.domain, z.login.cookies_all) == ("wechat", "zhaopin.com", ("at", "rt"))


def test_zhaopin_pc_search_posts_the_page_s_own_json_body():
    seen = []
    page = FakeBrowser(pc("zhaopin_pc_list.json"), seen=seen, cookies=LOGIN_COOKIES)
    items = Zhaopin().fetch(page, {"keyword": "护士", "city": "杭州"}, 2)
    assert [i["number"] for i in items] == ["CCL0000000001J00000000001", "CCL0000000002J00000000002"]
    assert (seen[0].method, str(seen[0].url)) == ("POST", PC_URL) and seen[0].headers["content-type"] == "application/json"
    assert json.loads(seen[0].content) == PC_BODY
    assert seen[0].headers["accept"] == "application/json, text/plain, */*"
    assert "x-zp-version" not in seen[0].headers                  # cgate's headers belong to cgate


def test_zhaopin_pc_search_without_a_city_uses_the_sites_nationwide_code():
    seen = []
    Zhaopin().fetch(FakeBrowser(pc("zhaopin_pc_list.json"), seen=seen, cookies=LOGIN_COOKIES), {"keyword": "x"}, 1)
    assert json.loads(seen[0].content)["S_SOU_WORK_CITY"] == "489"
    with pytest.raises(ValueError, match="智联招聘暂不支持城市「火星」"):
        Zhaopin().fetch(FakeBrowser(pc("zhaopin_pc_list.json"), cookies=LOGIN_COOKIES), {"keyword": "x", "city": "火星"}, 1)


def test_zhaopin_pc_search_refused_session_is_login_required():
    page = FakeBrowser(pc("zhaopin_pc_anonymous.json"), cookies=LOGIN_COOKIES)       # isVerification=1: no rows
    with pytest.raises(LoginRequired, match="isVerification=1"):
        Zhaopin().fetch(page, SEARCH, 1)


@pytest.mark.parametrize("cookies", [{}, {"at": "a"}, {"rt": "r"}, {"at": "", "rt": "r"}])
def test_zhaopin_pc_search_without_login_cookies_sends_nothing(cookies):
    seen = []
    with pytest.raises(LoginRequired, match="at/rt"):
        Zhaopin().fetch(FakeBrowser(pc("zhaopin_pc_list.json"), seen=seen, cookies=cookies), SEARCH, 1)
    assert seen == []


def test_zhaopin_pc_search_failures_are_not_empty_lists():
    ok = {"code": 200, "data": {"count": 0, "isEndPage": 1, "isVerification": 0, "list": []}}
    assert Zhaopin().fetch(FakeBrowser(ok, cookies=LOGIN_COOKIES), SEARCH, 1) == []
    for body, error in [({"code": 211}, Blocked), ({"code": 500}, StructureError), ({"code": 200}, StructureError),
                        ({"code": 200, "data": {"isVerification": 0}}, StructureError),
                        ({"code": 200, "data": {"list": None}}, StructureError)]:
        with pytest.raises(error):
            Zhaopin().fetch(FakeBrowser(body, cookies=LOGIN_COOKIES), SEARCH, 1)
    with pytest.raises(Blocked):
        Zhaopin().fetch(FakeBrowser({}, status=429, cookies=LOGIN_COOKIES), SEARCH, 1)
    with pytest.raises(ParseError):
        Zhaopin().fetch(FakeBrowser(text="<html>challenge</html>", cookies=LOGIN_COOKIES), SEARCH, 1)


def test_zhaopin_pc_rows_are_complete_when_the_list_has_the_jd():
    z, items = Zhaopin(), pc("zhaopin_pc_list.json")["data"]["list"]
    first = derive(z, items[0], None)
    assert missing(first) == [] and (first["salary_min"], first["salary_max"], first["salary_unit"]) == (4000, 8000, "元/月")
    assert (first["city"], first["district"], first["education"], first["experience"]) == ("柳州", "柳南区", "中专/中技", "1-3年")
    assert first["description"].startswith("岗位职责\n1.上门护理")
    second = derive(z, items[1], None)
    assert (second["salary_min"], second["salary_max"]) == (10000, 15000) and missing(second) == ["description"]


def test_zhaopin_warmup_loads_the_home_page_and_waits_for_the_fingerprint_in_a_browser_only():
    page = FakeBrowser()
    Zhaopin().warmup(page, SEARCH)
    assert page.calls == [("goto", "https://www.zhaopin.com/"), ("wait_cookie", "smidV2")]
    Zhaopin().warmup(client({}), SEARCH)                         # an http client has no page: nothing to do
    with pytest.raises(httpx.ReadTimeout):                       # no fingerprint: the engine retries the task
        Zhaopin().warmup(FakeBrowser(cookie_timeout=True), SEARCH)


def test_zhaopin_pc_item_needs_no_detail_hop_but_cgate_still_has_one():
    z = Zhaopin()
    page = FakeBrowser({"code": 200, "data": DETAIL}, cookies=LOGIN_COOKIES)
    assert z.detail(page, JOB) is None and page.calls == []      # no request, no navigation
    assert z.detail(client({"code": 200, "data": DETAIL}), JOB) == DETAIL


# ---- ciic ----
def test_ciic_happy_path():
    seen = []
    jobs = Ciic().fetch(client(text=page(li() + li("cd34", "护士", "丙", "上海|上海|黄浦")), seen=seen),
                        {**SEARCH, "city": "广州"}, 1)
    assert [j["uuid"] for j in jobs] == ["ab12", "cd34"]
    assert jobs[0]["city"] == "广州" and jobs[1]["city"] == "上海"
    assert "district" not in jobs[0] and jobs[1]["district"] == "黄浦" and jobs[0]["education"] == "本科"
    assert seen[0].url.params["workplace"] == "广州"


def test_ciic_city_is_sent_as_short_name():
    seen = []
    Ciic().fetch(client(text=page(), seen=seen), {**SEARCH, "city": " 杭州市 "}, 1)
    assert seen[0].url.params["workplace"] == "杭州"        # "杭州市" returns 0 rows on the real site
    Ciic().fetch(client(text=page(), seen=seen), SEARCH, 1)
    assert "workplace" not in seen[1].url.params


def test_ciic_empty_skeleton_vs_blocked():
    assert Ciic().fetch(client(text=page()), SEARCH, 1) == []
    for bad in ("<html>captcha</html>", "<title>职位列表-中智招聘网</title>", "main-inner-container"):
        with pytest.raises(StructureError):
            Ciic().fetch(client(text=bad), SEARCH, 1)


@pytest.mark.parametrize("status", [403, 429])
def test_ciic_http_block(status):
    with pytest.raises(Blocked):
        Ciic().fetch(client(text="x", status=status), SEARCH, 1)


def test_ciic_row_without_title_is_parse_error():
    with pytest.raises(ParseError):
        Ciic().fetch(client(text=page('<li class="position-info-item" uuid="ab12"><h3><a title="c">c</a></h3></li>')),
                     SEARCH, 1)


def test_ciic_job_id_detail_meta_row():
    c = Ciic()
    job = c.fetch(client(text=page(li())), SEARCH, 1)[0]
    assert c.job_id(job) == "ab12"
    with pytest.raises(StructureError):
        c.job_id({"title": "x"})
    html_ = ('<div class="job-info-container" v-if="!fixFlag"><p><span>浙江|杭州</span><i class="vline"></i>'
             '<span>招聘1人</span><i class="vline"></i><span>1-3年</span><i class="vline"></i><span>本科</span></p>'
             '<span>更新于: 2026-09-16</span></div>'
             '<h4>职位描述</h4> <p class="describe-content">做前端<br>要求&amp;细则</p>'
             '<h4>公司简介</h4><p class="describe-content">公司</p>')
    d = c.detail(client(text=html_), job)
    assert d == {"description": "做前端<br>要求&细则", "refresh_date": "2026-09-16", "experience": "1-3年"}
    with pytest.raises(ParseError):
        c.detail(client(text="<html></html>"), job)
    m = c.meta(job, d)
    assert m["source_url"].endswith("uuid=ab12") and m["published_at"] == "2026-09-16T00:00:00+08:00"
    assert m["published_at_kind"] == "refresh"
    assert c.meta(job, None)["published_at_kind"] == "unknown"
    assert c.row(job, d) == {"title": "前端开发", "company": "乙公司", "city": "广州", "district": None,
                             "education": "本科", "experience": "1-3年", "salary_text": "10-20K",
                             "description": "做前端<br>要求&细则"}
    assert c.row(job, None)["description"] is None and c.row(job, None)["experience"] is None
