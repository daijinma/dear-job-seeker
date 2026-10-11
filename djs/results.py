"""What the results page needs: the jobs of one search (every task with the same keyword and city) in a few orders,
which of them lack a core element and why, and plain-Chinese wording for things that went wrong."""
from __future__ import annotations

import re
from collections import Counter

from djs import providers, query, service, taskqueue
from djs.normalize import plain_text  # noqa: F401  third-party job text as plain text: the page shows it with textContent

PAGE_MAX = 5000
SORTS = ("mixed", "salary", "company")   # the orders the page offers (query has one more: newest, for the tool)


def _job(row: dict) -> dict:
    return {**service.load_job(row), "label": providers.label(row["provider"]),
            "has_description": bool(row["has_description"])}


def _per_site(counts: dict[str, int]) -> list[dict]:
    order = {name: i for i, name in enumerate(providers.names())}  # registry order: chips keep their place
    return [{"provider": p, "label": providers.label(p), "count": n}
            for p, n in sorted(counts.items(), key=lambda kv: order.get(kv[0], len(order)))]


def _sites(conn, base: dict) -> dict[str, int]:
    """Jobs per platform for the chips: every platform the search found anything on (0 when none of its jobs pass the
    filters, so the chip keeps its place), each with the number of its own jobs that pass them."""
    seen = query.count_by_provider(conn, query.where(search=base["search"], complete_only=False))
    return {**dict.fromkeys(seen, 0), **query.count_by_provider(conn, query.where(**base))}


def _incomplete(conn, keyword, city, provider) -> dict:
    """The jobs of the search (and the provider filter) that lack a core element: how many, and how often each reason
    comes up (one job can have several)."""
    where, args = query.where(search=(keyword, city), provider=provider, complete_only=False)
    rows = conn.execute(f"SELECT {query.WHY} FROM jobs j WHERE {where} AND j.complete = 0", args).fetchall()
    logged_in = service.login_lookup()
    found = Counter(why for row in rows for why in service.reasons(dict(row), logged_in))
    ranked = sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))   # the most common reason first
    return {"count": len(rows), "reasons": [{"text": t, "count": n} for t, n in ranked]}


def search_jobs(conn, keyword, city, provider=None, sort="mixed", limit=30, offset=0, complete_only=True,
                min_month_salary=None, dedupe=True) -> dict:
    """One page of the jobs any task of the search (keyword + city) has seen, each job once (`dedupe`: the same job on
    several sites is its best record, with `also_on` naming the others). `complete_only` leaves out jobs that lack a core
    element, in the rows and in every count; `incomplete` always describes those jobs, so the page can say what it is
    not showing. `min_month_salary` keeps the jobs whose pay per month can reach it. `total` follows the provider
    filter; `all_total` (what the heading says: each job once), `merged` (records folded into another site's) and
    `per_site` (each site's own jobs) count the whole search, so the filter chips do not shrink while one is selected."""
    if sort not in SORTS:
        raise ValueError("未知的排序方式")
    base = {"search": (keyword, city), "complete_only": complete_only, "min_month_salary": min_month_salary}
    rows, total = query.find(conn, query.where(**base, provider=provider), sort=sort, dedupe=dedupe,
                             limit=max(1, min(int(limit), PAGE_MAX)), offset=max(0, int(offset)))
    found = service.explain([_job(r) for r in rows])
    for job in found:
        del job["detail_error"]   # raw error text stays out of the page: incomplete_reason says it in Chinese
    everywhere = query.where(**base)
    all_total = query.count(conn, everywhere, dedupe) if provider else total
    tasks = taskqueue.of_search(conn, keyword, city)
    return {"jobs": found,
            "total": total,
            "all_total": all_total,
            "merged": query.count(conn, everywhere, dedupe=False) - all_total,
            "per_site": _per_site(_sites(conn, base)),
            "incomplete": _incomplete(conn, keyword, city, provider),
            "searching": any(t["status"] in taskqueue.ACTIVE for t in tasks),
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
        f"SELECT {query.WHY} FROM jobs j WHERE j.provider = ? AND j.complete = 0 "
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
