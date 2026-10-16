"""51job (前程无忧). Keyword search, optional city. Two paths, picked by the client's kind:

http, anonymous: the mini-program API cupid.51job.com; the list has no JD, so detail() does a hop.
sign = HmacSHA256("/{path}?api_key=51job&timestamp={sec}&{query}", key).hex, key = the ASCII bytes of
env DJS_JOB51_HMAC_KEY (the hex text itself, NOT hex-decoded). The server verifies the raw query
string, so the signed string and the requested URL must be byte-identical: build the whole URL
ourselves and never pass params= to httpx. Success is status "1"; "110011" = signature rejected.

browser: the PC search we.51job.com/api/job/search-pc, unsigned, same envelope, and its list item carries the JD
(`jobDescribe`; that it is the full text is not confirmed). It answers signed out too. Aliyun's WAF once wanted the
page's own scripts to have set `acw_sc__v2` first: the search page is still loaded first, but the cookie is no longer
waited for (on 2026-10-16 the page did not set it in 20 s and the search answered without it).
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from urllib.parse import quote, urlencode

import httpx

from ._browser import XHR_ACCEPT, is_browser
from .base import Login, Provider, StructureError, env, get_json, need, tag_names

HOST = "https://cupid.51job.com"
PKG_VERSION = "450"
PC_PAGE = "https://we.51job.com/pc/search"
PC_URL = "https://we.51job.com/api/job/search-pc"
PC_PARAMS = {"api_key": "51job", "searchType": 2, "sortType": 0, "source": 1, "scene": 7, "pageCode": "sou|sou|soulb"}
NATIONWIDE = "000000"       # the PC search's own "全国" area code (cupid takes no jobArea instead)
WAF_COOKIE = "acw_sc__v2"
LOGIN_URL = "https://login.51job.com/login.php?lang=c&url=https%3A%2F%2Fwe.51job.com%2Fpc%2Fmy%2Fmyjob"


def signed_url(path: str, params: dict) -> tuple[str, str]:
    """Returns (url, signature) for the exact query string that will be sent."""
    q = f"/{path}?api_key=51job&timestamp={int(time.time())}"
    q += "".join(f"&{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in params.items())
    sig = hmac.new(env("DJS_JOB51_HMAC_KEY").encode(), q.encode(), hashlib.sha256).hexdigest()
    return HOST + q, sig


def _ok(d: dict, what: str) -> dict:
    if d.get("status") != "1":
        raise StructureError(f"job51 {what} status={d.get('status')!r} version={PKG_VERSION} body={str(d)[:150]}")
    return d


def _get(client, path: str, params: dict) -> dict:
    url, sig = signed_url(path, params)
    return _ok(get_json(client.get(url, headers={"sign": sig, "From-Domain": "51job_weixin_wxapp",
                                                 "uuid": uuid.uuid4().hex})), path)


def _where(job: dict) -> tuple[str | None, str | None]:
    """City and district: cupid has them as fields, the PC list only as one text ('眉山·东坡区')."""
    place = job.get("jobAreaLevelDetail") or {}
    if place.get("cityString"):
        return place["cityString"], place.get("districtString") or None
    city, _, district = (job.get("jobAreaString") or "").replace("-", "·").partition("·")
    return city or None, district or None


def _welfare(job: dict, info: dict) -> list[str]:
    """The welfare tags: the list item names some, the detail's comma-joined text (or its tag list) all of them."""
    return max(tag_names(info.get("welfare")), tag_names(info.get("jobWelfareAllDataList"), "wordText"),
               tag_names(job.get("jobWelfareCodeDataList"), "chineseTitle"), key=len)


class Job51(Provider):
    name = "job51"
    label = "前程无忧"
    kind = "keyword"
    auth = "optional"
    city_filter = "server"
    interval = 3.0
    min_interval = 1.5
    page_size = 50
    first_page = 1
    page_cap = 20
    transports = ("http", "browser")   # a login does not switch to the browser: logged in, the PC search hung on the WAF
    requires_env = ("DJS_JOB51_HMAC_KEY",)
    note = ("Needs env DJS_JOB51_HMAC_KEY; hard cap 1000 results (20 pages); city optional; "
            "the PC search (set_transport browser) gained nothing over this one in live runs")
    login = Login(url=LOGIN_URL, method="password", domain="51job.com", url_prefix="https://we.51job.com/pc/my/",
                  unlocks="暂无明显提升（不登录也能完整搜索）", helps=False, valid_days=30)

    def warmup(self, client, payload: dict) -> None:
        if is_browser(client):
            query = {"jobArea": self.city_code(payload.get("city")) or NATIONWIDE,
                     "keyword": payload.get("keyword", ""), "searchType": 2, "keywordType": ""}
            client.goto(f"{PC_PAGE}?{urlencode(query)}")
            try:
                client.wait_cookie(WAF_COOKIE, timeout=5)
            except httpx.ReadTimeout:   # ponytail: the search answers without it today; a WAF answer is the verdict then
                pass

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        if is_browser(client):
            return self._fetch_pc(client, payload, page)
        params = {"keyword": payload["keyword"], "pageSize": self.page_size, "pageNum": page,
                  "searchType": 2, "scene": 12, "sortType": 0}
        if city := self.city_code(payload.get("city")):
            params["jobArea"] = city
        return need(_get(client, "open/noauth/searchOptimization", params), "resultbody", "job", "items")

    def _fetch_pc(self, client, payload: dict, page: int) -> list[dict]:
        params = {**PC_PARAMS, "timestamp": int(time.time()), "keyword": payload["keyword"],
                  "jobArea": self.city_code(payload.get("city")) or NATIONWIDE, "pageNum": page,
                  "pageSize": self.page_size}
        d = _ok(get_json(client.get(PC_URL, params=params, headers={"Accept": XHR_ACCEPT})), "search-pc")
        return need(d, "resultbody", "job", "items")

    def job_id(self, job: dict) -> str:
        return str(need(job, "jobId", typ=(str, int)))

    def detail(self, client, job: dict) -> dict | None:
        if is_browser(client):    # the PC item is the whole job, and cupid is signed and another origin than the page
            return None
        jid = self.job_id(job)
        body = need(_get(client, f"open/noauth/jobs/detail/base/{jid}", {"jobId": jid}), "resultbody", typ=dict)
        need(body, "detailJobInfo", typ=dict)
        return body

    def meta(self, job: dict, detail: dict | None) -> dict:
        jid, pinyin = job.get("jobId"), job.get("hrefAreaPinYin")
        url = f"https://jobs.51job.com/{pinyin}/{jid}.html" if jid and pinyin else None
        issue = job.get("issueDateString") or job.get("updateDateTime")    # the PC item has only the latter
        at = f"{issue.replace(' ', 'T')}+08:00" if issue else None  # HR can refresh: never 'original'
        return {"source_url": url, "apply_url": url, "published_at": at,
                "published_at_kind": "refresh" if at else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        info = (detail or {}).get("detailJobInfo") or {}
        city, district = _where(job)
        # provideSalaryString has the unit and the paid months; jobSalaryMin/Max have neither (empty for yearly pay).
        # jobDescribe is empty in cupid's list; the PC list has the JD there.
        return {"title": job.get("jobName"), "company": job.get("fullCompanyName") or job.get("companyName"),
                "city": city, "district": district,
                "education": job.get("degreeString"), "experience": job.get("workYearString"),
                "benefits": _welfare(job, info), "salary_text": job.get("provideSalaryString"),
                "description": info.get("jobDescribe") or job.get("jobDescribe") or None}

    def probe_payload(self) -> dict:
        return {"keyword": "算法工程师"}
