"""Zhaopin (zhaopin.com) mini-program gateway. Keyword search; JD needs a detail hop.

All four x-zp-* headers are required (none -> code 211). They are constants the mini-program
itself ships, not credentials. x-zp-version is a capture-day version: an outdated one is
reported through the business code check (version is put in the error text), never as [].
The list envelope uses `statusCode`, the detail envelope uses `code`.
Hard cap: 3 pages x 20; each city slice has its own independent quota.
"""
from __future__ import annotations

from .base import Blocked, Provider, StructureError, get_json, need

HEADERS = {"x-zp-channel": "wxxiaochengxu", "x-zp-business-system": "73",
           "x-zp-platform": "12", "x-zp-version": "4.1.321"}


def _check_code(code, where: str) -> None:
    if code in (210, 211):
        raise Blocked(f"zhaopin {where} code:{code} version={HEADERS['x-zp-version']}")
    if code not in (200, "200"):
        raise StructureError(f"zhaopin {where} code:{code!r} version={HEADERS['x-zp-version']}")


class Zhaopin(Provider):
    name = "zhaopin"
    label = "智联招聘"
    kind = "keyword"
    auth = "public"
    city_filter = "server"
    interval = 3.0
    min_interval = 1.5
    page_size = 20          # pageSize param is ignored by the server
    first_page = 1
    page_cap = 3
    note = "x-zp-* headers required; hard cap 60 results (3 pages), city slices have separate quota"

    BASE = "https://cgate.zhaopin.com/positionbusiness"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        params = {"S_SOU_FULL_INDEX": payload["keyword"], "pageIndex": page, "pageSize": self.page_size}
        if city := self.city_code(payload.get("city")):
            params["S_SOU_WORK_CITY"] = city
        r = client.get(f"{self.BASE}/searchrecommend/searchPositions", params=params, headers=HEADERS)
        d = get_json(r)
        _check_code(d.get("statusCode", d.get("code")), "list")
        return need(d, "data", "list")

    def job_id(self, job: dict) -> str:
        return str(need(job, "number", typ=(str, int)))

    def detail(self, client, job: dict) -> dict | None:
        r = client.get(f"{self.BASE}/position/getPositionModule",
                       params={"number": self.job_id(job)}, headers=HEADERS)
        d = get_json(r)
        _check_code(d.get("code"), "detail")
        return need(d, "data", typ=dict)

    def meta(self, job: dict, detail: dict | None) -> dict:
        number = job.get("number")
        url = f"https://www.zhaopin.com/jobdetail/{number}.htm" if number else None
        pt = job.get("publishTime")
        # publishTime is bumped by refreshes, so it is never the original publish time.
        published_at = f"{pt.replace(' ', 'T')}+08:00" if pt else None
        return {"source_url": url, "apply_url": url, "published_at": published_at,
                "published_at_kind": "refresh" if published_at else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        return {"title": job.get("name"), "company": job.get("companyName"),
                "city": job.get("workCity"), "salary_text": job.get("salary60"),
                "description": _jd(detail)}

    def probe_payload(self) -> dict:
        return {"keyword": "算法工程师"}


def _jd(detail: dict | None) -> str | None:
    desc = ((detail or {}).get("position") or {}).get("desc") or {}
    return desc.get("description") or None
