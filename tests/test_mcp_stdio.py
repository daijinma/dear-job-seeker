import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {"status", "provider_list", "crawl_start", "search", "wait_for_search", "task_list", "jobs_get", "job_detail",
            "search_list", "login", "login_status", "login_check", "logout", "set_frequency", "set_transport",
            "egress_add", "egress_remove", "egress_list"}


def test_djs_entrypoint_lists_tools(tmp_path):
    djs_bin = Path(sys.executable).with_name("djs")
    # cwd=tmp_path: the server autoloads ./.env, which must not be the developer's real one
    params = StdioServerParameters(command=str(djs_bin), env={**os.environ, "DJS_HOME": str(tmp_path)}, cwd=tmp_path)

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}

    assert asyncio.run(asyncio.wait_for(go(), 30)) == EXPECTED
