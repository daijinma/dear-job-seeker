"""Mohrss + Bytedance tests (shixiseng skipped: needs a Node runtime). Fixtures are SYNTHETIC."""
import json
from pathlib import Path

import httpx
import pytest

from djs.providers.base import Blocked, StructureError
from djs.providers.bytedance import Bytedance
from djs.providers.mohrss import Mohrss

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


def page(name="mohrss_page.json"):
    return fx(name)["html"]


CASES = [(Mohrss(), dict(text='<input name="findjoblist" value="[]">'), dict(text="<html></html>")),
         (Bytedance(), dict(body={"code": 0, "data": {"job_post_list": []}}), dict(body={"code": 1, "data": {}}))]
IDS = [c[0].name for c in CASES]


def test_class_attrs():
    got = {p.name: (p.label, p.kind, p.auth, p.interval, p.min_interval, p.page_size, p.first_page, p.page_cap)
           for p, *_ in CASES}
    assert got == {"mohrss": ("公共招聘网", "keyword", "public", 1.0, 0.5, 20, 1, None),
                   "bytedance": ("字节跳动招聘", "keyword", "public", 0.8, 0.4, 20, 0, None)}
    assert all(p.note and p.min_interval >= 0.3 for p, *_ in CASES)


@pytest.mark.parametrize("p,empty,_", CASES, ids=IDS)
def test_empty_list_is_valid(p, empty, _):
    assert p.fetch(client(**empty), SEARCH, p.first_page) == []


@pytest.mark.parametrize("p,_e,_b", CASES, ids=IDS)
@pytest.mark.parametrize("status", [403, 429])
def test_blocked(p, _e, _b, status):
    with pytest.raises(Blocked):
        p.fetch(client({}, status=status), SEARCH, p.first_page)


@pytest.mark.parametrize("p,_e,broken", CASES, ids=IDS)
def test_structure_break(p, _e, broken):
    with pytest.raises(StructureError):
        p.fetch(client(**broken), SEARCH, p.first_page)


def test_non_json_is_structure_error():
    with pytest.raises(StructureError):
        Bytedance().fetch(client(text="<html>captcha</html>"), SEARCH, 0)
    with pytest.raises(StructureError):
        Mohrss().fetch(client(text='name="findjoblist" value="[not json]"'), SEARCH, 1)


@pytest.mark.parametrize("p,_e,_b", CASES, ids=IDS)
def test_job_id_missing(p, _e, _b):
    with pytest.raises(StructureError):
        p.job_id({})


def test_mohrss_happy_and_mapping():
    seen = []
    p = Mohrss()
    jobs = p.fetch(client(text=page(), seen=seen), {"keyword": "PMO(专员)", "city": "北京"}, 1)
    q = seen[0].url.params
    assert q["textfield"] == "PMO（专员）" and q["AREA"] == "110100" and q["pageNo"] == "1"
    assert [p.job_id(j) for j in jobs] == ["J1", "abc", "J3"]
    assert p.row(jobs[0], None) == {"title": "算法工程师", "company": "示例公司", "city": "北京", "district": "朝阳区",
                                    "description": "负责算法", "salary_text": "6000-9000元/月", "salary_min": 6000,
                                    "salary_max": 9000, "salary_unit": "元/月", "salary_months": None}
    r = p.row(jobs[1], None)   # 429004 is not a canonical city: fall back to the site's own label
    assert "salary_text" not in r and r["description"] is None and r["city"] == "仙桃市"
    one_bound = p.row(jobs[2], None)   # one wage bound is a single figure in the text; there is no range to store
    assert one_bound["salary_text"] == "4500元/月" and "salary_min" not in one_bound
    assert p.meta(jobs[0], None)["source_url"] == "http://example.org/j1"
    assert p.meta(jobs[1], None)["source_url"] is None


def test_mohrss_dates_are_refresh_and_hr_contacts_are_never_returned():
    p = Mohrss()
    jobs = p.fetch(client(text=page()), SEARCH, 1)
    assert p.meta(jobs[0], None)["published_at"] == "2026-09-23T00:00:00+08:00"
    assert p.meta(jobs[0], None)["published_at_kind"] == "refresh"
    assert p.meta(jobs[1], None)["published_at"] is None and p.meta(jobs[1], None)["published_at_kind"] == "unknown"
    assert all("aae004" not in j and "aae005" not in j for j in jobs)        # fixture carries both on job 1


def test_mohrss_city_and_nationwide_area():
    seen = []
    Mohrss().fetch(client(text=page(), seen=seen), {"keyword": "x", "city": "杭州市"}, 1)
    Mohrss().fetch(client(text=page(), seen=seen), SEARCH, 1)
    assert seen[0].url.params["AREA"] == "330100"
    assert seen[1].url.params["AREA"] == ""       # always sent: the server session is sticky


def test_mohrss_findjoblist_must_be_a_list_of_objects():
    with pytest.raises(StructureError):
        Mohrss().fetch(client(text='name="findjoblist" value="[1, 2]"'), SEARCH, 1)


def test_bytedance_city_goes_to_location_code_list():
    seen = []
    Bytedance().fetch(client(fx("bytedance_list.json"), seen=seen), {**SEARCH, "city": "杭州"}, 0)
    assert json.loads(seen[0].content) == {"keyword": "算法", "limit": 20, "offset": 0, "location_code_list": ["CT_52"]}


def test_bytedance_happy_and_mapping():
    seen = []
    p = Bytedance()
    jobs = p.fetch(client(fx("bytedance_list.json"), seen=seen), SEARCH, 2)
    assert json.loads(seen[0].content) == {"keyword": "算法", "limit": 20, "offset": 40}
    assert [p.job_id(j) for j in jobs] == ["7001", "7002", "7003"]
    assert p.row(jobs[0], None) == {"title": "后端工程师", "company": "字节跳动", "city": "北京、杭州",
                                    "district": "海淀区、临平区", "salary_text": None,
                                    "description": "岗位职责 A\n\n任职要求 B"}
    assert p.row(jobs[1], None)["city"] is None and p.row(jobs[1], None)["description"] is None
    assert p.row(jobs[2], None)["city"] == "杭州"       # no city_list: fall back to city_info
    m = p.meta(jobs[0], None)
    assert m["source_url"] == "https://joinbytedance.com/search/7001"
    assert m["published_at"] == "2023-11-14T22:13:20+00:00" and m["published_at_kind"] == "original"
    assert p.meta(jobs[1], None)["published_at_kind"] == "unknown"
