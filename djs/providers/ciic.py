"""CIIC (ciiczhaopin.com). Keyword search over SERVER-RENDERED HTML (no list JSON API).

Zero hits remove the whole list container and write no empty-state text, so "0 rows" alone
cannot tell "really empty" from "redirected / layout changed". Only when BOTH skeleton
anchors (present on empty and non-empty pages) exist is 0 rows a valid empty.
The site caps at 400 results (20 pages); beyond that it silently falls back to page 1,
hence page_cap=20. City is the short Chinese name passed as `workplace` ("杭州"; "杭州市" or an unknown
name returns 0 rows, never a nationwide list).
"""
from __future__ import annotations

import html
import re

from .base import ParseError, Provider, StructureError, check_http, need

BASE = "https://www.ciiczhaopin.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
SKELETON = ("职位列表-中智招聘网", "main-inner-container")

ROW_RE = re.compile(r'<li class="position-info-item" uuid="([0-9a-f]+)".*?</li>', re.S)
FIELDS = {
    "title": re.compile(r'class="job-name ellipsis"[^>]*>\s*<a[^>]*title="([^"]*)"', re.S),
    "salary": re.compile(r'class="job-salary ellipsis" title="([^"]*)"'),
    "address": re.compile(r'class="job-address ellipsis" title="([^"]*)"'),
    "education": re.compile(r'class="job-education ellipsis" title="([^"]*)"'),
    "companyName": re.compile(r'<h3>\s*<a title="([^"]*)"', re.S),
}
# The page has two describe-content blocks (job + company); anchor on the job heading.
JD_RE = re.compile(r'职位描述</h4>\s*<p class="describe-content">(.*?)</p>', re.S)
REFRESH_RE = re.compile(r"更新于[:：]\s*(\d{4}-\d{2}-\d{2})")
# The detail page's header is a row of class-less spans: place, headcount, experience, education, update date.
INFO_RE = re.compile(r'<div class="job-info-container"[^>]*>(.*?)</div>', re.S)
SPAN_RE = re.compile(r"<span>([^<]*)</span>")
EXPERIENCE_RE = re.compile(r"^(\d+-\d+年|\d+年以[上下]|应届生?|经验不限|不限)$")


class Ciic(Provider):
    name = "ciic"
    label = "中智招聘"
    kind = "keyword"
    auth = "public"
    city_filter = "server"
    interval = 3.0
    jitter = 1.0
    min_interval = 1.5
    page_size = 20
    first_page = 1
    page_cap = 20
    note = "SSR HTML parsing; 400-result cap (20 pages), out-of-range pages silently repeat page 1"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        params = {"page": page, "keyword": payload["keyword"]}
        if city := self.city_code(payload.get("city")):
            params["workplace"] = city
        r = client.get(f"{BASE}/position/list", params=params, headers={"User-Agent": UA})
        check_http(r)
        missing = [a for a in SKELETON if a not in r.text]
        if missing:
            raise StructureError(f"ciic list: skeleton anchors missing {missing}, len={len(r.text)}")
        return [_parse_row(m.group(0), m.group(1)) for m in ROW_RE.finditer(r.text)]

    def job_id(self, job: dict) -> str:
        return str(need(job, "uuid", typ=str))

    def detail(self, client, job: dict) -> dict | None:
        uuid = self.job_id(job)
        r = client.get(f"{BASE}/position/detail", params={"uuid": uuid}, headers={"User-Agent": UA})
        check_http(r)
        m = JD_RE.search(r.text)
        if not m:
            raise ParseError(f"ciic detail {uuid}: 职位描述 block not found, len={len(r.text)}")
        refresh = REFRESH_RE.search(r.text)
        box = INFO_RE.search(r.text)
        spans = [html.unescape(v).strip() for v in SPAN_RE.findall(box.group(1))] if box else []
        # JD kept as-is (contains <br>); a short JD is the employer's, not a failure.
        return {"description": html.unescape(m.group(1)).strip(),
                "refresh_date": refresh.group(1) if refresh else None,
                "experience": next((v for v in spans if EXPERIENCE_RE.match(v)), None)}

    def meta(self, job: dict, detail: dict | None) -> dict:
        uuid = job.get("uuid")
        url = f"{BASE}/position/detail?uuid={uuid}" if uuid else None
        d = (detail or {}).get("refresh_date")
        # Only a date exists ("更新于"), so T00:00 is a convention, and the kind is refresh.
        published_at = f"{d}T00:00:00+08:00" if d else None
        return {"source_url": url, "apply_url": url, "published_at": published_at,
                "published_at_kind": "refresh" if published_at else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        return {"title": job.get("title"), "company": job.get("companyName"),
                "city": job.get("city"), "district": job.get("district"), "education": job.get("education"),
                "experience": (detail or {}).get("experience"), "salary_text": job.get("salary") or None,
                "description": (detail or {}).get("description") or None}

    def probe_payload(self) -> dict:
        return {"keyword": "前端"}


def _parse_row(row: str, uuid: str) -> dict:
    out = {"uuid": uuid}
    for key, rx in FIELDS.items():
        m = rx.search(row)
        if m:
            out[key] = html.unescape(m.group(1)).strip()
    for key in ("title", "companyName"):
        if not out.get(key):
            raise ParseError(f"ciic row {uuid}: {key} not found, row={row[:200]!r}")
    if out.get("address"):
        # "广东|广州" -> 广州, "上海|上海|黄浦" -> 上海 + 黄浦: province | city | district.
        parts = [p for p in out["address"].split("|") if p]
        out["city"] = parts[1] if len(parts) > 1 else parts[0]
        if len(parts) > 2:
            out["district"] = parts[2]
    return out
