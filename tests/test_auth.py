"""The login framework: what counts as logged in comes from each platform's Login spec, never from per-platform code.
The browser window is a fake page the test scripts over time, so no real window opens and no network is used."""
import json
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from djs import auth, browser, providers
from djs.egress import NoEgressAvailable
from djs.providers.base import Login, Provider

SECRET = "TOPSECRET-VALUE"
ANY = Login(url="https://site.test/login", method="qr", domain="site.test", cookies_any=("wt2", "zp_at"), unlocks="能看到薪资")
ALL = replace(ANY, method="wechat", cookies_any=(), cookies_all=("at", "rt"))
LANDING = Login(url="https://login.site.test/?next=my", method="password", domain="site.test",
                url_prefix="https://we.site.test/my/", valid_days=30)
PROBED = replace(ANY, probe_url="https://site.test/api/me", probe_fail_text="请先登录")


def platform(name: str, spec: Login | None) -> Provider:
    return type(name, (Provider,), {"name": name, "label": name, "auth": "optional" if spec else "public", "login": spec})()


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.setattr(auth, "POLL_SECONDS", 0.01)
    provs = {n: platform(n, spec) for n, spec in
             (("any", ANY), ("all", ALL), ("landing", LANDING), ("probed", PROBED), ("plain", None))}
    monkeypatch.setattr(providers, "get", lambda n: provs[n] if n in provs else (_ for _ in ()).throw(KeyError(n)))
    monkeypatch.setattr(providers, "names", lambda: list(provs))


def cookie(name, value="v", domain=".site.test", expires=-1):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires}


class Page:
    """What auth.login sees of the window. `steps` = one (url, cookies) per look at it; the last one repeats."""

    def __init__(self, steps):
        self.steps, self.looks, self.visited, self.cleared, self.closed = steps, 0, [], [], False
        self.context = SimpleNamespace(cookies=self._cookies, clear_cookies=lambda name: self.cleared.append(name))

    def _step(self):
        return self.steps[min(max(self.looks - 1, 0), len(self.steps) - 1)]

    def _cookies(self):
        self.looks += 1
        return list(self._step()[1])

    @property
    def url(self):
        return self._step()[0]

    def goto(self, url, **kw):
        self.visited.append((url, kw))

    def is_closed(self):
        return self.closed


def show(monkeypatch, page):
    """auth.login's window is this page; the returned list records when it opens and when it closes."""
    events = []

    @contextmanager
    def open_page(provider_name, headless, proxy=None):
        events.append(("open", provider_name, headless))
        try:
            yield page
        finally:
            page.closed = True
            events.append(("close", provider_name))

    monkeypatch.setattr(browser, "open_page", open_page)
    return events


def on(url, *cookies):
    return (url, list(cookies))


LOGIN_URL = "https://site.test/login"


# ============================================================ what counts as logged in
def test_any_of_the_cookies_is_enough_and_the_window_closes_by_itself(monkeypatch):
    page = Page([on(LOGIN_URL), on(LOGIN_URL, cookie("junk")), on(LOGIN_URL, cookie("junk"), cookie("zp_at", SECRET, expires=time.time() + 9e5),
                                                                cookie("other", "x", domain=".elsewhere.test"))])
    events = show(monkeypatch, page)
    got = auth.login("any", timeout=5)
    assert got["logged_in"] is True and got["state"] == "logged_in" and got["method"] == "qr"
    assert events == [("open", "any", False), ("close", "any")]   # headed, and closed by auth itself
    assert page.visited == [(LOGIN_URL, {"wait_until": "domcontentloaded"})] and page.closed
    assert auth.load_cookies("any") == {"junk": "v", "zp_at": SECRET}   # the platform's own cookies, nothing else the browser holds


@pytest.mark.parametrize("name, early, late", [
    ("any", [cookie("nope")], [cookie("wt2")]),
    ("all", [cookie("at")], [cookie("at"), cookie("rt")])])   # all of them: one is not enough
def test_the_cookie_rules(monkeypatch, name, early, late):
    page = Page([on(LOGIN_URL, *early), on(LOGIN_URL, *early), on(LOGIN_URL, *late)])
    show(monkeypatch, page)
    assert auth.login(name, timeout=5)["state"] == "logged_in"
    assert page.looks == 3   # it kept looking until the rule held


