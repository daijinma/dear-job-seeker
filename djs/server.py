"""MCP stdio server. Tools are thin wrappers over djs.service; errors come back as {"error": msg}."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from djs import db, engine, envfile, service

POLL_SECONDS = 1.0   # how often wait_for_search reads the search again
MAX_WAIT = 600       # the longest one wait_for_search call blocks, whatever the caller asks for
INSTRUCTIONS = """Searches Chinese job sites from the user's own machine and keeps the results in a local database.
Recommended flow:
1. search(keyword, city): starts the search on every site usable right now and returns at once.
2. wait_for_search(keyword, city): blocks until no site is working any more (or `timeout`) and returns the search
   `status` and the per-site outcome: found, no_results (the site had nothing: not an error), failed, waiting_login
   (needs the user: login tool), retrying (the site failed and tries again by itself at `retry_at`, `reason` says why:
   the call does not wait for it; read what the others found, ask again later for more) or running. The search `status`:
   - running: a site still works (if it timed out, call again, or read what is there and check `searching`).
   - retrying: no site works and at least one failed and waits for its next try: not finished, and not a success.
   - waiting_login: a site needs the user (login tool).
   - done: every site finished: found, no_results or failed.
   If every site is retrying, check status() for the exits and the reason before searching again.
3. jobs_get(keyword, city, ...): reads the results, complete jobs only and each job once (`also_on` names the other
   sites that carry it). Put the role in title (every word must be in the job title, e.g. title="会计": without it,
   jobs of companies merely named "…会计服务…" come back too), conditions and skills in text (every word in the title,
   the description or the welfare tags `benefits`, e.g. text="双休" or text="Django"), and a hard pay floor in
   floor_month_salary (yuan per month, all units converted: floor_month_salary=8000 keeps the jobs whose LOWEST pay is
   8000 or more, "8000 以上"; min_month_salary=8000 keeps every job whose range reaches 8000, 6k-9k too;
   max_month_salary caps where the range starts). Narrow by city as well; page with limit and next_offset.
4. job_detail(job_id): the full description and welfare tags (`benefits`, null when the site lists none) of the few
   jobs you shortlist. jobs_get returns only short summaries.
Compare pay only through salary_month_min / salary_month_max: salary_text is the site's own wording (per day, per
hour, per year) and the figures behind it are in salary_unit.
If results look wrong or a search does not move, call status first: workers, queue, exits (the proxies requests go
out through, or direct), browser, logins and each platform's latest problem (failed, waiting for a login, waiting to
retry) in one call, without touching the network. If `status` shows `update.state == "ready"`, tell the user that a new
version waits in the app: it installs from the app, not from here."""


@asynccontextmanager
async def _lifespan(_server):
    """Ends with the connection. A client that goes away while a login window is open leaves `login` waiting in a thread
    that would keep this process, and the window, alive until its timeout: close the windows now. Blocking on purpose
    (it is the end; an await here could be cancelled before it ran)."""
    try:
        yield
    finally:
        service.shutdown()


mcp = FastMCP("dear-job-seeker", instructions=INSTRUCTIONS, lifespan=_lifespan)


def _on_sigterm(signum, frame) -> None:
    """A host that stops the server with SIGTERM: close what is ours (login windows), then leave at once. The default
    would kill the process with a window open; a normal exit would wait for the thread that reads stdin, which only
    returns when the client closes the pipe."""
    service.shutdown()
    os._exit(0)


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
    return await asyncio.to_thread(_run, service.provider_list)  # the login state may ask the attached browser (blocking socket)


@mcp.tool()
async def status() -> dict:
    """What is going on, in one call and without touching the network: the version and data folder; per platform whether
    THIS process's worker is alive (the app, another MCP server and `djs work` may run workers on the same database
    too), the task it runs now (keyword, city, since, pages, completing {done, total}) and the tasks that failed and
    wait for their next try (`retrying`: task_id, reason, retry_at); the queue by status; `exits`: the egress pool as
    egress_list shows it (passwords masked; id, provider scope with '*' = shared by every platform, ok / fail counts)
    plus `cooling_down` (the exit rests for at least one platform), just the one 'direct' exit when no proxy is
    configured; the browser a login window would start (or why there is none), DJS_CDP_URL, the open login
    windows and the platform profiles in use; each login's state and end; per platform its missing keys (names only),
    cooling-down exits and, when its newest task failed, waits for a login or waits for its next try (status
    retrying, with kind, reason and retry_at), why. If every site is retrying, read the exits and the reason here
    before searching again. Also the app's own version, where it runs from and `update` {current, latest, state, note}:
    state `ready` means a newer version is downloaded and waits in the app (it installs from the app, not from here).
    Call it first when results look wrong or a search does not move. Example: status()."""
    return await asyncio.to_thread(_run, service.status)  # the login state may ask the attached browser (blocking socket)


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
    where the platform needs one) and return at once with the search key, the platforms started, their task ids and
    `skipped`: the platforms that could not take this search (no code for the city, keys or browser missing), each
    with its reason, while the others go on. `providers` names the platforms instead (BOSS 直聘 opens a browser window
    and is used only when named); it is an error only when none of them can start. A city may be written as people
    write it: "浙江杭州" and "杭州市西湖区" count as 杭州. max_pages: result pages per platform. Then call
    wait_for_search. Example: search(keyword="Python 后端", city="杭州")."""
    return await asyncio.to_thread(_run, service.search, keyword, city, providers, max_pages)  # reads the login state


