"""Public provider tests. Fixtures in tests/fixtures are SYNTHETIC (built from documented shapes)."""
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from djs.providers.base import Blocked, LoginRequired, StructureError
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
        p.fetch(client(text="<html>captcha</html>"), SEARCH, p.first_page)


@pytest.mark.parametrize("p,_", PROVIDERS, ids=IDS)
def test_job_id_missing(p, _):
    with pytest.raises(StructureError):
        p.job_id({})


# ---- nowcoder ----
def test_nowcoder_happy_and_mapping():
    seen = []
    p = NowCoder()
    jobs = p.fetch(client(fx("nowcoder_list.json"), seen=seen), SEARCH, 1)
    assert [p.job_id(j) for j in jobs] == ["463980", "463981"]
    assert seen[0].method == "POST" and "jobCity" not in parse_qs(seen[0].content.decode())   # no city, no filter
    m = p.meta(jobs[0], None)
    assert m["published_at"] == "2026-08-28T06:54:59+00:00" and m["published_at_kind"] == "original"
    assert m["source_url"] == "https://www.nowcoder.com/jobs/detail/463980"
    assert p.meta(jobs[1], None)["published_at_kind"] == "unknown"
    assert p.row(jobs[0], None) == {"title": "算法工程师", "company": "示例科技", "city": "杭州、上海",
                                    "salary_text": "20-40K·14薪", "description": "熟悉机器学习"}
    r = p.row(jobs[1], None)   # empty jobCityList falls back to jobCity; salaryMonth is only for the K/month unit
    assert r["salary_text"] == "200-300元/天" and r["city"] == "北京"
    assert r["description"] is None and r["company"] is None


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
    assert [p.job_id(j) for j in jobs] == ["j1", "2", "3"]
    assert p.row(jobs[0], None) == {"title": "护士", "company": "示例医院", "city": "北京、杭州-西湖区",
                                    "salary_text": "8000-12000元/月·14薪", "description": "【工作职责】护理"}
    assert p.row(jobs[1], None)["salary_text"] == "面议" and p.row(jobs[1], None)["city"] is None
    assert p.row(jobs[2], None)["salary_text"] == "101-150元/天"      # unit comes from the site; 12 months: no suffix
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
    assert p.row(jobs[0], d) == {"title": "兼职造价员", "company": "示例咨询有限公司", "city": "南京",
                                 "salary_text": "6-9K", "description": "<p>负责造价</p>"}
    assert p.row(jobs[0], None)["description"] is None
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
