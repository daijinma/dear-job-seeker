"""Iguopin (iguopin.com). Keyword search, anonymous JSON POST, list item is the detail.

Quirks (verified in the source repo): payload must be nested {search, recom} (flat payload silently
returns code:1 + data:null); `total` is capped at 400 so it is not a real count; page > 20 wraps
around to duplicates (hence page_cap=20); an empty result has `list: null` with `total: 0`.
City: `district` takes a dotted GB/T 2260 path ("000000.330000.330100" = 杭州; a bare "330100" or a name
returns 0 rows), see djs.cities. A job can list several cities (`district_list`).
"""
from __future__ import annotations

from .base import Blocked, Provider, StructureError, get_json, join_places, months_suffix, need

URL = "https://gp-api.iguopin.com/api/jobs/v1/recom-job"
OK_CODE = 200                      # business success is 200, not 0
RECOM = {"update_time": True, "company_nature": True, "hot_job": True}   # copied from the real request
BUSY_MSG = "访问人数过多"            # ambiguous wording; treated as Blocked (cooldown beats a retry loop)


class Iguopin(Provider):
    name = "iguopin"
    label = "国聘"
    kind = "keyword"
    auth = "public"
    city_filter = "server"
    interval = 3.0
    min_interval = 1.0
    page_size = 20
    first_page = 1
    page_cap = 20
    note = "cap 20 (400 results); null list+total 0 = valid empty"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        search = {"page": page, "page_size": self.page_size, "keyword": payload["keyword"]}
        if city := self.city_code(payload.get("city")):
            search["district"] = [city]
        r = client.post(URL, headers={"content-type": "application/json"},
                        json={"search": search, "recom": RECOM})
        d = get_json(r)
        if d.get("code") != OK_CODE:
            msg = str(d.get("msg") or "")
            if BUSY_MSG in msg:
                raise Blocked(f"iguopin busy msg={msg[:80]!r}")
            raise StructureError(f"iguopin code={d.get('code')!r} msg={msg[:80]!r}")
        data = need(d, "data", typ=dict)
        if "list" not in data:
            raise StructureError("iguopin: data.list missing")
        return _items(data)

    def job_id(self, job: dict) -> str:
        return str(need(job, "job_id", typ=(str, int)))

    def meta(self, job: dict, detail: dict | None) -> dict:
        # refresh_time is a refresh stamp, not the original publish time.
        jid, rt = job.get("job_id"), job.get("refresh_time")
        return {"source_url": f"https://www.iguopin.com/job/detail?id={jid}" if jid else None, "apply_url": None,
                "published_at": f"{rt.replace(' ', 'T')}+08:00" if rt else None,
                "published_at_kind": "refresh" if rt else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        places = (d.get("area_cn") for d in job.get("district_list") or [] if isinstance(d, dict))
        return {"title": job.get("job_name"), "company": job.get("company_name"),
                "city": join_places(places), "salary_text": _salary(job), "description": job.get("contents")}

    def probe_payload(self) -> dict:
        return {"keyword": "算法"}


def _items(data: dict) -> list[dict]:
    """null list is a valid empty only when total is 0; otherwise the structure broke."""
    lst = data["list"]
    if lst is None:
        if data.get("total"):
            raise StructureError(f"iguopin: list is null but total={data['total']!r}")
        return []
    if not isinstance(lst, list):
        raise StructureError(f"iguopin: data.list is {type(lst).__name__}")
    return lst


def _salary(job: dict) -> str | None:
    """The unit comes from the site (`wage_unit_cn`: 元/月, 元/天, ...), so no code table is needed."""
    if job.get("is_negotiable"):
        return "面议"
    lo, hi, unit = job.get("min_wage"), job.get("max_wage"), job.get("wage_unit_cn")
    if not (lo and hi and unit):
        return None
    return f"{lo}-{hi}{unit}{months_suffix(job.get('months'))}"
