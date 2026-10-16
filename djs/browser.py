"""Browser capture transport: a tab of an ordinary browser standing in for httpx.Client when a site refuses plain HTTP.

The browser is Chrome (else Edge) started by us on a persistent profile shared with login or, with DJS_CDP_URL set, a new tab
of the user's own. Providers keep one parsing path: BrowserClient.get/post return real httpx.Responses.

Sites blank a page that an automation driver controls, so nothing here is one: the browser is an ordinary
process with a debugging port, a page is only navigated and asked questions with in-page scripts (no Page, Runtime or
Network domain switched on, no binding, no injected script), cookies are read and written at the browser level. The login
window is the same browser, asked even less (plain_window).
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, closing, contextmanager, suppress
from functools import partialmethod
from itertools import count
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

from djs import db, filelock, pacer
from djs.egress import NoEgressAvailable

CHANNELS = ("chrome", "msedge")   # the browser of the machine, in the order they are tried (every Windows has Edge)
# Where a CHANNELS entry is installed: the macOS app, the path under Program Files / Local AppData on Windows, the
# commands on Linux.
INSTALLED = {
    "chrome": ("Google Chrome", ("Google", "Chrome", "Application", "chrome.exe"), ("google-chrome", "google-chrome-stable")),
    "msedge": ("Microsoft Edge", ("Microsoft", "Edge", "Application", "msedge.exe"), ("microsoft-edge",)),
}
# Flags of every browser we start, login window and crawl alike. The keychain pair is what the profiles of earlier versions
# were made with: without it their stored cookies could not be decrypted. A debugging port alone makes navigator.webdriver
# true, which the last flag undoes.
WINDOW_FLAGS = ("--no-first-run", "--no-default-browser-check", "--use-mock-keychain", "--password-store=basic",
                "--lang=zh-CN", "--disable-blink-features=AutomationControlled")
CRAWL_FLAGS = ("--window-size=1280,900",)   # the window experiment E1 ran in (headless would open at 756x469)
QUIT_SECONDS = 5     # what a browser of ours gets at each of the three steps of closing it (Browser.close, terminate, kill)
START_SECONDS = 20   # how long a browser of ours gets to listen and show its first page
CDP_HINT = ("Cannot reach a browser at {url}. Start Chrome with --remote-debugging-port=9222 "
            "--user-data-dir=<a new empty folder> and set DJS_CDP_URL=http://127.0.0.1:9222")
POLL_SECONDS = 0.05   # between two looks at a page that is still loading or has not set its cookie yet
SETTLE_SECONDS = 1.0  # how long a page must stand still (document complete, same address) for settle() to be satisfied
FETCH_MARGIN = 10     # seconds the browser gets, beyond a fetch's own timeout, to say that the page's fetch is over
BLANK = ("about:", "chrome-error:")   # where a tab ends when nothing was loaded (a 204, a refused redirect, an error page)
# a navigation that ended the page's script context, which is only a reason to look again; and a tab that is gone
LOST = ("context was destroyed", "cannot find context", "navigated or closed", "promise was collected")
GONE = ("target closed", "session with given id", "no session with given id", "no target with given id")
# expressions evaluated bare, and the functions called with an argument (see _script)
POLL_JS = "[location.href, document.readyState]"
HREF_JS = "location.href"
HTML_JS = "document.documentElement.outerHTML"
COOKIE_JS = "n => document.cookie.split('; ').some(c => c.startsWith(n + '='))"
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
    """No browser to start or the CDP endpoint unreachable: no retry helps, the message says what to do."""


class BrowserGone(BrowserUnavailable):
    """The browser quit while we were talking to it (the user closed it, or it crashed)."""


class BrowserBusy(NoEgressAvailable):
    """The profile is in use (login, another task): handled like a cooling-down exit, retry later, nothing burned."""


def cdp_url() -> str | None:
    return os.environ.get("DJS_CDP_URL", "").strip() or None


def ready() -> bool:
    """A crawl in a browser can start: the user's own is attached, or one is installed to start."""
    return cdp_url() is not None or find_browser() is not None


