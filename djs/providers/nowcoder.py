"""NowCoder (nowcoder.com). Keyword search, POST form, JD is in the list item (no detail hop).

City: the form field `jobCity` takes the plain city name (found in the site's own search bundle); an unknown
name yields 0 rows, never a nationwide list. A job can list several cities (`jobCityList`).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from djs.normalize import salary_range

from .base import Provider, StructureError, get_json, join_places, months_suffix, need

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
# eduLevel is a code; 0 and 5000 were checked against live job texts, the others follow the site's own ladder
EDUCATION = {0: "不限", 4000: "大专", 5000: "本科", 6000: "硕士", 7000: "博士"}


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
        # the site answers 0 rows for any query containing a space ("Python 开发" -> 0, "Python开发" -> 4 in 杭州)
        form = {"query": "".join(payload["keyword"].split()), "page": page, "pageSize": self.page_size}
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
                "education": EDUCATION.get(job.get("eduLevel")),
                "description": _description(job),
                **_salary(job)}

    def probe_payload(self) -> dict:
        return {"keyword": "算法"}


def _salary(job: dict) -> dict:
    """salaryType gives the unit: 2 = K/month (+ salaryMonth payments), 1 = yuan/day. 0-9999999 is the site's "no
    salary" and an unknown type is not guessed at: both give no salary at all."""
    lo, hi, months = job.get("salaryMin"), job.get("salaryMax"), job.get("salaryMonth")
    kind = str(job.get("salaryType") or "")
    if kind == "1":
        numbers, text = salary_range(lo, hi, "元/天"), f"{lo}-{hi}元/天"
    elif kind == "2" and isinstance(lo, int) and isinstance(hi, int):
        numbers, text = salary_range(lo * 1000, hi * 1000, "元/月", months), f"{lo}-{hi}K{months_suffix(months)}"
    else:
        return {}
    return {"salary_text": text, **numbers} if numbers else {}


def _description(job: dict) -> str | None:
    """`ext` is itself a JSON string: `infos` is the intro and duties, `requirements` what the candidate needs (some
    jobs repeat one text in both)."""
    try:
        ext = json.loads(job.get("ext") or "")
    except (ValueError, TypeError):
        return None
    texts = [ext.get(k) for k in ("infos", "requirements")] if isinstance(ext, dict) else []
    return "\n\n".join(dict.fromkeys(t.strip() for t in texts if isinstance(t, str) and t.strip())) or None
