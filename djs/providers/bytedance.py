"""ByteDance own careers portal (jobs.bytedance.com). Single-company, keyword search, JD is in the list.

Paging is offset based: offset = page * page_size. Path /api/v1/search/job/posts works; the old
/public/supplier/... path returns 400.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .base import Provider, StructureError, get_json, need

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


class Bytedance(Provider):
    name = "bytedance"
    label = "字节跳动招聘"
    kind = "keyword"
    auth = "public"
    interval = 0.8
    min_interval = 0.4
    page_size = 20
    first_page = 0
    page_cap = None
    note = "Single-company portal (ByteDance only); offset paging; JD in list"

    URL = "https://jobs.bytedance.com/api/v1/search/job/posts"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        r = client.post(self.URL, headers={"User-Agent": UA},
                        json={"keyword": payload["keyword"],
                              "limit": self.page_size, "offset": page * self.page_size})
        d = get_json(r)
        if d.get("code") != 0:
            raise StructureError(f"bytedance code:{d.get('code')!r} body={str(d)[:150]}")
        return need(d, "data", "job_post_list")

    def job_id(self, job: dict) -> str:
        return str(need(job, "id", typ=(str, int)))

    def meta(self, job: dict, detail: dict | None) -> dict:
        # joinbytedance.com/search/{id} is where jobs.bytedance.com/en/position/{id}/detail redirects to.
        jid = job.get("id")
        url = f"https://joinbytedance.com/search/{jid}" if jid else None
        pt = job.get("publish_time")      # epoch milliseconds, spread over years, so a real publish time
        ts = datetime.fromtimestamp(pt / 1000, tz=timezone.utc).isoformat() \
            if isinstance(pt, (int, float)) and pt > 0 else None
        return {"source_url": url, "apply_url": url,
                "published_at": ts, "published_at_kind": "original" if ts else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        jd = "\n\n".join(s for s in (str(job.get(k) or "").strip() for k in ("description", "requirement")) if s)
        return {"title": job.get("title"), "company": "字节跳动",
                "city": (job.get("city_info") or {}).get("name"), "salary_text": None,
                "description": jd or None}

    def probe_payload(self) -> dict:
        return {"keyword": "算法"}
