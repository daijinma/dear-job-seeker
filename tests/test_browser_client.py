"""The crawl client of djs.browser (BrowserClient, open_browser, attached_cookies) against the stand-in for the browser's
DevTools endpoint (tests/fake_devtools.py): the exact commands it sends (and the ones it never sends), how a tab that went
away, a page that never loads or an answer that never comes end, and the browser it starts or, with DJS_CDP_URL, the
user's own. No real browser; test_browser_client_real.py has one."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

import fake_devtools
from djs import browser, db, pacer, results
from djs.providers.base import Blocked, check_http, get_json
from fake_devtools import HOLD, DevTools

process = pytest.mark.skipif(sys.platform == "win32", reason="the fake browser is a script")
PROBE = SimpleNamespace(name="probe", needs_headed=False)
URL = "https://site.test/jobs"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path / "home"))
    for var in ("DJS_CDP_URL", "FAKE_BROWSER", "DJS_BROWSER_HEADLESS"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "home"


@pytest.fixture
def dev():
    fake = DevTools("about:blank", noise=True)
    yield fake
    fake.quit()


class Page:
    """The tab behind the fake endpoint, as the client sees it: `href` and `state` (what location.href and
    document.readyState answer, or `polls`, a list of (href, state) handed out one per look, the last one for good),
    `fetch` (what an in-page fetch answers: a dict, an Exception for an error answer, HOLD for none), the fetches seen, and
    a function for anything else a test registers in `functions` (by its text) or `bare` (an expression)."""

    def __init__(self, dev: DevTools):
        self.dev, self.href, self.state, self.polls, self.errors = dev, URL, "complete", [], {}
        self.fetch, self.fetches, self.html, self.looks = {"status": 200, "headers": [], "text": "{}"}, [], "<html></html>", 0
        self.functions = {browser.FETCH_JS: self._fetch}
        self.bare = {browser.POLL_JS: self._poll, browser.HREF_JS: lambda: self.href, browser.HTML_JS: lambda: self.html}
        dev.handlers.update({"Page.navigate": self._navigate, "Runtime.evaluate": self._evaluate})

    def _navigate(self, params, session):
        if params["url"] in self.errors:
            return {"frameId": "frame", "errorText": self.errors[params["url"]]}
        self.href = self.dev.pages[self.dev.attached[session]] = params["url"]
        return {"frameId": "frame", "loaderId": "loader"}

    def _poll(self):
        self.looks += 1
        pair = self.polls.pop(0) if len(self.polls) > 1 else (self.polls[0] if self.polls else (self.href, self.state))
        if isinstance(pair, Exception):   # e.g. the context a redirect destroyed
            raise pair
        return list(pair)

    def _fetch(self, arg):
        self.fetches.append(arg)
        return self.fetch

    def _evaluate(self, params, session):
        expression = params["expression"]
        if expression in self.bare:
            value = self.bare[expression]()
        else:
            js = next((js for js in self.functions if expression.startswith(f"({js})(")), None)
            assert js, f"nobody answers {expression[:80]!r}"
            arg = expression[len(js) + 3:-1]
            value = self.functions[js](None if arg == "undefined" else json.loads(arg))
        if value is HOLD:
            return HOLD
        if isinstance(value, Throws):
            return {"result": {"type": "object", "subtype": "error", "description": str(value)},
                    "exceptionDetails": {"text": "Uncaught", "exception": {"description": str(value)}}}
        if isinstance(value, Exception):
            raise value
        return {"result": {"type": "undefined"} if value is None else {"type": "object", "value": value}}


class Throws(Exception):
    """The page's script threw: an exceptionDetails answer, not an error answer."""


def looks(dev):
    """The Runtime.evaluate messages so far."""
    return [m for m in dev.messages if m["method"] == "Runtime.evaluate"]


@pytest.fixture
def site(dev):
    cdp = browser._Cdp(dev.address)
    page = Page(dev)
    yield SimpleNamespace(dev=dev, page=page, client=browser.BrowserClient(cdp, PROBE, "direct", 0.0, 0.0), cdp=cdp)
    cdp.close()


@pytest.fixture
def waits(monkeypatch):
    """Every pacing call, without waiting."""
    seen = []
    monkeypatch.setattr(pacer, "wait", lambda *args: seen.append(args))
    return seen


def mistake(error):
    """A page-level error answer, as the browser words it."""
    return ValueError(error)


