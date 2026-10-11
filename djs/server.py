"""MCP stdio server. Tools are thin wrappers over djs.service; errors come back as {"error": msg}."""
from __future__ import annotations

import asyncio
import sys
import time

from mcp.server.fastmcp import FastMCP

from djs import db, engine, envfile, service

POLL_SECONDS = 1.0   # how often wait_for_search reads the search again
MAX_WAIT = 600       # the longest one wait_for_search call blocks, whatever the caller asks for
INSTRUCTIONS = """Searches Chinese job sites from the user's own machine and keeps the results in a local database.
Recommended flow:
1. search(keyword, city): starts the search on every site usable right now and returns at once.
2. wait_for_search(keyword, city): blocks until every site is done (or `timeout`) and returns the per-site outcome:
   found, no_results (the site had nothing: not an error), failed, waiting_login (needs the user: login tool) or
   running. If it timed out, call it again, or read what is there and check `searching`.
3. jobs_get(keyword, city, ...): reads the results, complete jobs only and each job once (`also_on` names the other
   sites that carry it). Narrow it with min_month_salary / max_month_salary (yuan per month, all units converted),
   text (every word must be in the title or description) and city; page with limit and next_offset.
4. job_detail(job_id): the full description of the few jobs you shortlist. jobs_get returns only short summaries.
Compare pay only through salary_month_min / salary_month_max: salary_text is the site's own wording (per day, per
hour, per year) and the figures behind it are in salary_unit."""

mcp = FastMCP("dear-job-seeker", instructions=INSTRUCTIONS)


def _run(fn, *args, **kwargs):
    """One short-lived connection per call (calls may land on different threads)."""
    try:
        conn = db.connect()
        try:
            result = fn(conn, *args, **kwargs)
        finally:
            conn.close()
        return result if isinstance(result, dict) else {"result": result}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def provider_list() -> dict:
    """List platforms with type, auth, capture transport, effective frequency and login state."""
    return await asyncio.to_thread(_run, service.provider_list)  # the login state may need a browser (plain thread)


@mcp.tool()
def crawl_start(provider: str, keyword: str | None = None, city: str | None = None,
                tenant: str | None = None, max_pages: int | None = None, detail: str | bool = "auto") -> dict:
    """Queue a crawl task. A finished job has title, company, city, a salary range (two numbers) and a job
    description. detail: 'auto' (default) requests a job's detail page only while one of these is still missing,
    'always' for every job that has no stored detail yet, 'never' keeps the list items as they are (job_detail
    loads one on demand); true / false mean always / never."""
    return _run(service.crawl_start, provider, keyword, city, tenant, max_pages, detail)


@mcp.tool()
async def search(keyword: str, city: str | None = None, providers: list[str] | None = None, max_pages: int = 1) -> dict:
    """Start a search on every platform usable right now (no login required, no browser window, keys set, and a city
    where the platform needs one) and return at once with the search key and the task ids. `providers` names the
    platforms instead (BOSS 直聘 opens a browser window and is used only when named). max_pages: result pages per
    platform. Then call wait_for_search. Example: search(keyword="Python 后端", city="杭州")."""
    return await asyncio.to_thread(_run, service.search, keyword, city, providers, max_pages)  # reads the login state


@mcp.tool()
async def wait_for_search(keyword: str, city: str | None = None, timeout: float = 120) -> dict:
    """Wait until every platform of a search has finished (at most `timeout` seconds, up to 600) and return its
    status: per platform the outcome (found | no_results | failed | waiting_login | running), job counts and, while a
    platform fetches details, completing {done, total}. `timed_out: true` means it was still running. Give the same
    keyword and city as to search. Example: wait_for_search(keyword="Python 后端", city="杭州", timeout=120)."""
    deadline = time.monotonic() + max(0.0, min(timeout, MAX_WAIT))
    while True:
        status = await asyncio.to_thread(_run, service.search_status, keyword, city)
        if "error" in status or status["status"] != "running":
            return status
        if time.monotonic() >= deadline:
            return {**status, "timed_out": True}
        await asyncio.sleep(POLL_SECONDS)


@mcp.tool()
def task_list(status: str | None = None, limit: int = 20) -> dict:
    """List recent tasks, optionally by status. stats has pages, jobs_new, jobs_seen, details_ok, details_failed and
    how many of the task's jobs are complete / incomplete; outcome is found | no_results | failed | waiting_login |
    running, with completing {done, total} while the task fetches details. Example: task_list(status="running")."""
    return _run(service.task_list, status, limit)