def _headless(prov) -> bool:
    forced = os.environ.get("DJS_BROWSER_HEADLESS", "").strip()
    return forced == "1" if forced in ("0", "1") else not prov.needs_headed


def _proxy_flags(exit_id: str | None) -> list[str]:
    """--proxy-server for an exit that is a proxy. The flag takes no credentials, and answering a proxy's authentication
    challenge would need the Fetch domain, which a site's detection could see: a proxy with a user name is refused (A31)."""
    if not exit_id or exit_id == "direct":
        return []
    u = urlsplit(exit_id)
    if u.username:
        raise BrowserUnavailable("Pages loaded this way cannot use a proxy that needs a user name and password: "
                                 "use a proxy without one, or the direct exit")
    return [f"--proxy-server={u.scheme}://{u.netloc}"]


def no_browser_hint() -> str:
    """What to do on a machine with neither Chrome nor Edge."""
    return "No browser found: install Google Chrome or Microsoft Edge."


@contextmanager
def profile_lock(provider_name: str, wait: float = 3.0):
    """A Chromium profile belongs to one process at a time: login and browser crawls take turns. It is not free either
    while a browser of ours that could not be closed (see _Browser.close) still runs on it."""
    with open(db.profile_dir(provider_name, create=True) / ".lock", "a+") as f, ExitStack() as held:
        try:
            held.enter_context(filelock.locked(f, wait))
        except filelock.LockTimeout:
            raise BrowserBusy("正在使用浏览器，请稍后再试") from None
        if _stuck(db.profile_dir(provider_name) / "browser"):
            raise BrowserBusy("正在使用浏览器，请稍后再试")
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


# ------------------------------------------------------------------ the login window: a plain browser process

def on_domain(cookies: list[dict], domain: str) -> list[dict]:
    """The cookies of `domain` and its subdomains, nothing else the browser happens to hold."""
    def mine(c: dict) -> bool:
        d = c.get("domain", "").lstrip(".")
        return d == domain or d.endswith("." + domain)
    return [c for c in cookies if mine(c)]


def _installed(channel: str) -> Path | None:
    """The executable of a CHANNELS entry on this machine, None when it is not installed."""
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
    """(kind, executable) of the browser we start, login window and crawl alike: Chrome, else Edge (kinds 'chrome',
    'msedge'). None when the machine has none."""
    for channel in CHANNELS:
        if path := _installed(channel):
            return channel, path
    return None


def executable() -> Path:
    """The browser we start (see find_browser)."""
    found = find_browser()
    if found is None:
        raise BrowserUnavailable(no_browser_hint())
    return found[1]


class _Silent(BrowserUnavailable):
    """The browser did not answer a command in time."""


class _Refused(BrowserUnavailable):
    """The browser answered a command with an error."""


