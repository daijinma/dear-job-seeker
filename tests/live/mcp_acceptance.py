"""Live MCP acceptance (T5): the server over real stdio, the way an assistant uses it, against the real data folder and the
real sites. By hand only; pytest skips this module unless DJS_LIVE=1.

    python tests/live/mcp_acceptance.py [--djs PROGRAM] [--boss | --platforms a,b] [--keyword K --city C] [--title T --floor N]
                                        [--timeout S] [--browser-platform job51 | none]

Sequence: status, provider_list, login_status, search (every platform usable now; BOSS 直聘 only with --boss) and
wait_for_search, jobs_get with a title and a pay floor, job_detail of the first hit, login_check of one platform with a
login (not BOSS), and, on a fresh folder, the browser transport: `set_transport` to browser on one platform (default 前程无忧, signed out), a
search on it that must return complete jobs through a browser of ours, and no browser process left. `--djs` starts the packaged program as an assistant does
(the console `djs` of the app) instead of the source tree; `--platforms` searches only the named platforms, with no
detail request to BOSS and no login window (used for the BOSS check on a copy of the data folder; with BOSS in it a
`boss_fields` step lists which core elements its jobs carry). Then, on a FRESH temporary data folder: `login` on one platform with a login (not BOSS): the window
must start, the client leaves after 25 s, and no browser process of that profile may remain. Prints a pass / fail table;
the exit code is 1 when a step failed."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

if __name__ != "__main__" and not os.environ.get("DJS_LIVE"):
    import pytest
    pytest.skip("live MCP acceptance: set DJS_LIVE=1 (it searches real sites; run it by hand)", allow_module_level=True)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
LOGIN_FIRST = ("kq36", "zhaopin", "job51")   # a login window on one of these before 猎聘; never BOSS
STAY = 25                                    # seconds the client keeps the login window open before it leaves
STATES = ("logged_in", "expired", "not_logged_in")
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Report:
    def __init__(self):
        self.rows = []

    async def step(self, name: str, work):
        """Run `work` (async, returns (result, note)); an exception is a failed step."""
        begun = time.monotonic()
        try:
            result, note = await work()
        except Exception as e:
            result, note = FAIL, f"{type(e).__name__}: {e}"
        self.rows.append((name, result, time.monotonic() - begun, note))

    def show(self) -> int:
        width = max(len(name) for name, *_ in self.rows)
        print(f"\n{'step'.ljust(width)}  result  seconds  note")
        for name, result, seconds, note in self.rows:
            print(f"{name.ljust(width)}  {result.ljust(6)}  {seconds:7.1f}  {note}")
        failed = [name for name, result, *_ in self.rows if result == FAIL]
        tail = f"; failed: {', '.join(failed)}" if failed else ""
        print(f"\n{len(self.rows) - len(failed)} of {len(self.rows)} steps passed or skipped{tail}")
        return 1 if failed else 0


PROGRAM: str | None = None   # --djs: the packaged console program, started with no arguments as an assistant's config does


def params(home: str | None, cwd: Path) -> StdioServerParameters:
    """The server as a host starts it. `cwd` is an empty folder: the server autoloads ./.env, which must not be this checkout's."""
    env = {**os.environ}
    if home:
        env["DJS_HOME"] = home
    if PROGRAM:
        return StdioServerParameters(command=PROGRAM, args=[], env=env, cwd=cwd)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH")) if p)
    return StdioServerParameters(command=sys.executable, args=["-m", "djs.server"], env=env, cwd=cwd)


async def call(session: ClientSession, name: str, timeout: float = 120, **args) -> dict:
    result = await session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=timeout))
    text = "".join(block.text for block in result.content if block.type == "text")
    if result.isError:
        raise RuntimeError(f"{name} was refused: {text[:300]}")
    return json.loads(text)


def commands() -> list[str]:
    """The command line of every process on this machine."""
    if sys.platform == "win32":
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"],
                             capture_output=True, text=True, timeout=60).stdout
        return out.splitlines()
    return subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=30).stdout.splitlines()


def browsers_of(profile: Path) -> list[str]:
    """The processes that run on this profile folder (a browser, and what it started)."""
    return [line.strip() for line in commands() if str(profile) in line]


async def until(condition, seconds: float):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.5)
    return True


