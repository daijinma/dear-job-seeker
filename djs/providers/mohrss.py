"""Mohrss public recruitment site (job.mohrss.gov.cn). Keyword search, SSR page, JD is in the list.

Jobs are JSON in `<input name="findjoblist" value="[...]">`. A zero-hit query still renders the
input with value="[]"; a missing input means the page structure changed (StructureError).
`city` must be a national administrative division code (e.g. 110100); other codes make the site 500.
The raw items also carry HR names/phones (aae004/aae005); row() deliberately does not map them.
"""
from __future__ import annotations

import html
import json
import re

from .base import ParseError, Provider, StructureError, check_http, need

# Anchor on name: the page has several <input value="[...]"> and a loose regex runs into the next array.
LIST_RE = re.compile(r'name="findjoblist"\s+value="(\[.*?\])"', re.S)
# ASCII parentheses in the query make the site return a page without the input; full-width works.
PAREN = str.maketrans("()", "（）")


class Mohrss(Provider):
    name = "mohrss"
    label = "公共招聘网"
    kind = "keyword"
    auth = "public"
    interval = 1.0
    min_interval = 0.5
    page_size = 20
    first_page = 1
    page_cap = None
    note = "AREA needs national division codes; list carries JD; items include HR contact data (not stored)"

    URL = "http://job.mohrss.gov.cn/cjobs/jobinfolist/listJobinfolist"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        # The server session is sticky: always send AREA, or the previous query's value leaks in.
        params = {"pageNo": page, "textfield": payload["keyword"].translate(PAREN),
                  "AREA": payload.get("city") or ""}
        r = client.get(self.URL, params=params)
        check_http(r)
        m = LIST_RE.search(r.text)
        if not m:
            raise StructureError("mohrss: findjoblist input not found")
        try:
            jobs = json.loads(html.unescape(m.group(1)))
        except ValueError as e:
            raise ParseError(f"mohrss: findjoblist not valid JSON: {m.group(1)[:120]!r}") from e
        return jobs

    def job_id(self, job: dict) -> str:
        # acb200 = job id; aab001 is the EMPLOYER id and would merge a company's jobs into one.
        try:
            return str(need(job, "acb200", typ=(str, int)))
        except StructureError:
            return str(need(job, "md5", typ=str))

    def meta(self, job: dict, detail: dict | None) -> dict:
        # s_aae395..398 look like dates but have no documented meaning, so published_at stays unknown.
        url = job.get("ace760") or None
        return {"source_url": url, "apply_url": url,
                "published_at": None, "published_at_kind": "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        return {"title": job.get("aca112"), "company": job.get("aab004"),
                "city": job.get("area_"), "salary_text": _salary(job.get("acb241"), job.get("acb242")),
                "description": job.get("acb22a") or None}

    def probe_payload(self) -> dict:
        return {"keyword": "算法"}


def _salary(lo, hi) -> str | None:
    def num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None
    lo, hi = num(lo), num(hi)
    if lo and hi:
        return f"{lo:g}-{hi:g}元/月"
    return f"{(lo or hi):g}元/月" if (lo or hi) else None