class _Cdp:
    """The DevTools protocol over one websocket, safe for two threads (a worker waiting for its page while the exit handler
    asks the browser to quit): a reader thread hands each answer to the call that asked, by command id, and the browser's
    events are skipped. Every waiting call fails when the socket closes. `session_id` addresses a page of a flattened
    session. (The errors below say "window", not "browser": djs.results reads that word as "none is installed", which none
    of them means.)"""

    def __init__(self, address: str):
        self._ids, self._connection = count(1), ExitStack()  # websockets wants its connection used as a context manager
        self._waiting: dict[int, queue.SimpleQueue] = {}     # command id -> where its answer goes (None: the socket closed)
        self._lock, self._sending, self._closed = threading.Lock(), threading.Lock(), False
        try:
            # no size limit: a cookie jar can pass the default 1 MiB; a browser that does not say goodbye back (it is
            # frozen) is let go after 2 s, not the 10 s that would eat into the deadline of _Browser.close
            self._ws = self._connection.enter_context(connect(address, max_size=None, open_timeout=10, close_timeout=2))
        except (OSError, WebSocketException) as e:
            raise BrowserUnavailable(f"Cannot talk to the browser at {address}") from e
        self._reader = threading.Thread(target=self._read, name="devtools-reader", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        try:
            for raw in self._ws:
                try:
                    reply = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(reply, dict):   # neither an answer nor an event
                    continue
                with self._lock:
                    waiter = self._waiting.pop(reply.get("id"), None)   # no waiter: an answer to a call that gave up
                if waiter is not None:
                    waiter.put(reply)
        except (ConnectionClosed, WebSocketException, OSError):
            pass
        finally:
            with self._lock:
                self._closed, waiters = True, list(self._waiting.values())
            for waiter in waiters:
                waiter.put(None)

    def call(self, method: str, timeout: float = 10, session_id: str | None = None, **params) -> dict:
        n, answer = next(self._ids), queue.SimpleQueue()
        message = {"id": n, "method": method, "params": params, **({"sessionId": session_id} if session_id else {})}
        with self._lock:
            if self._closed:
                raise BrowserGone("The window has quit")
            self._waiting[n] = answer
        try:
            try:
                with self._sending:
                    self._ws.send(json.dumps(message))
            except ConnectionClosed:
                raise BrowserGone("The window has quit") from None
            try:
                reply = answer.get(timeout=max(timeout, 0))
            except queue.Empty:
                raise _Silent(f"The window did not answer {method}") from None
        finally:
            with self._lock:
                self._waiting.pop(n, None)
        if reply is None:
            raise BrowserGone("The window has quit")
        if "error" in reply:
            raise _Refused(f"{method}: {reply['error'].get('message')}")
        return reply["result"]

    def close(self) -> None:
        self._connection.close()
        self._reader.join(1)   # it ends by itself once the socket is closed


def _expired(cookies: list[dict]) -> list[dict]:
    """The same cookies set again with an expiry long past, which is how a browser is told to drop them."""
    return [{"name": c["name"], "value": "", "domain": c["domain"], "path": c["path"], "secure": c["secure"],
             "httpOnly": c["httpOnly"], "expires": 1} for c in cookies]


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
        """Every cookie the browser holds: name, value, domain, path and expires (-1 for a session cookie)."""
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
        if dead := _expired([c for c in on_domain(self.cookies(), domain) if c["name"] in names]):
            self._ask("Storage.setCookies", cookies=dead)


def _own_group() -> dict:
    """Popen arguments that make the browser lead a group of its own, which is how it and everything it starts (renderers,
    GPU and utility processes) are reached at once: a session on POSIX (killpg), a process group on Windows (taskkill /T)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _group_alive(pgid: int) -> bool:
    """Some process of the group `pgid` is alive. A group only zombies are left in does not count (macOS answers
    PermissionError for it): they hold nothing. POSIX only: on Windows nothing tells once the browser itself has gone."""
    if sys.platform == "win32":
        return False
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _exited(proc: subprocess.Popen, seconds: float) -> bool:
    try:
        proc.wait(seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def _gone(proc: subprocess.Popen, seconds: float) -> bool:
    """Wait up to `seconds` until the browser has exited and every process it started has followed. The browser is reaped
    before its group is looked at: the id of a group that still has a member cannot be anyone else's."""
    until = time.monotonic() + seconds
    if not _exited(proc, seconds):
        return False
    while _group_alive(proc.pid):
        if time.monotonic() >= until:
            return False
        time.sleep(0.02)
    return True


def _signal_tree(proc: subprocess.Popen, kill: bool, timeout: float) -> None:
    """SIGTERM (kill: SIGKILL) to the browser and everything it started: the group it leads, which also holds the children
    left behind when the browser itself has quit. Windows knows no soft signal that reaches a browser without a window, so
    taskkill /T /F ends the tree both times (giving up on taskkill after `timeout`), and kill() the browser alone when
    taskkill cannot."""
    if sys.platform != "win32":
        with suppress(ProcessLookupError, PermissionError):  # nothing left in the group, or only zombies
            os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
        return
    try:
        done = subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0  # no console window flashing up
    except (OSError, subprocess.SubprocessError):  # no taskkill, or it hung
        done = False
    if not done and proc.poll() is None:
        proc.kill()


def _left(until: float) -> float:
    """Seconds left until the monotonic time `until`."""
    return max(until - time.monotonic(), 0)


def _within(deadline: float, seconds: float) -> float:
    """`seconds`, or what is left until `deadline` if that is less."""
    return min(seconds, _left(deadline))


_alive: set[_Browser] = set()  # the browsers of ours that run (or could not be closed); what is left at exit is closed there


class _Browser:
    """A browser process of ours with everything it starts, and its DevTools socket. Whatever happens, close() leaves no
    process behind, or says that it could not."""

    def __init__(self, command: list[str], profile: Path):
        self.cdp: _Cdp | None = None
        self.profile, self.ended = profile, False
        self._closing = threading.Lock()
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, **_own_group())
        except OSError as e:
            raise BrowserUnavailable(f"Cannot start the browser {command[0]}: {e}") from e
        _alive.add(self)

    def running(self) -> bool:
        """The browser, or a process it started, is alive. Never waits."""
        return not self.ended and (self.proc.poll() is None or _group_alive(self.proc.pid))

    def _quit(self, deadline: float) -> bool:
        """Ask the browser to quit over its socket and wait for it to leave, together QUIT_SECONDS. A browser that never
        listened has no socket and nobody to ask."""
        if self.cdp is None:
            return False
        until = time.monotonic() + _within(deadline, QUIT_SECONDS)
        with suppress(BrowserUnavailable):  # already gone is as good; one that does not answer is dealt with below
            self.cdp.call("Browser.close", timeout=_left(until))
        return _gone(self.proc, _left(until))

    def _end(self, deadline: float, kill: bool) -> bool:
        """Signal what is left of the browser's tree (SIGKILL when `kill`, else SIGTERM) and give it QUIT_SECONDS to go,
        signalling included."""
        until = time.monotonic() + _within(deadline, QUIT_SECONDS)
        if not _gone(self.proc, 0):
            _signal_tree(self.proc, kill, _left(until))
        return _gone(self.proc, _left(until))

    def close(self) -> bool:
        """Leave no process of this browser behind, in one deadline of three QUIT_SECONDS: ask it to quit (Browser.close),
        else terminate its whole tree, else kill it, waiting each time until the last process is reaped. True when it and
        everything it started are gone. False, said on stderr, when something outlived the deadline: the browser then
        stays in `_alive`, its profile counts as busy (see profile_lock) and the next close() tries again. Safe to call
        twice and from two threads (the owner and the exit handler)."""
        with self._closing:
            if self.ended:
                return True
            deadline = time.monotonic() + 3 * QUIT_SECONDS
            try:
                self.ended = self._quit(deadline) or self._end(deadline, kill=False)
            finally:  # also when interrupted above
                self.ended = self.ended or self._end(deadline, kill=True)
                self._settle()
            return self.ended

    def _settle(self) -> None:
        """After the last step: forget the browser when it is gone and say so when it is not. The socket is closed last,
        when a frozen browser's goodbye is the only thing left to wait for."""
        if self.ended:
            _alive.discard(self)
            with suppress(OSError):  # the port file names a browser that no longer exists
                (self.profile / "DevToolsActivePort").unlink(missing_ok=True)
        else:
            print(f"djs: the browser on {self.profile} (pid {self.proc.pid}) was still running {3 * QUIT_SECONDS:g} s after "
                  "it was asked to quit, terminated and killed; its profile stays busy until it is gone", file=sys.stderr)
        if self.cdp is not None:
            self.cdp.close()


