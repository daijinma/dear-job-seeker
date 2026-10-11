"""Login state for providers with auth='login' or 'optional'.

Two halves, deliberately separate:
  cookie store  - save/load/invalidate/logout/status (plain files, no browser)
  login flow    - login(): a headed browser page (djs.browser), the user scans the QR code themselves
Cookie VALUES never leave this module except via load_cookies() (engine -> httpx client);
they are never logged, and status() never returns them.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from djs import browser
from djs.db import profile_dir  # the cookie store and the browser profile live in the same private directory

LOGIN = {
    "boss": {
        "url": "https://www.zhipin.com/web/user/?ka=bticket",
        "domain": "zhipin.com",
        "auth_cookie_names": ("wt2", "zp_at"),
        "valid_days": 7,  # heuristic only; the server (code 37) has the final say
    },
}


# ---------------------------------------------------------------- cookie store

def _cookie_file(provider: str) -> Path:
    return profile_dir(provider) / "cookies.json"


def _read(provider: str) -> dict | None:
    try:
        return json.loads(_cookie_file(provider).read_text())
    except (OSError, ValueError):
        return None


def _write(provider: str, state: dict) -> None:
    """Atomic: temp file created 0600 in the 0700 profile dir, then renamed over."""
    d = profile_dir(provider, create=True)
    tmp = d / "cookies.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _cookie_file(provider))


def save_cookies(provider: str, cookies: dict[str, str]) -> None:
    _write(provider, {"cookies": cookies, "saved_at": time.time(), "invalid": False})


def _is_fresh(provider: str, state: dict) -> bool:
    max_age = LOGIN[provider]["valid_days"] * 86400
    return not state.get("invalid") and time.time() - state.get("saved_at", 0) < max_age


def load_cookies(provider: str) -> dict[str, str] | None:
    state = _read(provider)
    if state is None or not _is_fresh(provider, state):
        return None
    return state.get("cookies") or None


def invalidate(provider: str) -> None:
    """Server said the cookie is dead: keep the file, stop using it."""
    state = _read(provider)
    if state is not None:
        _write(provider, {**state, "invalid": True})


def logout(provider: str) -> None:
    shutil.rmtree(profile_dir(provider), ignore_errors=True)


def logged_in(provider: str) -> bool:
    """A usable session. With DJS_CDP_URL the user's own browser decides (its cookies); otherwise our saved cookies."""
    if not browser.cdp_url():
        return load_cookies(provider) is not None
    with browser.open_context(provider, headless=True) as ctx:
        return bool(_auth_cookies(ctx.cookies(), LOGIN[provider]))


def status(provider: str) -> dict:
    state = _read(provider)
    try:
        ok = logged_in(provider)
    except browser.BrowserUnavailable:  # the attached browser is not reachable right now: cannot tell
        ok = False
    return {"logged_in": ok, "saved_at": state.get("saved_at") if state else None}


# ------------------------------------------------------------------ login flow

def _auth_cookies(browser_cookies: list[dict], cfg: dict) -> dict[str, str]:
    """Site cookies, but only if an auth cookie is among them (else {})."""
    site = {c["name"]: c["value"] for c in browser_cookies if cfg["domain"] in c.get("domain", "")}
    return site if any(n in site for n in cfg["auth_cookie_names"]) else {}


def _wait_for_login(ctx, cfg: dict, timeout: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _auth_cookies(ctx.cookies(), cfg)
        if found:
            return found
        time.sleep(1)
    raise TimeoutError(f"login not completed within {timeout:.0f}s")


def login(provider: str, timeout: float = 180) -> dict:
    cfg = LOGIN[provider]
    with browser.open_page(provider, headless=False) as page:  # same profile and flags as crawling
        page.goto(cfg["url"])
        save_cookies(provider, _wait_for_login(page.context, cfg, timeout))
    return status(provider)
