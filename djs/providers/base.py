"""Provider contract. Platform facts are CLASS ATTRIBUTES (they change with the code);
user overrides live in the provider_settings table and are resolved by djs.pacer.

fetch(client, payload, page) -> list[dict]
  - keyword kind: payload = {"keyword": ..., "city": ...}
  - tenant  kind: payload = {"tenant": ...}
  - [] means ONLY "success code + expected structure + list really empty".
    Anything else MUST raise below — never return [] on a block/odd shape, or a blocked
    exit looks healthy.
Providers never sleep: pacing is enforced by the egress client (djs.egress / djs.pacer).
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class FetchError(Exception):
    def __init__(self, msg: str = "", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class Blocked(FetchError):
    """Confirmed risk-control / rate limit. Exit goes into cooldown immediately."""


class LoginRequired(Blocked):
    """Cookie missing/expired. For auth='login' providers the task becomes waiting_login
    (not a cooldown); for public providers it is treated as a block."""


class StructureError(FetchError):
    """Parsed fine but shape/business code is not what we expect."""


class ParseError(StructureError):
    """Could not parse at all (non-JSON, regex miss)."""


class Delisted(FetchError):
    """Job is offline. Expected terminal state, not a failure; never retried."""


def retry_after(r) -> float | None:
    ra = r.headers.get("Retry-After", "").strip()
    if ra.isdigit():
        return float(ra)
    try:
        return max(0.0, (parsedate_to_datetime(ra) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def check_http(r) -> None:
    if r.status_code == 401:
        raise LoginRequired("HTTP 401", retry_after=retry_after(r))
    if r.status_code in (403, 429):
        raise Blocked(f"HTTP {r.status_code}", retry_after=retry_after(r))
    r.raise_for_status()


def get_json(r) -> dict:
    check_http(r)
    try:
        d = r.json()
    except ValueError as e:
        raise ParseError(f"non-JSON body: {r.text[:120]!r}") from e
    if not isinstance(d, dict):
        raise StructureError(f"top level is {type(d).__name__}, not object")
    return d


def need(obj, *path, typ=list):
    """Strict getter: every level must exist, leaf must be `typ` and non-None/non-empty-str."""
    o = obj
    for i, k in enumerate(path):
        if not isinstance(o, dict) or k not in o:
            raise StructureError(f"missing {'.'.join(map(str, path[:i + 1]))}")
        o = o[k]
    if o is None or o == "" or not isinstance(o, typ):
        raise StructureError(f"{'.'.join(map(str, path))} is {o!r:.60}, want {typ}")
    return o


class Provider:
    name: str = ""
    label: str = ""                # display name shown to end users (UI), e.g. "牛客网"
    # ---- platform TYPE (two independent axes) ----
    kind: str = "keyword"          # 'keyword' (search by keyword[+city]) | 'tenant' (one company site per task)
    auth: str = "public"           # 'public' (no cookie) | 'login' (needs the user's own cookie from djs.auth)
    requires_city: bool = False    # True = no nationwide search; fetch() rejects missing city
    # ---- frequency / paging facts (user can override interval/jitter/max_pages) ----
    interval: float = 3.0          # min seconds between two requests of this provider (global, per egress)
    jitter: float = 0.3            # random extra on top of interval
    min_interval: float = 0.5      # safety floor: set_frequency below this is rejected unless force=True
    page_size: int = 20
    first_page: int = 0
    page_cap: int | None = None    # known hard paging ceiling of the platform; None = unknown
    enabled: bool = True
    note: str = ""                 # one-line platform caveat shown by provider_list

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        raise NotImplementedError

    def job_id(self, job: dict) -> str:
        """Missing id MUST raise StructureError (use need), never return 'None'."""
        raise NotImplementedError

    def meta(self, job: dict, detail: dict | None) -> dict:
        """Return {source_url, apply_url, published_at, published_at_kind}; None when unknown.
        published_at_kind: 'original' | 'refresh' | 'unknown' (refresh time must not pose as original)."""
        return {"source_url": None, "apply_url": None, "published_at": None, "published_at_kind": "unknown"}

    def detail(self, client, job: dict) -> dict | None:
        """None = list item already is the detail (no second hop)."""
        return None

    def probe_payload(self) -> dict:
        raise NotImplementedError
