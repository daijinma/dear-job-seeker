"""MCP stdio server. Tools are thin wrappers over djs.service; errors come back as {"error": msg}."""
from __future__ import annotations

import asyncio
import sys

from mcp.server.fastmcp import FastMCP

from djs import db, engine, envfile, service

mcp = FastMCP("dear-job-seeker")


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
                tenant: str | None = None, max_pages: int | None = None, detail: bool = True) -> dict:
    """Queue a crawl task. detail=False is quick mode (list items only); load one job's full text with job_detail."""
    return _run(service.crawl_start, provider, keyword, city, tenant, max_pages, detail)


@mcp.tool()
def task_list(status: str | None = None, limit: int = 20) -> dict:
    """List recent tasks, optionally by status."""
    return _run(service.task_list, status, limit)


@mcp.tool()
def jobs_get(task_id: int | None = None, provider: str | None = None, limit: int = 50, with_detail: bool = False) -> dict:
    """Read stored jobs."""
    return _run(service.jobs_get, task_id, provider, limit, with_detail)


@mcp.tool()
async def job_detail(job_id: int) -> dict:
    """Full description of one stored job: fetched from the platform once, then served from the database."""
    return await asyncio.to_thread(_run, service.job_detail, job_id)  # paced network call: keep the event loop free


@mcp.tool()
def search_list(limit: int = 20) -> dict:
    """Recent searches (same keyword + city) with status, job counts and per-platform progress."""
    return _run(service.searches, limit)


@mcp.tool()
async def login(provider: str) -> dict:
    """Open a browser for the user to log in (blocks up to 180s); resumes waiting tasks on success."""
    return await asyncio.to_thread(_run, service.login, provider)


@mcp.tool()
async def login_status(provider: str) -> dict:
    """Whether a valid login exists for the provider."""
    return await asyncio.to_thread(_run, service.login_status, provider)


@mcp.tool()
def logout(provider: str) -> dict:
    """Delete the stored cookies/profile."""
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
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
