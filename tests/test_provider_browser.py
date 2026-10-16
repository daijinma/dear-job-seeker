"""What providers share about the browser client (providers/_browser.py): the visit rule for the anti-bot token and the
one retry of an API call inside the visit. The client is a recording stand-in with the methods of the client API."""
import httpx
import pytest

from djs.providers import _browser
from djs.providers._browser import api_get, fresh_visit, is_browser
from djs.providers.base import Blocked

TOKEN = "__tok__"
URL = "https://site.test/page"


class Page:
    """Every call is logged. `script` is what the page's scripts have written by each read of the token after a load
    (None: nothing yet, the last step stays); `held` is what the profile holds before any load."""

    def __init__(self, held=None, script=()):
        self.jar = {TOKEN: held} if held else {}
        self.script, self.writes, self.calls, self.answers = list(script), [], [], []

    def clear_cookie(self, name):
        self.calls.append(("clear_cookie", name))
        self.jar.pop(name, None)

    def cookie_value(self, name, url=None):
        self.calls.append(("cookie_value", name))
        if self.writes:
            step = self.writes.pop(0)
            if step is not None:
                self.jar[name] = step
        return self.jar.get(name)

    def goto(self, url, timeout=30):
        self.calls.append(("goto", url))
        self.writes = list(self.script)

    def settle(self, ms):
        self.calls.append(("settle", ms))

    def get(self, url, paced=True, **kwargs):
        self.calls.append(("get", url, paced))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(_browser, "RENEW_SECONDS", 0.2)
    monkeypatch.setattr(_browser, "POLL_SECONDS", 0.01)


def test_a_browser_can_goto_and_an_httpx_client_cannot():
    assert is_browser(Page()) and not is_browser(httpx.Client())


def test_the_first_visit_clears_the_stale_token_then_loads_and_waits_for_a_value():
    page = Page(held="stale", script=["fresh"])
    fresh_visit(page, URL, TOKEN)
    assert page.calls == [("clear_cookie", TOKEN), ("cookie_value", TOKEN), ("goto", URL), ("settle", 8000),
                          ("cookie_value", TOKEN)]
    assert page.jar[TOKEN] == "fresh"


def test_a_later_visit_keeps_the_token_and_needs_a_value_that_differs_from_the_one_before_the_load():
    page = Page(script=["v1"])
    fresh_visit(page, URL, TOKEN)
    page.calls.clear()
    page.script = ["v1", "v1", "v2"]               # the page writes the same value first: that is no renewal
    fresh_visit(page, URL, TOKEN, settle_ms=500)
    assert page.calls == [("cookie_value", TOKEN), ("goto", URL), ("settle", 500), ("cookie_value", TOKEN),
                          ("cookie_value", TOKEN), ("cookie_value", TOKEN)]   # never cleared again
    assert page.jar[TOKEN] == "v2"


def test_a_token_that_is_not_renewed_blocks_the_visit():
    page = Page(script=["v1"])
    fresh_visit(page, URL, TOKEN)
    page.calls.clear()
    page.script = []                               # the page wrote nothing this time
    with pytest.raises(Blocked, match="token not renewed"):
        fresh_visit(page, URL, TOKEN)
    page.script = ["v1"]                           # or wrote the value it held
    with pytest.raises(Blocked, match="token not renewed"):
        fresh_visit(page, URL, TOKEN)
    assert ("clear_cookie", TOKEN) not in page.calls and page.calls.count(("goto", URL)) == 2


def test_a_token_that_never_appears_blocks_the_first_visit_too():
    with pytest.raises(Blocked, match="token not renewed"):
        fresh_visit(Page(held="stale"), URL, TOKEN)


def test_the_value_may_arrive_late():
    page = Page(script=[None, None, "late"])
    fresh_visit(page, URL, TOKEN)
    assert page.calls.count(("cookie_value", TOKEN)) == 4   # before the load, then three polls


def test_api_get_on_an_httpx_client_is_a_plain_get():
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": 1})))
    assert api_get(http, "https://site.test/api", params={"a": 1}).json() == {"ok": 1}


def test_api_get_inside_a_visit_is_not_paced_and_retries_once_after_the_page_settled():
    ok = httpx.Response(200, json={})
    page = Page()
    page.answers = [httpx.ConnectError("context destroyed"), ok]
    assert api_get(page, "https://site.test/api") is ok
    assert page.calls == [("get", "https://site.test/api", False), ("settle", 8000), ("get", "https://site.test/api", False)]


def test_api_get_gives_up_after_the_second_connect_error():
    page = Page()
    page.answers = [httpx.ConnectError("one"), httpx.ConnectError("two")]
    with pytest.raises(httpx.ConnectError, match="two"):
        api_get(page, "https://site.test/api")