def test_a_landing_page_can_be_the_signal_and_cookies_then_say_nothing(monkeypatch):
    page = Page([on("https://login.site.test/?next=my", cookie("analytics")), on("https://we.site.test/other", cookie("analytics")),
                 on("https://we.site.test/my/myjob", cookie("analytics"))])
    show(monkeypatch, page)
    assert auth.login("landing", timeout=5)["state"] == "logged_in" and page.looks == 3
    assert auth.load_cookies("landing") == {"analytics": "v"}


def test_the_first_rule_a_platform_sets_decides(monkeypatch):
    both = replace(ANY, url_prefix="https://site.test/home")
    monkeypatch.setattr(providers.get("any"), "login", both)
    page = Page([on("https://site.test/home")])   # on the landing page, but without the cookie
    show(monkeypatch, page)
    with pytest.raises(TimeoutError):
        auth.login("any", timeout=0.1)


@pytest.mark.parametrize("domain", [".other.test", "notsite.test", ".site.test.evil.test"])
def test_cookies_of_other_domains_never_count(monkeypatch, domain):
    show(monkeypatch, Page([on(LOGIN_URL, cookie("wt2", domain=domain))]))
    with pytest.raises(TimeoutError):
        auth.login("any", timeout=0.1)


@pytest.mark.parametrize("domain", ["site.test", ".site.test", "www.site.test", "a.b.site.test"])
def test_the_domain_and_its_subdomains_count(monkeypatch, domain):
    show(monkeypatch, Page([on(LOGIN_URL, cookie("wt2", domain=domain))]))
    assert auth.login("any", timeout=5)["logged_in"] is True


