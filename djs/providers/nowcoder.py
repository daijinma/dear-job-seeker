"""NowCoder (nowcoder.com). Keyword search, POST form, JD is in the list item (no detail hop).

City: the form field `jobCity` takes the plain city name (found in the site's own search bundle); an unknown
name yields 0 rows, never a nationwide list. A job can list several cities (`jobCityList`).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .base import Provider, StructureError, get_json, join_places, months_suffix, need

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


class NowCoder(Provider):
    name = "nowcoder"
    label = "牛客网"
    kind = "keyword"
    auth = "public"
    city_filter = "server"
    interval = 0.6
    min_interval = 0.3
    page_size = 20
    first_page = 1
    page_cap = None
    note = "POST form, has JD in list, no detail hop"

    URL = "https://www.nowcoder.com/np-api/u/job/search"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        form = {"query": payload["keyword"], "page": page, "pageSize": self.page_size}
        if city := self.city_code(payload.get("city")):
            form["jobCity"] = city
        r = client.post(self.URL, data=form,
                        headers={"User-Agent": UA, "Referer": "https://www.nowcoder.com/jobs/fulltime"})
        d = get_json(r)
        if d.get("code") != 0:
            raise StructureError(f"nowcoder code:{d.get('code')!r} msg={d.get('msg')!r}")
        return need(d, "data", "datas")

    def job_id(self, job: dict) -> str:
        return str(need(job, "id", typ=(str, int)))

    def meta(self, job: dict, detail: dict | None) -> dict:
        # createTime is the real creation time; updateTime/refreshTime are refresh stamps, never published_at.
        jid, ct = job.get("id"), job.get("createTime")
        ok = isinstance(ct, (int, float)) and ct > 0
        return {"source_url": f"https://www.nowcoder.com/jobs/detail/{jid}" if jid else None, "apply_url": None,
                "published_at": datetime.fromtimestamp(ct / 1000, tz=timezone.utc).isoformat() if ok else None,
                "published_at_kind": "original" if ok else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        company = job.get("recommendInternCompany") or {}
        return {"title": job.get("jobName"),
                "company": company.get("companyName"),
                "city": join_places(job.get("jobCityList") or [job.get("jobCity")]),
                "salary_text": _salary(job),
                "description": _requirements(job)}

    def probe_payload(self) -> dict:
        return {"keyword": "算法"}


def _salary(job: dict) -> str | None:
    """salaryType gives the unit: 2 = K/month (+ salaryMonth), 1 = yuan/day. Unknown unit -> None."""
    lo, hi = job.get("salaryMin"), job.get("salaryMax")
    if not (isinstance(lo, int) and isinstance(hi, int) and 0 < lo and 0 < hi < 9999999):
        return None
    kind = str(job.get("salaryType") or "")
    if kind == "1":
        return f"{lo}-{hi}元/天"
    if kind == "2":
        return f"{lo}-{hi}K{months_suffix(job.get('salaryMonth'))}"
    return None


def _requirements(job: dict) -> str | None:
    """`ext` is itself a JSON string; the JD lives in its `requirements` key."""
    try:
        v = json.loads(job.get("ext") or "")["requirements"]
    except (ValueError, TypeError, KeyError):
        return None
    return v if isinstance(v, str) and v else None