# ============================================================ goto
def test_goto_makes_a_tab_attaches_to_it_and_navigates_without_the_domains_detection_reads(site):
    site.client.goto(URL)
    dev, methods = site.dev, site.dev.methods()
    assert methods[:3] == ["Target.createTarget", "Target.attachToTarget", "Page.navigate"]
    assert set(methods[3:]) == {"Runtime.evaluate"}
    assert dev.calls[:3] == [("Target.createTarget", {"url": "about:blank"}),
                             ("Target.attachToTarget", {"targetId": "page-2", "flatten": True}),
                             ("Page.navigate", {"url": URL})]
    assert [m.get("sessionId") for m in dev.messages[:3]] == [None, None, "session-1"]   # the page is reached by its session
    assert {m["sessionId"] for m in dev.messages[2:]} == {"session-1"}
    assert all(m["params"]["returnByValue"] is True for m in looks(dev))
    for method in methods:   # what a site's detection script can see: none of these is ever switched on or added
        assert not method.endswith(".enable") and "addScript" not in method and "Binding" not in method and "Fetch." not in method


def test_the_next_goto_closes_the_tab_of_the_one_before(site):
    site.client.goto(URL)
    site.client.goto(URL + "?2")
    assert [p for m, p in site.dev.calls if m == "Target.closeTarget"] == [{"targetId": "page-2"}]
    assert list(site.dev.pages) == ["page-1", "page-3"]   # the browser's own first page stays, ours is the newest only
    site.dev.pages.pop("page-3")   # the user closed it
    site.client.goto(URL)   # closing what is gone is no failure
    assert list(site.dev.pages) == ["page-1", "page-4"]


def test_goto_waits_for_its_turn_like_an_http_request(site, waits, monkeypatch):
    monkeypatch.setattr(site.client, "_pace", ("boss", "http://p:1", 4.0, 2.0))
    site.client.goto(URL)
    assert waits == [("boss", "http://p:1", 4.0, 2.0)]


def test_a_navigation_the_browser_refuses_is_a_connection_error(site):
    site.page.errors[URL] = "net::ERR_CONNECTION_REFUSED"
    with pytest.raises(httpx.ConnectError, match="ERR_CONNECTION_REFUSED"):
        site.client.goto(URL)
    assert "Runtime.evaluate" not in site.dev.methods()   # nothing to look at: it failed
    assert len(site.dev.pages) == 2   # the tab stays until the next goto or the end of the session closes it


@pytest.mark.parametrize("href", ["about:blank", "", "about:srcdoc", "chrome-error://chromewebdata/"])
def test_a_page_that_ends_blank_is_never_an_empty_success(site, href):
    site.page.polls = [(href, "complete")]
    with pytest.raises(httpx.ConnectError, match="empty page"):
        site.client.goto(URL)


def test_another_host_is_a_landing_the_provider_judges_not_an_error(site):
    site.page.polls = [("https://passport.other.test/check", "complete")]
    site.client.goto(URL)
    assert site.page.looks == 1


def test_a_page_that_never_completes_times_out(site):
    site.page.state = "interactive"
    begun = time.monotonic()
    with pytest.raises(httpx.ReadTimeout, match="did not finish loading"):
        site.client.goto(URL, timeout=0.4)
    assert 0.3 < time.monotonic() - begun < 5 and site.page.looks > 2


def test_goto_looks_again_until_the_document_is_complete(site):
    site.page.polls = [(URL, "loading"), (URL, "interactive"), (URL, "complete")]
    site.client.goto(URL)
    assert site.page.looks == 3


def test_a_context_destroyed_by_a_redirect_while_looking_is_only_one_more_look(site):
    site.page.polls = [mistake("Execution context was destroyed."), mistake("Cannot find context with specified id"),
                       mistake("Inspected target navigated or closed"), ("https://site.test/after", "complete")]
    site.client.goto(URL)
    assert site.page.looks == 4


def test_a_tab_that_is_gone_ends_goto_at_once(site):
    site.page.polls = [("https://site.test/", "loading")]
    site.dev.handlers["Page.navigate"] = lambda params, session: site.dev.pages.pop("page-2") and {"frameId": "f"}
    begun = time.monotonic()
    with pytest.raises(httpx.ConnectError):
        site.client.goto(URL, timeout=30)
    assert time.monotonic() - begun < 5


def test_a_browser_that_quits_ends_goto_with_browser_gone(site):
    site.dev.handlers["Page.navigate"] = lambda params, session: HOLD
    threading.Timer(0.3, site.dev.quit).start()
    with pytest.raises(browser.BrowserGone):
        site.client.goto(URL)


def test_a_browser_that_never_answers_page_navigate_times_out(site):
    site.dev.handlers["Page.navigate"] = lambda params, session: HOLD
    with pytest.raises(httpx.ReadTimeout):
        site.client.goto(URL, timeout=0.3)


# ============================================================ request: the bytes, the answer, the failures
def client_of(site):
    site.client.goto(URL)
    return site.client


