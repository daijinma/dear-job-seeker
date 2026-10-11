"""康强医疗网 (kq36.com): a medical-jobs site rendering HTML (Classic ASP, UTF-8). Searching needs the user's login.

The one auth cookie is `usercookies`; its value holds %5F-style escapes and goes out raw (the engine attaches the
saved cookie to the http client). Without a valid one a search answers HTTP 200 with a ~120-byte
`<script>alert('请先登陆才能搜索更多')` page: LoginRequired. The keyword goes out the way the site's own search box
sends it, JavaScript escape() ('护士' -> '%u62A4%u58EB'), with S_Type=0 (the box's default 1 searches company names,
0 job titles). City is the site's (provinceid, cityid) pair, tabled in djs.cities as "15-146". 15 cards per page; the
list has no JD, so detail() GETs /job/<id>. A phone number costs points per reveal (a POST): never requested.
"""
from __future__ import annotations

import re
import string

from djs import cities
from djs.normalize import plain_text

from .base import Login, LoginRequired, ParseError, Provider, StructureError, check_http, need

BASE = "https://www.kq36.com"
LIST_URL = BASE + "/job_list.asp"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": BASE + "/"}
WALL = "请先登陆才能搜索更多"
SEARCH_BOX = 'name="keyword"'      # the header's search input: on every list page, with results or without
PROBE_URL = LIST_URL + "?Job_ClassI_Id=2&Job_ClassII_Id=49&provinceid=15&cityid=146"   # walled when anonymous
ESCAPE_SAFE = frozenset(string.ascii_letters + string.digits + "@*_+-./")     # what JavaScript's escape() keeps

CARD_MARK = 'class="job-card-box"'
CARD_RE = re.compile(r'<li class="job-card-box".*?</div>\s*</li>', re.S)       # the card ends with </div></li>
ID_TITLE_RE = re.compile(r'<a href="/job/(\d+)"[^>]*class="job-name"[^>]*>(.*?)</a>', re.S)
SALARY_RE = re.compile(r'class="job-salary"[^>]*>(.*?)</span>', re.S)
TAG_LIST_RE = re.compile(r'<ul class="tag-list"[^>]*>(.*?)</ul>', re.S)      # region, experience, education, job type
TAG_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
COMPANY_RE = re.compile(r'class="boss-name"[^>]*>(.*?)</span>', re.S)
DATE_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})发布")
JD_RE = re.compile(r'<div class="job-sec-text"[^>]*>(.*?)</div>', re.S)     # the 1st; the intro has more classes


def js_escape(text: str) -> str:
    """JavaScript's escape(): '%XX' below U+0100, '%uXXXX' per UTF-16 unit above."""
    def one(ch: str) -> str:
        if ch in ESCAPE_SAFE:
            return ch
        if ord(ch) < 256:
            return f"%{ord(ch):02X}"
        units = ch.encode("utf-16-be")
        return "".join(f"%u{units[i:i + 2].hex().upper()}" for i in range(0, len(units), 2))
    return "".join(map(one, text))


def _page(r) -> str:
    check_http(r)
    text = r.content.decode("utf-8", "replace")
    if WALL in text:
        raise LoginRequired("kq36: login wall (请先登陆才能搜索更多): the session is missing or expired")
    return text


def _text(rx: re.Pattern, card: str) -> str | None:
    m = rx.search(card)
    return plain_text(m.group(1)) or None if m else None


def _day(card: str) -> str | None:
    """'2026.7.13发布' -> '2026-07-13'."""
    m = DATE_RE.search(card)
    return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}" if m else None


def _parse_card(card: str) -> dict:
    ident = ID_TITLE_RE.search(card)
    if not ident:
        raise ParseError(f"kq36 card without a /job/<id> title link: {card[:200]!r}")
    box = TAG_LIST_RE.search(card)
    tags = [" ".join(plain_text(t).split()) for t in TAG_RE.findall(box.group(1))] if box else []
    region, experience, education, *_ = tags + [""] * 3
    return {"id": ident.group(1), "title": plain_text(ident.group(2)), "salary": _text(SALARY_RE, card),
            "region": region.replace(" ", ""), "experience": experience or None, "education": education or None,
            "company": _text(COMPANY_RE, card), "published": _day(card)}


class Kq36(Provider):
    name = "kq36"
    label = "康强医疗网"
    kind = "keyword"
    auth = "login"
    requires_city = False
    city_filter = "server"
    interval = 2.5
    jitter = 1.0
    min_interval = 1.0
    page_size = 15
    first_page = 1
    note = "Needs your own login (account and password or SMS). Medical jobs; job-title keyword, optional city."
    login = Login(url=BASE + "/", method="password", domain="kq36.com", cookies_any=("usercookies",),
                  probe_url=PROBE_URL, probe_fail_text=WALL, unlocks="可以搜索岗位（不登录搜不了）")

    def _list_url(self, payload: dict, page: int) -> str:
        query = f"S_Type=0&keyword={js_escape(payload['keyword'])}"
        if city := self.city_code(payload.get("city")):
            province, _, city_id = city.partition("-")
            query += f"&provinceid={province}&cityid={city_id}"
        return f"{LIST_URL}?{query}&page={page}"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        text = _page(client.get(self._list_url(payload, page), headers=HEADERS))
        cards = [_parse_card(m.group(0)) for m in CARD_RE.finditer(text)]
        if len(cards) != text.count(CARD_MARK):
            raise ParseError(f"kq36 list: {text.count(CARD_MARK)} job cards on the page, {len(cards)} read")
        if not cards and SEARCH_BOX not in text:
            raise StructureError(f"kq36 list: no job cards and no search box, len={len(text)}")
        return cards

    def job_id(self, job: dict) -> str:
        return str(need(job, "id", typ=str))

    def detail(self, client, job: dict) -> dict | None:
        text = _page(client.get(f"{BASE}/job/{self.job_id(job)}", headers=HEADERS))
        m = JD_RE.search(text)
        if not m:
            raise ParseError(f"kq36 detail {job['id']}: job description block not found, len={len(text)}")
        return {"description": m.group(1)}

    def meta(self, job: dict, detail: dict | None) -> dict:
        day = job.get("published")
        return {"source_url": f"{BASE}/job/{job.get('id')}" if job.get("id") else None, "apply_url": None,
                "published_at": f"{day}T00:00:00+08:00" if day else None,
                "published_at_kind": "unknown"}     # the list says "发布" but the site may re-stamp refreshed jobs

    def row(self, job: dict, detail: dict | None) -> dict:
        parts = [p for p in job.get("region", "").split("·") if p]      # province·city·district
        return {"title": job.get("title"), "company": job.get("company"),
                "city": cities.norm(parts[1]) if len(parts) > 1 else None,
                "district": parts[2] if len(parts) > 2 else None,
                "education": job.get("education"), "experience": job.get("experience"),
                "salary_text": job.get("salary"), "description": (detail or {}).get("description")}

    def probe_payload(self) -> dict:
        return {"keyword": "护士", "city": "北京"}
