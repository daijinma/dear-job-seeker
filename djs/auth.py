"""A platform's login: the saved session and the browser window the user logs in through.

What counts as logged in is declared by each platform (djs.providers.base.Login); this module has no per-platform
code. Two halves:
  session store - save / load / invalidate / logout / status / check: plain files, plus one probe request at most
  login flow    - login() / cancel_login(): a headed window on the shared browser profile (djs.browser) in which the
                  user logs in themselves; the window closes by itself once the platform's own rule says it worked
Cookie VALUES leave this module only through load_cookies() (engine -> httpx client): they are never logged, put into
an error or returned by status().
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from djs import browser, providers
from djs.db import profile_dir  # the cookie store and the browser profile live in the same private directory
from djs.egress import UA
from djs.providers.base import Login

POLL_SECONDS = 1     # how often the login window is looked at
PROBE_SECONDS = 3    # least time between two probe requests while a login is being confirmed
PROBE_TIMEOUT = 15


class LoginCancelled(RuntimeError):
    """The user cancelled: the window was closed on purpose and nothing was saved."""


_pending: dict[str, threading.Event] = {}  # platform -> set() cancels the login that is waiting for the user


def _spec(provider: str) -> Login:
    spec = providers.get(provider).login
    if spec is None:
        raise ValueError(f"{provider} does not need login")
    return spec


# ---------------------------------------------------------------- cookie store

def _cookie_file(provider: str) -> Path:
    return profile_dir(provider) / "cookies.json"


def _read(provider: str) -> dict | None:
    try:
        state = json.loads(_cookie_file(provider).read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _write(provider: str, state: dict) -> None:
    """Atomic: temp file created 0600 in the 0700 profile dir, then renamed over."""
    d = profile_dir(provider, create=True)
    tmp = d / "cookies.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _cookie_file(provider))


def save_cookies(provider: str, cookies: dict[str, str], expires: dict[str, float] | None = None) -> None:
    """`expires`: unix time of each cookie that has one (a session cookie has none)."""
    _write(provider, {"cookies": cookies, "expires": expires or {}, "saved_at": time.time(), "invalid": False})


def _expires_at(spec: Login, state: dict) -> float:
    """When the saved session runs out: the cookies that carry the login say so themselves (the last of an 'any of'
    set, the first of an 'all of' set), else saved_at + valid_days. Other cookies of the site must not count: a
    long-lived analytics cookie would keep a dead session green."""
    cookies, expires = state.get("cookies", {}), state.get("expires", {})
    names, pick = (spec.cookies_any, max) if spec.cookies_any else (spec.cookies_all, min)
    have = [n for n in names if n in cookies]
    if have and all(n in expires for n in have):
        return pick(expires[n] for n in have)
    return state.get("saved_at", 0) + spec.valid_days * 86400


def _stored(provider: str, spec: Login) -> tuple[str, dict | None]:
    """'logged_in' | 'expired' | 'not_logged_in' judged from the cookie file alone, and what the file holds."""
    state = _read(provider)
    if state is None:
        return "not_logged_in", None
    dead = state.get("invalid") or _expires_at(spec, state) <= time.time()
    return ("expired" if dead else "logged_in"), state


def load_cookies(provider: str) -> dict[str, str] | None:
    """The saved cookies while the session is usable (what the engine puts on an http client), else None."""
    state, saved = _stored(provider, _spec(provider))
    return (saved.get("cookies") or None) if state == "logged_in" else None


def invalidate(provider: str) -> None:
    """The platform said the session is dead: keep the file (the pages show 已过期), stop using it."""
    state = _read(provider)
    if state is not None:
        _write(provider, {**state, "invalid": True})


def logout(provider: str) -> None:
    shutil.rmtree(profile_dir(provider), ignore_errors=True)


# ------------------------------------------------------------ what a session is

def _on_domain(browser_cookies: list[dict], domain: str) -> list[dict]:
    """The cookies of the platform's own domain (and its subdomains), nothing else the browser happens to hold."""
    def mine(c: dict) -> bool:
        d = c.get("domain", "").lstrip(".")
        return d == domain or d.endswith("." + domain)
    return [c for c in browser_cookies if mine(c)]


def _values(site: list[dict]) -> dict[str, str]:
    return {c["name"]: c["value"] for c in site}


def _dated(site: list[dict]) -> dict[str, float]:
    return {c["name"]: c["expires"] for c in site if c.get("expires", -1) > 0}


def _signed_in(spec: Login, cookies: dict[str, str], url: str) -> bool:
    """The platform's success rule; the first one it sets decides."""
    if spec.cookies_any:
        return any(n in cookies for n in spec.cookies_any)
    if spec.cookies_all:
        return all(n in cookies for n in spec.cookies_all)
    return bool(spec.url_prefix) and url.startswith(spec.url_prefix)


