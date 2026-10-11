"""Browser capture transport: a Playwright page standing in for httpx.Client when a site refuses plain HTTP.

The browser is our own persistent profile (shared with login) or, with DJS_CDP_URL set, a new tab of the user's own
Chromium. Providers keep one parsing path: BrowserClient.get/post return real httpx.Responses.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from contextlib import ExitStack, closing, contextmanager
from functools import partialmethod
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from djs import db, filelock, pacer
from djs.egress import NoEgressAvailable

INSTALL_HINT = "Playwright/Chromium missing. Run: pip install playwright && playwright install chromium"
# Browsers already on the machine first, so most people never download Playwright's own Chromium (None = that one)
CHANNELS = ("chrome", "msedge", None)
CDP_HINT = ("Cannot reach a browser at {url}. Start Chrome with --remote-debugging-port=9222 "
            "--user-data-dir=<a new empty folder> and set DJS_CDP_URL=http://127.0.0.1:9222")
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"  # the one tell Boss reads
# fetch() inside a page that already ran the site's scripts: cookies and anti-bot tokens are the browser's own
FETCH_JS = """async ({url, method, headers, body, timeout}) => {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), timeout);
  try {
    const r = await fetch(url, {method, headers, body, credentials: 'include', signal: ctl.signal});
    return {status: r.status, headers: [...r.headers], text: await r.text()};
  } catch (e) { return {error: e.name === 'AbortError' ? 'timeout' : String(e)}; } finally { clearTimeout(timer); }
}"""
DECODED_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}  # no longer true of the decoded body


class BrowserUnavailable(RuntimeError):
    """Playwright/Chromium missing or the CDP endpoint unreachable: no retry helps, the message says what to do."""


class BrowserBusy(NoEgressAvailable):
    """The profile is in use (login, another task): handled like a cooling-down exit, retry later, nothing burned."""


def cdp_url() -> str | None:
    return os.environ.get("DJS_CDP_URL", "").strip() or None


def ready() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _pw():
    try:
        import playwright.sync_api as pw
    except ImportError as e:
        raise BrowserUnavailable(INSTALL_HINT) from e
    return pw


def _headless(prov) -> bool:
    forced = os.environ.get("DJS_BROWSER_HEADLESS", "").strip()
    return forced == "1" if forced in ("0", "1") else not prov.needs_headed


def proxy_settings(exit_id: str | None) -> dict | None:
    """Playwright wants credentials apart from the server: http://u:p@h:8080 -> {server, username, password}."""
    if not exit_id or exit_id == "direct":
        return None
    u = urlsplit(exit_id)
    proxy = {"server": f"{u.scheme}://{u.netloc.rpartition('@')[2]}"}
    if u.username:
        proxy.update(username=unquote(u.username), password=unquote(u.password or ""))
    return proxy


def no_browser_hint() -> str:
    """What to do on a machine with neither Chrome, Edge nor Playwright's Chromium."""
    if getattr(sys, "frozen", False):  # the packaged app has no `playwright` command; its own `djs` runs the installer
        install = f'"{Path(sys.executable).with_stem("djs")}" install-browser'
    else:
        install = "playwright install chromium"
    return ("No browser found. Install Google Chrome or Microsoft Edge, or download Chromium once "
            f"(several hundred MB): {install}")


def install_chromium() -> int:
    """Download Playwright's own Chromium with the driver inside the playwright package, which is also present in the
    packaged app. Returns the installer's exit code."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env  # what `playwright install` runs
    node, cli = compute_driver_executable()
    return subprocess.run([node, cli, "install", "chromium"], env=get_driver_env()).returncode


def _not_installed(e: Exception, channel: str | None) -> bool:
    """Playwright's wording for "that browser is not on this machine" (channel None: its own Chromium download)."""
    return (f"Chromium distribution '{channel}' is not found" if channel else "Executable doesn't exist") in str(e)


def launch_context(pw, provider_name: str, headless: bool, proxy: str | None = None):
    """THE place that starts the persistent profile, so login and crawling share cookies and flags."""
    for channel in CHANNELS:
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(db.profile_dir(provider_name) / "browser"), channel=channel, headless=headless, locale="zh-CN",
                viewport={"width": 1280, "height": 800}, proxy=proxy_settings(proxy),
                args=["--disable-blink-features=AutomationControlled"])
        except _pw().Error as e:
            if _not_installed(e, channel):
                continue
            raise
        ctx.add_init_script(STEALTH_JS)
        return ctx
    raise BrowserUnavailable(no_browser_hint())


