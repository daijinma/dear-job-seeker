"""What providers share about the client they are handed: djs.browser.BrowserClient (a real page) or httpx.Client."""
from __future__ import annotations

import weakref

import httpx

XHR_ACCEPT = "application/json, text/plain, */*"     # what the sites' own scripts send; a bare fetch() sends */*


def is_browser(client) -> bool:
    """BrowserClient can goto(); httpx.Client cannot (the same test Provider.warmup uses)."""
    return hasattr(client, "goto")


# clients whose stale anti-bot token was already dropped: only the first visit of a session needs it, and clearing it
# on every visit makes the site bounce each load through its security check (a navigation that kills the next call)
_fresh: "weakref.WeakSet" = weakref.WeakSet()


def fresh_visit(client, url: str, token: str, settle_ms: int = 8000) -> None:
    """Start a visit: wait for the platform's pace, load `url`, let the page's own redirects finish and wait for its
    scripts to write `token`. The API call that follows belongs to the same visit (see api_get)."""
    if client not in _fresh:
        client.page.context.clear_cookies(name=token)  # a token left in the profile would look present at once
        _fresh.add(client)
    client.goto(url)
    _settle(client, settle_ms)
    for _ in range(3):
        try:
            return client.wait_cookie(token, timeout=10)
        except httpx.ConnectError:   # the page navigated (security check bounce) while we looked: let it finish
            _settle(client, settle_ms)
        except httpx.ReadTimeout:    # no token: the API answer (37 -> Blocked) is the verdict
            return


def _settle(client, settle_ms: int) -> None:
    try:
        client.page.wait_for_load_state("networkidle", timeout=settle_ms)
    except Exception:  # a page that keeps polling never goes idle
        pass


def api_get(client, url: str, **kwargs):
    """GET inside the visit fresh_visit started: no second pacing wait (the page may be gone by then). One retry when
    a late redirect of the page destroyed the call."""
    if not is_browser(client):
        return client.get(url, **kwargs)
    try:
        return client.get(url, paced=False, **kwargs)
    except httpx.ConnectError:
        _settle(client, 8000)
        return client.get(url, paced=False, **kwargs)