def test_a_fetch_answer_becomes_a_real_httpx_response(site, waits):
    site.page.fetch = {"status": 200, "text": '{"code": 0, "name": "岗位"}', "headers": [
        ["content-encoding", "gzip"], ["content-length", "3"], ["transfer-encoding", "chunked"],
        ["content-type", "application/json; charset=gbk"], ["x-id", "7"]]}
    r = client_of(site).get("/api/list", params={"page": 2})
    assert isinstance(r, httpx.Response) and r.status_code == 200
    assert get_json(r) == {"code": 0, "name": "岗位"} and "岗位" in r.text   # a gbk label must not garble the text
    assert r.headers["x-id"] == "7" and "content-encoding" not in r.headers and "transfer-encoding" not in r.headers
    assert r.headers["content-length"] == str(len(r.content))   # the decoded body's length, not the wire value
    assert str(r.request.url) == "https://site.test/api/list?page=2"   # a relative URL is resolved against the page


def test_status_codes_reach_the_providers_own_helpers(site, waits):
    site.page.fetch = {"status": 403, "text": "", "headers": [["retry-after", "30"]]}
    with pytest.raises(Blocked) as e:
        check_http(client_of(site).get("/x"))
    assert e.value.retry_after == 30.0
    site.page.fetch = {"status": 404, "text": "", "headers": []}
    with pytest.raises(httpx.HTTPStatusError):
        site.client.get("/x").raise_for_status()


CALLS = [("GET", "/api", {"params": {"q": "py", "page": 2}}),
         ("GET", "/api?x=1", {}),
         ("GET", "/api?x=1", {"params": {"q": "py"}}),
         ("GET", "https://other.test/a b", {"headers": {"X-Requested-With": "XMLHttpRequest"}}),
         ("POST", "/form", {"data": {"query": "算法", "page": 1}}),
         ("POST", "/json", {"json": {"a": [1, 2], "b": "岗位"}}),
         ("PUT", "/raw", {"content": "plain text"})]


@pytest.mark.parametrize("method, url, kwargs", CALLS)
def test_sends_what_an_httpx_client_would_send(site, waits, method, url, kwargs):
    seen = []
    http = httpx.Client(base_url="https://site.test", transport=httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200)))
    http.request(method, url, **kwargs)
    client_of(site).request(method, url, **kwargs)
    want, got = seen[0], site.page.fetches[0]
    assert (got["method"], got["url"]) == (method, str(want.url))
    assert (got["body"] or "").encode() == want.content
    for name in ("content-type", "x-requested-with"):   # the headers a page may set; the rest is the browser's own
        assert got["headers"].get(name) == want.headers.get(name)


def test_the_fetch_runs_in_the_page_and_waits_for_its_promise(site, waits):
    client_of(site).get("/a")
    call = looks(site.dev)[-1]["params"]
    assert call["awaitPromise"] is True and call["returnByValue"] is True and call["expression"].startswith(f"({browser.FETCH_JS})(")


def test_the_timeout_is_passed_to_the_page_in_milliseconds(site, waits):
    client_of(site).get("/a", timeout=5)
    site.client.get("/b")
    assert [sent["timeout"] for sent in site.page.fetches] == [5000, 20000]


def test_a_relative_url_asks_the_page_for_its_address_an_absolute_one_does_not(site, waits):
    site.page.href = "https://site.test/home"
    client_of(site)
    before = len(looks(site.dev))
    site.client.get("https://other.test/x")
    assert len(looks(site.dev)) == before + 1   # the fetch alone
    site.client.get("/y")
    assert len(looks(site.dev)) == before + 3   # location.href, then the fetch
    assert [f["url"] for f in site.page.fetches] == ["https://other.test/x", "https://site.test/y"]


def test_get_post_and_goto_are_paced_like_http_and_a_visit_that_was_paced_is_not_paced_again(site, waits, monkeypatch):
    monkeypatch.setattr(site.client, "_pace", ("boss", "http://p:1", 4.0, 2.0))
    site.page.functions["() => 1"] = lambda arg: 1
    c = site.client
    c.goto(URL)
    c.get("/a")
    c.post("/b")
    c.get("/c", paced=False)
    c.html(), c.url, c.cookies(), c.cookie_value("x"), c.eval("() => 1")
    assert waits == [("boss", "http://p:1", 4.0, 2.0)] * 3   # goto, get, post: everything else stayed off the network


@pytest.mark.parametrize("fetch, expected", [
    ({"error": "timeout"}, httpx.ReadTimeout),
    ({"error": "TypeError: Failed to fetch"}, httpx.ConnectError),
    (mistake("Execution context was destroyed."), httpx.ConnectError),
    (mistake("Cannot find context with specified id"), httpx.ConnectError),
    (mistake("Inspected target navigated or closed"), httpx.ConnectError),
    (mistake("Promise was collected"), httpx.ConnectError),
    (mistake("Target closed."), httpx.ConnectError),
    (mistake("Session with given id not found."), httpx.ConnectError),
    (Throws("ReferenceError: x is not defined"), httpx.ConnectError),
    (None, httpx.ConnectError)])   # nothing came back at all: never an empty response
