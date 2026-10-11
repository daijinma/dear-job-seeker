"""Boss Zhipin (zhipin.com): the C-end JSON API, called from inside a real browser page.

Plain HTTP and headless browsers get code 37 ("environment abnormal") even with a valid cookie; a headed browser that
loaded the search page first (its scripts write a fresh __zp_stoken__) gets code 0, logged in or not. Anonymous visitors
see every field but the salary, which Boss shows after login only (auth='optional'). Code 37 and every business code but
"not logged in" map to Blocked (cooldown + retry): a risk-control answer must never send the user into a login loop.
"""
from __future__ import annotations

from contextlib import suppress
from urllib.parse import urlencode

import httpx

from ._browser import XHR_ACCEPT
from .base import Blocked, Login, LoginRequired, Provider, get_json, need

BASE = "https://www.zhipin.com"
SEARCH_URL = BASE + "/wapi/zpgeek/search/joblist.json"
DETAIL_URL = BASE + "/wapi/zpgeek/job/detail.json"
SEARCH_PAGE = BASE + "/web/geek/jobs"
# fetch() runs inside the page, which adds its own User-Agent, Referer, Origin and cookies
HEADERS = {"Accept": XHR_ACCEPT, "X-Requested-With": "XMLHttpRequest"}
NATIONWIDE = "100010000"                 # Boss's own "全国" city code; the other city codes live in djs.cities
STOKEN = "__zp_stoken__"                 # anti-bot token the page's scripts write on every load
# Deleted, not blanked: wait_cookie() takes "name=" with an empty value as present
EXPIRE_STOKEN = (f"() => ['', ';domain=.zhipin.com'].forEach(d => document.cookie = "
                 f"'{STOKEN}=;path=/;expires=Thu, 01 Jan 1970 00:00:00 GMT' + d)")
HREF = "() => location.href"
NO_SALARY = ("", "-", "--", "-K", "-k")  # what Boss draws where it hides the salary


def check_body(d: dict) -> dict:
    """Return zpData for code 0; classify every other business code."""
    code, msg = d.get("code"), str(d.get("message", ""))
    if code == 0:
        return need(d, "zpData", typ=dict)
    if "未登录" in msg:
        raise LoginRequired(f"Boss code={code} {msg}")
    raise Blocked(f"Boss code={code} {msg}")


def _salary(*texts) -> str | None:
    """The first salary text that is real. Under risk control Boss draws the digits as private-use glyphs of a
    per-session font: undecodable once off the page, so they count as missing like the placeholders."""
    for text in texts:
        text = (text or "").strip()
        if text not in NO_SALARY and not any("\ue000" <= ch <= "\uf8ff" for ch in text):
            return text
    return None


class Boss(Provider):
    name = "boss"
    label = "BOSS 直聘"
    kind = "keyword"
    auth = "optional"
    requires_city = False
    city_filter = "server"
    interval = 8.0           # 8-20 s between searches is the range proven in earlier runs; risk control is per account/IP
    jitter = 6.0
    min_interval = 6.0
    page_size = 15
    first_page = 1
    page_cap = 5
    transports = ("browser",)
    needs_headed = True
    note = ("Needs a real browser window (plain HTTP gets code 37). The salary shows only after login. "
            "Keep the default pace: risk control hits the account and the IP for hours.")
    login = Login(url="https://www.zhipin.com/web/user/?ka=bticket", method="qr", domain="zhipin.com",
                  cookies_any=("wt2", "zp_at"), unlocks="能看到薪资")

    def warmup(self, client, payload: dict) -> None:
        """Load the search page so its scripts write a fresh __zp_stoken__. A token left in the profile would satisfy
        wait_cookie at once and Boss then answers 37, so it is deleted and the page loaded again. Without a new token
        after 15 s the first request still decides: its answer (37 -> Blocked) is the real verdict."""
        url = self._search_page(payload)
        client.goto(url)
        client.eval(EXPIRE_STOKEN)
        client.goto(url)
        with suppress(httpx.ReadTimeout):
            client.wait_cookie(STOKEN, timeout=15)
        if "_security_check" in client.eval(HREF):  # Boss's human-verification page: no request gets past it
            raise Blocked("Boss redirected to its security check page")

    def _search_page(self, payload: dict) -> str:
        query = {"query": payload.get("keyword"), "city": self.city_code(payload.get("city"))}
        query = {k: v for k, v in query.items() if v}
        return SEARCH_PAGE + (f"?{urlencode(query)}" if query else "")

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        params = {"query": payload["keyword"], "city": self.city_code(payload.get("city")) or NATIONWIDE,
                  "page": page, "pageSize": self.page_size, "scene": 1}
        zp = check_body(get_json(client.get(SEARCH_URL, params=params, headers=HEADERS)))
        return need(zp, "jobList", typ=list)

    def job_id(self, job: dict) -> str:
        return need(job, "encryptJobId", typ=str)

    def detail(self, client, job: dict) -> dict | None:
        params = {"securityId": need(job, "securityId", typ=str)}
        if job.get("lid"):
            params["lid"] = job["lid"]
        zp = check_body(get_json(client.get(DETAIL_URL, params=params, headers=HEADERS)))
        need(zp, "jobInfo", typ=dict)
        return zp

    def meta(self, job: dict, detail: dict | None) -> dict:
        # lastLoginTime is the recruiter's activity, not a posting date: leave published_at unknown.
        return {"source_url": f"{BASE}/job_detail/{self.job_id(job)}.html", "apply_url": None,
                "published_at": None, "published_at_kind": "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        info = (detail or {}).get("jobInfo") or {}
        # the detail's salaryDesc is the one Boss fills first; both are empty until the user is logged in
        return {"title": job.get("jobName"), "company": job.get("brandName"),
                "city": job.get("cityName") or info.get("locationName"), "district": job.get("areaDistrict"),
                "education": job.get("jobDegree") or info.get("degreeName"),
                "experience": job.get("jobExperience") or info.get("experienceName"),
                "salary_text": _salary(info.get("salaryDesc"), job.get("salaryDesc")),
                "description": info.get("postDescription")}

    def probe_payload(self) -> dict:
        return {"keyword": "python", "city": "北京"}
