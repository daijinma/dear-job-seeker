"""What the results page needs: the jobs of one search (every task with the same keyword and city), plain text for
untrusted job descriptions, and plain-Chinese wording for things that went wrong."""
from __future__ import annotations

import html
import re

from djs import providers

PAGE_MAX = 5000
SORTS = {  # new: newest discovery first, a page keeps its own order within the same second | company: grouped by company
    "new": "j.first_seen_at DESC, j.id",
    "company": "(j.company IS NULL OR j.company = ''), j.company, j.id",
}
# One search = the tasks with the same keyword and city; a task without a city is the search made without one.
_SEARCH = "json_extract(payload, '$.keyword') = :k AND IFNULL(json_extract(payload, '$.city'), '') = :c"
_OF_SEARCH = f"j.id IN (SELECT job_id FROM task_jobs WHERE task_id IN (SELECT id FROM tasks WHERE {_SEARCH}))"
# A job is complete when every core element is present. The data contract's `missing` list will replace this predicate.
_COMPLETE = "(j.title <> '' AND j.company <> '' AND j.city <> '' AND j.salary_text <> '' AND j.description <> '')"
_SCOPE = f"{_OF_SEARCH} AND (:complete = 0 OR {_COMPLETE})"   # all jobs of the search, or only its complete ones
_COLS = ("j.id, j.provider, j.title, j.company, j.city, j.salary_text, j.source_url, j.published_at, j.first_seen_at, "
         "(j.description IS NOT NULL AND j.description <> '') AS has_description")


def _label(name: str) -> str:
    try:
        return providers.get(name).label or name
    except KeyError:  # a platform that was removed after the job was stored
        return name


def _job(row) -> dict:
    return {**dict(row), "label": _label(row["provider"]), "has_description": bool(row["has_description"])}


def _per_site(counts: dict[str, int]) -> list[dict]:
    order = {name: i for i, name in enumerate(providers.names())}  # registry order: chips keep their place while counts grow
    return [{"provider": p, "label": _label(p), "count": n}
            for p, n in sorted(counts.items(), key=lambda kv: order.get(kv[0], len(order)))]


def search_jobs(conn, keyword, city, provider=None, sort="new", limit=30, offset=0, complete_only=False) -> dict:
    """One page of the jobs any task of the search (keyword + city) has seen, each job once. `total` follows the
    provider filter; `per_site` counts the whole search, so the filter chips do not shrink while one is selected.
    `complete_only` leaves out jobs that lack a core element, in the rows and in every count."""
    if sort not in SORTS:
        raise ValueError("未知的排序方式")
    args = {"k": keyword, "c": city or "", "p": provider or None, "complete": int(bool(complete_only)),
            "limit": max(1, min(int(limit), PAGE_MAX)), "offset": max(0, int(offset))}
    counts = {r["provider"]: r["n"] for r in conn.execute(
        f"SELECT j.provider, COUNT(*) AS n FROM jobs j WHERE {_SCOPE} GROUP BY j.provider", args)}
    rows = conn.execute(
        f"SELECT {_COLS} FROM jobs j WHERE {_SCOPE} AND (:p IS NULL OR j.provider = :p) "
        f"ORDER BY {SORTS[sort]} LIMIT :limit OFFSET :offset", args).fetchall()
    tasks = conn.execute(f"SELECT id, status FROM tasks WHERE {_SEARCH} ORDER BY id", args).fetchall()
    return {"jobs": [_job(r) for r in rows],
            "total": counts.get(provider, 0) if provider else sum(counts.values()),
            "per_site": _per_site(counts),
            "searching": any(t["status"] in ("pending", "running") for t in tasks),
            "task_ids": [t["id"] for t in tasks]}


def description(conn, job_id) -> str | None:
    """The full text already stored for a job, if there is any."""
    row = conn.execute("SELECT description FROM jobs WHERE id=?", (job_id,)).fetchone()
    return (row["description"] or None) if row else None


# ---- untrusted text ----
_DROP = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_BREAK = re.compile(r"<\s*br\b[^>]*>|<\s*/\s*(?:p|div|li|h[1-6]|tr)\s*>", re.I)
_TAG = re.compile(r"</?[a-z!][^>]*>", re.I)


def plain_text(raw) -> str:
    """Job descriptions are third-party HTML (<br>, <p>, entities, whatever the site sent): keep the line breaks,
    drop every tag. The page shows the result with textContent, so nothing left in it can run as markup."""
    text = _TAG.sub("", _BREAK.sub("\n", _DROP.sub("", str(raw or ""))))
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---- friendly errors ----
_ERRORS = (  # (kind, pattern on the raw error text, what the user reads); the first match wins
    ("env", r"missing environment|MissingEnv|environment variable", "还没配置这个网站需要的密钥"),
    ("browser", r"playwright|chromium|browser", "需要先安装浏览器组件"),
    ("login", r"LoginRequired|login required|未登录", "需要先登录"),
    ("timeout", r"login not completed", "等了三分钟还没有登录成功，请再试一次"),
    ("blocked", r"Blocked|\b(?:403|429)\b|code[ =:]*37\b|limiting requests|cooling down", "被网站暂时限制，稍后再试"),
    ("city", r"暂不支持城市", "这个网站暂时不支持这个城市"),
    ("city", r"必须指定城市|city is required", "这个网站需要先填写城市"),
    ("structure", r"StructureError|ParseError", "网站页面变了，暂时读不出来"),
    ("network", r"timeout|timed out|ConnectError|NetworkError|ConnectionError|ReadError|RemoteProtocolError",
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


def annotate_searches(conn, searches: list[dict]) -> list[dict]:
    """Per site of each search: the newest task id (what retry acts on) and, when it had an error, the kind and the
    plain-Chinese reason. The raw error text stays out of the UI."""
    for s in searches:
        ids = s["task_ids"]
        newest = {r["provider"]: r["id"] for r in conn.execute(
            f"SELECT provider, MAX(id) AS id FROM tasks WHERE id IN ({','.join('?' * len(ids))}) GROUP BY provider", ids)}
        for site in s["per_site"]:
            error = site.pop("last_error", None)
            site["task_id"] = newest.get(site["provider"])
            site["kind"], site["reason"] = classify(error) if error or site["status"] == "dead" else (None, None)
    return searches