def _stuck(profile: Path) -> bool:
    """A browser of ours that close() could not end still runs on `profile`. Those that have gone since are settled."""
    for leftover in [b for b in list(_alive) if b.profile == profile]:  # (a copy: other threads add and remove browsers)
        if leftover.running():
            return True
        leftover.close()  # nothing is left to wait for: this only records that it went
    return False


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
def _own_browser(provider_name: str, url: str, headless: bool, flags=()):
    """A browser process of ours on the shared profile, under the profile lock; yields its _Cdp and the id of its first page."""
    exe = executable()  # before the lock: with no browser there is nothing to wait for
    with profile_lock(provider_name):
        profile = db.profile_dir(provider_name, create=True) / "browser"
        _clear_leftovers(profile)
        started = _Browser([str(exe), f"--user-data-dir={profile}", "--remote-debugging-port=0", *WINDOW_FLAGS, *flags,
                            *(["--headless=new"] if headless else []), url], profile)
        try:
            deadline = time.monotonic() + START_SECONDS
            started.cdp = _Cdp(_listening(profile, started.proc, deadline))
            yield started.cdp, _first_page(started.cdp, deadline)
        finally:
            started.close()


@contextmanager
def _own_window(provider_name: str, url: str, headless: bool):
    """The window in a browser process of ours on the shared profile, under the profile lock."""
    with _own_browser(provider_name, url, headless) as (cdp, page):
        yield Window(cdp, page)


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

    Sites blank a page that a test driver controls, even one we never call, so this is an ordinary browser process with a
    debugging port of which only the browser-level socket is used: nothing is attached to the page, no script is
    injected. It runs on the profile that crawling shares (cookies set here are read there), under the profile lock, and
    the process, with everything it started, is gone when the block ends however it ends. (Should one outlive the deadline
    of _Browser.close, that is said on stderr and the profile stays busy, BrowserBusy, until it is gone: the block's own
    result is not thrown away for it.) With DJS_CDP_URL it is a new tab of the user's own browser instead, which is left
    alone apart from that tab. `headless` is for tests."""
    address = cdp_url()
    with (_users_window(address, url) if address else _own_window(provider_name, url, headless)) as window:
        yield window


def _response(req: httpx.Request, status: int, headers, text: str) -> httpx.Response:
    resp = httpx.Response(status, request=req, content=text.encode(),
                          headers=[(k, v) for k, v in headers if k.lower() not in DECODED_HEADERS])
    resp.encoding = "utf-8"  # the text arrives decoded, whatever charset the site declared
    return resp


def _script(js: str, arg=None) -> str:
    """The expression that calls the function `js` with `arg` (JSON-able) in the page."""
    return f"({js})({'undefined' if arg is None else json.dumps(arg)})"


def _sent_to(cookies: list[dict], url: str) -> list[dict]:
    """The cookies a browser sends to `url`: the cookie's domain is the host or a parent of it (a host-only cookie: the
    host itself) and its path a parent of the URL's."""
    u = urlsplit(url)
    host, path = u.hostname or "", u.path or "/"

    def sent(c: dict) -> bool:
        domain, base = c["domain"], c["path"]
        return bool(host) and (host == domain.lstrip(".") or (domain.startswith(".") and host.endswith(domain))) \
            and (path == base or path.startswith(base if base.endswith("/") else base + "/"))

    return [c for c in cookies if sent(c)]


