"""Zhaopin (zhaopin.com). Keyword search; the list item carries the JD. Two paths, picked by the client's kind:

http, anonymous: the mini-program gateway cgate.zhaopin.com. All four x-zp-* headers are required (none -> code 211).
They are constants the mini-program itself ships, not credentials. x-zp-version is a capture-day version: an outdated
one is reported through the business code check (version is put in the error text), never as []. The list envelope uses
`statusCode`, the detail envelope uses `code`. Hard cap: 3 pages x 20; each city slice has its own independent quota.

browser, logged in: the PC search the site's own /jobs page calls, POST fe-api.zhaopin.com/c/i/search/positions with the
page's at/rt login cookies in the body (without them: isVerification=1 and no rows). The home page is loaded first, so
the anti-bot layer's device fingerprint cookie exists. Its items have the shape of cgate's.

The list item already embeds the whole job (`jobDetailData`, the same shape the detail request returns), JD included,
so detail() is only cgate's fallback for an item whose embedded JD is empty. `salaryReal` (the platform's monthly
equivalent, lower bound +1) is not used: `salary60` is the advertised figure and keeps its own unit ("100元/时").
"""
from __future__ import annotations

from ._browser import XHR_ACCEPT, is_browser
from .base import Blocked, Login, LoginRequired, Provider, StructureError, get_json, need, tag_names

HOME = "https://www.zhaopin.com/"
PC_URL = "https://fe-api.zhaopin.com/c/i/search/positions"
NATIONWIDE = "489"        # the PC search's own "全国" city code (cgate takes no city instead)
FINGERPRINT = "smidV2"    # 数美 device-fingerprint cookie the page's scripts write; fetches without it are blocked
HEADERS = {"x-zp-channel": "wxxiaochengxu", "x-zp-business-system": "73",
           "x-zp-platform": "12", "x-zp-version": "4.1.321"}


def _check_code(code, where: str) -> None:
    if code in (210, 211):
        raise Blocked(f"zhaopin {where} code:{code} version={HEADERS['x-zp-version']}")
    if code not in (200, "200"):
        raise StructureError(f"zhaopin {where} code:{code!r} version={HEADERS['x-zp-version']}")


def _login_tokens(client) -> tuple[str, str]:
    """The at/rt login cookies the browser holds (HttpOnly ones too: document.cookie cannot see those)."""
    jar = {c["name"]: c["value"] for c in client.page.context.cookies(HOME)}
    if not (jar.get("at") and jar.get("rt")):
        raise LoginRequired("zhaopin PC search needs the at/rt login cookies; log in first")
    return jar["at"], jar["rt"]


class Zhaopin(Provider):
    name = "zhaopin"
    label = "智联招聘"
    kind = "keyword"
    auth = "optional"
    city_filter = "server"
    interval = 3.0
    min_interval = 1.5
    page_size = 20          # pageSize param is ignored by cgate
    first_page = 1
    page_cap = 3
    transports = ("http", "browser")
    login_transport = "browser"
    home = HOME
    note = ("cgate: x-zp-* headers required, hard cap 60 results (3 pages), city slices have separate quota; "
            "logged in, the PC search runs in a browser instead")
    login = Login(url=HOME, method="wechat", domain="zhaopin.com", cookies_all=("at", "rt"),
                  unlocks="能搜到另一批岗位（数量和不登录时相近）")

    BASE = "https://cgate.zhaopin.com/positionbusiness"

    def warmup(self, client, payload: dict) -> None:
        super().warmup(client, payload)       # a browser loads `home`; an http client has no goto
        if is_browser(client):
            client.wait_cookie(FINGERPRINT, timeout=45)

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        if is_browser(client):
            return self._fetch_pc(client, payload, page)
        params = {"S_SOU_FULL_INDEX": payload["keyword"], "pageIndex": page, "pageSize": self.page_size}
        if city := self.city_code(payload.get("city")):
            params["S_SOU_WORK_CITY"] = city
        r = client.get(f"{self.BASE}/searchrecommend/searchPositions", params=params, headers=HEADERS)
        d = get_json(r)
        _check_code(d.get("statusCode", d.get("code")), "list")
        return need(d, "data", "list")

    def _fetch_pc(self, client, payload: dict, page: int) -> list[dict]:
        at, rt = _login_tokens(client)
        body = {"S_SOU_FULL_INDEX": payload["keyword"],
                "S_SOU_WORK_CITY": self.city_code(payload.get("city")) or NATIONWIDE, "pageIndex": page,
                "pageSize": self.page_size, "eventScenario": "pcSearchedSouSearch", "anonymous": 0, "platform": 13,
                "version": "0.0.0", "at": at, "rt": rt}
        d = get_json(client.post(PC_URL, json=body, headers={"Accept": XHR_ACCEPT}))
        _check_code(d.get("code"), "PC list")
        data = need(d, "data", typ=dict)
        if data.get("isVerification") == 1:
            raise LoginRequired("zhaopin PC list: the session was refused (isVerification=1); log in again")
        return need(data, "list")

    def job_id(self, job: dict) -> str:
        return str(need(job, "number", typ=(str, int)))

    def detail(self, client, job: dict) -> dict | None:
        if is_browser(client):    # the PC item is the whole job, and cgate is another origin than the page
            return None
        r = client.get(f"{self.BASE}/position/getPositionModule",
                       params={"number": self.job_id(job)}, headers=HEADERS)
        d = get_json(r)
        _check_code(d.get("code"), "detail")
        return need(d, "data", typ=dict)

    def meta(self, job: dict, detail: dict | None) -> dict:
        number = job.get("number")
        url = f"https://www.zhaopin.com/jobdetail/{number}.htm" if number else None
        pt = job.get("publishTime")
        # publishTime is bumped by refreshes, so it is never the original publish time.
        published_at = f"{pt.replace(' ', 'T')}+08:00" if pt else None
        return {"source_url": url, "apply_url": url, "published_at": published_at,
                "published_at_kind": "refresh" if published_at else "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        return {"title": job.get("name"), "company": job.get("companyName"),
                "city": job.get("workCity"), "district": job.get("cityDistrict"),
                "education": job.get("education"), "experience": job.get("workingExp"),
                "benefits": _welfare(job, detail), "salary_text": job.get("salary60"),
                "description": _jd(job.get("jobDetailData")) or _jd(detail)}

    def probe_payload(self) -> dict:
        return {"keyword": "算法工程师"}


def _keywords(job_keyword) -> list[str]:
    return tag_names(job_keyword.get("keywords") if isinstance(job_keyword, dict) else None, "itemValue")


def _welfare(job: dict, detail: dict | None) -> list[str]:
    """The welfare tags the poster picked: `jobKeyword.keywords[].itemValue` of the list item, or of the detail when
    that names more (empty placeholders are skipped). `welfareLabel` and `welfareTags` exist too but were empty in
    every real item seen."""
    in_detail = (((detail or {}).get("position") or {}).get("other") or {}).get("jobKeyword")
    return max(_keywords(job.get("jobKeyword")), _keywords(in_detail), key=len)


def _jd(module: dict | None) -> str | None:
    """The JD of a job module: the detail response, or the `jobDetailData` embedded in a list item."""
    desc = ((module or {}).get("position") or {}).get("desc") or {}
    return desc.get("description") or None
