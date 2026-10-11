"""What providers share about the client they are handed: djs.browser.BrowserClient (a real page) or httpx.Client."""
from __future__ import annotations

XHR_ACCEPT = "application/json, text/plain, */*"     # what the sites' own scripts send; a bare fetch() sends */*


def is_browser(client) -> bool:
    """BrowserClient can goto(); httpx.Client cannot (the same test Provider.warmup uses)."""
    return hasattr(client, "goto")
