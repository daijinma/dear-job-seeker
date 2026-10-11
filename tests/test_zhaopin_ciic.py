"""Zhaopin / Ciic tests. All fixtures are SYNTHETIC (built from documented shapes)."""
import httpx
import pytest

from djs.providers.base import Blocked, ParseError, StructureError
from djs.providers.ciic import Ciic
from djs.providers.zhaopin import Zhaopin

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
            z.page_cap, z.requires_city) == ("zhaopin", "智联招聘", "keyword", "public", 3.0, 1.5, 20, 1, 3, False)
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
                             "education": "本科", "experience": "3-5年", "salary_text": "20K-40K",
                             "description": "写算法"}
    assert z.row(JOB, None)["description"] is None


def test_zhaopin_list_item_already_carries_the_jd():
    z = Zhaopin()
    assert z.row({**JOB, "jobDetailData": EMBEDDED}, None)["description"] == "列表里就有的职位描述"
    assert z.row({**JOB, "jobDetailData": EMBEDDED}, DETAIL)["description"] == "列表里就有的职位描述"   # list copy first
    empty = {"position": {"desc": {"description": ""}}}
    assert z.row({**JOB, "jobDetailData": empty}, DETAIL)["description"] == "写算法"   # the detail is the fallback


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