class _TabGone(httpx.ConnectError):
    """The tab we work in is closed, or none was ever opened: looking again is pointless."""


class BrowserClient:
    """The part of httpx.Client providers use (get/post/request), run as in-page fetch() calls in a tab of ours, plus
    browser-only primitives. Everything that touches the network is paced per (platform, exit) like an http request. One
    thread uses a client; the browser's own exit may come from another (see _Cdp)."""

    def __init__(self, cdp: _Cdp, prov, exit_id: str, interval: float, jitter: float):
        self._cdp, self._pace = cdp, (prov.name, exit_id, interval, jitter)
        self._tab = self._session = None   # our one tab and the session that reaches it

    def _page(self, method: str, timeout: float, **params) -> dict:
        """A command to our tab. What the engine retries comes out as an httpx error: a tab that is gone (_TabGone), a
        context that a navigation destroyed (ConnectError), a tab that did not answer (ReadTimeout)."""
        if self._session is None:
            raise _TabGone("no page is open: goto() first")
        try:
            return self._cdp.call(method, timeout, session_id=self._session, **params)
        except _Silent as e:
            raise httpx.ReadTimeout(str(e)) from None
        except _Refused as e:
            text = str(e).lower()
            if any(word in text for word in GONE):
                raise _TabGone(str(e)) from None
            if any(word in text for word in LOST):
                raise httpx.ConnectError(str(e)) from None
            raise

    def _eval(self, expression: str, timeout: float = 10, wait: bool = False):
        """The value (as JSON) an expression has in our tab; `wait`: of the promise it returns. A script that threw is a
        ConnectError too."""
        out = self._page("Runtime.evaluate", timeout, expression=expression, returnByValue=True, awaitPromise=wait)
        if failure := out.get("exceptionDetails"):
            what = failure.get("exception", {}).get("description") or failure.get("text")
            raise httpx.ConnectError(f"The page's script failed: {what}")
        return out["result"].get("value")

    def request(self, method, url, *, params=None, headers=None, data=None, json=None, content=None, timeout=20,
                paced=True):
        """paced=False: the call belongs to a visit whose page load was already paced (see goto)."""
        # an httpx.Request encodes URL, params and body, so both transports send the same bytes
        req = httpx.Request(method, url if urlsplit(url).scheme else urljoin(self.url, url), params=params,
                            headers=headers, data=data, json=json, content=content)
        if paced:
            pacer.wait(*self._pace)
        arg = {"url": str(req.url), "method": method, "headers": dict(req.headers),
               "body": req.content.decode() or None, "timeout": timeout * 1000}
        try:
            out = self._eval(_script(FETCH_JS, arg), timeout + FETCH_MARGIN, wait=True)
        except httpx.HTTPError as e:
            e.request = req
            raise
        if not isinstance(out, dict):  # the page went before it said anything: never an empty response
            raise httpx.ConnectError("The page gave no answer", request=req)
        if "error" in out:
            raise (httpx.ReadTimeout if out["error"] == "timeout" else httpx.ConnectError)(out["error"], request=req)
        return _response(req, out["status"], out["headers"], out["text"])

    get = partialmethod(request, "GET")
    post = partialmethod(request, "POST")

    def goto(self, url: str, timeout: float = 30) -> None:
        """Load `url` in a new tab (ours of before is closed) and wait until its document is complete, in `timeout` seconds
        all told. A refused navigation, and a tab that ends on no page at all (blank, or the browser's error page), are
        ConnectErrors, never an empty success. Where it landed after redirects is for the caller to judge."""
        pacer.wait(*self._pace)
        deadline = time.monotonic() + timeout
        self.close()
        self._tab = self._cdp.call("Target.createTarget", url="about:blank")["targetId"]
        self._session = self._cdp.call("Target.attachToTarget", targetId=self._tab, flatten=True)["sessionId"]
        if refused := self._page("Page.navigate", _left(deadline), url=url).get("errorText"):
            raise httpx.ConnectError(f"{url}: {refused}")
        while True:
            try:
                href, state = self._eval(POLL_JS, _within(deadline, 10))
            except _TabGone:
                raise
            except httpx.ConnectError:  # a redirect destroyed the context under the look: look again
                href, state = None, None
            except httpx.ReadTimeout:  # the tab says nothing: that is the deadline, as far as it goes
                raise httpx.ReadTimeout(f"{url} did not finish loading in {timeout:g} s") from None
            if state == "complete":
                break
            if time.monotonic() >= deadline:
                raise httpx.ReadTimeout(f"{url} did not finish loading in {timeout:g} s")
            time.sleep(POLL_SECONDS)
        if not href or href.startswith(BLANK):
            raise httpx.ConnectError(f"{url} ended on an empty page ({href or 'no address'})")

    def wait_cookie(self, name: str, timeout: float = 10) -> None:
        """Block until the page's own scripts have set cookie `name` (anti-bot tokens are written by JS after load)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self._eval(_script(COOKIE_JS, name), _within(deadline, 10), wait=True):   # a page that hangs ends it too
                    return
            except _TabGone:
                raise
            except (httpx.ConnectError, httpx.ReadTimeout):  # the page navigated or did not answer: look again, if time is left
                pass
            if time.monotonic() >= deadline:
                raise httpx.ReadTimeout(f"Cookie {name} was not set within {timeout:g} s")
            time.sleep(POLL_SECONDS)

    def settle(self, ms: int) -> None:
        """Wait until the document is complete and the address has not changed for SETTLE_SECONDS (a page's own redirects
        are over then), at most `ms` milliseconds. Never raises: what is wrong shows with the caller's next call."""
        end = time.monotonic() + ms / 1000
        seen, since = None, time.monotonic()
        while (now := time.monotonic()) < end:
            try:
                href, state = self._eval(POLL_JS, _left(end))
            except _TabGone:
                return
            except httpx.ConnectError:
                href, state = None, None
            except Exception:  # the browser quit, the page does not answer
                return
            here = href if state == "complete" else None
            if here is None or here != seen:
                seen, since = here, now
            elif now - since >= SETTLE_SECONDS:
                return
            time.sleep(min(0.1, _left(end)))

    def eval(self, js: str, arg=None):
        """The function `js` called with `arg` in the page, its result (a promise is awaited) by value."""
        return self._eval(_script(js, arg), wait=True)

    def html(self) -> str:
        return self._eval(HTML_JS)

    @property
    def url(self) -> str:
        """Where the page is now ('' before the first goto)."""
        return self._eval(HREF_JS) if self._session else ""

    def cookies(self, url: str | None = None) -> list[dict]:
        """The cookies the browser sends to `url`, or without one every cookie it holds (the profile is the platform's own):
        HttpOnly ones too, read at the browser level where document.cookie cannot see those. No page is asked."""
        jar = self._cdp.call("Storage.getCookies")["cookies"]
        return jar if url is None else _sent_to(jar, url)

    def cookie_value(self, name: str, url: str | None = None) -> str | None:
        found = [c for c in self.cookies(url) if c["name"] == name]
        return max(found, key=lambda c: len(c["path"]))["value"] if found else None  # the most specific path first, as sent

    def clear_cookie(self, name: str) -> None:
        """Make the browser forget every cookie called `name`, on whatever domain (a token's name belongs to its site)."""
        if dead := _expired([c for c in self._cdp.call("Storage.getCookies")["cookies"] if c["name"] == name]):
            self._cdp.call("Storage.setCookies", cookies=dead)

    def close(self) -> None:
        """Close our tab; the browser and whatever else it has open are not ours to touch."""
        tab, self._tab, self._session = self._tab, None, None
        if tab:
            with suppress(BrowserUnavailable):  # the user may have closed it, or the browser quit
                self._cdp.call("Target.closeTarget", targetId=tab)


def attached_cookies() -> list[dict]:
    """Every cookie of the browser DJS_CDP_URL names, read at the browser level: no tab is opened."""
    address = cdp_url()
    if address is None:
        raise BrowserUnavailable("DJS_CDP_URL does not name a browser")
    with closing(_Cdp(_browser_socket(address))) as cdp:
        return cdp.call("Storage.getCookies")["cookies"]


@contextmanager
def open_browser(prov, exit_id: str, interval: float, jitter: float):
    """A paced BrowserClient: a browser of ours on the platform's profile (a proxy exit is its proxy, one with a login is
    refused; a window only where the platform needs one) or, with DJS_CDP_URL, the user's own, of which only our tab is
    touched and closed. Our browser is gone, with everything it started, when the block ends however it ends."""
    with ExitStack() as stack:
        if address := cdp_url():
            cdp = stack.enter_context(closing(_Cdp(_browser_socket(address))))
        else:
            cdp, _ = stack.enter_context(_own_browser(prov.name, "about:blank", _headless(prov),
                                                 [*CRAWL_FLAGS, *_proxy_flags(exit_id)]))
        client = BrowserClient(cdp, prov, exit_id, interval, jitter)
        stack.callback(client.close)  # before the browser or the socket goes
        yield client