def login_platform(rows: list[dict], logged_in_first: bool) -> str | None:
    """A platform with a login that is not BOSS 直聘 (only a logged-in one when `logged_in_first` and there is one)."""
    rows = [r for r in rows if r["provider"] != "boss"]
    order = {name: i for i, name in enumerate(LOGIN_FIRST)}
    rows.sort(key=lambda r: (not (logged_in_first and r["logged_in"]), r["provider"] == "liepin", order.get(r["provider"], len(order))))
    return rows[0]["provider"] if rows else None


async def real_data_folder(args, report: Report, cwd: Path) -> list[dict]:
    """Everything but the fresh-folder login. Returns the login_status rows."""
    ctx: dict = {}
    async with stdio_client(params(None, cwd)) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def status():
                got = await call(s, "status")
                ctx["status"] = got
                shaped = (set(got["update"]) == {"current", "latest", "state", "note"} and {"version", "frozen", "path"} <= set(got["app"])
                          and {"workers", "queue", "exits", "browser", "logins", "platforms"} <= set(got))
                return (PASS if shaped else FAIL), (f"v{got['version']} at {got['app']['path']}; update {got['update']['state']}; "
                                                    f"data {got['data_dir']}")

            async def provider_list():
                ctx["providers"] = rows = (await call(s, "provider_list"))["result"]
                unset = [r["name"] for r in rows if not r["configured"]]
                workers = ctx.get("status", {}).get("workers", {})
                named = len(rows) == len(workers) and all(r["name"] in workers for r in rows)   # the same platforms as status
                return (PASS if named else FAIL), f"{len(rows)} platforms" + (f"; keys missing: {', '.join(unset)}" if unset else "")

            async def login_status():
                ctx["logins"] = rows = (await call(s, "login_status"))["result"]
                bad = [r["provider"] for r in rows if r["state"] not in STATES]
                have = [r["provider"] for r in rows if r["logged_in"]]
                return (FAIL if bad or not rows else PASS), f"{len(rows)} with a login; logged in: {', '.join(have) or 'none'}"

            async def search():
                if args.platforms:
                    got = await call(s, "search", keyword=args.keyword, city=args.city, providers=args.platforms.split(","))
                else:
                    got = await call(s, "search", keyword=args.keyword, city=args.city)
                if args.boss and not args.platforms:
                    boss = await call(s, "search", keyword=args.keyword, city=args.city, providers=["boss"])
                    got = {**got, "providers": [*got["providers"], *boss["providers"]], "task_ids": [*got["task_ids"], *boss["task_ids"]],
                           "skipped": [*got["skipped"], *boss["skipped"]]}
                ctx["search"] = got
                skipped = ", ".join(f"{x['provider']} ({x['reason'][:40]})" for x in got["skipped"])
                return (PASS if got["task_ids"] else FAIL), f"{len(got['providers'])} started" + (f"; skipped: {skipped}" if skipped else "")

            async def wait():
                if "search" not in ctx:
                    return SKIP, "no search was started"
                got = await call(s, "wait_for_search", timeout=args.timeout + 60, keyword=args.keyword, city=args.city)
                outcomes: dict[str, int] = {}
                for site in got["per_site"]:
                    outcomes[site["outcome"]] = outcomes.get(site["outcome"], 0) + 1
                asked, answered = set(ctx["search"]["providers"]), {site["provider"] for site in got["per_site"]}
                ok = got["status"] != "running" and not got.get("timed_out") and asked <= answered and outcomes.get("found", 0) > 0
                found = ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items()))
                return (PASS if ok else FAIL), f"{got['status']}; {found}; {got['whole']} jobs"

            async def jobs_get():
                if args.platforms == "boss":   # its jobs are list-only, so never complete: boss_fields reads them instead
                    return SKIP, "BOSS 直聘 only: see boss_fields"
                every = await call(s, "jobs_get", keyword=args.keyword, city=args.city, limit=1)
                title = args.title or args.keyword
                got = await call(s, "jobs_get", keyword=args.keyword, city=args.city, title=title, floor_month_salary=args.floor, limit=5)
                words = title.casefold().split()
                wrong = [j["id"] for j in got["jobs"]
                         if not (j["salary_month_min"] or 0) >= args.floor or not all(w in j["title"].casefold() for w in words)]
                ctx["hit"] = (got["jobs"] or every["jobs"] or [None])[0]
                ok = not wrong and got["total"] <= every["total"] and every["total"] > 0 and got["returned"] == len(got["jobs"])
                note = f"{got['total']} of {every['total']} jobs have {title!r} in the title and pay from {args.floor}"
                note += f"; against the filter: {wrong}" if wrong else ""
                return (PASS if ok else FAIL), note + ("" if got["jobs"] else "; the first hit is any job")

            async def job_detail():
                hit = ctx.get("hit")
                if hit is None:
                    return SKIP, "no job was stored"
                if hit["provider"] == "boss":   # a detail request to BOSS is what its risk control punishes
                    return SKIP, "the first hit is a BOSS 直聘 job: no detail request is made to it"
                got = await call(s, "job_detail", job_id=hit["id"])
                ok = "error" not in got and got["id"] == hit["id"] and bool(got["description"])
                note = got.get("error") or f"{hit['provider']} {hit['title']!r}: {len(got['description'])} characters, benefits {got['benefits']}"
                return (PASS if ok else FAIL), note

            async def login_check():
                name = login_platform(ctx.get("logins", []), logged_in_first=True)
                if name is None:
                    return SKIP, "no platform with a login besides BOSS 直聘"
                got = await call(s, "login_check", provider=name)
                note = f"{name}: {got.get('state') or got}" + ("" if got.get("logged_in") else " (no saved login: nothing was asked)")
                return (PASS if got.get("state") in STATES else FAIL), note

            async def boss_fields():
                if "boss" not in (args.platforms or "") and not args.boss:
                    return SKIP, "BOSS 直聘 was not searched"
                got = await call(s, "jobs_get", keyword=args.keyword, city=args.city, provider="boss", complete_only=False,
                                 fields="full", limit=50)
                jobs = got["jobs"]
                if not jobs:
                    return FAIL, "no BOSS 直聘 job was stored"
                have = {
                    "title": lambda j: bool(j.get("title")), "company": lambda j: bool(j.get("company")),
                    "city": lambda j: bool(j.get("city")),
                    "salary": lambda j: isinstance(j.get("salary_month_min"), int) and isinstance(j.get("salary_month_max"), int),
                    "description": lambda j: bool((j.get("description") or "").strip()),
                    "district": lambda j: bool(j.get("district")), "education": lambda j: bool(j.get("education")),
                    "experience": lambda j: bool(j.get("experience")), "benefits": lambda j: bool(j.get("benefits")),
                    "source_url": lambda j: bool(j.get("source_url")),
                }
                counts = {k: sum(1 for j in jobs if f(j)) for k, f in have.items()}
                table = ", ".join(f"{k} {n}/{len(jobs)}" for k, n in counts.items())
                core = all(counts[k] == len(jobs) for k in ("title", "company", "city", "salary"))
                note = f"{len(jobs)} jobs: {table}; missing as stored: {sorted({m for j in jobs for m in ([j['missing']] if isinstance(j.get('missing'), str) else j.get('missing') or []) if m})}"
                return (PASS if core else FAIL), note

            for name, work in (("status", status), ("provider_list", provider_list), ("login_status", login_status), ("search", search),
                               ("wait_for_search", wait), ("jobs_get", jobs_get), ("job_detail", job_detail), ("login_check", login_check),
                               ("boss_fields", boss_fields)):
                await report.step(name, work)
    return ctx.get("logins", [])


