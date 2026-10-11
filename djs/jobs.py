"""The jobs table. Every derived column comes from one place: what a platform says is read by derive, and what follows
from the stored values (completeness, the monthly salary, the duplicate fingerprint) by _derived, on every write; the
crawl, job_detail and the schema backfill all write through here."""
from __future__ import annotations

import json

from djs.normalize import fingerprint, missing, monthly, parse_salary, plain_text

SALARY = ("salary_min", "salary_max", "salary_unit", "salary_months")
# What a provider derives from a list item (row) and its metadata (meta).
FIELDS = ("title", "company", "city", "district", "education", "experience", "salary_text", *SALARY, "description",
          "source_url", "published_at", "published_at_kind")


def derive(prov, job: dict, detail: dict | None) -> dict:
    """Every derived column for a list item and, once fetched, its detail. Salary figures the provider read from
    structured fields win, else the salary text is parsed; the JD is stored as plain text."""
    got = {**prov.row(job, detail), **prov.meta(job, detail)}
    fields = {k: got.get(k) for k in FIELDS}
    fields["description"] = plain_text(fields["description"]) or None
    if None in (fields["salary_min"], fields["salary_max"]):
        fields.update(parse_salary(fields["salary_text"]) or dict.fromkeys(SALARY))
    return fields


def gaps(stored) -> list[str]:
    """The core elements a stored `missing` text names (comma-joined); none for a complete job."""
    return [name for name in (stored or "").split(",") if name]


def _derived(fields: dict) -> dict:
    """The columns that follow from the others: which core elements are missing, the salary per month, the fingerprint
    that marks one job posted on several sites. Computed from the values about to be stored, so they never disagree."""
    lacking = missing(fields)
    return {"missing": ",".join(lacking), "complete": int(not lacking), **monthly(fields),
            "fingerprint": fingerprint(fields)}


def _merged(stored: dict, fresh: dict) -> dict:
    """The provider's values over the stored ones. What it does not know now leaves the stored value alone, and a
    new salary replaces the old one as a whole (months included)."""
    merged = {k: stored[k] if v is None else v for k, v in fresh.items()}
    if fresh["salary_min"] is not None:
        merged.update({k: fresh[k] for k in SALARY})
    return merged


def _stored(conn, job_pk: int) -> dict:
    return dict(conn.execute(f"SELECT {', '.join(FIELDS)} FROM jobs WHERE id=?", (job_pk,)).fetchone())


def _write(conn, job_pk: int, fields: dict) -> None:
    cols = {**fields, **_derived(fields)}
    conn.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), job_pk))


def insert(conn, task_id: int, prov, source_job_id: str, job: dict) -> int:
    """A job seen for the first time, from its list item."""
    fields = derive(prov, job, None)
    cols = {"provider": prov.name, "source_job_id": source_job_id, "task_id": task_id,
            "payload": json.dumps(job, ensure_ascii=False), **fields, **_derived(fields)}
    marks = ", ".join("?" * len(cols))
    return conn.execute(f"INSERT INTO jobs({', '.join(cols)}) VALUES ({marks})", tuple(cols.values())).lastrowid


def rederive(conn, job_pk: int, prov, job: dict, detail: dict | None) -> None:
    """Recompute the derived columns from a raw item and its detail, then judge completeness again."""
    _write(conn, job_pk, _merged(_stored(conn, job_pk), derive(prov, job, detail)))


def refresh(conn, job_pk: int) -> None:
    """Compute the derived columns again from what is stored (a job whose platform can no longer derive anything)."""
    _write(conn, job_pk, _stored(conn, job_pk))


def update(conn, job_pk: int, prov, job: dict, detail: dict | None) -> None:
    """Store a detail fetched after the fact and re-derive from it."""
    conn.execute("UPDATE jobs SET detail=?, detail_error=NULL WHERE id=?",
                 (json.dumps(detail, ensure_ascii=False) if detail else None, job_pk))
    rederive(conn, job_pk, prov, job, detail)


def fail_detail(conn, job_pk: int, error: Exception) -> None:
    conn.execute("UPDATE jobs SET detail_error=? WHERE id=?", (str(error), job_pk))


def awaiting_detail(conn, task_id: int, everything: bool) -> list:
    """The jobs this task saw that have no stored detail yet: the incomplete ones, or all of them."""
    return conn.execute(
        "SELECT j.id, j.payload, j.missing FROM jobs j JOIN task_jobs t ON t.job_id=j.id "
        "WHERE t.task_id=? AND j.detail IS NULL AND (? OR j.complete=0) ORDER BY j.id",
        (task_id, everything)).fetchall()


def tally(conn, task_id: int) -> dict:
    """How many of the jobs a task saw are complete."""
    total, complete = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(j.complete), 0) FROM task_jobs t JOIN jobs j ON j.id=t.job_id "
        "WHERE t.task_id=?", (task_id,)).fetchone()
    return {"complete": complete, "incomplete": total - complete}
