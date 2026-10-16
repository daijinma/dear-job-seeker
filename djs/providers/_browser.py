"""What providers share about the client they are handed: djs.browser.BrowserClient (a real page) or httpx.Client."""
from __future__ import annotations

import time
import weakref

import httpx

from .base import Blocked

XHR_ACCEPT = "application/json, text/plain, */*"     # what the sites' own scripts send; a bare fetch() sends */*
SETTLE_MS = 8000        # how long a page gets to finish its own redirects
RENEW_SECONDS = 10      # how long its scripts get to write a new token after the load
POLL_SECONDS = 0.25


def is_browser(client) -> bool:
    """BrowserClient can goto(); httpx.Client cannot (the same test Provider.warmup uses)."""
    return hasattr(client, "goto")


# clients whose stale anti-bot token was already dropped: only the first visit of a session needs it, and clearing it
# on every visit makes the site bounce each load through its security check (a navigation that kills the next call)
_fresh: "weakref.WeakSet" = weakref.WeakSet()


def fresh_visit(client, url: str, token: str, settle_ms: int = SETTLE_MS) -> None:
    """Start a visit: wait for the platform's pace, load `url`, let the page's own redirects finish and wait for its
    scripts to write a new `token`. The API call that follows belongs to the same visit (see api_get). A token that
    still holds the value it had before the load is not new: no call is made on a token of unknown age (Blocked)."""
    if client not in _fresh:
        client.clear_cookie(token)  # a token left in the profile would look present at once
        _fresh.add(client)
    before = client.cookie_value(token)
    client.goto(url)
    client.settle(settle_ms)
    deadline = time.monotonic() + RENEW_SECONDS
    while not (now := client.cookie_value(token)) or now == before:
        if time.monotonic() >= deadline:
            raise Blocked(f"{token}: token not renewed within {RENEW_SECONDS} s of loading {url}")
        time.sleep(POLL_SECONDS)


def api_get(client, url: str, **kwargs):
    """GET inside the visit fresh_visit started: no second pacing wait (the page may be gone by then). One retry when
    a late redirect of the page destroyed the call."""
    if not is_browser(client):
        return client.get(url, **kwargs)
    try:
        return client.get(url, paced=False, **kwargs)
    except httpx.ConnectError:
        client.settle(SETTLE_MS)
        return client.get(url, paced=False, **kwargs)
