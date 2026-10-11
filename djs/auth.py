"""Login cookies for providers with auth='login'.

Two halves, deliberately separate:
  cookie store  - save/load/invalidate/logout/status (plain files, no browser)
  login flow    - login(): headful Playwright, user scans the QR code themselves
Cookie VALUES never leave this module except via load_cookies() (engine -> httpx client);
they are never logged, and status() never returns them.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from djs import db

LOGIN = {
    "boss": {
        "url": "https://www.zhipin.com/web/user/?ka=bticket",
        "domain": "zhipin.com",
        "auth_cookie_names": ("wt2", "zp_at"),
        "valid_days": 7,  # heuristic only; the server (code 37) has the final say
    },
}

# Hides the one automation tell Boss reads (wiki 29); without it the login page redirects away.
_STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
_INSTALL_HINT = "Playwright/Chromium missing. Run: pip install playwright && playwright install chromium"


# ---------------------------------------------------------------- cookie store

def profile_dir(provider: str) -> Path:
    return db.home() / "profiles" / provider


def _cookie_file(provider: str) -> Path:
    return profile_dir(provider) / "cookies.json"


def _read(provider: str) -> dict | None:
    try:
        return json.loads(_cookie_file(provider).read_text())
    except (OSError, ValueError):
        return None


def _write(provider: str, state: dict) -> None:
    """Atomic: temp file created 0600 in the 0700 profile dir, then renamed over."""
    d = profile_dir(provider)
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
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


def status(provider: str) -> dict:
    state = _read(provider)
    return {
        "logged_in": load_cookies(provider) is not None,
        "saved_at": state.get("saved_at") if state else None,
    }


# ------------------------------------------------------------------ login flow

def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(_INSTALL_HINT) from e
    return sync_playwright


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


def _open_context(pw, provider: str):
    try:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir(provider) / "browser"), headless=False, locale="zh-CN")
    except Exception as e:  # playwright raises its own Error for a missing browser binary
        raise RuntimeError(f"{_INSTALL_HINT} ({type(e).__name__})") from e
    ctx.add_init_script(_STEALTH_JS)
    return ctx


def login(provider: str, timeout: float = 180) -> dict:
    cfg = LOGIN[provider]
    sync_playwright = _sync_playwright()
    profile_dir(provider).mkdir(parents=True, exist_ok=True)
    profile_dir(provider).chmod(0o700)
    with sync_playwright() as pw:
        ctx = _open_context(pw, provider)
        try:
            (ctx.pages[0] if ctx.pages else ctx.new_page()).goto(cfg["url"])
            save_cookies(provider, _wait_for_login(ctx, cfg, timeout))
        finally:
            ctx.close()
    return status(provider)
