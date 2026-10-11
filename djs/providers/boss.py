"""Boss Zhipin (zhipin.com) via the logged-in C-end JSON API, using the user's own cookie.

Known risk: Boss has answered this endpoint with code 37 ("environment abnormal") even for valid
logins when called outside a real browser. 37 is a risk-control signal, NOT an expired login, so it
maps to Blocked (cooldown + retry) and never sends the user into a login loop. EXPERIMENTAL.
"""
from __future__ import annotations

from djs.providers.base import (Blocked, LoginRequired, Provider, StructureError,
                                get_json, need)

BASE = "https://www.zhipin.com"
SEARCH_URL = BASE + "/wapi/zpgeek/search/joblist.json"
DETAIL_URL = BASE + "/wapi/zpgeek/job/detail.json"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE + "/",
    "Origin": BASE,
    "X-Requested-With": "XMLHttpRequest",
}
NATIONWIDE = "100010000"

# Subset of the 373-entry table in rpa-client boss/city_codes.go (names without 市).
CITY_CODES = {
    "北京": "101010100", "上海": "101020100", "广州": "101280100", "深圳": "101280600",
    "杭州": "101210100", "成都": "101270100", "武汉": "101200100", "南京": "101190100",
    "西安": "101110100", "苏州": "101190400", "长沙": "101250100", "郑州": "101180100",
    "东莞": "101281600", "青岛": "101120200", "沈阳": "101070100", "宁波": "101210400",
    "厦门": "101230200", "合肥": "101220100", "济南": "101120100", "福州": "101230100",
    "无锡": "101190200", "佛山": "101280800", "大连": "101070200", "珠海": "101280700",
    "昆明": "101290100", "天津": "101030100", "重庆": "101040100",
}


def city_code(city: str | None) -> str:
    if not city:
        return NATIONWIDE
    if city not in CITY_CODES:
        raise StructureError(f"unknown Boss city {city!r}; known: {', '.join(CITY_CODES)}")
    return CITY_CODES[city]


def check_body(d: dict) -> dict:
    """Return zpData for code 0; classify every other business code."""
    code, msg = d.get("code"), str(d.get("message", ""))
    if code == 0:
        return need(d, "zpData", typ=dict)
    if "未登录" in msg:
        raise LoginRequired(f"Boss code={code} {msg}")
    raise Blocked(f"Boss code={code} {msg}")


class Boss(Provider):
    name = "boss"
    label = "BOSS 直聘"
    kind = "keyword"
    auth = "login"
    requires_city = False
    interval = 6.0
    jitter = 2.0
    min_interval = 4.0
    page_size = 15
    first_page = 1
    page_cap = 5
    note = "Needs your own QR login (login tool). Personal account: ban risk, keep the default pace."

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        params = {"query": payload["keyword"], "city": city_code(payload.get("city")),
                  "page": page, "pageSize": self.page_size, "scene": 1}
        zp = check_body(get_json(client.get(SEARCH_URL, params=params, headers=HEADERS)))
        return need(zp, "jobList", typ=list)

    def job_id(self, job: dict) -> str:
        return need(job, "encryptJobId", typ=str)

    def detail(self, client, job: dict) -> dict | None:
        params = {"securityId": need(job, "securityId", typ=str)}
        zp = check_body(get_json(client.get(DETAIL_URL, params=params, headers=HEADERS)))
        need(zp, "jobInfo", typ=dict)
        return zp

    def meta(self, job: dict, detail: dict | None) -> dict:
        # lastLoginTime is the recruiter's activity, not a posting date: leave published_at unknown.
        return {"source_url": f"{BASE}/job_detail/{self.job_id(job)}.html", "apply_url": None,
                "published_at": None, "published_at_kind": "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        info = (detail or {}).get("jobInfo", {})
        return {"title": job.get("jobName"), "company": job.get("brandName"),
                "city": job.get("cityName"), "salary_text": job.get("salaryDesc"),
                "description": info.get("postDescription")}

    def probe_payload(self) -> dict:
        return {"keyword": "python", "city": "北京"}
