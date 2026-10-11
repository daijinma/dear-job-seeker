"""Reading jobs: which ones (a search, a task, a platform, filters), in which order, each job once. The results page and
the MCP tool both read through here, so they agree on what a duplicate is and on what a month of pay is."""
from __future__ import annotations

import json

from djs import cities, providers, taskqueue

MAX_WORDS = 10
# Of two records of one job, the one with more of these columns filled is the better one
FILLED = ("district", "education", "experience", "description", "published_at", "source_url")
# A queued or running task will still ask for this job's detail: it has none and no failed attempt on record, and the
# task does not run with detail 'never' (a JSON false reads as 0).
DUE = (f"(j.detail IS NULL AND j.detail_error IS NULL AND j.id IN (SELECT job_id FROM task_jobs WHERE task_id IN "
       f"(SELECT t.id FROM tasks t WHERE t.status IN {taskqueue.ACTIVE_SQL} "
       "AND IFNULL(json_extract(t.payload, '$.detail'), 'auto') NOT IN ('never', 0)))) AS detail_due")
WHY = f"j.provider, j.missing, j.salary_text, j.detail_error, {DUE}"   # what service.reasons needs to explain a job
COLS = ("id provider title company city district education experience salary_text salary_min salary_max salary_unit "
        "salary_months salary_month_min salary_month_max source_url published_at first_seen_at missing complete "
        "detail_error fingerprint").split()
_ROW = (", ".join(f"j.{c}" for c in COLS)
        + f", {DUE}, (j.description IS NOT NULL AND j.description <> '') AS has_description")
# How many of the search's words (:words, a JSON list) a job's title holds, in any case
_TITLE_HITS = "(SELECT COUNT(*) FROM json_each(:words) WHERE instr(lower(j.title), lower(value)) > 0)"
SORTS = {  # newest: the latest found first | mixed: the jobs whose title holds more of the search's words first, then the
           # platforms in turn, each one's newest first | salary: highest monthly pay first, none last | company
    "newest": "shown.first_seen_at DESC, shown.id DESC",
    "mixed": f"{_TITLE_HITS} DESC, shown.turn, shown.site, shown.id",
    "salary": "shown.salary_month_max IS NULL, shown.salary_month_max DESC, shown.salary_month_min DESC, "
              "shown.turn, shown.site, shown.id",
    "company": "(shown.company IS NULL OR shown.company = ''), shown.company, shown.id",
}

_FILLED = " + ".join(f"(NULLIF(j.{c}, '') IS NOT NULL)" for c in FILLED)
_RANK = "complete DESC, filled DESC, first_seen_at DESC, id DESC"   # the best record first
_JOB = "COALESCE(fingerprint, id)"   # a record without a fingerprint is a job of its own
# One platform often posts the same title more than once (another level, pay, JD): those are different postings and
# are never merged. hit: the jobs that pass the filters | ranked: each with its place (nth) among its platform's
# records of the job | best: its rank among the records at that place on every platform (1 = the one that is shown,
# the others are folded into it) | shown: the best records, each platform's turn and its place in the registry
_RANKED = f"ranked AS (SELECT hit.*, ROW_NUMBER() OVER (PARTITION BY {_JOB}, provider ORDER BY {_RANK}) AS nth FROM hit)"
_BEST = f"best AS (SELECT ranked.*, ROW_NUMBER() OVER (PARTITION BY {_JOB}, nth ORDER BY {_RANK}) AS dup FROM ranked)"
_SHOWN = ("shown AS (SELECT best.*, ROW_NUMBER() OVER (PARTITION BY provider ORDER BY first_seen_at DESC, id) AS turn, "
          "IFNULL(o.pos, 1e9) AS site FROM best "
          "LEFT JOIN (SELECT value AS name, key AS pos FROM json_each(:order)) o ON o.name = best.provider "
          "WHERE NOT :dedupe OR dup = 1)")


def _words(text, name: str) -> list[str]:
    words = (text or "").split()
    if len(words) > MAX_WORDS:
        raise ValueError(f"{name} takes at most {MAX_WORDS} words")
    return words


