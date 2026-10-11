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

import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from djs import cities


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


class MissingEnv(FetchError):
    """A required signing variable is unset (never retryable by itself)."""


def env(name: str) -> str:
    """Read signing material lazily at call time, so providers import without keys."""
    v = os.environ.get(name, "").strip()
    if not v:
        raise MissingEnv(f"environment variable {name} is not set")
    return v


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


def join_places(names) -> str | None:
    """Jobs can list several cities; keep them all so 'city contains X' stays true for each."""
    return "、".join(n for n in names if n) or None


def split_places(names, sep: str = "-") -> tuple[str | None, str | None]:
    """['广州-黄埔区', '杭州'] -> ('广州、杭州', '黄埔区'): the cities and the districts of a job that lists several places."""
    parts = [(n or "").partition(sep)[::2] for n in names]
    return join_places(dict.fromkeys(c for c, _ in parts)), join_places(dict.fromkeys(d for _, d in parts))


def district_of(area: str | None, city: str | None) -> str | None:
    """The district in a 58-group place label: '西湖区·翠苑' and '杭州·余杭' both give the first part that is not the city."""
    return next((p for p in (area or "").split("·") if p and p != city), None)


def months_suffix(months) -> str:
    """'·14薪' for more than 12 monthly payments; 12 or fewer is the default and not worth showing."""
    return f"·{months}薪" if isinstance(months, int) and months > 12 else ""


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
    auth: str = "public"           # 'public' (no cookie) | 'optional' (works anonymously, login unlocks more data,
                                   # e.g. Boss salary) | 'login' (needs the user's own cookie from djs.auth)
    requires_city: bool = False    # True = no nationwide search; fetch() rejects missing city
    city_filter: str = "none"      # who applies payload['city']: 'server' = fetch() sends it to the site |
                                   # 'client' = the site cannot filter, the engine drops rows whose row()['city']
                                   # lacks it | 'none' = no city notion
    # ---- frequency / paging facts (user can override interval/jitter/max_pages) ----
    interval: float = 3.0          # min seconds between two requests of this provider (global, per egress)
    jitter: float = 0.3            # random extra on top of interval
    min_interval: float = 0.5      # safety floor: set_frequency below this is rejected unless force=True
    page_size: int = 20
    first_page: int = 0
    page_cap: int | None = None    # known hard paging ceiling of the platform; None = unknown
    enabled: bool = True
    note: str = ""                 # one-line platform caveat shown by provider_list
    requires_env: tuple[str, ...] = ()   # env vars holding signing keys (never shipped in the repo)
    # ---- capture transport (user can override with set_transport) ----
    transports: tuple[str, ...] = ("http",)   # 'http' (httpx) | 'browser' (a real Chromium page; DJS_CDP_URL makes it
                                              # a new tab of the user's own browser). First = default.
    needs_headed: bool = False     # True = the site refuses headless browsers, so the browser transport opens a window
    home: str = ""                 # page the browser loads first so the site's own scripts write their anti-bot cookies

    def missing_env(self) -> list[str]:
        return [n for n in self.requires_env if not os.environ.get(n, "").strip()]

    @property
    def has_detail(self) -> bool:
        """True when detail() makes a second request (the default says the list item already is the detail)."""
        return type(self).detail is not Provider.detail

    def city_code(self, city: str | None) -> str | None:
        """What the site's server expects for `city`; None = no city given (nationwide search).
        A missing required city or one we have no code for is a ValueError: never search nationwide by accident."""
        if not city:
            if self.requires_city:
                raise ValueError(f"{self.label}必须指定城市")
            return None
        if self.city_filter != "server":
            return city  # the site takes no city code; nothing to map
        c = cities.code(self.name, city)
        if c is None:
            shown = "、".join(cities.supported(self.name)[:10])
            raise ValueError(f"{self.label}暂不支持城市「{city}」，可选：{shown}等")
        return c

    def fetch(self, client, payload: dict, page: int) -> list[dict]:
        raise NotImplementedError

    def job_id(self, job: dict) -> str:
        """Missing id MUST raise StructureError (use need), never return 'None'."""
        raise NotImplementedError

    def row(self, job: dict, detail: dict | None) -> dict:
        """The jobs columns this platform publishes for a list item (and its detail once fetched); leave out the rest.
        title, company, city, district, education, experience, salary_text, description (HTML is fine, the engine
        makes it plain text) and, only when the site gives the figures as fields with a certain unit,
        salary_min/salary_max/salary_unit/salary_months from djs.normalize.salary_range. With salary text alone,
        return salary_text: the engine parses it."""
        raise NotImplementedError

    def meta(self, job: dict, detail: dict | None) -> dict:
        """Return {source_url, apply_url, published_at, published_at_kind}; None when unknown.
        published_at_kind: 'original' | 'refresh' | 'unknown' (refresh time must not pose as original)."""
        return {"source_url": None, "apply_url": None, "published_at": None, "published_at_kind": "unknown"}

    def detail(self, client, job: dict) -> dict | None:
        """None = list item already is the detail (no second hop)."""
        return None

    def warmup(self, client, payload: dict) -> None:
        """Runs once after the client opens. A browser client loads `home` first; an http client has no goto."""
        if self.home and hasattr(client, "goto"):
            client.goto(self.home)

    def probe_payload(self) -> dict:
        raise NotImplementedError