# ============================================================ the platform's own check
def fake_get(monkeypatch, *answers):
    """Answers httpx.get in turn (the last one repeats); an Exception is raised instead. Returns the recorded calls."""
    calls = []

    def get(url, **kw):
        calls.append((url, kw))
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        status, text = answer
        return httpx.Response(status, text=text, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    return calls


def test_a_probe_has_to_agree_before_the_login_counts(monkeypatch):
    monkeypatch.setattr(auth, "PROBE_SECONDS", 0)
    page = Page([on(LOGIN_URL, cookie("wt2", SECRET))])
    show(monkeypatch, page)
    calls = fake_get(monkeypatch, (200, "请先登录"), httpx.ConnectError("no answer yet"), (403, "no"), (302, ""), (200, '{"code":0}'))
    assert auth.login("probed", timeout=5)["state"] == "logged_in"
    assert len(calls) == 5   # a wall text, a dropped connection, 403 and a redirect all mean "not yet"
    url, kw = calls[-1]
    assert url == "https://site.test/api/me" and kw["cookies"] == {"wt2": SECRET} and kw["trust_env"] is False
    assert kw["headers"]["Referer"] == "https://site.test/" and kw["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert SECRET not in url   # the cookie travels as a cookie


def test_probe_requests_are_spaced_while_waiting(monkeypatch):
    monkeypatch.setattr(auth, "PROBE_SECONDS", 30)
    show(monkeypatch, Page([on(LOGIN_URL, cookie("wt2"))]))
    calls = fake_get(monkeypatch, (200, "请先登录"))
    with pytest.raises(TimeoutError):
        auth.login("probed", timeout=0.3)   # about thirty looks, one question
    assert len(calls) == 1


def test_a_login_without_a_probe_counts_at_once(monkeypatch):
    show(monkeypatch, Page([on(LOGIN_URL, cookie("wt2"))]))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("nothing to ask"))
    assert auth.login("any", timeout=5)["logged_in"] is True


# ============================================================ ending a login without success
def test_a_login_that_never_happens_times_out_with_nothing_saved(monkeypatch):
    page = Page([on(LOGIN_URL, cookie("junk", SECRET))])
    events = show(monkeypatch, page)
    with pytest.raises(TimeoutError, match="秒内没有登录成功，请再试一次") as e:
        auth.login("any", timeout=0.1)
    assert SECRET not in str(e.value) and events[-1] == ("close", "any")
    assert auth.status("any")["state"] == "not_logged_in" and auth.load_cookies("any") is None


def test_cancel_closes_the_window_and_saves_nothing(monkeypatch):
    page = Page([on(LOGIN_URL, cookie("junk"))])
    events = show(monkeypatch, page)
    outcome = {}

    def run():
        try:
            auth.login("any", timeout=30)
        except BaseException as e:   # the thread reports what login raised
            outcome["error"] = e

    thread = threading.Thread(target=run)
    thread.start()
    while page.looks < 3:   # the user is looking at the window
        time.sleep(0.01)
    assert auth.cancel_login("any") is True
    thread.join(5)
    assert not thread.is_alive() and isinstance(outcome["error"], auth.LoginCancelled) and str(outcome["error"]) == "已取消登录"
    assert events[-1] == ("close", "any") and page.closed   # the window is really gone
    assert auth.cancel_login("any") is False and auth.cancel_login("all") is False   # nothing pending any more
    assert auth.status("any")["state"] == "not_logged_in"


def test_a_window_the_user_closed_ends_the_login_in_plain_chinese(monkeypatch):
    page = Page([on(LOGIN_URL)])
    page.closed = True
    show(monkeypatch, page)
    page.closed = True   # the fake window closes it again on exit; here the user did it first
    with pytest.raises(RuntimeError, match="登录窗口被关闭了，请重新登录"):
        auth.login("any", timeout=5)


def test_a_missing_browser_is_explained_in_chinese_and_a_busy_profile_already_is(monkeypatch):
    @contextmanager
    def nothing(provider_name, headless, proxy=None):
        raise browser.BrowserUnavailable("No browser found. Install Google Chrome or Microsoft Edge")
        yield

    monkeypatch.setattr(browser, "open_page", nothing)
    with pytest.raises(browser.BrowserUnavailable, match="^打不开浏览器，没法登录。No browser found"):
        auth.login("any")

    @contextmanager
    def busy(provider_name, headless, proxy=None):
        raise browser.BrowserBusy("正在使用浏览器，请稍后再试")
        yield

    monkeypatch.setattr(browser, "open_page", busy)
    with pytest.raises(NoEgressAvailable, match="正在使用浏览器，请稍后再试"):
        auth.login("any")
    assert auth.cancel_login("any") is False   # a login that could not start leaves nothing to cancel


def test_logging_in_again_forgets_the_cookies_of_the_session_that_died(monkeypatch):
    page = Page([on(LOGIN_URL, cookie("wt2"))])
    show(monkeypatch, page)
    auth.login("any", timeout=5)
    assert page.cleared == ["wt2", "zp_at"]   # nothing saved yet: the profile's own leftovers must not pass for a login
    auth.invalidate("any")
    again = Page([on(LOGIN_URL, cookie("wt2"))])
    show(monkeypatch, again)
    auth.login("any", timeout=5)
    assert again.cleared == ["wt2", "zp_at"]   # expired: the same
    third = Page([on(LOGIN_URL, cookie("wt2"))])
    show(monkeypatch, third)
    auth.login("any", timeout=5)
    assert third.cleared == []   # a session that still works is left alone


def test_the_users_own_browser_is_never_cleaned(monkeypatch):
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:9222")
    page = Page([on(LOGIN_URL, cookie("wt2"))])
    show(monkeypatch, page)

    @contextmanager
    def attached(provider_name, headless, proxy=None):
        yield SimpleNamespace(cookies=lambda: [cookie("wt2")])

    monkeypatch.setattr(browser, "open_context", attached)
    assert auth.login("any", timeout=5)["logged_in"] is True and page.cleared == []


# ============================================================ the saved session
def save(provider="any", cookies=None, expires=None):
    auth.save_cookies(provider, cookies or {"wt2": SECRET}, expires)


def rewrite(provider, **changes):
    f = auth.profile_dir(provider) / "cookies.json"
    f.write_text(json.dumps({**json.loads(f.read_text()), **changes}))


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_the_store_is_private_and_atomic():
    save(expires={"wt2": time.time() + 9e5})
    d = auth.profile_dir("any")
    assert mode(d) == 0o700 and mode(d / "cookies.json") == 0o600 and not (d / "cookies.json.tmp").exists()
    assert json.loads((d / "cookies.json").read_text())["expires"].keys() == {"wt2"}   # only cookies that have an expiry
    assert auth.load_cookies("any") == {"wt2": SECRET}


def test_login_saves_the_expiry_of_the_cookies_that_have_one(monkeypatch):
    soon = time.time() + 3 * 86400
    show(monkeypatch, Page([on(LOGIN_URL, cookie("wt2", expires=soon), cookie("session"))]))
    auth.login("any", timeout=5)
    assert json.loads((auth.profile_dir("any") / "cookies.json").read_text())["expires"] == {"wt2": soon}   # a session cookie has none


def test_the_three_states():
    assert auth.status("any")["state"] == "not_logged_in"
    save()
    assert auth.status("any")["state"] == "logged_in"
    auth.invalidate("any")
    assert (auth.status("any")["state"], auth.status("any")["logged_in"]) == ("expired", False)
    assert mode(auth.profile_dir("any") / "cookies.json") == 0o600 and auth.load_cookies("any") is None   # kept, no longer used
    save()   # logging in again
    assert auth.status("any")["state"] == "logged_in"
    auth.logout("any")
    assert auth.status("any")["state"] == "not_logged_in" and not auth.profile_dir("any").exists()
    auth.logout("any")   # idempotent
    auth.invalidate("any")   # nothing to invalidate: no error, no file made up
    assert not auth.profile_dir("any").exists()


def test_a_corrupt_file_is_no_login():
    save()
    (auth.profile_dir("any") / "cookies.json").write_text("{not json")
    assert auth.status("any")["state"] == "not_logged_in" and auth.load_cookies("any") is None


def test_status_has_exactly_what_the_pages_need():
    save(expires={"wt2": time.time() + 86400})
    got = auth.status("any")
    assert set(got) == {"logged_in", "state", "saved_at", "expires_at", "method", "unlocks"}
    assert (got["method"], got["unlocks"]) == ("qr", "能看到薪资") and got["saved_at"] == pytest.approx(time.time(), abs=5)
    assert auth.status("all")["method"] == "wechat" and auth.status("all")["state"] == "not_logged_in"
    assert auth.status("all")["saved_at"] is None and auth.status("all")["expires_at"] is None


def test_the_session_ends_when_the_login_cookies_say_so():
    now = time.time()
    save(cookies={"wt2": "a", "zp_at": "b", "analytics": "c"}, expires={"wt2": now + 1000, "zp_at": now + 5000, "analytics": now + 9e7})
    assert auth.status("any")["expires_at"] == pytest.approx(now + 5000)   # any of them keeps it going: the last one counts
    save("all", {"at": "a", "rt": "b", "analytics": "c"}, {"at": now + 1000, "rt": now + 9000, "analytics": now + 9e7})
    assert auth.status("all")["expires_at"] == pytest.approx(now + 1000)   # all of them are needed: the first one counts
    save("all", {"at": "a", "rt": "b", "analytics": "c"}, {"at": now - 10, "rt": now + 9000, "analytics": now + 9e7})
    assert auth.status("all")["state"] == "expired" and auth.load_cookies("all") is None   # a long-lived analytics cookie does not help


def test_without_an_expiry_the_age_counts():
    save()
    assert auth.status("any")["expires_at"] == pytest.approx(time.time() + 7 * 86400, abs=5)   # Login.valid_days
    rewrite("any", saved_at=time.time() - 8 * 86400)
    assert (auth.status("any")["state"], auth.load_cookies("any")) == ("expired", None)
    save("landing", {"slife": "x"})   # no login cookie to read an expiry from
    assert auth.status("landing")["expires_at"] == pytest.approx(time.time() + 30 * 86400, abs=5)
    save("all", {"at": "a", "rt": "b"}, {"at": time.time() + 99})   # only one of the two says when: not enough
    assert auth.status("all")["expires_at"] == pytest.approx(time.time() + 7 * 86400, abs=5)


def test_a_file_written_before_expiries_were_saved_still_reads():
    f = auth.profile_dir("any", create=True) / "cookies.json"
    f.write_text(json.dumps({"cookies": {"wt2": "old"}, "saved_at": time.time() - 86400, "invalid": False}))
    assert auth.load_cookies("any") == {"wt2": "old"} and auth.status("any")["state"] == "logged_in"


def test_a_platform_without_a_login_has_no_session_to_ask_about():
    for call in (auth.status, auth.load_cookies, auth.login, auth.check, auth.logged_in):
        with pytest.raises(ValueError, match="plain does not need login"):
            call("plain")
    with pytest.raises(KeyError):
        auth.status("no-such-platform")


# ============================================================ asking the platform
def test_check_without_a_probe_is_the_offline_rule_and_asks_nobody(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("nothing to ask"))
    assert auth.check("any")["state"] == "not_logged_in"
    save()
    assert auth.check("any")["state"] == "logged_in"


def test_check_marks_a_session_the_platform_rejects_as_expired(monkeypatch):
    save("probed")
    calls = fake_get(monkeypatch, (200, '{"code": 0}'))
    assert auth.check("probed")["state"] == "logged_in" and calls[0][1]["cookies"] == {"wt2": SECRET}
    for answer in ((200, "请先登录"), (403, "no"), (302, "")):
        save("probed")
        fake_get(monkeypatch, answer)
        got = auth.check("probed")
        assert (got["state"], got["logged_in"]) == ("expired", False), answer
        assert auth.load_cookies("probed") is None   # and it stays so: the pages show 已过期 from now on
    assert mode(auth.profile_dir("probed") / "cookies.json") == 0o600


def test_check_is_silent_about_a_platform_it_could_not_ask(monkeypatch):
    save("probed")
    for answer in (httpx.ConnectError("offline"), httpx.ReadTimeout("slow"), (503, "down"), (429, "later")):
        fake_get(monkeypatch, answer)
        with pytest.raises(httpx.HTTPError):   # an error, not an answer: the session is left alone
            auth.check("probed")
        assert auth.status("probed")["state"] == "logged_in"


def test_check_asks_nothing_when_there_is_no_session(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no session, nobody to ask for"))
    assert auth.check("probed")["state"] == "not_logged_in"
    save("probed")
    auth.invalidate("probed")
    assert auth.check("probed")["state"] == "expired"


# ============================================================ the user's own browser (DJS_CDP_URL)
def attached(monkeypatch, cookies):
    monkeypatch.setenv("DJS_CDP_URL", "http://127.0.0.1:9222")

    @contextmanager
    def open_context(provider_name, headless, proxy=None):
        if isinstance(cookies, Exception):
            raise cookies
        yield SimpleNamespace(cookies=lambda: cookies)

    monkeypatch.setattr(browser, "open_context", open_context)


def test_the_attached_browser_decides_when_the_platform_has_a_cookie_signal(monkeypatch):
    save()   # saved cookies are not the user's browser
    attached(monkeypatch, [cookie("junk")])
    assert auth.logged_in("any") is False and auth.status("any")["state"] == "not_logged_in"
    auth.invalidate("any")
    assert auth.status("any")["state"] == "expired"   # it had a session once
    attached(monkeypatch, [cookie("zp_at", SECRET)])
    assert auth.logged_in("any") is True and auth.status("any")["state"] == "logged_in"
    assert SECRET not in json.dumps(auth.status("any"))


def test_an_unreachable_browser_is_no_answer_for_status_and_an_error_for_the_engines_gate(monkeypatch):
    attached(monkeypatch, browser.BrowserUnavailable("Cannot reach a browser"))
    assert auth.status("any")["state"] == "not_logged_in"
    with pytest.raises(browser.BrowserUnavailable):
        auth.logged_in("any")


def test_a_landing_page_login_has_no_cookie_to_read_in_the_users_browser(monkeypatch):
    save("landing", {"slife": "x"})
    attached(monkeypatch, [])   # would say "no": but there is no cookie signal to ask it about
    assert auth.logged_in("landing") is True and auth.status("landing")["state"] == "logged_in"


# ============================================================ cookie values stay inside
def test_cookie_values_are_never_returned_logged_or_put_into_an_error(monkeypatch, caplog, capsys):
    caplog.set_level("DEBUG")
    save("probed", {"wt2": SECRET})
    seen = [json.dumps(auth.status("probed"))]
    fake_get(monkeypatch, (200, '{"code": 0}'))
    seen.append(json.dumps(auth.check("probed")))
    fake_get(monkeypatch, httpx.ConnectError("offline"))
    with pytest.raises(httpx.HTTPError) as e:
        auth.check("probed")
    seen.append(str(e.value))
    show(monkeypatch, Page([on(LOGIN_URL, cookie("junk", SECRET))]))
    with pytest.raises(TimeoutError) as t:
        auth.login("any", timeout=0.05)
    seen.append(str(t.value))
    auth.cancel_login("any")
    out = capsys.readouterr()
    assert not [s for s in seen + [caplog.text, out.out, out.err] if SECRET in s]
    assert auth.load_cookies("probed") == {"wt2": SECRET}   # the one way out
