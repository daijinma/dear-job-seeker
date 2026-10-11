"""58 同城 (job58) mini-program API. Keyword search, city REQUIRED (no nationwide search).

List: no signature, but `thirdKey` must be non-empty (query + header) and reused across pages of one
task (kept in payload["_cursor"]). With a valid city, a missing data.searchList means zero results
(same shape as "city missing"), so the city check in fetch() must stay BEFORE parsing.
Detail (luna.58.com): md5(sorted "k=v&..." + env DJS_JOB58_LUNA_SALT); code 1 = ok.
Risk control answers with a redirect to the home page (HTML / too many redirects) -> Blocked.
"""
from __future__ import annotations

import hashlib
import random
import string
import time

import httpx

from .base import Blocked, ParseError, Provider, StructureError, env, get_json, need

APPID = "wxc97b21c63d084d92"
APP_VER = "681"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Mac")
HEADERS = {"appid": APPID, "xweb_xhr": "1", "user-agent": UA, "accept-encoding": "gzip",
           "referer": f"https://servicewechat.com/{APPID}/{APP_VER}/page-frame.html"}
SEARCH_URL = "https://wxapp.58.com/list/searchList"
LUNA_URL = "https://luna.58.com/api/v2/info"
CATE_ID = 674  # full-time jobs


def luna_sign(params: dict) -> str:
    s = "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "sign")
    return hashlib.md5((s + env("DJS_JOB58_LUNA_SALT")).encode()).hexdigest()


def _rnd(n: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def _json(r, what: str) -> dict:
    try:
        return get_json(r)
    except ParseError as e:
        if r.text[:200].lstrip().lower().startswith(("<!doctype html", "<html")):
            raise Blocked(f"job58 {what}: got home-page HTML (risk control)") from e
        raise


class Job58(Provider):
    name = "job58"
    label = "58同城"
    kind = "keyword"
    auth = "public"
    requires_city = True
    city_filter = "server"
    interval = 1.5
    min_interval = 0.75
    page_size = 50
    first_page = 1
    requires_env = ("DJS_JOB58_LUNA_SALT",)
    note = "Needs env DJS_JOB58_LUNA_SALT; city required (major cities); risk control redirects to HTML"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        cid = self.city_code(payload.get("city"))
        cur = payload.setdefault("_cursor", {"thirdKey": _rnd(64), "done": False,
                                             "pageFreshId": str(int(time.time() * 1000)) + _rnd(20)})
        if cur["done"]:
            return []
        params = {"cityId": cid, "dispCityId": cid, "cateId": CATE_ID, "dispCateId": CATE_ID, "dispCates": "",
                  "key": payload["keyword"], "page": page, "queryList": "{}", "abtest": "",
                  "pageSize": self.page_size, "uuid": str(int(time.time() * 1000)) + _rnd(20),
                  "pageFreshId": cur["pageFreshId"], "thirdKey": cur["thirdKey"], "appCode": 0}
        d = _json(client.get(SEARCH_URL, params=params, headers={**HEADERS, "thirdkey": cur["thirdKey"], "appcode": "0"}),
                  "search")
        if d.get("code") != 0:
            raise StructureError(f"job58 search code={d.get('code')!r} version={APP_VER} body={str(d)[:150]}")
        data = need(d, "data", typ=dict)
        groups = need(data, "searchList", typ=list) if "searchList" in data else []
        if not groups:
            return []
        if groups[0].get("hasMore") is False:  # a full last page would not look short
            cur["done"] = True
        return need(groups[0], "list", typ=list)

    def job_id(self, job: dict) -> str:
        return str(need(job, "infoId", typ=(str, int)))

    def detail(self, client, job: dict) -> dict:
        session = str(int(time.time() * 1000)) + _rnd(32)
        params = {"app": "wxzzmp", "lon": "", "lat": "", "session": session, "timestamp": int(time.time() * 1000),
                  "mediauid": "", "devidmd5": "IMEI", "utm_source": "58un", "spm": "", "brand": "apple",
                  "os": "mac", "model": "", "city": job.get("dispLocalName") or "bj", "infoid": self.job_id(job)}
        params["sign"] = luna_sign(params)
        headers = {**HEADERS, "content-type": "application/json", "version": "2.2", "id58": "",
                   "cookie": f'PPU="";id58=;sessionid={session};abtest=;'}
        try:
            r = client.get(LUNA_URL, params=params, headers=headers)
        except httpx.TooManyRedirects as e:
            raise Blocked("job58 detail: redirected to home page (risk control)") from e
        d = _json(r, "detail")
        if d.get("code") != 1:
            raise StructureError(f"job58 detail code={d.get('code')!r} version={APP_VER}")
        return need(d, "data", typ=dict)

    def meta(self, job: dict, detail: dict | None) -> dict:
        url = job.get("originUrl") or None
        pt = ((detail or {}).get("extParamMap") or {}).get("posttime")
        at = f"{pt.replace(' ', 'T')}+08:00" if isinstance(pt, str) and len(pt) >= 19 else None
        return {"source_url": url, "apply_url": url, "published_at": at,
                "published_at_kind": "refresh" if at else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        ext = (detail or {}).get("extParamMap") or {}
        return {"title": job.get("title"), "company": job.get("companyName"), "city": job.get("location"),
                "salary_text": ext.get("salary"), "description": ext.get("content") or None}

    def probe_payload(self) -> dict:
        return {"keyword": "服务员", "city": "北京"}