def test_in_page_failures_become_the_httpx_errors_the_engine_retries(site, waits, fetch, expected):
    site.page.fetch = fetch
    with pytest.raises(httpx.HTTPError) as e:
        client_of(site).get("/api")
    assert isinstance(e.value, expected) and isinstance(e.value, httpx.ReadTimeout) == (expected is httpx.ReadTimeout)
    assert str(e.value.request.url) == "https://site.test/api"


def test_a_tab_the_user_closed_during_the_session_is_a_connection_error(site, waits):
    client_of(site)
    site.dev.pages.pop("page-2")
    with pytest.raises(httpx.ConnectError):
        site.client.get("/api")


def test_a_fetch_the_browser_never_answers_is_a_timeout(site, waits, monkeypatch):
    monkeypatch.setattr(browser, "FETCH_MARGIN", 0.2)
    site.page.fetch = HOLD
    begun = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        client_of(site).get("/api", timeout=0.2)
    assert 0.3 < time.monotonic() - begun < 5


def test_a_browser_that_quits_during_a_fetch_is_browser_gone_not_a_network_error(site, waits):
    site.page.fetch = HOLD
    client_of(site)
    threading.Timer(0.3, site.dev.quit).start()
    with pytest.raises(browser.BrowserGone) as e:
        site.client.get("/api", timeout=30)
    assert not isinstance(e.value, httpx.HTTPError)


def test_a_request_before_any_page_was_loaded_fails_instead_of_asking_nothing(site, waits):
    assert site.client.url == ""
    with pytest.raises(httpx.ConnectError, match="no page"):
        site.client.get("https://site.test/api")
    assert site.page.fetches == [] and "Runtime.evaluate" not in site.dev.methods()


def test_a_waiting_fetch_and_a_close_from_another_thread_share_the_connection(site, waits):
    """The worker waits for its fetch while the exit handler (or _Browser.close) asks the browser to quit."""
    site.page.fetch = HOLD
    client_of(site)
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(_attempt(lambda: site.client.get("/api", timeout=30))))
    worker.start()
    deadline = time.monotonic() + 5
    while not site.dev.held and time.monotonic() < deadline:
        time.sleep(0.01)
    assert site.dev.held   # the fetch is under way
    site.cdp.call("Browser.close")   # the other thread, on the same connection: it gets its answer at once
    worker.join(10)
    assert not worker.is_alive() and isinstance(outcome[0], browser.BrowserGone) and site.dev.gone.is_set()


def _attempt(fn):
    try:
        return fn()
    except BaseException as e:   # the thread reports what it raised
        return e


# ============================================================ eval, html, url, settle
def test_eval_calls_the_function_with_the_argument_and_returns_the_value(site):
    site.page.functions["n => n * 2"] = lambda n: n * 2
    site.page.functions["() => ({a: [1, 2]})"] = lambda arg: {"a": [1, 2]}
    c = client_of(site)
    assert c.eval("n => n * 2", 21) == 42 and c.eval("() => ({a: [1, 2]})") == {"a": [1, 2]}
    call = looks(site.dev)[-1]["params"]
    assert call["awaitPromise"] is True and call["returnByValue"] is True and call["expression"] == "(() => ({a: [1, 2]}))(undefined)"


def test_eval_returns_none_for_no_value_and_a_connection_error_for_a_script_that_threw(site):
    site.page.functions["() => {}"] = lambda arg: None
    site.page.functions["() => boom"] = lambda arg: Throws("ReferenceError: boom is not defined")
    c = client_of(site)
    assert c.eval("() => {}") is None
    with pytest.raises(httpx.ConnectError, match="boom is not defined"):
        c.eval("() => boom")


def test_html_and_url_are_what_the_page_says(site):
    site.page.html = "<html><body>你好</body></html>"
    c = client_of(site)
    assert c.html() == "<html><body>你好</body></html>" and c.url == URL


def test_settle_returns_once_the_page_stood_still_for_the_settle_time(site, monkeypatch):
    monkeypatch.setattr(browser, "SETTLE_SECONDS", 0.3)
    c = client_of(site)
    begun = time.monotonic()
    c.settle(5000)
    assert 0.25 < time.monotonic() - begun < 2


def test_settle_gives_up_after_its_limit_on_a_page_that_keeps_moving(site):
    counter = iter(range(10 ** 6))
    site.page.bare[browser.POLL_JS] = lambda: [f"https://site.test/{next(counter)}", "complete"]
    c = client_of(site)
    begun = time.monotonic()
    c.settle(400)
    assert 0.35 < time.monotonic() - begun < 3


def test_settle_waits_for_a_document_that_is_still_loading(site, monkeypatch):
    monkeypatch.setattr(browser, "SETTLE_SECONDS", 0.2)
    c = client_of(site)
    site.page.polls = [(URL, "loading")] * 4 + [(URL, "complete")]
    begun = time.monotonic()
    c.settle(5000)
    assert time.monotonic() - begun > 0.4 and site.page.looks >= 8


