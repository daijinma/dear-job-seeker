"""Entry of the packaged app. The window exe and the console exe share this dispatcher; they differ only in what
running without arguments does: open the window, or serve MCP over stdio."""
from __future__ import annotations

import sys

# What the window needs on each OS, imported without creating any window (CI cannot open one)
GUI_BACKENDS = {"darwin": ("AppKit", "Foundation", "WebKit"),
                "win32": ("webview.platforms.winforms", "webview.platforms.edgechromium")}


def selftest() -> None:
    """Prove the build is complete without opening a window: platforms, UI files, database schema, GUI backend,
    the Playwright driver and the MCP tools."""
    import asyncio
    import importlib
    import os
    import tempfile

    from playwright.sync_api import sync_playwright

    from djs import db, providers, server, ui_app

    for module in ("webview", *GUI_BACKENDS.get(sys.platform, ())):
        importlib.import_module(module)
    for name in providers.names():
        providers.get(name)  # providers are imported by name, which a bundler cannot see
    assert "</html>" in ui_app.page()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
        os.environ["DJS_HOME"] = home
        conn = db.connect()
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        conn.close()
    with sync_playwright():  # starts the bundled driver process
        pass
    assert "provider_list" in {t.name for t in asyncio.run(server.mcp.list_tools())}
    print("selftest ok")


def serve_mcp() -> None:
    """Stdio MCP server. Its transport closes stdout when the client goes away, and the packaged program flushes
    sys.stdout and sys.__stdout__ once more on exit, so by then both have to be something that is still open."""
    from djs import server
    sys.argv[1:] = []  # server.main() reads sys.argv; without arguments it serves MCP
    try:
        server.main()
    finally:
        sys.stdout = sys.__stdout__ = sys.stderr


def main(default: str = "ui") -> None:
    """`ui` opens the window, `mcp` serves stdio, `call` / `work` are the scripting CLI, `install-browser` downloads
    Chromium, `--selftest` checks the build. Without arguments: `default`."""
    args = sys.argv[1:] or [default]
    if args == ["--selftest"]:
        return selftest()
    if args == ["install-browser"]:
        from djs import browser
        sys.exit(browser.install_chromium())
    if args == ["mcp"]:
        return serve_mcp()
    from djs import server
    sys.argv[1:] = args
    server.main()


if __name__ == "__main__":
    main()
