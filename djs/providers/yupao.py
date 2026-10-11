"""Yupao (鱼泡直聘) mini-program API. Keyword search; the list item already carries the full JD.

Anonymous access works (empty token/uid). Signing: see _yupao_sign. A non-JSON / non-object body is
treated as Blocked (Aliyun WAF challenge page), a non-zero business code as StructureError with the
package version in the text so a rotated salt is easy to spot. Page size is fixed ~15 by the server;
the end is detected by short/empty page, page_cap is only a runaway fuse (~500 results per query).
The list has no original publish time (sortTime is a refresh/sort time). A job's page on the web site is
WEB_PAGE with its numeric jobId (jobBizId is the API's id).
Education, experience and the welfare are tags in `showTags` (type 17, 15 and 4); the place is `areaInfo` (city +
district).
"""
from __future__ import annotations

import re

from . import _yupao_sign as ys
from .base import Blocked, Provider, StructureError, check_http, need, tag_names

URL = "https://yupao-prod.yupaowang.com/job/v2/search/job/search"
WEB_PAGE = "https://www.yupao.com/zhaogong/{}.html"
TAG_EXPERIENCE, TAG_EDUCATION, TAG_BENEFIT = 15, 17, 4     # showTags[].type; 4: 包吃, 包住, 五险 ... (6, 月结, is a pay term)


def _json(r) -> dict:
    check_http(r)
    try:
        d = r.json()
    except ValueError as e:
        raise Blocked(f"yupao: non-JSON response (WAF?): {r.text[:120]!r}") from e
    if not isinstance(d, dict):
        raise Blocked(f"yupao: top level is {type(d).__name__}, not object (WAF?)")
    return d


class Yupao(Provider):
    name = "yupao"
    label = "鱼泡直聘"
    kind = "keyword"
    auth = "public"
    city_filter = "server"
    interval = 3.0
    min_interval = 1.5
    page_size = 15
    first_page = 1
    page_cap = 34
    requires_env = ("DJS_YUPAO_NONCE_KEY", "DJS_YUPAO_SIGN_SALT")
    note = ("Needs env DJS_YUPAO_NONCE_KEY and DJS_YUPAO_SIGN_SALT; JD is in the list; "
            "~500 results per query; WAF may block")

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        city = self.city_code(payload.get("city"))
        body = {"keywords": payload["keyword"], "currentPage": page, "pageSize": self.page_size,
                "areaIds": [city] if city else [],
                "listType": 1, "reselectArea": False, "wechat_token": ys.APPID}
        d = _json(client.post(URL, json=body, headers=ys.headers(body)))
        if d.get("code") != 0:
            raise StructureError(f"yupao code:{d.get('code')!r} message={d.get('message')!r} version={ys.VER}")
        return need(d, "data", "list")

    def job_id(self, job: dict) -> str:
        return str(need(job, "jobBizId", typ=(str, int)))

    def meta(self, job: dict, detail: dict | None) -> dict:
        st = job.get("sortTime")
        at = f"{st.replace(' ', 'T')}+08:00" if st else None
        page_id = job.get("jobId")
        url = WEB_PAGE.format(page_id) if str(page_id or "").isdigit() else None
        return {"source_url": url, "apply_url": None, "published_at": at,
                "published_at_kind": "refresh" if at else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        title = re.sub(r"<[^>]+>", "", job.get("title") or "") or None
        area = job.get("areaInfo") or {}
        shown = [t for t in job.get("showTags") or [] if isinstance(t, dict)]
        tags = {t.get("type"): t.get("name") for t in shown}
        return {"title": title, "company": (job.get("companyInfo") or {}).get("enterpriseName") or None,
                "city": area.get("cityName") or job.get("address"), "district": area.get("countyName") or None,
                "education": tags.get(TAG_EDUCATION), "experience": tags.get(TAG_EXPERIENCE),
                "benefits": tag_names([t for t in shown if t.get("type") == TAG_BENEFIT], "name"),
                "salary_text": (job.get("showPerfect") or {}).get("salary"), "description": job.get("detail") or None}

    def probe_payload(self) -> dict:
        return {"keyword": "普工"}