@pytest.mark.parametrize("trouble", ["context", "tab", "browser", "silent", "script"])
def test_settle_never_raises(site, monkeypatch, trouble):
    monkeypatch.setattr(browser, "SETTLE_SECONDS", 0.2)
    c = client_of(site)
    if trouble == "context":
        site.page.polls = [mistake("Execution context was destroyed.")]
    elif trouble == "tab":
        site.dev.pages.pop("page-2")
    elif trouble == "browser":
        site.dev.quit()
        time.sleep(0.2)
    elif trouble == "silent":
        site.dev.handlers["Runtime.evaluate"] = lambda params, session: HOLD
    elif trouble == "script":
        site.page.polls = [Throws("TypeError: x")]
    begun = time.monotonic()
    c.settle(600)
    assert time.monotonic() - begun < 3


# ============================================================ cookies stay at the browser level
def cookie(name, value="v", domain=".site.test", path="/", **more):
    return {"name": name, "value": value, "domain": domain, "path": path, "expires": -1, "secure": False, "httpOnly": False, **more}


def test_cookies_are_those_that_go_with_the_url(site):
    site.dev.cookies = [cookie("a", "1"), cookie("b", "2", "www.site.test"), cookie("c", "3", "api.site.test"),
                        cookie("d", "4", ".other.test"), cookie("e", "5", "notsite.test"), cookie("f", "6", path="/admin"),
                        cookie("g", "7", "site.test")]   # a host-only cookie of the bare domain
    c = site.client
    names = lambda url=None: sorted(x["name"] for x in c.cookies(url))   # noqa: E731
    assert names("https://www.site.test/jobs") == ["a", "b"]
    assert names("https://site.test/") == ["a", "g"]
    assert names("https://api.site.test/admin/x") == ["a", "c", "f"]
    assert names("https://nowhere.test/") == []
    assert names() == ["a", "b", "c", "d", "e", "f", "g"]   # no url: all the browser holds
    assert "Target.createTarget" not in site.dev.methods()   # nothing of this touched a page


def test_cookies_without_a_url_are_all_the_browser_holds_and_no_page_is_asked(site):
    site.dev.cookies = [cookie("a", "1"), cookie("d", "4", ".other.test")]
    c = client_of(site)
    site.dev.pages.pop("page-2")   # the tab is gone, which makes no difference to a cookie
    before = len(site.dev.calls)
    assert [x["name"] for x in c.cookies()] == ["a", "d"] and c.cookie_value("d") == "4" and c.cookie_value("none") is None
    assert site.dev.methods()[before:] == ["Storage.getCookies"] * 3


def test_cookie_value_is_the_most_specific_one_or_none(site):
    site.dev.cookies = [cookie("tok", "outer"), cookie("tok", "inner", path="/jobs"), cookie("other", "x")]
    c = site.client
    assert c.cookie_value("tok", URL) == "inner" and c.cookie_value("tok", "https://site.test/") == "outer"
    assert c.cookie_value("none", URL) is None and c.cookie_value("tok", "https://nowhere.test/") is None


def test_clear_cookie_expires_that_name_everywhere_and_nothing_else(site):
    site.dev.cookies = [cookie("tok", "a", ".site.test", secure=True, httpOnly=True), cookie("tok", "b", "www.site.test", "/x"),
                        cookie("tok", "c", ".other.test"), cookie("keep", "d")]
    site.client.clear_cookie("tok")
    assert [c["name"] for c in site.dev.cookies] == ["keep"]
    [sent] = [params["cookies"] for method, params in site.dev.calls if method == "Storage.setCookies"]
    assert all(c["value"] == "" and c["expires"] < time.time() for c in sent)   # an expiry long past is how a browser drops one
    assert {(c["domain"], c["path"], c["secure"], c["httpOnly"]) for c in sent} == {
        (".site.test", "/", True, True), ("www.site.test", "/x", False, False), (".other.test", "/", False, False)}
    site.dev.calls.clear()
    site.client.clear_cookie("tok")
    assert site.dev.methods() == ["Storage.getCookies"]   # nothing left: nothing is sent


# ============================================================ wait_cookie
def test_wait_cookie_polls_until_the_page_has_set_it(site):
    answers = iter([False, False, False, True])
    site.page.functions[browser.COOKIE_JS] = lambda name: next(answers) if name == "tok" else pytest.fail(name)
    c = client_of(site)
    c.wait_cookie("tok", timeout=5)
    assert sum(1 for m in looks(site.dev) if m["params"]["expression"].startswith(f"({browser.COOKIE_JS})(")) == 4


