"""Liepin (liepin.com) mini-program gateway. Keyword search, anonymous; the list has no JD, so detail() does a hop.

Six headers are required, only their presence is checked: the client info, a new trace id per request and ONE virtual
device id per install (a rotating one reads as a bot), kept in DJS_HOME. Hard ceiling: 2 pages x 40 = 80 results per
keyword (page 3 is always empty). Keyword matching is strict: a long compound keyword returns 0 ("行程策划师" 0, "行程策划"
40). There is no city parameter, so the engine filters by the row's city (city_filter='client'). A missing
`jobCardList` is how the gateway says "no results". The detail is the mini-program's job-detail call on api-ac (the list
is api-c); the web pages are IP-scored and stay blocked for hours, so nothing falls back to them. version, version_code
and client_id drift with mini-program releases: the version is put into error texts.
"""
from __future__ import annotations

import json
import secrets
import string

from djs import db

from .base import Delisted, Login, ParseError, Provider, StructureError, get_json, need, split_places

LIST_URL = "https://api-c.liepin.com/api/com.liepin.searchfront4c.wechat-search-job-by-main"
DETAIL_URL = "https://api-ac.liepin.com/api/com.liepin.job.capp.job.job-detail"
VER, VER_CODE, CLIENT_ID = "4.4.18", 40418, 80001      # the mini-program release captured on 2026-09-19
ID_CHARS = string.digits + string.ascii_lowercase      # the mini-program's ids are 0-9a-z, not hex
DEVICE_FILE = "liepin-device-id"
COMPACT = {"separators": (",", ":")}


def _id16() -> str:
    return "".join(secrets.choice(ID_CHARS) for _ in range(16))


def _device_id() -> str:
    """One id per install, like the real client keeps one: written on first use, read back on every request."""
    path = db.home() / DEVICE_FILE
    if path.exists() and len(known := path.read_text().strip()) == 16:
        return known
    path.write_text(new := _id16())
    return new


def _headers() -> dict:
    info = {"client_id": CLIENT_ID, "version": VER, "version_code": VER_CODE, "dev_type": 1,
            "channel_code": "mp_tx_weixin"}
    return {"Content-Type": "application/json", "x-fscp-std-info": json.dumps(info, **COMPACT),
            "x-fscp-trace-id": _id16(), "x-fscp-bi-stat": json.dumps({"virtual_device_id": _device_id()}, **COMPACT),
            "x-client-type": "wxa", "x-fscp-version": "1.1"}


def _post(client, url: str, data: dict) -> dict:
    body = {"data": data, "version": VER, "version_code": VER_CODE, "dev_type": 1, "client_id": CLIENT_ID}
    d = get_json(client.post(url, json=body, headers=_headers()))
    if d.get("flag") != 1:
        raise StructureError(f"liepin flag:{d.get('flag')!r} version={VER} body={str(d)[:150]}")
    return d


class Liepin(Provider):
    name = "liepin"
    label = "猎聘"
    kind = "keyword"
    auth = "optional"
    city_filter = "client"
    interval = 4.0          # the JD costs one request per job: 80 jobs at 12 s would take 20 minutes
    jitter = 2.0
    min_interval = 2.0
    page_size = 40
    first_page = 0
    page_cap = 2
    note = "Mini-program API: at most 80 results per keyword, strict keyword match, no city filter (applied after)."
    login = Login(url="https://www.liepin.com/", method="qr", domain="liepin.com", cookies_any=("lt_auth",),
                  unlocks="暂无明显提升（不登录也能完整搜索）")

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        condition = {"keyword": payload["keyword"], "sortType": "intelligent", "pageSize": self.page_size,
                     "currentPage": page}
        d = _post(client, LIST_URL, {"condition": condition,
                                     "passThroughFrom": {"ckId": "", "fkId": "", "skId": "", "scene": "input"}})
        inner = need(d, "data", "data", typ=dict)
        return need(inner, "jobCardList") if "jobCardList" in inner else []

    def job_id(self, job: dict) -> str:
        return str(need(job.get("job") or job, "jobId", typ=(str, int)))

    def detail(self, client, job: dict) -> dict | None:
        jid, kind = self.job_id(job), str((job.get("job") or {}).get("jobKind") or "2")
        if not jid.isdigit():
            raise StructureError(f"liepin job-detail needs a numeric jobId, got {jid!r}")
        data = need(_post(client, DETAIL_URL, {"jobId": int(jid), "jobKind": kind}), "data", typ=dict)
        if data.get("expired"):
            raise Delisted(f"liepin job-detail: posting expired jobId={jid}")
        jd = (data.get("jobDuty") or "").replace("\x00", "").strip()
        if not jd:
            raise ParseError(f"liepin job-detail: jobDuty empty jobId={jid} version={VER}")
        return {"jd": jd, "workArea": data.get("workArea"), "education": data.get("requireEduLevel"),
                "experience": data.get("requireWorkYears")}

    def meta(self, job: dict, detail: dict | None) -> dict:
        j = job.get("job") or {}
        url, stamp = j.get("link") or None, j.get("refreshTime")      # "yyyyMMddHHmmss": the refresh time
        at = (f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[8:10]}:{stamp[10:12]}:{stamp[12:]}+08:00"
              if isinstance(stamp, str) and len(stamp) == 14 and stamp.isdigit() else None)
        return {"source_url": url, "apply_url": url, "published_at": at,
                "published_at_kind": "refresh" if at else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        j, comp, d = job.get("job") or {}, job.get("comp") or {}, detail or {}
        city, district = split_places([j.get("dq")])        # dq is "北京-朝阳区", or just a city
        return {"title": j.get("title"), "company": comp.get("compName"), "city": city,
                "district": district or (d.get("workArea") or "").partition("-")[2] or None,
                "education": j.get("requireEduLevel") or d.get("education"),
                "experience": j.get("requireWorkYears") or d.get("experience"),
                "salary_text": j.get("salary"), "description": d.get("jd")}

    def probe_payload(self) -> dict:
        return {"keyword": "算法工程师"}