def _probe(spec: Login, cookies: dict[str, str]) -> bool:
    """The platform's own verdict on a session: a 2xx answer whose body lacks `probe_fail_text`. No probe configured:
    nothing to ask, the session counts. Raises httpx.HTTPError when the platform cannot be asked (a 429 or 5xx
    included: a site in trouble says nothing about our session)."""
    if not spec.probe_url:
        return True
    login_page = urlsplit(spec.url)
    r = httpx.get(spec.probe_url, cookies=cookies, timeout=PROBE_TIMEOUT, trust_env=False,
                  headers={"User-Agent": UA, "Referer": f"{login_page.scheme}://{login_page.netloc}/",
                           "X-Requested-With": "XMLHttpRequest"})
    if r.status_code == 429 or r.status_code >= 500:
        r.raise_for_status()
    return r.is_success and not (spec.probe_fail_text and spec.probe_fail_text in r.text)


def _by_attached_browser(spec: Login) -> bool:
    """With DJS_CDP_URL the login lives in the user's own browser, whose cookies say whether it is there."""
    return bool(browser.cdp_url() and (spec.cookies_any or spec.cookies_all))


def _attached(provider: str, spec: Login) -> bool:
    with browser.open_context(provider, headless=True) as ctx:
        return _signed_in(spec, _values(_on_domain(ctx.cookies(), spec.domain)), "")


def logged_in(provider: str) -> bool:
    """A usable session. Raises browser.BrowserUnavailable when the attached browser cannot be reached."""
    spec = _spec(provider)
    return _attached(provider, spec) if _by_attached_browser(spec) else _stored(provider, spec)[0] == "logged_in"


def status(provider: str) -> dict:
    """{logged_in, state, saved_at, expires_at, method, unlocks}; state is 'logged_in' | 'expired' (the platform or
    the clock ended the session) | 'not_logged_in'. Never contains cookie values."""
    spec = _spec(provider)
    state, saved = _stored(provider, spec)
    if _by_attached_browser(spec):
        try:
            attached = _attached(provider, spec)
        except browser.BrowserUnavailable:  # the user's browser is not reachable right now: cannot tell
            attached = False
        state = "logged_in" if attached else ("expired" if state == "expired" else "not_logged_in")
    saved = saved or {}
    return {"logged_in": state == "logged_in", "state": state, "saved_at": saved.get("saved_at"),
            "expires_at": _expires_at(spec, saved) if saved else None, "method": spec.method, "unlocks": spec.unlocks}


def check(provider: str) -> dict:
    """Ask the platform once whether the saved session still works; a dead one is marked expired. A platform that
    cannot be reached (httpx.HTTPError) changes nothing: not being able to ask is not an answer."""
    spec = _spec(provider)
    cookies = load_cookies(provider)
    if cookies and not _probe(spec, cookies):
        invalidate(provider)
    return status(provider)


# ------------------------------------------------------------------ login flow

def _confirmed(spec: Login, cookies: dict[str, str]) -> bool:
    try:
        return _probe(spec, cookies)
    except httpx.HTTPError:  # no answer yet: keep waiting rather than give up on a login that may be fine
        return False


def _wait_for_login(page, spec: Login, timeout: float, cancel: threading.Event) -> list[dict]:
    """Look at the window once a second until the platform's success rule holds (and its probe agrees). Returns the
    cookies of the platform's domain."""
    deadline = time.monotonic() + timeout
    probed_at = -PROBE_SECONDS
    while True:
        if page.is_closed():
            raise RuntimeError("登录窗口被关闭了，请重新登录")
        site = _on_domain(page.context.cookies(), spec.domain)
        if _signed_in(spec, _values(site), page.url) and time.monotonic() - probed_at >= PROBE_SECONDS:
            probed_at = time.monotonic()
            if _confirmed(spec, _values(site)):
                return site
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{timeout:.0f} 秒内没有登录成功，请再试一次")
        if cancel.wait(POLL_SECONDS):
            raise LoginCancelled("已取消登录")


def _forget_dead_session(ctx, spec: Login) -> None:
    """The profile may still hold the cookies of the session that died. Left there they would pass for a fresh login."""
    for name in (*spec.cookies_any, *spec.cookies_all):
        ctx.clear_cookies(name=name)


def login(provider: str, timeout: float = 180) -> dict:
    """Open the platform's login page in a window and wait up to `timeout` seconds for the user to log in. The cookies
    are saved with their expiry and the window closes by itself. Raises LoginCancelled (cancel_login), TimeoutError
    or RuntimeError (the window was closed); the messages are plain Chinese."""
    spec = _spec(provider)
    stale = _stored(provider, spec)[0] != "logged_in"
    cancel = _pending[provider] = threading.Event()
    try:
        with browser.open_page(provider, headless=False) as page:  # same profile and flags as crawling
            if stale and not browser.cdp_url():  # never touch the cookies of the user's own browser
                _forget_dead_session(page.context, spec)
            page.goto(spec.url, wait_until="domcontentloaded")
            site = _wait_for_login(page, spec, timeout, cancel)
    except browser.BrowserUnavailable as e:
        raise browser.BrowserUnavailable(f"打不开浏览器，没法登录。{e}") from e
    finally:
        if _pending.get(provider) is cancel:
            del _pending[provider]
    save_cookies(provider, _values(site), _dated(site))
    return status(provider)


def cancel_login(provider: str) -> bool:
    """Stop the login that is waiting for the user: its window closes. False when none is pending."""
    cancel = _pending.get(provider)
    if cancel is not None:
        cancel.set()
    return cancel is not None
