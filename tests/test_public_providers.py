"""Public provider tests. Fixtures in tests/fixtures are SYNTHETIC (built from documented shapes)."""
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from djs.providers.base import Blocked, LoginRequired, StructureError, district_of, split_places, tag_names
from djs.providers.chinahr import Chinahr
from djs.providers.iguopin import Iguopin
from djs.providers.nowcoder import NowCoder

FIX = Path(__file__).parent / "fixtures"
SEARCH = {"keyword": "算法"}


def fx(name):
    return json.loads((FIX / name).read_text())


def client(body=None, status=200, text=None, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(req)
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


PROVIDERS = [
    (NowCoder(), {"code": 0, "data": {"datas": []}}),
    (Iguopin(), {"code": 200, "data": {"total": 0, "list": None}}),
    (Chinahr(), {"code": 1, "data": {"jobItems": []}}),
]
IDS = [p.name for p, _ in PROVIDERS]


def test_class_attrs():
    got = {p.name: (p.kind, p.auth, p.interval, p.min_interval, p.page_size, p.first_page, p.page_cap)
           for p, _ in PROVIDERS}
    assert got == {"nowcoder": ("keyword", "public", 0.6, 0.3, 20, 1, None),
                   "iguopin": ("keyword", "public", 3.0, 1.0, 20, 1, 20),
                   "chinahr": ("keyword", "public", 0.8, 0.5, 50, 0, None)}
    assert all(p.note for p, _ in PROVIDERS)


@pytest.mark.parametrize("p,empty", PROVIDERS, ids=IDS)
def test_empty_list_is_valid(p, empty):
    assert p.fetch(client(empty), SEARCH, p.first_page) == []


@pytest.mark.parametrize("p,_", PROVIDERS, ids=IDS)
@pytest.mark.parametrize("status", [403, 429])
def test_blocked(p, _, status):
    with pytest.raises(Blocked) as e:
        p.fetch(client({}, status=status), SEARCH, p.first_page)
    assert not isinstance(e.value, LoginRequired)


@pytest.mark.parametrize("p,_", PROVIDERS, ids=IDS)
def test_401_login_required(p, _):
    with pytest.raises(LoginRequired):
        p.fetch(client({}, status=401), SEARCH, p.first_page)


@pytest.mark.parametrize("p,_", PROVIDERS, ids=IDS)
@pytest.mark.parametrize("body", [{}, {"code": 99, "data": None}, {"code": 0, "data": {}}])
def test_structure_break(p, _, body):
    with pytest.raises(StructureError):
        p.fetch(client(body), SEARCH, p.first_page)


@pytest.mark.parametrize("p,_", PROVIDERS, ids=IDS)
def test_non_json_is_structure_error(p, _):
    with pytest.raises(StructureError):
        p.fetch(client(text="<html>under maintenance</html>"), SEARCH, p.first_page)


@pytest.mark.parametrize("p,_", PROVIDERS, ids=IDS)
def test_job_id_missing(p, _):
    with pytest.raises(StructureError):
        p.job_id({})


# ---- nowcoder ----
def test_nowcoder_happy_and_mapping():
    seen = []
    p = NowCoder()
    jobs = p.fetch(client(fx("nowcoder_list.json"), seen=seen), SEARCH, 1)
    assert [p.job_id(j) for j in jobs] == ["463980", "463981", "463982", "463983"]
    assert seen[0].method == "POST" and "jobCity" not in parse_qs(seen[0].content.decode())   # no city, no filter
    m = p.meta(jobs[0], None)
    assert m["published_at"] == "2026-08-28T06:54:59+00:00" and m["published_at_kind"] == "original"
    assert m["source_url"] == "https://www.nowcoder.com/jobs/detail/463980"
    assert p.meta(jobs[1], None)["published_at_kind"] == "unknown"
    assert p.row(jobs[0], None) == {"title": "算法工程师", "company": "示例科技", "city": "杭州、上海", "education": "本科",
                                    "description": "负责算法研发\n\n熟悉机器学习", "salary_text": "20-40K·14薪",
                                    "salary_min": 20000, "salary_max": 40000, "salary_unit": "元/月",
                                    "salary_months": 14}
    r = p.row(jobs[1], None)   # empty jobCityList falls back to jobCity; salaryMonth is only for the K/month unit
    assert (r["salary_text"], r["salary_min"], r["salary_max"], r["salary_unit"], r["salary_months"]) == (
        "200-300元/天", 200, 300, "元/天", None)
    assert r["city"] == "北京" and r["description"] is None and r["company"] is None and r["education"] is None


def test_nowcoder_no_salary_sentinel_unknown_type_and_repeated_description():
    p = NowCoder()
    sentinel, odd = (p.row(j, None) for j in p.fetch(client(fx("nowcoder_list.json")), SEARCH, 1)[2:])
    for r in (sentinel, odd):   # "0-9999999" and an unknown salaryType are not salaries
        assert not any(k.startswith("salary") for k in r)
    assert sentinel["education"] == "不限" and sentinel["description"] == "同一段文字"   # infos == requirements: once
    assert odd["description"] is None                                                  # a non-text requirement is no JD


def test_nowcoder_city_is_sent_as_plain_name():
    seen = []
    NowCoder().fetch(client(fx("nowcoder_list.json"), seen=seen), {"keyword": "算法", "city": "杭州市"}, 1)
    assert parse_qs(seen[0].content.decode())["jobCity"] == ["杭州"]


# ---- iguopin ----
def test_iguopin_happy_and_mapping():
    seen = []
    p = Iguopin()
    jobs = p.fetch(client(fx("iguopin_list.json"), seen=seen), {"keyword": "护士", "city": "杭州"}, 1)
    body = json.loads(seen[0].content)
    assert body["search"]["keyword"] == "护士" and body["search"]["district"] == ["000000.330000.330100"]
    assert "recom" in body
    assert [p.job_id(j) for j in jobs] == ["j1", "2", "3", "4"]
    assert p.row(jobs[0], None) == {"title": "护士", "company": "示例医院", "city": "北京、杭州", "district": "西湖区",
                                    "education": "本科", "experience": "3-5年", "description": "【工作职责】护理",
                                    "salary_text": "8000-12000元/月·14薪", "salary_min": 8000, "salary_max": 12000,
                                    "salary_unit": "元/月", "salary_months": 14}
    negotiable = p.row(jobs[1], None)   # 0-0 wages are "negotiable", never a salary
    assert negotiable["salary_text"] == "面议" and negotiable["city"] is None and "salary_min" not in negotiable
    daily = p.row(jobs[2], None)         # the unit comes from the site; 12 months: no suffix in the text
    assert (daily["salary_text"], daily["salary_min"], daily["salary_unit"]) == ("101-150元/天", 101, "元/天")
    assert "salary_text" not in p.row(jobs[3], None)                # a unit we have no column value for is not guessed
    m = p.meta(jobs[0], None)
    assert m["published_at"] == "2026-09-12T10:00:00+08:00" and m["published_at_kind"] == "refresh"
    assert m["source_url"] == "https://www.iguopin.com/job/detail?id=j1"


def test_iguopin_municipality_is_province_level_and_no_city_omits_district():
    seen = []
    p = Iguopin()
    p.fetch(client(fx("iguopin_list.json"), seen=seen), {"keyword": "护士", "city": "北京市"}, 1)
    p.fetch(client(fx("iguopin_list.json"), seen=seen), SEARCH, 1)
    assert json.loads(seen[0].content)["search"]["district"] == ["000000.110000"]
    assert "district" not in json.loads(seen[1].content)["search"]


def test_iguopin_unknown_city_is_refused_not_nationwide():
    seen = []
    with pytest.raises(ValueError, match="国聘暂不支持城市「火星」"):
        Iguopin().fetch(client(fx("iguopin_list.json"), seen=seen), {"keyword": "护士", "city": "火星"}, 1)
    assert seen == []                                                  # nothing was sent


def test_iguopin_null_list_nonzero_total_is_structure_error():
    with pytest.raises(StructureError):
        Iguopin().fetch(client({"code": 200, "data": {"total": 5, "list": None}}), SEARCH, 1)


def test_iguopin_flat_payload_response_is_structure_error():
    with pytest.raises(StructureError):
        Iguopin().fetch(client({"code": 1, "msg": "", "data": None}), SEARCH, 1)


def test_iguopin_busy_message_is_blocked():
    with pytest.raises(Blocked):
        Iguopin().fetch(client({"code": 500, "msg": "访问人数过多"}), SEARCH, 1)


# ---- chinahr ----
def test_chinahr_happy_detail_and_mapping():
    seen = []
    p = Chinahr()
    jobs = p.fetch(client(fx("chinahr_list.json"), seen=seen), SEARCH, 0)
    assert json.loads(seen[0].content) == {"page": 0, "pageSize": 50, "keyWord": "算法"}
    d = p.detail(client(fx("chinahr_detail.json"), seen=seen), jobs[0])
    assert seen[1].url.params["id"] == jobs[0]["jobId"]
    assert d == {"jobDesc": "<p>负责造价</p>", "companyDesc": "示例简介"}
    assert p.row(jobs[0], d) == {"title": "兼职造价员", "company": "示例咨询有限公司", "city": "南京", "district": "鼓楼区",
                                 "education": "本科", "experience": "1年以上", "salary_text": "6-9K",
                                 "description": "<p>负责造价</p>"}
    assert p.row(jobs[0], None)["description"] is None
    province = p.row(jobs[1], None)   # a job placed in a whole province: the platform's own label, no district
    assert (province["city"], province["district"], province["salary_text"]) == ("湖北省", None, "面议")
    m = p.meta(jobs[0], d)
    assert m["source_url"] == f"https://www.chinahr.com/detail/{jobs[0]['jobId']}"   # /job/<id>.html is a search page
    assert m["published_at"] is None and m["published_at_kind"] == "unknown"


def test_chinahr_city_goes_to_localId():
    seen = []
    Chinahr().fetch(client(fx("chinahr_list.json"), seen=seen), {"keyword": "销售", "city": "杭州"}, 0)
    assert json.loads(seen[0].content) == {"page": 0, "pageSize": 50, "keyWord": "销售", "localId": "55"}


def test_chinahr_detail_failures():
    p, job = Chinahr(), {"jobId": "x"}
    with pytest.raises(StructureError):
        p.detail(client({"code": 1, "data": {}}), job)
    with pytest.raises(Blocked):
        p.detail(client({}, status=429), job)
    ok_empty = {"code": 1, "data": {"jobInfo": {}}}
    assert p.detail(client(ok_empty), job) == {"jobDesc": "", "companyDesc": ""}


# ---- place helpers
def test_split_places_keeps_every_city_and_every_district_once():
    assert split_places(["广州-黄埔区", "杭州", "广州-天河区", None]) == ("广州、杭州", "黄埔区、天河区")
    assert split_places(["湖北省"]) == ("湖北省", None) and split_places([]) == (None, None)
    assert split_places(["北京|朝阳"], sep="|") == ("北京", "朝阳")


def test_district_of_skips_the_city_label():
    assert district_of("西湖区·翠苑", "杭州") == "西湖区" and district_of("杭州·余杭", "杭州") == "余杭"
    assert district_of("余杭", "杭州") == "余杭" and district_of("杭州", "杭州") is None and district_of(None, None) is None


def test_tag_names_reads_the_tags_a_site_sends_in_its_order_and_skips_what_is_no_name():
    assert tag_names(["五险一金", " 双休 ", "", "  ", None, 3, {"a": 1}]) == ["五险一金", "双休"]
    assert tag_names(("包吃", "包住")) == ["包吃", "包住"]
    assert tag_names("五险一金, 双休,,餐补 ") == ["五险一金", "双休", "餐补"]               # one text joined with commas
    objects = [{"name": "五险"}, {"name": " "}, {"name": None}, {"other": "x"}, "裸字符串", None, {"name": "双休"}]
    assert tag_names(objects, "name") == ["五险", "双休"]                                   # with a key: that field of each object
    assert tag_names("五险一金,双休", "name") == []                                         # a text has no objects to read a field of
    for odd in (None, 3, {"五险": 1}, b"x", [], ""):
        assert tag_names(odd) == [] and tag_names(odd, "name") == []


def test_nowcoder_query_has_no_spaces():
    """The site answers 0 rows for a query containing a space; the provider joins the words."""
    seen = []
    NowCoder().fetch(client({"code": 0, "data": {"datas": []}}, seen=seen), {"keyword": "Python  开发 \t后端"}, 1)
    assert parse_qs(seen[0].content.decode())["query"] == ["Python开发后端"]