def test_wait_cookie_times_out(site):
    site.page.functions[browser.COOKIE_JS] = lambda name: False
    c = client_of(site)
    begun = time.monotonic()
    with pytest.raises(httpx.ReadTimeout, match="never"):
        c.wait_cookie("never", timeout=0.4)
    assert 0.3 < time.monotonic() - begun < 5


def test_wait_cookie_on_a_page_that_never_answers_ends_at_its_own_timeout(site):
    c = client_of(site)
    site.dev.handlers["Runtime.evaluate"] = lambda params, session: HOLD   # a page whose script never returns
    begun = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        c.wait_cookie("tok", timeout=0.4)
    assert time.monotonic() - begun < 3   # not the 10 s one look may take


def test_wait_cookie_looks_again_when_a_redirect_destroys_the_context(site):
    answers = iter([mistake("Execution context was destroyed."), mistake("Cannot find context with specified id"), True])
    site.page.functions[browser.COOKIE_JS] = lambda name: next(answers)
    client_of(site).wait_cookie("tok", timeout=5)


def test_wait_cookie_on_a_tab_that_is_gone_fails_at_once(site):
    site.page.functions[browser.COOKIE_JS] = lambda name: False
    c = client_of(site)
    site.dev.pages.pop("page-2")
    begun = time.monotonic()
    with pytest.raises(httpx.ConnectError):
        c.wait_cookie("tok", timeout=30)
    assert time.monotonic() - begun < 5


# ============================================================ what the user is told, and what is installed
def test_ready_means_a_browser_to_start_or_the_users_own(monkeypatch):
    monkeypatch.setattr(browser, "find_browser", lambda: None)
    assert browser.ready() is False
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:9222")
    assert browser.ready() is True   # nothing needs installing: the browser is the user's
    monkeypatch.delenv("DJS_CDP_URL")
    monkeypatch.setattr(browser, "find_browser", lambda: ("msedge", "/x/msedge"))
    assert browser.ready() is True


def test_the_hint_is_one_sentence_naming_chrome_and_edge_and_nothing_to_run():
    hint = browser.no_browser_hint()
    assert "Google Chrome" in hint and "Microsoft Edge" in hint and hint.count(".") == 1
    assert not any(word in hint.lower() for word in ("playwright", "pip", "install-browser", "download", "several hundred"))
    assert results.classify(hint)[0] == "browser"   # the results page turns it into "install a browser"


def test_the_cookies_of_the_users_browser_are_read_at_the_browser_level_and_no_tab_is_opened(dev, monkeypatch):
    dev.cookies = [cookie("wt2", "SECRET", ".zhipin.com")]
    monkeypatch.setenv("DJS_CDP_URL", dev.http_url)
    assert browser.attached_cookies() == dev.cookies
    assert dev.methods() == ["Storage.getCookies"] and not dev.gone.is_set()


def test_attached_cookies_say_how_to_start_the_users_browser_when_it_cannot_be_reached(monkeypatch):
    with pytest.raises(browser.BrowserUnavailable, match="DJS_CDP_URL"):
        browser.attached_cookies()   # nothing names a browser
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:1")
    with pytest.raises(browser.BrowserUnavailable, match=r"remote-debugging-port=9222.*DJS_CDP_URL") as e:
        browser.attached_cookies()
    assert "http://127.0.0.1:1" in str(e.value)


# ============================================================ the user's own browser (DJS_CDP_URL)
def test_in_the_users_browser_the_client_makes_and_closes_only_its_own_tabs(dev, monkeypatch, home):
    Page(dev)
    monkeypatch.setenv("DJS_CDP_URL", dev.http_url)
    dev.pages["page-1"] = "https://users.test/mail"
    with browser.open_browser(PROBE, "http://u:p@proxy.test:1", 0.0, 0.0) as c:   # the exit is not ours to apply there
        c.goto(URL)
        c.goto(URL + "?2")
        assert dev.pages == {"page-1": "https://users.test/mail", "page-3": URL + "?2"}   # theirs stays, ours is one tab
        assert not (home / "profiles").exists()   # no profile of ours, so no lock either
    assert dev.pages == {"page-1": "https://users.test/mail"} and not dev.gone.is_set()
    assert "Browser.close" not in dev.methods()
    assert [p["targetId"] for m, p in dev.calls if m == "Target.closeTarget"] == ["page-2", "page-3"]


def test_in_the_users_browser_no_process_is_started_or_signalled(dev, monkeypatch):
    Page(dev)
    monkeypatch.setenv("DJS_CDP_URL", dev.http_url)
    refuse = lambda *a, **k: pytest.fail("nothing is started or signalled here")   # noqa: E731
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(browser, "_Browser", refuse)
    monkeypatch.setattr(browser, "_signal_tree", refuse)
    with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:
        c.goto(URL)
    assert not browser._alive


