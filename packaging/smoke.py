"""Smoke-test a PyInstaller build the way it will be used: the console `djs` (what an MCP client or a script runs)
and the window program. Usage: python packaging/smoke.py [dist-dir] [--no-launch]   (dist-dir defaults to dist)

Needs the build environment's Python (the project's dependencies provide the MCP client) and network access: one real
crawl runs against nowcoder."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the source tree, for the expected platform list

NAME = "找岗位助手"
FOLDER = "dear-job-seeker"  # the Windows build folder (see the spec)
WINDOWS = sys.platform == "win32"  # its window program is a GUI-subsystem exe: no stdout, no stdio MCP


def expect(condition, message: str) -> None:
    if not condition:
        raise SystemExit(f"SMOKE FAILED: {message}")


def ok(label: str) -> None:
    print(f"ok   {label}", flush=True)


def locate(dist: Path) -> tuple[Path, Path, Path | None]:
    """The window program, the console program and (macOS) the app bundle of the build in `dist`."""
    if sys.platform == "darwin":
        app = dist / f"{NAME}.app"
        return app / "Contents/MacOS" / NAME, app / "Contents/MacOS/djs", app
    folder = dist / FOLDER
    return folder / f"{NAME}.exe", folder / "djs.exe", None


def run(command: list, home: str, timeout: float = 120, check: bool = True) -> str:
    """Run a program of the build in a scratch home; its stdout. A non-zero exit fails the smoke test unless
    `check` is off."""
    done = subprocess.run([str(c) for c in command], capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, cwd=home, env={**os.environ, "DJS_HOME": home})
    expect(done.returncode == 0 or not check, f"{Path(command[0]).name} {' '.join(map(str, command[1:]))} exited "
                                              f"{done.returncode}\n{done.stdout}{done.stderr}")
    return done.stdout


def call(console: Path, home: str, tool: str, args: dict | None = None) -> dict:
    return json.loads(run([console, "call", tool, *([json.dumps(args)] if args else [])], home))


def check_selftest(window: Path, console: Path, home: str) -> None:
    expect("selftest ok" in run([console, "--selftest"], home), "djs --selftest did not print 'selftest ok'")
    out = run([window, "--selftest"], home)  # on Windows only the exit code counts: that program has no stdout
    expect(WINDOWS or "selftest ok" in out, f"{NAME} --selftest did not print 'selftest ok'")
    ok("selftest (console and window program)")


def check_cli(console: Path, home: str) -> None:
    from djs import providers
    platforms = call(console, home, "provider_list")["result"]
    expect(len(platforms) == len(providers.names()), f"provider_list shows {len(platforms)} platforms")
    expect(all(p["browser_ready"] for p in platforms), "the Playwright driver is not in the build")
    expect(call(console, home, "login_status", {"provider": "boss"})["logged_in"] is False, "login_status")
    ok(f"djs call provider_list: {len(platforms)} platforms, browser ready")


def check_crawl(console: Path, home: str) -> None:
    task = call(console, home, "crawl_start",
                {"provider": "nowcoder", "keyword": "python", "max_pages": 1, "detail": "never"})
    expect("task_id" in task, f"crawl_start answered {task}")
    said = run([console, "work", "--until-idle", "--timeout", "90"], home, 150, check=False)
    newest = call(console, home, "task_list")["result"][0]
    expect(newest["status"] == "done" and newest["stats"]["jobs_new"] > 0,
           f"the nowcoder crawl ended as {newest['status']} ({newest['last_error']}); `work` said: {said.strip()}")
    ok(f"crawl: nowcoder stored {newest['stats']['jobs_new']} jobs")


async def mcp_tools(program: Path, args: list[str], home: str) -> set[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=str(program), args=args, env={"DJS_HOME": home}, cwd=home)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return {t.name for t in (await session.list_tools()).tools}


def check_mcp(window: Path, console: Path, home: str) -> None:
    for program, args in [(console, []), *([] if WINDOWS else [(window, ["mcp"])])]:
        tools = asyncio.run(asyncio.wait_for(mcp_tools(program, args, home), 90))
        expect({"provider_list", "crawl_start"} <= tools, f"{program.name} serves only {sorted(tools)}")
        ok(f"MCP over stdio via `{' '.join([program.name, *args])}`: {len(tools)} tools")


def check_launch(app: Path, home: str) -> None:
    """Open the .app like a double-click; 8 seconds later it must still run and have written nothing to stderr."""
    err = Path(home) / "app.err"
    subprocess.run(["open", "-n", "--env", f"DJS_HOME={home}", "--stderr", str(err), str(app)], check=True)
    time.sleep(8)
    # pkill answers 0 only if it found the process, and is how the app is closed again; the full path keeps
    # a copy of the app the user may have open elsewhere out of it
    running = subprocess.run(["pkill", "-f", re.escape(f"{app}/Contents/MacOS/{NAME}")]).returncode == 0
    expect(running, "the app was not running 8 seconds after it was opened")
    expect(not err.read_text(encoding="utf-8").strip(), f"the app wrote to stderr:\n{err.read_text(encoding='utf-8')}")
    ok("the .app opens and keeps running")


def size_mb(path: Path) -> float:
    return sum(os.lstat(os.path.join(d, f)).st_size for d, _, files in os.walk(path) for f in files) / 1e6


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # the program names are Chinese; a Windows console would not print them
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", nargs="?", default="dist")
    parser.add_argument("--no-launch", action="store_true",
                        help="macOS: do not open the .app (that needs an unlocked desktop)")
    args = parser.parse_args()
    dist = Path(args.dist).resolve()
    window, console, app = locate(dist)
    expect(window.exists() and console.exists(), f"no build in {dist}")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
        check_selftest(window, console, home)
        check_cli(console, home)
        check_crawl(console, home)
        check_mcp(window, console, home)
        if app and not args.no_launch:
            check_launch(app, home)
    shipped = app or window.parent
    print(f"size: {size_mb(shipped):.0f} MB unpacked ({shipped.name})")
    print("smoke ok")


if __name__ == "__main__":
    main()