@mcp.tool()
def jobs_get(keyword: str | None = None, city: str | None = None, task_id: int | None = None,
             provider: str | None = None, min_month_salary: int | None = None, max_month_salary: int | None = None,
             text: str | None = None, complete_only: bool = True, dedupe: bool = True, fields: str = "summary",
             limit: int = 20, offset: int = 0) -> dict:
    """Read stored jobs as {jobs, total, returned, next_offset}. Where from: a search (the keyword and city given to
    search), a task_id and/or a provider; none = everything stored. Filters: city (the job's city), min_month_salary /
    max_month_salary (yuan per month: the job's pay range must overlap), text (every word in the title or description,
    any case). By default only complete jobs and each job once; incomplete ones come with complete_only=false and say
    what is `missing`; dedupe=false lists every record. fields='summary' returns short rows (salary_month_min/max are
    comparable across sites, description_snippet is 120 characters), 'full' the whole description. A search also says
    whether it is still `searching`. Example: jobs_get(keyword="Python 后端", city="杭州", min_month_salary=15000,
    text="Django", limit=10)."""
    return _run(service.jobs_get, keyword, city, task_id, provider, min_month_salary, max_month_salary, text,
                complete_only, dedupe, fields, limit, offset)


@mcp.tool()
async def job_detail(job_id: int) -> dict:
    """Full detail of one stored job (an id from jobs_get): fetched from the platform once, its fields (salary range,
    education, ...) derived again from it, then served from the database. Example: job_detail(job_id=42)."""
    return await asyncio.to_thread(_run, service.job_detail, job_id)  # paced network call: keep the event loop free


@mcp.tool()
def search_list(limit: int = 20) -> dict:
    """Recent searches (same keyword + city) with status, job counts and per-platform progress: outcome, how many of
    each platform's jobs are complete / incomplete, completing {done, total} while it fetches details. Example:
    search_list(limit=5)."""
    return _run(service.searches, limit)


@mcp.tool()
async def login(provider: str) -> dict:
    """Open a browser window on the platform's login page for the user to log in (blocks up to 180s). The window
    closes by itself once the login is detected; tasks waiting for it resume. Only for platforms that have a login
    (see login_status)."""
    return await asyncio.to_thread(_run, service.login, provider)


@mcp.tool()
async def login_status(provider: str | None = None) -> dict:
    """Login state of one platform, or (no platform) of every platform that has a login: state (logged_in,
    expired or not_logged_in), when it was saved and when it ends, how the user logs in and what logging in
    improves. Reads saved files only; login_check asks the platform."""
    return await asyncio.to_thread(_run, service.login_status, provider)


@mcp.tool()
async def login_check(provider: str | None = None) -> dict:
    """Ask the platform (one request) whether the saved login still works; a dead one is marked expired. Without a
    platform: all that have a login (one that cannot be reached keeps its state and carries an error)."""
    return await asyncio.to_thread(_run, service.login_check, provider)


@mcp.tool()
def logout(provider: str) -> dict:
    """Log out: delete the saved login and the browser profile of the platform."""
    return _run(service.logout, provider)


@mcp.tool()
def set_frequency(provider: str, interval: float | None = None, jitter: float | None = None,
                  max_pages: int | None = None, force: bool = False) -> dict:
    """Override request frequency; below the platform min_interval needs force=True."""
    return _run(service.set_frequency, provider, interval, jitter, max_pages, force)


@mcp.tool()
def set_transport(provider: str, transport: str | None = None) -> dict:
    """Choose how a platform is captured: 'http' (plain requests) or 'browser' (a real Chromium page; set DJS_CDP_URL
    to use a tab of your own Chrome instead). Only transports the platform supports; omit to reset to its default."""
    return _run(service.set_transport, provider, transport)


@mcp.tool()
def egress_add(url: str, provider: str = "*") -> dict:
    """Add an egress ('direct' or http(s) proxy URL), shared ('*') or dedicated to one provider."""
    return _run(service.egress_add, url, provider)


@mcp.tool()
def egress_remove(url: str) -> dict:
    """Remove an egress."""
    return _run(service.egress_remove, url)


@mcp.tool()
def egress_list() -> dict:
    """List egress with health."""
    return _run(service.egress_list)


def main() -> None:
    """`djs` = stdio MCP server; `djs ui` = desktop app; `djs call|work` = scripting CLI."""
    argv = sys.argv[1:]
    if argv[:1] == ["ui"]:
        from djs import ui_app
        return ui_app.main(argv[1:])
    if argv[:1] in (["call"], ["work"]):
        from djs import cli
        sys.exit(cli.main(argv))
    envfile.load()
    engine.start_worker()
    service.start_login_check()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