def test_in_the_users_browser_a_tab_is_closed_when_the_block_fails_and_when_the_user_closed_it_already(dev, monkeypatch):
    Page(dev)
    monkeypatch.setenv("DJS_CDP_URL", dev.http_url)
    with pytest.raises(RuntimeError):
        with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:
            c.goto(URL)
            raise RuntimeError("stop")
    assert list(dev.pages) == ["page-1"]
    with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:
        c.goto(URL)
        dev.pages.pop("page-3")   # the user closed it
    assert list(dev.pages) == ["page-1"] and not dev.gone.is_set()


def test_the_users_browser_may_be_named_by_its_websocket(dev, monkeypatch):
    Page(dev)
    monkeypatch.setenv("DJS_CDP_URL", dev.address)
    with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:
        c.goto(URL)


def test_an_unreachable_users_browser_says_how_to_start_one(monkeypatch):
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:1")
    with pytest.raises(browser.BrowserUnavailable, match=r"remote-debugging-port=9222.*DJS_CDP_URL") as e:
        with browser.open_browser(PROBE, "direct", 0.0, 0.0):
            pass
    assert "http://127.0.0.1:1" in str(e.value)


# ============================================================ a browser of ours (a script that behaves like one)
class Started(list):
    """Every process started so far, and the script they run."""
    script: object


