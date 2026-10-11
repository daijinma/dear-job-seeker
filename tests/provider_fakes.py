"""Test doubles shared by the provider tests: an httpx client with a canned answer, and a stand-in for
djs.browser.BrowserClient (the requests of an httpx client under the same names, plus the page primitives)."""
from types import SimpleNamespace

import httpx


def client(body=None, status=200, text=None, content=None, seen=None, **kwargs) -> httpx.Client:
    """An httpx client whose every request is answered with the given JSON `body`, `text` or raw `content`."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, text=text) if text is not None else httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)


class FakeBrowser:
    """get/post go through a canned httpx client; goto, eval and wait_cookie only record what the provider asked for.
    `cookies` is what the page holds ({name: value}), `href` what location.href answers, `cookie_timeout` makes
    wait_cookie give up the way BrowserClient does (httpx.ReadTimeout)."""

    def __init__(self, body=None, status=200, text=None, seen=None, cookies=None, href="https://site.test/",
                 cookie_timeout=False):
        self.http = client(body, status, text, seen=seen)
        jar = [{"name": name, "value": value} for name, value in (cookies or {}).items()]
        self.page = SimpleNamespace(context=SimpleNamespace(cookies=lambda url: jar))
        self.calls, self.href, self.cookie_timeout = [], href, cookie_timeout

    def get(self, url, **kwargs):
        return self.http.get(url, **kwargs)

    def post(self, url, **kwargs):
        return self.http.post(url, **kwargs)

    def goto(self, url, timeout=30):
        self.calls.append(("goto", url))

    def eval(self, js, arg=None):
        self.calls.append(("eval", js))
        return self.href

    def wait_cookie(self, name, timeout=10):
        self.calls.append(("wait_cookie", name))
        if self.cookie_timeout:
            raise httpx.ReadTimeout("cookie never came")
