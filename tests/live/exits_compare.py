"""Live exit comparison (L7): the same search on every public platform, once direct and once through a proxy app on this
machine, each on a throwaway data folder. By hand only; pytest skips this module unless DJS_LIVE=1.

    python tests/live/exits_compare.py [--proxy http://127.0.0.1:7897] [--gap 300] [--only a,b] [--keyword K --city C] [--timeout S]

Platforms with a login (BOSS 直聘, 康强) have none on a fresh folder and are not asked; 猎聘 is left out by name. A platform
that did not answer cleanly in the direct run is not asked again through the proxy (the first refusal ends its run).
Besides the signing keys of the real data folder (`.env`, copied for the run, deleted after) nothing of it is used. Prints a
table; the exit's country is asked once from a neutral service per exit, and its address is not printed."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

if __name__ != "__main__" and not os.environ.get("DJS_LIVE"):
    import pytest
    pytest.skip("live exit comparison: set DJS_LIVE=1 (it searches real sites; run it by hand)", allow_module_level=True)

os.environ.setdefault("DJS_LIVE", "1")   # run by hand: the helper module below has the same guard

import httpx
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from mcp_acceptance import call, params

LEFT_OUT = {"boss", "liepin", "kq36"}
ECHO = "https://ipinfo.io/country"


def country(proxy: str | None) -> str:
    try:
        return httpx.get(ECHO, proxy=proxy, trust_env=False, timeout=15).text.strip()[:8]
    except httpx.HTTPError as e:
        return f"? ({type(e).__name__})"


async def run(label: str, root: Path, keys: Path | None, proxy: str | None, only: set[str] | None, args) -> dict[str, dict]:
    """One search on a fresh data folder; {platform: {outcome, jobs, seconds, reason}}."""
    home = root / label
    home.mkdir()
    if keys and keys.exists():
        shutil.copy(keys, home / ".env")
    async with stdio_client(params(str(home), root / "empty")) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            if proxy:
                await call(s, "egress_add", url=proxy)
            rows = (await call(s, "provider_list"))["result"]
            names = [r["name"] for r in rows if r["auth"] != "login" and r["configured"] and r["name"] not in LEFT_OUT
                     and (only is None or r["name"] in only)]
            started = time.monotonic()
            await call(s, "search", keyword=args.keyword, city=args.city, providers=names)
            seen: dict[str, dict] = {}
            while len(seen) < len(names) and time.monotonic() - started < args.timeout:
                # `call` keeps `timeout` for itself, and this tool has an argument of that name
                reply = await s.call_tool("wait_for_search", {"keyword": args.keyword, "city": args.city, "timeout": 5},
                                          read_timeout_seconds=timedelta(seconds=60))
                got = json.loads("".join(block.text for block in reply.content if block.type == "text"))
                for site in got["per_site"]:
                    if site["outcome"] != "running" and site["provider"] not in seen:
                        seen[site["provider"]] = {"outcome": site["outcome"], "jobs": site["jobs"],
                                                  "seconds": time.monotonic() - started,
                                                  "reason": (site.get("reason") or site.get("last_error") or "")[:60]}
            used = [(e["id"], e["total_ok"], e["total_fail"]) for e in (await call(s, "status"))["exits"]]
            print(f"{label}: exits used (id, ok, failed): {used}", flush=True)
            for name in names:
                seen.setdefault(name, {"outcome": "timeout", "jobs": 0, "seconds": args.timeout, "reason": ""})
            return seen


async def main(args) -> int:
    real_keys = Path.home() / ".dear-job-seeker" / ".env"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "empty").mkdir()
        print(f"direct: exit country {country(None)}; through {args.proxy}: {country(args.proxy)}", flush=True)
        only = set(args.only.split(",")) if args.only else None
        direct = await run("direct", root, real_keys, None, only, args)
        clean = {n for n, r in direct.items() if r["outcome"] in ("found", "no_results")}
        print(f"direct done; waiting {args.gap} s before the proxy run", flush=True)
        await asyncio.sleep(args.gap)
        proxied = await run("proxy", root, real_keys, args.proxy, clean, args)
    print(f"\n{'platform':10} {'direct':26} {'through ' + args.proxy.split('//')[-1]:30}")
    for name in sorted(direct):
        def cell(r):
            return f"{r['outcome']} {r['jobs']} jobs {r['seconds']:.0f}s" + (f" ({r['reason']})" if r["reason"] else "") if r else "not asked"
        print(f"{name:10} {cell(direct[name]):26} {cell(proxied.get(name)):30}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--gap", type=int, default=300, help="seconds between the two runs")
    ap.add_argument("--keyword", default="测试工程师")
    ap.add_argument("--city", default="成都")
    ap.add_argument("--only", default="", help="comma-separated platform names; default every public one")
    ap.add_argument("--timeout", type=int, default=600, help="seconds one run may take")
    sys.exit(asyncio.run(main(ap.parse_args())))
