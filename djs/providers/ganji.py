"""赶集 (ganji) mini-program API. Keyword search, city REQUIRED (city goes in the `cid` header).

No signature and no secret. Anonymous mode returns a fixed 8 items per (city, keyword) and page 2 is
rejected (code 4), so page_cap=1. The detail `localname` comes from the item's weappQuery and is
load-bearing: a wrong one silently returns an empty JD, hence the JD check in detail().
Only relative times ("今天") exist, so published_at is always unknown.
The user-facing page is the share URL the detail response carries (the detail path itself is a JSON API).
"""
from __future__ import annotations

from .base import Blocked, Provider, StructureError, get_json, need

APPID = "wx8a53973ba8aa631e"
APP_VER = "191"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Mac")
COMMON = {"bgtype": "weapp_gjtoc", "platform": "weapp", "os": "mac", "user-agent": UA,
          "referer": f"https://servicewechat.com/{APPID}/{APP_VER}/page-frame.html"}
LIST_URL = "https://gj.58.com/gjNetworkOne/jobCenter/list/job"
DETAIL_BASE = "https://gj.58.com/job/detail"
SCENE_KEY = "wechat_mini_search_feed"
BLOCKED_CODE = -1000005


def jd_text(result: dict) -> str:
    """JD sits in the heterogeneous info[] block that has `position_publisher_area_v1`."""
    for block in need(result, "info", typ=list):
        arr = block.get("position_publisher_area_v1") if isinstance(block, dict) else None
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            text = (arr[0].get("position_desc") or {}).get("text")
            if isinstance(text, str) and text:
                return text
    raise StructureError("ganji detail: position_publisher_area_v1[0].position_desc.text not found")


class Ganji(Provider):
    name = "ganji"
    label = "赶集网"
    kind = "keyword"
    auth = "public"
    requires_city = True
    city_filter = "server"
    interval = 1.5
    min_interval = 0.75
    page_size = 8
    first_page = 1
    page_cap = 1
    note = "No key needed; city required; anonymous mode returns only 8 results per (city, keyword)"

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        cid = self.city_code(payload.get("city"))
        r = client.post(LIST_URL, json={"sceneKey": SCENE_KEY, "keyQuery": payload["keyword"], "pageNum": page},
                        headers={**COMMON, "content-type": "application/json", "version": "10.40.00", "cid": cid})
        d = get_json(r)
        code = d.get("code")
        if code == BLOCKED_CODE:
            raise Blocked(f"ganji list blocked code={code!r} version={APP_VER}")
        if code != 0:
            raise StructureError(f"ganji list code={code!r} version={APP_VER} body={str(d)[:150]}")
        # jobInfos also carries section-header cards without infoId
        return [j for j in need(d, "data", "jobInfos", typ=list) if "infoId" in j]

    def job_id(self, job: dict) -> str:
        return str(need(job, "infoId", typ=(str, int)))

    def detail(self, client, job: dict) -> dict:
        list_name = need(job, "weappQuery", "listName", typ=str)
        params = {"format": "json", "platform": "weapp", "appid": "56", "version": "10.30.90",
                  "localname": need(job, "weappQuery", "localName", typ=str)}
        d = get_json(client.get(f"{DETAIL_BASE}/{list_name}/{self.job_id(job)}", params=params, headers=COMMON))
        if d.get("status") != "0":
            raise StructureError(f"ganji detail status={d.get('status')!r} infoId={self.job_id(job)} version={APP_VER}")
        result = need(d, "result", typ=dict)
        jd_text(result)  # status 0 without a JD block = structure change / wrong localname
        return result

    def meta(self, job: dict, detail: dict | None) -> dict:
        share = ((detail or {}).get("other") or {}).get("share") or {}
        url = (share.get("data") or {}).get("url") or None
        return {"source_url": url, "apply_url": url, "published_at": None, "published_at_kind": "unknown"}

    def row(self, job: dict, detail: dict | None) -> dict:
        return {"title": job.get("title"), "company": job.get("enterpriseName"), "city": job.get("area"),
                "salary_text": job.get("salary"), "description": jd_text(detail) if detail else None}

    def probe_payload(self) -> dict:
        return {"keyword": "服务员", "city": "北京"}
