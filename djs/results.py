"""What the results page needs: the jobs of one search (every task with the same keyword and city) in a few orders,
which of them lack a core element and why, and plain-Chinese wording for things that went wrong."""
from __future__ import annotations

import json
import re
from collections import Counter

from djs import providers, service
from djs.normalize import plain_text  # noqa: F401  third-party job text as plain text: the page shows it with textContent

PAGE_MAX = 5000
# One search = the tasks with the same keyword and city; a task without a city is the search made without one.
_SEARCH = "json_extract(payload, '$.keyword') = :k AND IFNULL(json_extract(payload, '$.city'), '') = :c"
_OF_SEARCH = f"j.id IN (SELECT job_id FROM task_jobs WHERE task_id IN (SELECT id FROM tasks WHERE {_SEARCH}))"
_SCOPE = f"{_OF_SEARCH} AND (:complete = 0 OR j.complete = 1)"   # all jobs of the search, or only its complete ones
_FIELDS = ("id", "provider", "title", "company", "city", "district", "education", "experience", "salary_text",
           "salary_min", "salary_max", "salary_unit", "salary_months", "source_url", "published_at", "first_seen_at",
           "missing", "complete", "detail_error", "has_description")
_WHY = "j.provider, j.missing, j.salary_text, j.detail_error"   # what service.reasons needs to say why a job is incomplete
# A day, an hour or a year of pay as a month of pay, so that they sort together (a year is divided, not multiplied)
_PER_MONTH = {"元/月": 1, "元/天": 21.75, "元/时": 174, "元/年": 1 / 12}
_MONTHLY = "CASE j.salary_unit " + " ".join(f"WHEN '{u}' THEN j.salary_max * {k!r}" for u, k in _PER_MONTH.items()) + " END"
# Every job with its turn among its own site's jobs (newest first, a page keeps its own order within the same second),
# its site's place in the registry and its monthly pay: what the orders below sort by.
_RANKED = (
    "SELECT " + ", ".join(f"j.{f}" for f in _FIELDS if f != "has_description")
    + ", (j.description IS NOT NULL AND j.description <> '') AS has_description, "
    "ROW_NUMBER() OVER (PARTITION BY j.provider ORDER BY j.first_seen_at DESC, j.id) AS turn, "
    f"IFNULL(o.pos, 1e9) AS site, {_MONTHLY} AS monthly FROM jobs j "
    "LEFT JOIN (SELECT value AS name, key AS pos FROM json_each(:order)) o ON o.name = j.provider "
    f"WHERE {_SCOPE} AND (:p IS NULL OR j.provider = :p)")
SORTS = {  # mixed: the sites in turn, each one's newest first | salary: highest monthly pay first, none last | company
    "mixed": "j.turn, j.site, j.id",
    "salary": "j.monthly IS NULL, j.monthly DESC, j.turn, j.site, j.id",
    "company": "(j.company IS NULL OR j.company = ''), j.company, j.id",
}


def _label(name: str) -> str:
    try:
        return providers.get(name).label or name
    except KeyError:  # a platform that was removed after the job was stored
        return name


def _job(row) -> dict:
    return {**service.load_job(row), "label": _label(row["provider"]), "has_description": bool(row["has_description"])}


def _per_site(counts: dict[str, int]) -> list[dict]:
    order = {name: i for i, name in enumerate(providers.names())}  # registry order: chips keep their place while counts grow
    return [{"provider": p, "label": _label(p), "count": n}
            for p, n in sorted(counts.items(), key=lambda kv: order.get(kv[0], len(order)))]


def _incomplete(conn, args) -> dict:
    """The jobs of the search (and the provider filter) that lack a core element: how many, and how often each reason
    comes up (one job can have several)."""
    rows = conn.execute(
        f"SELECT {_WHY} FROM jobs j WHERE {_OF_SEARCH} AND j.complete = 0 AND (:p IS NULL OR j.provider = :p)", args).fetchall()
    logged_in = service.login_lookup()
    found = Counter(why for row in rows for why in service.reasons(dict(row), logged_in))
    ranked = sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))   # the most common reason first
    return {"count": len(rows), "reasons": [{"text": t, "count": n} for t, n in ranked]}


