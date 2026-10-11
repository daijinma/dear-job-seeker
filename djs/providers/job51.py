"""51job (前程无忧) mini-program API. Keyword search, optional city; JD needs a detail hop.

sign = HmacSHA256("/{path}?api_key=51job&timestamp={sec}&{query}", key).hex, key = the ASCII bytes of
env DJS_JOB51_HMAC_KEY (the hex text itself, NOT hex-decoded). The server verifies the raw query
string, so the signed string and the requested URL must be byte-identical: build the whole URL
ourselves and never pass params= to httpx. Success is status "1"; "110011" = signature rejected.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from urllib.parse import quote

from .base import Provider, StructureError, env, get_json, need

HOST = "https://cupid.51job.com"
PKG_VERSION = "450"


def signed_url(path: str, params: dict) -> tuple[str, str]:
    """Returns (url, signature) for the exact query string that will be sent."""
    q = f"/{path}?api_key=51job&timestamp={int(time.time())}"
    q += "".join(f"&{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in params.items())
    sig = hmac.new(env("DJS_JOB51_HMAC_KEY").encode(), q.encode(), hashlib.sha256).hexdigest()
    return HOST + q, sig


def _get(client, path: str, params: dict) -> dict:
    url, sig = signed_url(path, params)
    d = get_json(client.get(url, headers={"sign": sig, "From-Domain": "51job_weixin_wxapp", "uuid": uuid.uuid4().hex}))
    if d.get("status") != "1":
        raise StructureError(f"job51 {path} status={d.get('status')!r} version={PKG_VERSION} body={str(d)[:150]}")
    return d


class Job51(Provider):
    name = "job51"
    label = "前程无忧"
    kind = "keyword"
    auth = "public"
    city_filter = "server"
    interval = 3.0
    min_interval = 1.5
    page_size = 50
    first_page = 1
    page_cap = 20
    requires_env = ("DJS_JOB51_HMAC_KEY",)
    note = "Needs env DJS_JOB51_HMAC_KEY; hard cap 1000 results (20 pages); city optional"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        params = {"keyword": payload["keyword"], "pageSize": self.page_size, "pageNum": page,
                  "searchType": 2, "scene": 12, "sortType": 0}
        if city := self.city_code(payload.get("city")):
            params["jobArea"] = city
        return need(_get(client, "open/noauth/searchOptimization", params), "resultbody", "job", "items")

    def job_id(self, job: dict) -> str:
        return str(need(job, "jobId", typ=(str, int)))

    def detail(self, client, job: dict) -> dict | None:
        jid = self.job_id(job)
        body = need(_get(client, f"open/noauth/jobs/detail/base/{jid}", {"jobId": jid}), "resultbody", typ=dict)
        need(body, "detailJobInfo", typ=dict)
        return body

    def meta(self, job: dict, detail: dict | None) -> dict:
        jid, pinyin = job.get("jobId"), job.get("hrefAreaPinYin")
        url = f"https://jobs.51job.com/{pinyin}/{jid}.html" if jid and pinyin else None
        issue = job.get("issueDateString")
        at = f"{issue.replace(' ', 'T')}+08:00" if issue else None  # HR can refresh: never 'original'
        return {"source_url": url, "apply_url": url, "published_at": at,
                "published_at_kind": "refresh" if at else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        info = (detail or {}).get("detailJobInfo") or {}
        return {"title": job.get("jobName"), "company": job.get("fullCompanyName") or job.get("companyName"),
                "city": (job.get("jobAreaLevelDetail") or {}).get("cityString"),
                "salary_text": job.get("provideSalaryString"), "description": info.get("jobDescribe") or None}

    def probe_payload(self) -> dict:
        return {"keyword": "算法工程师"}
