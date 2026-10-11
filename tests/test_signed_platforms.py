"""Yupao / Job51 / Job58 / Ganji tests. Fixtures are SYNTHETIC; keys are dummy values set via monkeypatch."""
import httpx
import pytest

from djs import db, engine, service, taskqueue
from djs.providers import _yupao_sign, job51 as j51mod
from djs.providers.base import Blocked, StructureError
from djs.providers.ganji import Ganji
from djs.providers.job51 import Job51
from djs.providers.job58 import Job58, luna_sign
from djs.providers.yupao import Yupao

KEYS = {"DJS_YUPAO_NONCE_KEY": "test-nonce-key", "DJS_YUPAO_SIGN_SALT": "test-salt",
        "DJS_JOB51_HMAC_KEY": "test-key", "DJS_JOB58_LUNA_SALT": "test-salt"}
ALL = [Yupao, Job51, Job58, Ganji]


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for k, v in KEYS.items():
        monkeypatch.setenv(k, v)


def client(body=None, status=200, text=None, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(status, text=text) if text is not None else httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- signing (expected values computed independently with openssl) ----
def test_signing_vectors(monkeypatch):
    assert _yupao_sign.canon({"b": "x", "a": 1, "l": [2, {"z": 1, "y": None}], "n": None}) == \
        'a=1&b=x&l=[2,{"y":null,"z":1}]&n=null&'
    assert _yupao_sign.sign({"b": "x", "a": 1}, "1", 2) == \
        "96d9b9825695985c225a082148a1f0316a5e70680416937b9b21c9d81502049c"
    assert _yupao_sign.yp_sign("", "n1.123") == "346780c0502a39a4cfbc3fa6e264ca58"
    assert luna_sign({"b": "2", "a": "1", "sign": "ignored"}) == "e0cc4f57140c5d3aecfee1ff7b423188"
    monkeypatch.setattr(j51mod.time, "time", lambda: 1000)
    url, sig = j51mod.signed_url("p", {"k": "a b"})
    assert url == "https://cupid.51job.com/p?api_key=51job&timestamp=1000&k=a%20b"
    assert sig == "e057b97d96a3526bf37720885c4b8388ce65686878c05a61b9d7d8a1221a21de"


def test_class_attrs():
    y, j1, j8, g = (c() for c in ALL)
    assert [(p.name, p.label, p.requires_city, p.auth) for p in (y, j1, j8, g)] == [
        ("yupao", "鱼泡直聘", False, "public"), ("job51", "前程无忧", False, "public"),
        ("job58", "58同城", True, "public"), ("ganji", "赶集网", True, "public")]
    assert y.requires_env == ("DJS_YUPAO_NONCE_KEY", "DJS_YUPAO_SIGN_SALT")
    assert j1.requires_env == ("DJS_JOB51_HMAC_KEY",) and j8.requires_env == ("DJS_JOB58_LUNA_SALT",)
    assert g.requires_env == () and g.page_cap == 1 and g.page_size == 8
    assert all(p.note and p.min_interval < p.interval and p.min_interval >= 0.5 for p in (y, j1, j8, g))


# ---- yupao ----
YP = {"_synthetic": True, "jobBizId": 77, "title": "<em>普工</em>", "address": "北京", "sortTime": "2026-09-14 16:20:32",
      "detail": "招普工", "showPerfect": {"salary": "4000元/月"}, "companyInfo": {"enterpriseName": "甲"}}


def test_yupao_happy_headers_and_mapping():
    seen = []
    jobs = Yupao().fetch(client({"code": 0, "data": {"list": [YP]}}, seen=seen), {"keyword": "普工", "city": "上海"}, 1)
    assert jobs == [YP]
    assert seen[0].headers["sign"] and seen[0].headers["x-yp-sign"]
    assert b'"areaIds":["25"]' in seen[0].content.replace(b" ", b"")
    p = Yupao()
    assert p.job_id(YP) == "77"
    assert p.row(YP, None) == {"title": "普工", "company": "甲", "city": "北京", "salary_text": "4000元/月",
                               "description": "招普工"}
    assert p.meta(YP, None) == {"source_url": None, "apply_url": None,
                                "published_at": "2026-09-14T16:20:32+08:00", "published_at_kind": "refresh"}


def test_yupao_failures():
    y = Yupao()
    assert y.fetch(client({"code": 0, "data": {"list": []}}), {"keyword": "x"}, 1) == []
    with pytest.raises(Blocked):
        y.fetch(client(status=429), {"keyword": "x"}, 1)
    with pytest.raises(Blocked):  # WAF challenge page
        y.fetch(client(text="<html>challenge</html>"), {"keyword": "x"}, 1)
    with pytest.raises(StructureError, match="9.37.1"):
        y.fetch(client({"code": 5, "message": "bad sign"}), {"keyword": "x"}, 1)
    with pytest.raises(StructureError):
        y.fetch(client({"code": 0, "data": {}}), {"keyword": "x"}, 1)
    with pytest.raises(ValueError, match="暂不支持城市"):
        y.fetch(client({"code": 0, "data": {"list": []}}), {"keyword": "x", "city": "火星"}, 1)
    with pytest.raises(StructureError):
        y.job_id({})


# ---- job51 ----
J51 = {"_synthetic": True, "jobId": 9, "jobName": "会计", "fullCompanyName": "乙公司", "provideSalaryString": "1-2万",
       "hrefAreaPinYin": "shanghai-ypq", "issueDateString": "2026-09-10 10:00:00",
       "jobAreaLevelDetail": {"cityString": "上海"}}
J51_DETAIL = {"detailJobInfo": {"jobDescribe": "做账"}}


def test_job51_happy_url_signed_and_mapping():
    seen = []
    body = {"status": "1", "resultbody": {"job": {"items": [J51]}}}
    assert Job51().fetch(client(body, seen=seen), {"keyword": "会计", "city": "上海市"}, 2) == [J51]
    req = seen[0]
    assert req.headers["sign"] and req.headers["From-Domain"] == "51job_weixin_wxapp"
    assert req.url.params["jobArea"] == "020000" and req.url.params["pageNum"] == "2"
    p = Job51()
    assert p.job_id(J51) == "9"
    assert p.row(J51, J51_DETAIL) == {"title": "会计", "company": "乙公司", "city": "上海",
                                      "salary_text": "1-2万", "description": "做账"}
    assert p.meta(J51, None)["source_url"] == "https://jobs.51job.com/shanghai-ypq/9.html"
    assert p.meta({}, None)["published_at_kind"] == "unknown"


def test_job51_detail_and_failures():
    p = Job51()
    assert p.detail(client({"status": "1", "resultbody": J51_DETAIL}), J51) == J51_DETAIL
    with pytest.raises(StructureError):
        p.detail(client({"status": "1", "resultbody": {"x": 1}}), J51)
    empty = {"status": "1", "resultbody": {"job": {"items": []}}}
    assert p.fetch(client(empty), {"keyword": "x"}, 1) == []
    with pytest.raises(StructureError):
        p.fetch(client({"status": "1", "resultbody": {"job": {}}}), {"keyword": "x"}, 1)
    with pytest.raises(StructureError, match="110011"):
        p.fetch(client({"status": "110011"}), {"keyword": "x"}, 1)
    with pytest.raises(Blocked):
        p.fetch(client(status=403), {"keyword": "x"}, 1)


# ---- job58 ----
J58 = {"_synthetic": True, "infoId": 5, "title": "服务员", "companyName": "丙", "location": "朝阳", "originUrl": "https://bj.58.com/x/5x.shtml",
       "dispLocalName": "bj"}
J58_DETAIL = {"extParamMap": {"salary": "5000元/月", "content": "端盘子", "posttime": "2026-09-14 10:00:00"}}


def test_job58_happy_cursor_and_mapping():
    seen = []
    body = {"code": 0, "data": {"searchList": [{"hasMore": False, "list": [J58]}]}}
    p, payload = Job58(), {"keyword": "服务员", "city": "北京"}
    assert p.fetch(client(body, seen=seen), payload, 1) == [J58]
    assert seen[0].url.params["cityId"] == "1" and seen[0].headers["thirdkey"] == payload["_cursor"]["thirdKey"]
    assert payload["_cursor"]["done"] and p.fetch(client(body), payload, 2) == []  # hasMore=False ends the session
    assert p.row(J58, J58_DETAIL) == {"title": "服务员", "company": "丙", "city": "朝阳",
                                      "salary_text": "5000元/月", "description": "端盘子"}
    assert p.meta(J58, J58_DETAIL)["published_at"] == "2026-09-14T10:00:00+08:00"
    assert p.meta(J58, None)["published_at_kind"] == "unknown"


def test_job58_shares_the_58_group_city_ids_with_ganji():
    seen = []
    Job58().fetch(client({"code": 0, "data": {"searchList": []}}, seen=seen), {"keyword": "x", "city": "西安"}, 1)
    assert seen[0].url.params["cityId"] == "483"        # verified live: xa.58.com


def test_job58_detail_signed_and_failures():
    p, seen = Job58(), []
    assert p.detail(client({"code": 1, "data": J58_DETAIL}, seen=seen), J58) == J58_DETAIL
    assert len(seen[0].url.params["sign"]) == 32
    with pytest.raises(StructureError):
        p.detail(client({"code": 4}), J58)
    with pytest.raises(Blocked):
        p.detail(client(text="<!DOCTYPE html><html>"), J58)
    with pytest.raises(ValueError, match="必须指定城市"):
        p.fetch(client({}), {"keyword": "x"}, 1)
    with pytest.raises(ValueError, match="暂不支持城市「火星」"):
        p.fetch(client({}), {"keyword": "x", "city": "火星"}, 1)
    assert p.fetch(client({"code": 0, "data": {"illegal": 0}}), {"keyword": "x", "city": "北京"}, 1) == []
    with pytest.raises(Blocked):
        p.fetch(client(status=403), {"keyword": "x", "city": "北京"}, 1)
    with pytest.raises(StructureError):
        p.fetch(client({"code": 0}), {"keyword": "x", "city": "北京"}, 1)


# ---- ganji ----
GJ = {"_synthetic": True, "infoId": 8, "title": "销售", "enterpriseName": "丁", "area": "房山", "salary": "8000元",
      "weappQuery": {"listName": "yewu", "localName": "bj"}}
GJ_SHARE = "https://h5.example/m-position-share/index.html?infoid=8"
GJ_RESULT = {"info": [{"position_publisher_area_v1": [{"position_desc": {"text": "卖货"}}]}],
             "other": {"share": {"data": {"url": GJ_SHARE}}}}


def test_ganji_happy_and_mapping():
    seen = []
    body = {"code": 0, "data": {"jobInfos": [{"dataType": "onejobheader", "text": "更多"}, GJ]}}
    p = Ganji()
    assert p.fetch(client(body, seen=seen), {"keyword": "销售", "city": "北京"}, 1) == [GJ]
    assert seen[0].headers["cid"] == "1"
    assert p.detail(client({"status": "0", "result": GJ_RESULT}), GJ) == GJ_RESULT
    assert p.row(GJ, GJ_RESULT) == {"title": "销售", "company": "丁", "city": "房山", "salary_text": "8000元",
                                    "description": "卖货"}
    # the detail path is a JSON API; the page a person can open is the share URL the detail response carries
    assert p.meta(GJ, GJ_RESULT) == {"source_url": GJ_SHARE, "apply_url": GJ_SHARE,
                                     "published_at": None, "published_at_kind": "unknown"}
    assert p.meta(GJ, None)["source_url"] is None


def test_ganji_failures():
    p, pl = Ganji(), {"keyword": "x", "city": "北京"}
    assert p.fetch(client({"code": 0, "data": {"jobInfos": []}}), pl, 1) == []
    with pytest.raises(Blocked):
        p.fetch(client({"code": -1000005}), pl, 1)
    with pytest.raises(Blocked):
        p.fetch(client(status=429), pl, 1)
    with pytest.raises(StructureError):
        p.fetch(client({"code": 4}), pl, 1)
    with pytest.raises(StructureError):
        p.fetch(client({"code": 0, "data": {}}), pl, 1)
    with pytest.raises(ValueError, match="必须指定城市"):
        p.fetch(client({}), {"keyword": "x"}, 1)
    with pytest.raises(ValueError, match="暂不支持城市"):   # 合肥 has no 58-group id
        p.fetch(client({}), {"keyword": "x", "city": "合肥"}, 1)
    with pytest.raises(StructureError):  # status 0 but no JD block
        p.detail(client({"status": "0", "result": {"info": []}}), GJ)
    with pytest.raises(StructureError):
        p.detail(client({"status": "0", "result": GJ_RESULT}), {"infoId": 8})


# ---- missing env ----
@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    return db.connect()


def test_missing_env_listing_and_crawl_start(conn, monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k)
    assert Yupao().missing_env() == ["DJS_YUPAO_NONCE_KEY", "DJS_YUPAO_SIGN_SALT"]
    assert Ganji().missing_env() == []
    monkeypatch.setenv("DJS_YUPAO_NONCE_KEY", "  ")
    assert "DJS_YUPAO_NONCE_KEY" in Yupao().missing_env()
    listing = {p["name"]: p for p in service.provider_list(conn)}
    assert listing["job51"]["configured"] is False and listing["job51"]["missing_env"] == ["DJS_JOB51_HMAC_KEY"]
    assert listing["ganji"]["configured"] is True and listing["nowcoder"]["configured"] is True
    with pytest.raises(ValueError, match="DJS_JOB51_HMAC_KEY"):
        service.crawl_start(conn, "job51", keyword="会计")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_crawl_start_requires_city(conn):
    with pytest.raises(ValueError, match="必须指定城市"):
        service.crawl_start(conn, "job58", keyword="x")
    with pytest.raises(ValueError, match="暂不支持城市"):
        service.crawl_start(conn, "job58", keyword="x", city="火星")
    assert service.crawl_start(conn, "job58", keyword="x", city="北京")["task_id"]


def test_engine_kills_task_when_env_disappears(conn, monkeypatch):
    tid = service.crawl_start(conn, "job51", keyword="会计")["task_id"]
    monkeypatch.delenv("DJS_JOB51_HMAC_KEY")
    engine.run_task(conn, taskqueue.claim(conn))
    row = conn.execute("SELECT status, last_error FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "dead" and "DJS_JOB51_HMAC_KEY" in row["last_error"]