@pytest.fixture
def fake_chrome(tmp_path, monkeypatch):
    """The browser the client starts is tests/fake_devtools.py."""
    chrome = fake_devtools.script(tmp_path / "fake-chrome")
    started, real = Started(), subprocess.Popen

    def spy(*args, **kwargs):
        started.append(real(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(browser, "find_browser", lambda: ("chrome", chrome))
    monkeypatch.setattr(subprocess, "Popen", spy)
    monkeypatch.setattr(browser, "QUIT_SECONDS", 0.5)
    started.script = chrome
    yield started
    for proc in started:   # a failed test must not leave a browser running on the developer's machine
        if browser._group_alive(proc.pid):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(10)


def profile():
    return db.profile_dir("probe", create=True) / "browser"


def lock_is_free(name="probe"):
    with browser.profile_lock(name, wait=0):   # BrowserBusy while someone holds it
        return True


@process
def test_the_client_runs_a_browser_on_the_platform_profile_under_its_lock_and_lets_it_quit(fake_chrome):
    with browser.open_browser(PROBE, "direct", 0.0, 0.0) as c:
        [proc] = fake_chrome
        assert isinstance(c, browser.BrowserClient)
        assert proc.args[1:3] == [f"--user-data-dir={profile()}", "--remote-debugging-port=0"] and proc.args[-1] == "about:blank"
        assert set(browser.WINDOW_FLAGS) | set(browser.CRAWL_FLAGS) <= set(proc.args) and "--headless=new" in proc.args
        assert not any(a.startswith("--proxy-server") for a in proc.args)
        with pytest.raises(browser.BrowserBusy):
            lock_is_free()   # while the client is open it owns the profile
    assert proc.returncode == 0 and lock_is_free() and not browser._alive   # it quit by itself when asked


@process
@pytest.mark.parametrize("needs_headed, forced, headless", [
    (False, None, True), (True, None, False), (True, "1", True), (False, "0", False), (True, "yes", False)])
def test_a_window_is_opened_only_where_the_platform_needs_one(fake_chrome, monkeypatch, needs_headed, forced, headless):
    if forced is not None:
        monkeypatch.setenv("DJS_BROWSER_HEADLESS", forced)
    with browser.open_browser(SimpleNamespace(name="probe", needs_headed=needs_headed), "direct", 0.0, 0.0):
        assert ("--headless=new" in fake_chrome[0].args) is headless


@process
@pytest.mark.parametrize("exit_id, flag", [
    ("http://proxy.test:8080", "--proxy-server=http://proxy.test:8080"),
    ("https://proxy.test:3128", "--proxy-server=https://proxy.test:3128"),
    ("socks5://127.0.0.1:1080", "--proxy-server=socks5://127.0.0.1:1080"),
    ("direct", None), (None, None)])
def test_a_proxy_exit_is_the_proxy_server_of_our_own_browser(fake_chrome, exit_id, flag):
    with browser.open_browser(PROBE, exit_id, 0.0, 0.0):
        flags = [a for a in fake_chrome[0].args if a.startswith("--proxy-server")]
        assert flags == ([flag] if flag else [])


@process
@pytest.mark.parametrize("exit_id", ["http://user:secret@proxy.test:8080", "http://only@proxy.test:3128",
                                     "socks5://u:p%40ss@proxy.test:1080"])
def test_a_proxy_with_a_user_name_is_refused_before_anything_starts(fake_chrome, home, exit_id):
    with pytest.raises(browser.BrowserUnavailable, match="user name and password") as e:
        with browser.open_browser(PROBE, exit_id, 0.0, 0.0):
            pass
    assert "secret" not in str(e.value) and "p%40ss" not in str(e.value)   # the credentials stay out of the message
    assert results.classify(str(e.value))[0] == "proxy"   # the user is told about the proxy, not to install a browser or retry
    assert fake_chrome == [] and not (home / "profiles").exists() and not browser._alive


@process
def test_no_browser_means_no_lock_taken_and_no_process(fake_chrome, monkeypatch):
    monkeypatch.setattr(browser, "find_browser", lambda: None)
    with pytest.raises(browser.BrowserUnavailable, match="No browser found"):
        with browser.open_browser(PROBE, "direct", 0.0, 0.0):
            pass
    assert fake_chrome == [] and lock_is_free() and not browser._alive


@process
@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_the_process_is_gone_after_any_exception_in_the_block(fake_chrome, error):
    with pytest.raises(error):
        with browser.open_browser(PROBE, "direct", 0.0, 0.0):
            raise error("stop")
    assert fake_chrome[0].poll() is not None and lock_is_free() and not browser._alive


@process
@pytest.mark.parametrize("mode, ended_by", [("stuck", signal.SIGTERM), ("deaf", getattr(signal, "SIGKILL", 9))])
def test_a_browser_that_does_not_quit_when_asked_is_terminated_and_then_killed(fake_chrome, monkeypatch, mode, ended_by):
    monkeypatch.setenv("FAKE_BROWSER", mode)
    with browser.open_browser(PROBE, "direct", 0.0, 0.0):
        pass
    assert fake_chrome[0].returncode == -ended_by and lock_is_free() and not browser._alive
    assert not (profile() / "DevToolsActivePort").exists()


@process
def test_what_the_browser_started_goes_with_it(fake_chrome, monkeypatch):
    monkeypatch.setenv("FAKE_BROWSER", "stuck,stubborn-helper")
    with browser.open_browser(PROBE, "direct", 0.0, 0.0):
        helper = int((profile() / "helper-pid").read_text())
        assert os.getpgid(helper) == fake_chrome[0].pid   # in the browser's group
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(helper, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(helper, 0)
    assert lock_is_free() and not browser._alive


@process
def test_a_browser_that_never_listens_or_quits_at_once_is_reported(fake_chrome, monkeypatch):
    monkeypatch.setenv("FAKE_BROWSER", "exit")
    with pytest.raises(browser.BrowserUnavailable, match="quit right after it started"):
        with browser.open_browser(PROBE, "direct", 0.0, 0.0):
            pass
    monkeypatch.setenv("FAKE_BROWSER", "silent")
    monkeypatch.setattr(browser, "START_SECONDS", 0.5)
    with pytest.raises(browser.BrowserUnavailable, match="did not start in time"):
        with browser.open_browser(PROBE, "direct", 0.0, 0.0):
            pass
    assert lock_is_free() and not browser._alive


@process
def test_a_busy_profile_is_busy_not_broken(fake_chrome, monkeypatch):
    real = browser.profile_lock
    monkeypatch.setattr(browser, "profile_lock", lambda name, wait=0.1: real(name, wait))   # the real wait is 3 s
    with browser.profile_lock("probe"):   # a login window, or another crawl
        with pytest.raises(browser.BrowserBusy):
            with browser.open_browser(PROBE, "direct", 0.0, 0.0):
                pass
    assert fake_chrome == []


@process
def test_a_browser_of_ours_left_on_the_profile_is_closed_before_a_new_one_starts(fake_chrome):
    orphan = subprocess.Popen([str(fake_chrome.script), f"--user-data-dir={profile()}", "--remote-debugging-port=0", "about:blank"])
    deadline = time.monotonic() + 10
    while browser._devtools_address(profile()) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert orphan.poll() is None   # the crashed run's browser is up
    with browser.open_browser(PROBE, "direct", 0.0, 0.0):
        assert orphan.wait(5) == 0   # it quit when asked, before the new one started
        assert fake_chrome[1].poll() is None


@process
def test_an_interpreter_that_exits_with_a_crawl_open_closes_the_browser(fake_chrome, tmp_path):
    code = f"""
import threading, time
from pathlib import Path
from types import SimpleNamespace
from djs import browser
browser.find_browser = lambda: ('chrome', Path({str(fake_chrome.script)!r}))

def hold():
    with browser.open_browser(SimpleNamespace(name='probe', needs_headed=False), 'direct', 0.0, 0.0):
        time.sleep(60)

threading.Thread(target=hold, daemon=True).start()
while not (browser._alive and next(iter(browser._alive)).cdp):
    time.sleep(0.05)
print(next(iter(browser._alive)).proc.pid, flush=True)
"""   # the main thread ends: the interpreter exits while the crawl is still open in a daemon thread
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "DJS_HOME": str(tmp_path / "other-home")})
    assert out.returncode == 0, out.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(int(out.stdout.split()[-1]), 0)   # the browser went with the interpreter