async def fresh_folder_login(logins: list[dict], report: Report, work: Path) -> None:
    """`login` on a data folder that never saw one: the window starts, the client leaves after STAY seconds, nothing stays."""
    name = login_platform(logins, logged_in_first=False)
    if name is None:
        report.rows.append(("login (fresh folder)", SKIP, 0.0, "no platform with a login besides BOSS 直聘"))
        return
    home = (work / "home").resolve()
    profile = home / "profiles" / name
    seen: dict = {}

    async def open_window():
        async with stdio_client(params(str(home), work)) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                begun = time.monotonic()
                waiting = asyncio.create_task(call(s, "login", timeout=300, provider=name))
                seen["opened"] = await until(lambda: waiting.done() or browsers_of(profile), 30)
                seen["processes"] = browsers_of(profile)
                seen["still_waiting"] = not waiting.done()
                if not waiting.done():
                    seen["listed"] = [w["provider"] for w in (await call(s, "status"))["browser"]["login_windows"]]
                    await asyncio.sleep(max(0.0, STAY - (time.monotonic() - begun)))
                seen["still_waiting_later"] = not waiting.done()
                if waiting.done():   # it ended by itself: the window did not start, or somebody logged in
                    seen["answer"] = waiting.exception() or waiting.result()
                waiting.cancel()   # the client leaves: its connection ends with the call still waiting
                await asyncio.gather(waiting, return_exceptions=True)
        ok = seen["opened"] and seen["processes"] and seen["still_waiting"] and seen["still_waiting_later"] and seen.get("listed") == [name]
        return (PASS if ok else FAIL), (f"{name}: browser started ({len(seen['processes'])} processes), the call waited, status listed the window"
                                        if ok else f"{name}: {seen.get('answer') or seen}")

    async def nothing_left():
        gone = await until(lambda: not browsers_of(profile), 20)
        left = browsers_of(profile)
        for line in left:   # only if this failed: no browser of ours is left running
            try:
                os.kill(int(line.split()[0]), getattr(signal, "SIGKILL", signal.SIGTERM))
            except (ValueError, OSError):
                pass
        return (PASS if gone else FAIL), (f"no process of {profile} after the client left" if gone else f"still running: {left[:3]}")

    await report.step("login (fresh folder)", open_window)
    await report.step("window closed after leaving", nothing_left)


