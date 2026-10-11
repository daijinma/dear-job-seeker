"""Chinahr (chinahr.com). Keyword search; the list has no JD, so detail() does a GET hop.

The search body field is `keyWord` (capital W). `keyword` is silently ignored and returns a
default list unrelated to the query.
City: `localId` takes chinahr's own city id (POST /newchr/open/dic {"keys": "city"}, see djs.cities); a foreign
id is silently ignored or gives an unrelated set. The site also returns jobs whose location is the whole province
or "全国", so row()['city'] is not always the searched city.
"""
from __future__ import annotations

from .base import Provider, StructureError, get_json, need


class Chinahr(Provider):
    name = "chinahr"
    label = "新华英才"
    kind = "keyword"
    auth = "public"
    city_filter = "server"
    interval = 0.8
    min_interval = 0.5
    page_size = 50
    first_page = 0
    page_cap = None
    note = "`keyWord` capital W; city via localId; detail GET open/job/detail?id="

    URL = "https://www.chinahr.com/newchr/open/job/search"
    DETAIL_URL = "https://www.chinahr.com/newchr/open/job/detail"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        body = {"page": page, "pageSize": self.page_size, "keyWord": payload["keyword"]}
        if city := self.city_code(payload.get("city")):
            body["localId"] = city
        r = client.post(self.URL, json=body, headers={"Content-Type": "application/json"})
        d = _ok(get_json(r), "list")
        return need(d, "data", "jobItems")

    def job_id(self, job: dict) -> str:
        return str(need(job, "jobId", typ=(str, int)))

    def detail(self, client, job: dict) -> dict | None:
        r = client.get(self.DETAIL_URL, params={"id": self.job_id(job)})
        data = need(_ok(get_json(r), "detail"), "data", typ=dict)
        info = need(data, "jobInfo", typ=dict)
        # jobDesc (HTML, stored as-is) may legitimately be empty for blue-collar jobs.
        return {"jobDesc": info.get("jobDesc") or "",
                "companyDesc": (data.get("comInfo") or {}).get("companyDesc") or ""}

    def meta(self, job: dict, detail: dict | None) -> dict:
        # The site returns no time field anywhere (list and detail), so published_at is always unknown.
        jid = job.get("jobId")
        url = f"https://www.chinahr.com/detail/{jid}" if jid else None
        return {"source_url": url, "apply_url": url,
                "published_at": None, "published_at_kind": "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        return {"title": job.get("jobName"), "company": job.get("comName"),
                "city": job.get("workPlace"), "salary_text": job.get("salary"),
                "description": (detail or {}).get("jobDesc") or None}

    def probe_payload(self) -> dict:
        return {"keyword": "算法"}


def _ok(d: dict, what: str) -> dict:
    if d.get("code") != 1:                     # success business code is 1
        raise StructureError(f"chinahr {what} code:{d.get('code')!r} body={str(d)[:150]}")
    return d