@contextmanager
def profile_lock(provider_name: str, wait: float = 3.0):
    """A Chromium profile belongs to one process at a time: login and browser crawls take turns."""
    with open(db.profile_dir(provider_name, create=True) / ".lock", "a+") as f, ExitStack() as held:
        try:
            held.enter_context(filelock.locked(f, wait))
        except filelock.LockTimeout:
            raise BrowserBusy("正在使用浏览器，请稍后再试") from None
        yield


def _attach(pw, url: str):
    try:
        return pw.chromium.connect_over_cdp(url, timeout=10_000)
    except _pw().Error as e:
        raise BrowserUnavailable(CDP_HINT.format(url=url)) from e


@contextmanager
def open_context(provider_name: str, headless: bool, proxy: str | None = None):
    """The context to work in: the default one of the user's own browser (CDP) or our locked persistent profile."""
    with _pw().sync_playwright() as pw, ExitStack() as stack:
        if url := cdp_url():
            yield stack.enter_context(closing(_attach(pw, url))).contexts[0]  # close() only disconnects here
        else:
            stack.enter_context(profile_lock(provider_name))
            yield stack.enter_context(closing(launch_context(pw, provider_name, headless, proxy)))


@contextmanager
def open_page(provider_name: str, headless: bool, proxy: str | None = None):
    with open_context(provider_name, headless, proxy) as ctx:
        page = ctx.new_page()  # always our own tab: whatever the user already has open stays untouched
        try:
            yield page
        finally:
            page.close()


@contextmanager
def _guard():
    """Playwright failures are network failures to the engine, which already retries those."""
    try:
        yield
    except _pw().TimeoutError as e:
        raise httpx.ReadTimeout(str(e)) from e
    except _pw().Error as e:
        raise httpx.ConnectError(str(e)) from e


def _response(req: httpx.Request, status: int, headers, text: str) -> httpx.Response:
    resp = httpx.Response(status, request=req, content=text.encode(),
                          headers=[(k, v) for k, v in headers if k.lower() not in DECODED_HEADERS])
    resp.encoding = "utf-8"  # the text arrives decoded, whatever charset the site declared
    return resp


class BrowserClient:
    """The part of httpx.Client providers use (get/post/request), run as in-page fetch() calls, plus browser-only
    primitives. Everything that touches the network is paced per (platform, exit) like an http request."""

    def __init__(self, page, prov, exit_id: str, interval: float, jitter: float):
        self.page, self._pace = page, (prov.name, exit_id, interval, jitter)

    def request(self, method, url, *, params=None, headers=None, data=None, json=None, content=None, timeout=20,
                paced=True):
        """paced=False: the call belongs to a visit whose page load was already paced (see goto)."""
        # an httpx.Request encodes URL, params and body, so both transports send the same bytes
        req = httpx.Request(method, urljoin(self.page.url, url), params=params, headers=headers, data=data,
                            json=json, content=content)
        if paced:
            pacer.wait(*self._pace)
        arg = {"url": str(req.url), "method": method, "headers": dict(req.headers),
               "body": req.content.decode() or None, "timeout": timeout * 1000}
        with _guard():
            out = self.page.evaluate(FETCH_JS, arg)
        if "error" in out:
            raise (httpx.ReadTimeout if out["error"] == "timeout" else httpx.ConnectError)(out["error"], request=req)
        return _response(req, out["status"], out["headers"], out["text"])

    get = partialmethod(request, "GET")
    post = partialmethod(request, "POST")

    def goto(self, url: str, timeout: float = 30) -> None:
        pacer.wait(*self._pace)
        with _guard():
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)

    def wait_cookie(self, name: str, timeout: float = 10) -> None:
        """Block until the page's own scripts have set cookie `name` (anti-bot tokens are written by JS after load)."""
        with _guard():
            self.page.wait_for_function("n => document.cookie.split('; ').some(c => c.startsWith(n + '='))",
                                        arg=name, timeout=timeout * 1000)

    def html(self) -> str:
        return self.page.content()

    def eval(self, js: str, arg=None):
        return self.page.evaluate(js, arg)

    def sniff(self, url_part: str, action, timeout: float = 15) -> httpx.Response:
        """The first response whose URL contains `url_part` while `action()` (a click, say) runs: the site's own XHR."""
        pacer.wait(*self._pace)
        with _guard():
            with self.page.expect_response(lambda r: url_part in r.url, timeout=timeout * 1000) as seen:
                action()
            r = seen.value
            return _response(httpx.Request(r.request.method, r.url), r.status, r.headers.items(), r.text())


@contextmanager
def open_browser(prov, exit_id: str, interval: float, jitter: float):
    """A paced BrowserClient in a fresh page. Call it from plain threads only (engine workers, the UI, to_thread):
    sync_playwright raises on a thread that runs an asyncio loop."""
    with open_page(prov.name, _headless(prov), exit_id) as page:
        yield BrowserClient(page, prov, exit_id, interval, jitter)