def where(*, search=None, task_id=None, provider=None, city=None, min_month_salary=None, max_month_salary=None,
          title=None, text=None, complete_only=True) -> tuple[str, dict]:
    """(WHERE clause for `jobs j`, named parameters). The jobs of a search (keyword, city: what its tasks saw), of a
    task and/or of a platform; located in `city`; whose pay per month can reach `min_month_salary` and start at or
    below `max_month_salary` (the job's range overlaps the asked one); whose title holds every word of `title` (the
    role), and whose title or description holds every word of `text` (skills that may sit anywhere), in any case. By
    default only complete jobs."""
    conds, args = [], {}

    def add(cond: str, **params) -> None:
        conds.append(cond)
        args.update(params)

    if search is not None:
        add(f"j.id IN (SELECT job_id FROM task_jobs WHERE task_id IN (SELECT id FROM tasks WHERE {taskqueue.SEARCH}))",
            k=search[0], c=search[1] or "")
    if task_id is not None:
        add("j.id IN (SELECT job_id FROM task_jobs WHERE task_id = :task)", task=task_id)
    if provider:
        add("j.provider = :provider", provider=provider)
    if city:
        add("instr(j.city, :city) > 0", city=cities.norm(city))
    if min_month_salary is not None:
        add("j.salary_month_max >= :min_month", min_month=min_month_salary)
    if max_month_salary is not None:
        add("j.salary_month_min <= :max_month", max_month=max_month_salary)
    for name, words, columns in (("title", title, ("title",)), ("text", text, ("title", "description"))):
        for i, word in enumerate(_words(words, name)):
            hit = " OR ".join(f"instr(lower(j.{column}), lower(:{name}{i})) > 0" for column in columns)
            add(f"({hit})", **{f"{name}{i}": word})
    if complete_only:
        add("j.complete = 1")
    return " AND ".join(conds) or "1", args


def _hit(where_sql: str) -> str:
    return ("hit AS (SELECT j.id, j.provider, j.fingerprint, j.complete, j.first_seen_at, j.company, j.source_url, "
            f"j.salary_month_min, j.salary_month_max, {_FILLED} AS filled FROM jobs j WHERE {where_sql})")


def count(conn, wh: tuple[str, dict], dedupe: bool = True) -> int:
    """How many jobs match; with `dedupe`, a job on several platforms counts once."""
    where_sql, args = wh
    return conn.execute(
        f"WITH {_hit(where_sql)}, {_RANKED}, {_BEST} SELECT COUNT(*) FROM best WHERE NOT :dedupe OR dup = 1",
        {**args, "dedupe": int(dedupe)}).fetchone()[0]


def count_by_provider(conn, wh: tuple[str, dict]) -> dict[str, int]:
    """How many records match, per platform: what that platform's own page lists (it never merges its own postings)."""
    where_sql, args = wh
    rows = conn.execute(f"WITH {_hit(where_sql)} SELECT provider, COUNT(*) AS n FROM hit GROUP BY provider", args)
    return {r["provider"]: r["n"] for r in rows}


def _folded(conn, wh: tuple[str, dict], jobs: list[dict]) -> dict[tuple, list[dict]]:
    """{(fingerprint, nth): the records folded into the job with that fingerprint and place, best first}, for a page."""
    where_sql, args = wh
    fingerprints = sorted({j["fingerprint"] for j in jobs if j["fingerprint"]})
    if not fingerprints:
        return {}
    rows = conn.execute(
        f"WITH {_hit(where_sql)}, {_RANKED}, {_BEST} SELECT fingerprint, nth, provider, id, source_url FROM best "
        "WHERE dup > 1 AND fingerprint IN (SELECT value FROM json_each(:fingerprints)) ORDER BY dup",
        {**args, "fingerprints": json.dumps(fingerprints)})
    found: dict[tuple, list[dict]] = {}
    for r in rows:
        found.setdefault((r["fingerprint"], r["nth"]), []).append(
            {"provider": r["provider"], "label": providers.label(r["provider"]), "id": r["id"],
             "source_url": r["source_url"]})
    return found


def find(conn, wh: tuple[str, dict], *, sort: str = "newest", limit: int = 20, offset: int = 0, dedupe: bool = True,
         description: bool = False, keyword: str | None = None) -> tuple[list[dict], int]:
    """(one page of the jobs matching `wh` in the order SORTS[sort], how many match in all). With `dedupe`, a job posted
    on several platforms appears once, as its best record (complete first, then more columns filled, then the newest);
    its `also_on` holds the records of the other platforms folded into it ({provider, label, id, source_url}), and
    every record stays on its own platform's page. `description` adds the full text. `keyword`: what the jobs were
    searched by; the order 'mixed' puts the jobs whose title holds more of its words first."""
    where_sql, args = wh
    words = json.dumps(list(dict.fromkeys((keyword or "").lower().split())))
    args = {**args, "dedupe": int(dedupe), "order": json.dumps(providers.names()), "limit": limit, "offset": offset,
            "words": words}
    cols = _ROW + ", shown.nth" + (", j.description" if description else "")
    rows = conn.execute(
        f"WITH {_hit(where_sql)}, {_RANKED}, {_BEST}, {_SHOWN} SELECT {cols} FROM shown JOIN jobs j ON j.id = shown.id "
        f"ORDER BY {SORTS[sort]} LIMIT :limit OFFSET :offset", args).fetchall()
    found = [dict(r) for r in rows]
    folded = _folded(conn, wh, found) if dedupe else {}
    for job in found:
        job["also_on"] = folded.get((job.pop("fingerprint"), job.pop("nth")), [])
    return found, count(conn, wh, dedupe)
