import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from djs import providers

EXPECTED = {"status", "provider_list", "crawl_start", "search", "wait_for_search", "task_list", "jobs_get", "job_detail",
            "search_list", "login", "login_cancel", "login_status", "login_check", "logout", "set_frequency", "set_transport",
            "egress_add", "egress_remove", "egress_list"}


def test_djs_entrypoint_lists_tools_and_answers_status_and_provider_list(tmp_path):
    djs_bin = Path(sys.executable).with_name("djs")
    # cwd=tmp_path: the server autoloads ./.env, which must not be the developer's real one
    params = StdioServerParameters(command=str(djs_bin), env={**os.environ, "DJS_HOME": str(tmp_path)}, cwd=tmp_path)

    async def tool(session, name):
        result = await session.call_tool(name, {})
        assert not result.isError
        return json.loads(result.content[0].text)

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                hello = await s.initialize()
                return (hello, {t.name for t in (await s.list_tools()).tools}, await tool(s, "status"), await tool(s, "provider_list"))

    hello, tools, status, listed = asyncio.run(asyncio.wait_for(go(), 60))
    assert hello.serverInfo.name == "dear-job-seeker" and 'update.state == "ready"' in hello.instructions   # what a host is told
    assert tools == EXPECTED
    assert set(status["update"]) == {"current", "latest", "state", "note"}
    assert status["update"]["state"] in ("none", "downloading", "ready", "failed")
    assert set(status["app"]) == {"version", "frozen", "path"} and status["app"]["frozen"] is False and status["app"]["path"]
    assert status["data_dir"] == str(tmp_path) and list(status["workers"]) == providers.names()   # the real server, a fresh data folder
    assert [row["name"] for row in listed["result"]] == providers.names() and len(listed["result"]) == 14
