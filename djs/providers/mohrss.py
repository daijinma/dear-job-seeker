"""Mohrss public recruitment site (job.mohrss.gov.cn). Keyword search, SSR page, JD is in the list.

Jobs are JSON in `<input name="findjoblist" value="[...]">`. A zero-hit query still renders the
input with value="[]"; a missing input means the page structure changed (StructureError).
City: `AREA` must be a GB/T 2260 code (330100 = 杭州, any level works); a city name makes the site 500.
The raw items carry HR names/phones (aae004/aae005); fetch() drops them so they are never stored.
"""
from __future__ import annotations

import html
import json
import re

from djs import cities
from djs.normalize import salary_range

from .base import ParseError, Provider, StructureError, check_http, need

# Anchor on name: the page has several <input value="[...]"> and a loose regex runs into the next array.
LIST_RE = re.compile(r'name="findjoblist"\s+value="(\[.*?\])"', re.S)
# ASCII parentheses in the query make the site return a page without the input; full-width works.
PAREN = str.maketrans("()", "（）")
HR_CONTACT = ("aae004", "aae005")


class Mohrss(Provider):
    name = "mohrss"
    label = "公共招聘网"
    kind = "keyword"
    auth = "public"
    city_filter = "server"
    interval = 1.0
    min_interval = 0.5
    page_size = 20
    first_page = 1
    page_cap = None
    note = "AREA needs national division codes; list carries JD; HR contact data is dropped, not stored"

    URL = "http://job.mohrss.gov.cn/cjobs/jobinfolist/listJobinfolist"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        # The server session is sticky: always send AREA, or the previous query's value leaks in.
        params = {"pageNo": page, "textfield": payload["keyword"].translate(PAREN),
                  "AREA": self.city_code(payload.get("city")) or ""}
        r = client.get(self.URL, params=params)
        check_http(r)
        m = LIST_RE.search(r.text)
        if not m:
            raise StructureError("mohrss: findjoblist input not found")
        try:
            jobs = json.loads(html.unescape(m.group(1)))
        except ValueError as e:
            raise ParseError(f"mohrss: findjoblist not valid JSON: {m.group(1)[:120]!r}") from e
        return _without_contacts(jobs)

    def job_id(self, job: dict) -> str:
        # acb200 = job id; aab001 is the EMPLOYER id and would merge a company's jobs into one.
        try:
            return str(need(job, "acb200", typ=(str, int)))
        except StructureError:
            return str(need(job, "md5", typ=str))

    def meta(self, job: dict, detail: dict | None) -> dict:
        # s_aae397 is the posting start date: some provinces keep the employer's date, others rewrite it at
        # import (every row of a batch shows the import day), so it is labeled refresh, never original.
        url = job.get("ace760") or None
        d = job.get("s_aae397")
        at = f"{d}T00:00:00+08:00" if isinstance(d, str) and len(d) == 10 else None
        return {"source_url": url, "apply_url": url,
                "published_at": at, "published_at_kind": "refresh" if at else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        # `area_` is a province label for some provinces and a city label for others; the code is authoritative.
        # aab302 is the name of the district the job's area code points at.
        return {"title": job.get("aca112"), "company": job.get("aab004"),
                "city": cities.gb_name(job.get("area")) or job.get("area_"),
                "district": job.get("aab302") or None,
                "description": job.get("acb22a") or None, **_salary(job.get("acb241"), job.get("acb242"))}

    def probe_payload(self) -> dict:
        return {"keyword": "算法"}


def _without_contacts(jobs) -> list[dict]:
    if not isinstance(jobs, list) or not all(isinstance(j, dict) for j in jobs):
        raise StructureError(f"mohrss: findjoblist is not a list of objects: {str(jobs)[:120]!r}")
    return [{k: v for k, v in j.items() if k not in HR_CONTACT} for j in jobs]


def _salary(lo, hi) -> dict:
    """acb241 / acb242 are the wage bounds in yuan per month; with only one bound given the text is a single figure."""
    def num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None
    lo, hi = num(lo), num(hi)
    if lo and hi:
        return {"salary_text": f"{lo:g}-{hi:g}元/月", **salary_range(lo, hi)}
    return {"salary_text": f"{(lo or hi):g}元/月"} if (lo or hi) else {}