def search_jobs(conn, keyword, city, provider=None, sort="mixed", limit=30, offset=0, complete_only=True) -> dict:
    """One page of the jobs any task of the search (keyword + city) has seen, each job once. `complete_only` leaves out
    jobs that lack a core element, in the rows and in every count; `incomplete` always describes those jobs, so the
    page can say what it is not showing. `total` follows the provider filter; `per_site` counts the whole search, so
    the filter chips do not shrink while one is selected."""
    if sort not in SORTS:
        raise ValueError("未知的排序方式")
    args = {"k": keyword, "c": city or "", "p": provider or None, "complete": int(bool(complete_only)),
            "limit": max(1, min(int(limit), PAGE_MAX)), "offset": max(0, int(offset)),
            "order": json.dumps(providers.names())}
    sites = {r["provider"]: r["complete"] if complete_only else r["n"] for r in conn.execute(
        "SELECT j.provider, COUNT(*) AS n, SUM(j.complete) AS complete FROM jobs j "
        f"WHERE {_OF_SEARCH} GROUP BY j.provider", args)}
    rows = conn.execute(
        f"SELECT {', '.join(_FIELDS)} FROM ({_RANKED}) j ORDER BY {SORTS[sort]} LIMIT :limit OFFSET :offset", args)
    found = service.explain([_job(r) for r in rows])
    for job in found:
        del job["detail_error"]   # raw error text stays out of the page: incomplete_reason says it in Chinese
    tasks = conn.execute(f"SELECT id, status FROM tasks WHERE {_SEARCH} ORDER BY id", args).fetchall()
    return {"jobs": found,
            "total": sites.get(provider, 0) if provider else sum(sites.values()),
            "per_site": _per_site(sites),
            "incomplete": _incomplete(conn, args),
            "searching": any(t["status"] in ("pending", "running") for t in tasks),
            "task_ids": [t["id"] for t in tasks]}


def description(conn, job_id) -> str | None:
    """The full text already stored for a job, if there is any."""
    row = conn.execute("SELECT description FROM jobs WHERE id=?", (job_id,)).fetchone()
    return (row["description"] or None) if row else None


# ---- friendly errors
_ERRORS = (  # (kind, pattern on the raw error text, what the user reads); the first match wins
    ("env", r"missing environment|MissingEnv|environment variable", "还没配置这个网站需要的密钥"),
    ("browser", r"playwright|chromium|browser", "需要先安装浏览器组件"),
    ("busy", r"正在使用浏览器", "浏览器正忙"),
    ("closed", r"登录窗口被关闭", "登录窗口被关掉了，请再试一次"),
    ("login", r"LoginRequired|login required|未登录", "需要先登录"),
    ("timeout", r"秒内没有登录成功", "等了三分钟还没有登录成功，请再试一次"),
    ("blocked", r"Blocked|\b(?:403|429)\b|code[ =:]*37\b|limiting requests|cooling down", "被网站暂时限制，稍后再试"),
    ("city", r"暂不支持城市", "这个网站暂时不支持这个城市"),
    ("city", r"必须指定城市|city is required", "这个网站需要先填写城市"),
    ("structure", r"StructureError|ParseError", "网站页面变了，暂时读不出来"),
    ("network", r"timeout|timed out|ConnectError|NetworkError|ConnectionError|ReadError|RemoteProtocolError|net::ERR",
     "网络不通，稍后再试"),
)
_OTHER = ("other", "没成功，可以稍后重试")


def classify(raw) -> tuple[str, str]:
    """(kind, plain-Chinese reason) for an error string as stored in tasks.last_error or raised by the service."""
    for kind, pattern, text in _ERRORS:
        if re.search(pattern, raw or "", re.I):
            return kind, text
    return _OTHER


def friendly_error(raw) -> str:
    return classify(raw)[1]


def _shared_reason(conn, task_ids: list[int], provider: str, logged_in) -> str | None:
    """The reason all the incomplete jobs of one site in this search have in common, or None when they differ."""
    rows = conn.execute(
        f"SELECT {_WHY} FROM jobs j WHERE j.provider = ? AND j.complete = 0 "
        f"AND j.id IN (SELECT job_id FROM task_jobs WHERE task_id IN ({','.join('?' * len(task_ids))}))",
        (provider, *task_ids))
    found = {service.incomplete_reason(dict(r), logged_in) for r in rows}
    return found.pop() if len(found) == 1 else None


def annotate_searches(conn, searches: list[dict]) -> list[dict]:
    """Per site of each search: the newest task id (what retry acts on) and, when it had an error, the kind and the
    plain-Chinese reason. A site whose jobs are all incomplete also gets `why`: the one reason they share (None when
    they differ). The raw error text stays out of the UI."""
    logged_in = service.login_lookup()
    for s in searches:
        ids = s["task_ids"]
        newest = {r["provider"]: r["id"] for r in conn.execute(
            f"SELECT provider, MAX(id) AS id FROM tasks WHERE id IN ({','.join('?' * len(ids))}) GROUP BY provider", ids)}
        for site in s["per_site"]:
            error = site.pop("last_error", None)
            site["task_id"] = newest.get(site["provider"])
            site["kind"], site["reason"] = classify(error) if error or site["status"] == "dead" else (None, None)
            site["why"] = (_shared_reason(conn, ids, site["provider"], logged_in)
                           if site["jobs"] and not site["complete"] else None)
    return searches
