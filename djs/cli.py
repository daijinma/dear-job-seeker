"""Scripting CLI: `djs call <tool> [json]` runs one MCP tool and prints its JSON result; `djs work` runs the
per-platform workers in the foreground. Together they let a script queue, wait and read without an MCP client."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import sys
import time

from djs import db, engine, envfile, taskqueue


def _out(result: dict) -> int:
    print(json.dumps(result, ensure_ascii=False))
    return 1 if "error" in result else 0


def _call(tool: str, raw: str) -> int:
    from djs import server  # late import: it builds the MCP server object
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    if tool not in names:
        return _out({"error": f"unknown tool {tool!r}; available: {', '.join(names)}"})
    try:
        args = json.loads(raw)
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        fn = getattr(server, tool)
        return _out(asyncio.run(fn(**args)) if inspect.iscoroutinefunction(fn) else fn(**args))
    except (ValueError, TypeError) as e:  # tools report their own failures as {"error"}; this is bad arguments
        return _out({"error": f"bad arguments for {tool}: {e}"})


def _line(t: dict) -> str:
    s = t["stats"] or {}
    tail = (f"pages={s.get('pages', 0)} new={s.get('jobs_new', 0)} seen={s.get('jobs_seen', 0)}"
            if t["status"] == "done" else t["last_error"] or "")
    return f"task {t['id']} {t['provider']} {t['status']} {tail}".rstrip()


def _work(until_idle: bool, timeout: float | None) -> int:
    conn = db.connect()
    active = taskqueue.active_ids(conn)  # taken before the workers start, so a fast task is still reported
    engine.start_worker()
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            now = taskqueue.active_ids(conn)
            for task_id in sorted(active - now):  # left the queue: done, dead or waiting for a login
                print(_line(taskqueue.get(conn, task_id)), flush=True)
            active = now
            if until_idle and not active:
                return 0
            if deadline is not None and time.monotonic() >= deadline:
                print(f"timeout: {len(active)} task(s) still active", file=sys.stderr)
                return 2 if until_idle else 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.stop_worker()
        conn.close()


def main(argv: list[str]) -> int:
    envfile.load()
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its per-request INFO line would drown the task lines
    parser = argparse.ArgumentParser(prog="djs", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    call = sub.add_parser("call", help="run one MCP tool and print its JSON result (exit 1 on error)")
    call.add_argument("tool")
    call.add_argument("args", nargs="?", default="{}", help='JSON object, e.g. \'{"provider": "nowcoder"}\'')
    work = sub.add_parser("work", help="run the workers in the foreground, one line per finished task")
    work.add_argument("--until-idle", action="store_true", help="stop once no task is queued or running")
    work.add_argument("--timeout", type=float, metavar="SEC", help="stop after SEC seconds (exit 2 if --until-idle)")
    a = parser.parse_args(argv)
    return _call(a.tool, a.args) if a.cmd == "call" else _work(a.until_idle, a.timeout)