async def fresh_folder_browser(args, report: Report, work: Path) -> None:
    """The browser transport on a data folder that never saw a login (only the platform keys are copied): `set_transport`
    to browser on one platform that has it (default 前程无忧: its PC search needs no login), a search on it that must
    return complete jobs through a browser of ours, then no browser process of that profile may remain."""
    name = args.browser_platform
    if name in ("", "none") or args.platforms:
        report.rows.append(("browser transport (fresh folder)", SKIP, 0.0, "not asked for"))
        return
    home = (work / "browser-home").resolve()
    home.mkdir()
    keys = Path(os.environ.get("DJS_HOME") or Path.home() / ".dear-job-seeker") / ".env"
    if keys.exists():
        shutil.copy(keys, home / ".env")
    profile = home / "profiles" / name

    async def through_browser():
        async with stdio_client(params(str(home), work)) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                before = (await call(s, "status"))["browser"]
                if not before.get("executable"):
                    return SKIP, f"no browser on this machine: {before.get('hint')}"
                await call(s, "set_transport", provider=name, transport="browser")
                started = await call(s, "search", keyword=args.keyword, city=args.city, providers=[name])
                got = await call(s, "wait_for_search", timeout=args.timeout + 60, keyword=args.keyword, city=args.city)
                site = next((x for x in got["per_site"] if x["provider"] == name), {})
                rows = await call(s, "jobs_get", keyword=args.keyword, city=args.city, provider=name, limit=20)
        ok = site.get("outcome") == "found" and rows["total"] > 0 and not started["skipped"]
        return (PASS if ok else FAIL), (f"{name} through {before['executable']['kind']}: {site.get('outcome')}, "
                                        f"{rows['total']} complete jobs" + ("" if ok else f"; {site.get('reason') or started['skipped']}"))

    async def nothing_left():
        gone = await until(lambda: not browsers_of(profile), 20)
        return (PASS if gone else FAIL), (f"no process of {profile} after the client left" if gone else f"still running: {browsers_of(profile)[:3]}")

    await report.step("browser transport (fresh folder)", through_browser)
    await report.step("browser closed after leaving", nothing_left)


async def run(args) -> int:
    report = Report()
    work = Path(tempfile.mkdtemp(prefix="djs-acceptance-"))
    try:
        cwd = work / "cwd"
        cwd.mkdir()
        logins = await real_data_folder(args, report, cwd)
        await fresh_folder_browser(args, report, work)
        if not args.platforms:
            await fresh_folder_login(logins, report, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report.show()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live MCP acceptance over real stdio (see the module's docstring).")
    parser.add_argument("--boss", action="store_true", help="also search BOSS 直聘 (it opens a browser window; off by default)")
    parser.add_argument("--platforms", default="", help="search only these platforms (comma-separated names); no login window")
    parser.add_argument("--djs", help="the packaged console program to start instead of the source tree")
    parser.add_argument("--browser-platform", default="job51", help="platform for the browser-transport step, or none")
    parser.add_argument("--keyword", default="测试工程师")
    parser.add_argument("--city", default="成都")
    parser.add_argument("--title", help="words every job title must contain (default: the keyword)")
    parser.add_argument("--floor", type=int, default=8000, help="lowest monthly pay the jobs_get step asks for")
    parser.add_argument("--timeout", type=float, default=420, help="seconds wait_for_search may block")
    args = parser.parse_args(argv)
    global PROGRAM
    PROGRAM = args.djs
    return asyncio.run(run(args))


def test_live_acceptance():
    assert main([]) == 0   # only with DJS_LIVE=1; BOSS 直聘 stays out


if __name__ == "__main__":
    sys.exit(main())