@mcp.tool()
async def wait_for_search(keyword: str, city: str | None = None, timeout: float = 120) -> dict:
    """Wait until no platform of a search is working any more (at most `timeout` seconds, up to 600) and return its
    status: the search `status`, per platform the outcome (found | no_results | failed | waiting_login | retrying |
    running), job counts (`whole`: the complete jobs of the whole search, each job once) and, while a platform fetches
    details, completing {done, total}. The search `status` is running (a platform still works; `timed_out: true` means
    the timeout hit first), retrying (none works, at least one failed and waits for its next try: not finished, not a
    success), waiting_login (a platform needs the user: login) or done (every platform finished: found, no_results or
    failed). A platform that failed and waits for its next try (retrying, with `reason` and `retry_at`, unix time)
    is not waited for; it runs again by itself later. If every platform is retrying, check status() for the exits and
    the reason before searching again. Give the same keyword and city as to search. Example:
    wait_for_search(keyword="Python 后端", city="杭州", timeout=120)."""
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
    retrying (failed, tries again at next_run_at) | running, with completing {done, total} while the task fetches
    details. Example: task_list(status="running")."""
    return _run(service.task_list, status, limit)


@mcp.tool()
def jobs_get(keyword: str | None = None, city: str | None = None, task_id: int | None = None,
             provider: str | None = None, min_month_salary: int | None = None, max_month_salary: int | None = None,
             floor_month_salary: int | None = None, title: str | None = None, text: str | None = None,
             complete_only: bool = True, dedupe: bool = True, fields: str = "summary", limit: int = 20,
             offset: int = 0) -> dict:
    """Read stored jobs as {jobs, total, returned, next_offset}. Where from: a search (the keyword and city given to
    search), a task_id and/or a provider; none = everything stored. Filters: city (the job's city); title (every word in
    the job TITLE, any case: the role, e.g. title="会计"); text (every word in the title, the description or the
    welfare tags `benefits`, any case: conditions and skills that may sit anywhere, e.g. text="双休"); pay in yuan per
    month, all units converted: floor_month_salary (the job's LOWEST pay is at least this: floor_month_salary=8000 keeps
    8k-12k and drops 6k-9k, use it for "8000 以上"), min_month_salary (the job's pay range REACHES this:
    min_month_salary=8000 keeps 6k-9k as well as 8k-12k), max_month_salary (the range starts at or below this). By
    default only complete jobs and each job once; incomplete ones come with complete_only=false and say what is
    `missing`; dedupe=false lists every record. fields='summary' returns short rows (salary_month_min/max are comparable
    across sites, benefits are the platform's welfare tags, description_snippet is 120 characters), 'full' the whole
    description. A search also says whether it is still `searching` (a site that failed and waits for its next try does
    not count). Example: jobs_get(keyword="会计", city="宁波", title="会计", text="双休", floor_month_salary=8000,
    limit=10)."""
    return _run(service.jobs_get, keyword=keyword, city=city, task_id=task_id, provider=provider,
                min_month_salary=min_month_salary, max_month_salary=max_month_salary,
                floor_month_salary=floor_month_salary, title=title, text=text, complete_only=complete_only,
                dedupe=dedupe, fields=fields, limit=limit, offset=offset)


@mcp.tool()
async def job_detail(job_id: int, raw: bool = False) -> dict:
    """One stored job (an id from jobs_get) with its whole description: fetched from the platform once, its fields
    (salary range, education, welfare tags ...) derived again from it, then served from the database. Returns what
    jobs_get returns with fields='full' (clean fields, description, benefits, missing and incomplete_reason, also_on),
    not the platform's own JSON; raw=true adds that as `detail`. Example: job_detail(job_id=42)."""
    return await asyncio.to_thread(_run, service.job_detail, job_id, raw)  # a paced network call: keep the loop free


@mcp.tool()
def search_list(limit: int = 20) -> dict:
    """Recent searches (same keyword + city) with status (running | retrying | waiting_login | done, as in
    wait_for_search: running only while some platform works; a platform that failed and waits for its next try does
    not count, and a search with one is retrying, not done), job counts (`whole`: complete jobs, each job once) and
    per-platform progress: outcome (found | no_results | failed | waiting_login | retrying | running; retrying comes
    with `reason` and `retry_at`), how many of each platform's jobs are complete / incomplete, completing
    {done, total} while it fetches details. Example: search_list(limit=5)."""
    return _run(service.searches, limit)


@mcp.tool()
async def login(provider: str) -> dict:
    """Open a browser window on the platform's login page for the user to log in (blocks up to 180s; login_cancel
    ends it early). The window closes by itself once the login is detected; tasks waiting for it resume. Only for
    platforms that have a login (see login_status)."""
    return await asyncio.to_thread(_run, service.login, provider)


@mcp.tool()
async def login_cancel(provider: str) -> dict:
    """Stop the login window that `login` opened for this platform; the waiting `login` call ends with an error.
    Tasks keep waiting for a login. Returns {provider, cancelled}: false when no window was open."""
    return await asyncio.to_thread(_run, service.login_cancel, provider)


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
    """Choose how a platform is captured: 'http' (plain requests) or 'browser' (a tab of Chrome or Edge; set DJS_CDP_URL
    to use a tab of your own Chrome instead). Only transports the platform supports; omit to reset to its default."""
    return _run(service.set_transport, provider, transport)


@mcp.tool()
def egress_add(url: str, provider: str = "*") -> dict:
    """Add an egress ('direct', an http(s) proxy URL, or host:port such as 127.0.0.1:7890, taken as http), shared ('*')
    or dedicated to one provider. Example: egress_add(url="127.0.0.1:7890")."""
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
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        mcp.run(transport="stdio")
    finally:
        signal.signal(signal.SIGTERM, previous)
        service.shutdown()  # also when the server ends by an interrupt or an error rather than a closed connection


if __name__ == "__main__":
    main()
