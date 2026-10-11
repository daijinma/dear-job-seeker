"""Browser capture transport: a Playwright page standing in for httpx.Client when a site refuses plain HTTP.

The browser is our own persistent profile (shared with login) or, with DJS_CDP_URL set, a new tab of the user's own
Chromium. Providers keep one parsing path: BrowserClient.get/post return real httpx.Responses.

The login window is the exception: sites blank a page that Playwright drives, so it is an ordinary browser process that
is only ever asked questions at the browser level (plain_window).
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, closing, contextmanager, suppress
from functools import partialmethod
from itertools import count
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

from djs import db, filelock, pacer
from djs.egress import NoEgressAvailable

INSTALL_HINT = "Playwright/Chromium missing. Run: pip install playwright && playwright install chromium"
# Browsers already on the machine first, so most people never download Playwright's own Chromium (None = that one)
CHANNELS = ("chrome", "msedge", None)
# Where a CHANNELS entry is installed: the macOS app, the path under Program Files / Local AppData on Windows, the
# commands on Linux. (None, Chromium itself, is looked up in _chromium.)
INSTALLED = {
    "chrome": ("Google Chrome", ("Google", "Chrome", "Application", "chrome.exe"), ("google-chrome", "google-chrome-stable")),
    "msedge": ("Microsoft Edge", ("Microsoft", "Edge", "Application", "msedge.exe"), ("microsoft-edge",)),
}
# Flags of the login window. The keychain pair is what Playwright passes too: without it the cookies the login stores
# would be encrypted with a key the crawling browser cannot read. A debugging port alone makes navigator.webdriver true,
# which the last flag undoes, as in launch_context.
WINDOW_FLAGS = ("--no-first-run", "--no-default-browser-check", "--use-mock-keychain", "--password-store=basic",
                "--lang=zh-CN", "--disable-blink-features=AutomationControlled")
QUIT_SECONDS = 5     # how long a browser of ours gets to leave after Browser.close, and again after terminate()
START_SECONDS = 20   # how long a browser of ours gets to listen and show its first page
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


class BrowserGone(BrowserUnavailable):
    """The browser quit while we were talking to it (the user closed it, or it crashed)."""


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


def profiles_in_use() -> list[str]:
    """The platforms whose browser profile is held right now (a login window, a browser crawl). Never waits."""
    root = db.home() / "profiles"
    busy = []
    for path in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            with profile_lock(path.name, wait=0):
                pass
        except BrowserBusy:
            busy.append(path.name)
    return busy


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


# ------------------------------------------------------------------ the login window: a plain browser process

def on_domain(cookies: list[dict], domain: str) -> list[dict]:
    """The cookies of `domain` and its subdomains, nothing else the browser happens to hold."""
    def mine(c: dict) -> bool:
        d = c.get("domain", "").lstrip(".")
        return d == domain or d.endswith("." + domain)
    return [c for c in cookies if mine(c)]


def _chromium() -> Path | None:
    """Chromium itself: the Linux distribution's, else the copy Playwright downloaded (asking Playwright starts its
    driver, so that is the last place to look)."""
    system = next(filter(None, map(shutil.which, ("chromium", "chromium-browser"))), None)
    if system:
        return Path(system)
    with _pw().sync_playwright() as p:
        own = Path(p.chromium.executable_path)
    return own if own.is_file() else None


def _installed(channel: str | None) -> Path | None:
    """The executable of a CHANNELS entry on this machine, None when it is not installed."""
    if channel is None:
        return _chromium()
    app, windows, commands = INSTALLED[channel]
    if sys.platform == "darwin":
        found = [base / f"{app}.app" / "Contents" / "MacOS" / app
                 for base in (Path("/Applications"), Path.home() / "Applications")]
    elif sys.platform == "win32":
        found = [Path(os.environ[v], *windows) for v in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData")
                 if v in os.environ]
    else:
        found = [Path(c) for c in map(shutil.which, commands) if c]
    return next((p for p in found if p.is_file()), None)


def find_browser() -> tuple[str, Path] | None:
    """(kind, executable) of the browser a login window starts in, the same pick as launch_context: Chrome, else Edge,
    else Chromium (kinds 'chrome', 'msedge', 'chromium'). None when the machine has none."""
    for channel in CHANNELS:
        if path := _installed(channel):
            return channel or "chromium", path
    return None


def executable() -> Path:
    """The browser a login window starts in (see find_browser)."""
    found = find_browser()
    if found is None:
        raise BrowserUnavailable(no_browser_hint())
    return found[1]


class _Cdp:
    """The DevTools protocol at the browser level over one websocket: send a command, wait for its answer. The browser
    sends events too; they are skipped. Not a line of this talks to a page. (The errors below say "window", not
    "browser": djs.results reads that word as "none is installed", which none of them means.)"""

    def __init__(self, address: str):
        self._ids, self._connection = count(1), ExitStack()  # websockets wants its connection used as a context manager
        try:
            # no size limit: a cookie jar can pass the default 1 MiB
            self._ws = self._connection.enter_context(connect(address, max_size=None, open_timeout=10))
        except (OSError, WebSocketException) as e:
            raise BrowserUnavailable(f"Cannot talk to the browser at {address}") from e

    def call(self, method: str, timeout: float = 10, **params) -> dict:
        n = next(self._ids)
        deadline = time.monotonic() + timeout
        try:
            self._ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                try:
                    reply = json.loads(self._ws.recv(timeout=max(deadline - time.monotonic(), 0)))
                except TimeoutError:
                    raise BrowserUnavailable(f"The window did not answer {method}") from None
                if reply.get("id") == n:
                    if "error" in reply:
                        raise BrowserUnavailable(f"{method}: {reply['error'].get('message')}")
                    return reply["result"]
        except ConnectionClosed:
            raise BrowserGone("The window has quit") from None

    def close(self) -> None:
        self._connection.close()


class Window:
    """The login window as the login sees it. Every question goes to the browser, none to the page: nothing is attached
    to it and no script runs in it, so the site cannot tell it from a window the user opened. Once the browser is gone
    the answers are empty (no cookies, no page: `closed()`)."""

    def __init__(self, cdp: _Cdp, target_id: str):
        self._cdp, self._id = cdp, target_id

    def _ask(self, method: str, **params) -> dict:
        try:
            return self._cdp.call(method, **params)
        except BrowserGone:
            return {}

    def _page(self) -> dict | None:
        return next((t for t in self._ask("Target.getTargets").get("targetInfos", []) if t["targetId"] == self._id), None)

    def cookies(self) -> list[dict]:
        """Every cookie the browser holds: name, value, domain, path and expires (-1 for a session cookie), as Playwright
        names them."""
        return self._ask("Storage.getCookies").get("cookies", [])

    def url(self) -> str:
        page = self._page()
        return page["url"] if page else ""

    def closed(self) -> bool:
        """The user closed the window: its page is gone, or the whole browser with it."""
        return self._page() is None

    def delete_cookies(self, names, domain: str) -> None:
        """Make the browser forget the cookies called `names` on `domain` (and its subdomains). The browser level has no
        delete: a cookie set again with an expiry long past is how it is told to drop one."""
        dead = [{"name": c["name"], "value": "", "domain": c["domain"], "path": c["path"], "secure": c["secure"],
                 "httpOnly": c["httpOnly"], "expires": 1}
                for c in on_domain(self.cookies(), domain) if c["name"] in names]
        if dead:
            self._ask("Storage.setCookies", cookies=dead)


def _exited(proc: subprocess.Popen, seconds: float) -> bool:
    try:
        proc.wait(seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


_alive: set[_Browser] = set()  # the browsers of ours that run; what is left at interpreter exit is closed there


class _Browser:
    """A browser process of ours and its DevTools socket. Whatever happens, close() leaves no process behind."""

    def __init__(self, command: list[str]):
        self.cdp: _Cdp | None = None
        self._closing = threading.Lock()
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        except OSError as e:
            raise BrowserUnavailable(f"Cannot start the browser {command[0]}: {e}") from e
        _alive.add(self)

    def close(self) -> None:
        """Ask the browser to quit and give it QUIT_SECONDS, else terminate it, else kill it. Safe to call twice and from
        two threads (the owner and the exit handler)."""
        with self._closing:
            _alive.discard(self)
            try:
                if self.cdp is not None:
                    with suppress(BrowserUnavailable):  # already gone is as good
                        self.cdp.call("Browser.close", timeout=QUIT_SECONDS)
                    self.cdp.close()
                    if _exited(self.proc, QUIT_SECONDS):
                        return
                self.proc.terminate()
                _exited(self.proc, QUIT_SECONDS)
            finally:  # also when interrupted above
                if self.proc.poll() is None:
                    self.proc.kill()
                self.proc.wait()


def _close_all() -> None:
    for started in list(_alive):
        with suppress(Exception):  # one stuck browser must not keep the others open
            started.close()


atexit.register(_close_all)


def _devtools_address(profile: Path) -> str | None:
    """The browser-level websocket of the browser running on `profile`, from the DevToolsActivePort file it writes there
    once it listens (port on the first line, socket path on the second); None until then."""
    try:
        port, path = (profile / "DevToolsActivePort").read_text().split()[:2]
    except (OSError, ValueError):
        return None
    return f"ws://127.0.0.1:{port}{path}" if port.isdigit() and path.startswith("/") else None


def _answers(address: str) -> bool:
    """The browser that wrote `address` into DevToolsActivePort still runs behind it: whatever is on that port names the
    same browser socket (its path carries a random id per browser run), so a stranger that took the port over is not
    taken for it."""
    host, path = urlsplit(address)[1:3]
    try:
        reply = httpx.get(f"http://{host}/json/version", timeout=2, trust_env=False).json()
        return urlsplit(reply["webSocketDebuggerUrl"]).path == path
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return False


def _clear_leftovers(profile: Path) -> None:
    """Free the profile for a new browser. One of ours that outlived a crashed run is asked to quit over its own socket
    (never a process picked by guessing at ids); a leftover DevToolsActivePort goes either way."""
    address = _devtools_address(profile)
    if address and _answers(address):
        with suppress(BrowserUnavailable), closing(_Cdp(address)) as cdp:
            cdp.call("Browser.close")
        deadline = time.monotonic() + QUIT_SECONDS
        while _answers(address):
            if time.monotonic() >= deadline:
                raise BrowserBusy("正在使用浏览器，请稍后再试")
            time.sleep(0.1)
    (profile / "DevToolsActivePort").unlink(missing_ok=True)


def _listening(profile: Path, proc: subprocess.Popen, deadline: float) -> str:
    """Wait until the browser we started listens; its address."""
    while (address := _devtools_address(profile)) is None:
        if proc.poll() is not None:  # e.g. the profile is open in another window of the browser, which took the job over
            raise BrowserUnavailable("The window quit right after it started. If the same profile is open in another "
                                     "window, close that one and try again.")
        if time.monotonic() >= deadline:
            raise BrowserUnavailable("The window did not start in time.")
        time.sleep(0.05)
    return address


def _first_page(cdp: _Cdp, deadline: float) -> str:
    """Id of the page the browser opened for the address it was started with (it can lag behind the socket)."""
    while True:
        pages = [t for t in cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        if pages:
            return pages[0]["targetId"]
        if time.monotonic() >= deadline:
            raise BrowserUnavailable("The window did not open the page.")
        time.sleep(0.05)


@contextmanager
def _own_window(provider_name: str, url: str, headless: bool):
    """The window in a browser process of ours on the shared profile, under the profile lock."""
    exe = executable()  # before the lock: with no browser there is nothing to wait for
    with profile_lock(provider_name):
        profile = db.profile_dir(provider_name, create=True) / "browser"
        _clear_leftovers(profile)
        started = _Browser([str(exe), f"--user-data-dir={profile}", "--remote-debugging-port=0", *WINDOW_FLAGS,
                            *(["--headless=new"] if headless else []), url])
        try:
            deadline = time.monotonic() + START_SECONDS
            started.cdp = _Cdp(_listening(profile, started.proc, deadline))
            yield Window(started.cdp, _first_page(started.cdp, deadline))
        finally:
            started.close()


def _browser_socket(address: str) -> str:
    """The browser-level websocket of the browser DJS_CDP_URL names (http://host:port, or a ws:// address as it is)."""
    if address.startswith(("ws://", "wss://")):
        return address
    try:
        return httpx.get(f"{address.rstrip('/')}/json/version", timeout=10, trust_env=False).json()["webSocketDebuggerUrl"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        raise BrowserUnavailable(CDP_HINT.format(url=address)) from e


@contextmanager
def _users_window(address: str, url: str):
    """The window as a new tab of the user's own browser; on exit only that tab is closed."""
    with closing(_Cdp(_browser_socket(address))) as cdp:
        target = cdp.call("Target.createTarget", url=url)["targetId"]
        try:
            yield Window(cdp, target)
        finally:
            with suppress(BrowserUnavailable):  # the user may have closed it, or quit
                cdp.call("Target.closeTarget", targetId=target)


@contextmanager
def plain_window(provider_name: str, url: str, headless: bool = False):
    """A browser window on `url` for the user to log in in; yields a Window.

    Sites blank a page that Playwright drives, even one we never call, so this is an ordinary browser process with a
    debugging port of which only the browser-level socket is used: nothing is attached to the page, no script is
    injected. It runs on the profile that crawling shares (cookies set here are read there), under the profile lock, and
    the process is gone when the block ends however it ends. With DJS_CDP_URL it is a new tab of the user's own browser
    instead, which is left alone apart from that tab. `headless` is for tests."""
    address = cdp_url()
    with (_users_window(address, url) if address else _own_window(provider_name, url, headless)) as window:
        yield window


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
