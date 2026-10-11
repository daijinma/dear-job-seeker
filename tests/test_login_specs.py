"""Every platform that can be logged in to says how, in a form the login framework can run (providers.base.Login)."""
from urllib.parse import urlsplit

import pytest

from djs import providers
from djs.providers.base import Login

METHODS = ("qr", "wechat", "password", "sms")
LOGIN_PROVIDERS = [n for n in providers.names() if providers.get(n).auth != "public"]
# What the research note (.trellis/tasks/10-11-djs-v1-1-0/research/platform-login.md) found for each platform
FOUND = {"boss": ("optional", "qr", "zhipin.com", "cookies_any", ("wt2", "zp_at")),
         "zhaopin": ("optional", "wechat", "zhaopin.com", "cookies_all", ("at", "rt")),
         "job51": ("optional", "password", "51job.com", "url_prefix", "https://we.51job.com/pc/my/"),
         "liepin": ("optional", "qr", "liepin.com", "cookies_any", ("lt_auth",)),
         "kq36": ("login", "password", "kq36.com", "cookies_any", ("usercookies",))}


def under(url: str, domain: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == domain or host.endswith("." + domain)


@pytest.mark.parametrize("name", providers.names())
def test_a_login_is_declared_exactly_where_the_platform_has_one(name):
    p = providers.get(name)
    assert p.auth in ("public", "optional", "login")
    assert (p.login is not None) == (p.auth != "public")
    assert (p.login_transport is None) or p.login is not None


@pytest.mark.parametrize("name", LOGIN_PROVIDERS)
def test_the_login_spec_is_complete_and_consistent(name):
    spec = providers.get(name).login
    assert isinstance(spec, Login) and spec.method in METHODS
    assert urlsplit(spec.url).scheme == "https" and under(spec.url, spec.domain)
    assert not spec.domain.startswith(".") and "/" not in spec.domain
    rules = [bool(spec.cookies_any), bool(spec.cookies_all), bool(spec.url_prefix)]
    assert sum(rules) == 1                                         # one success rule decides; a probe only double-checks
    assert all(name.strip() == name and name for name in spec.cookies_any + spec.cookies_all)
    assert not spec.url_prefix or (spec.url_prefix.startswith("https://") and under(spec.url_prefix, spec.domain))
    assert not spec.probe_url or under(spec.probe_url, spec.domain)
    assert not spec.probe_fail_text or spec.probe_url              # a fail text means nothing without a probe
    assert any("一" <= ch <= "鿿" for ch in spec.unlocks)  # one plain Chinese line for the UI
    assert spec.valid_days >= 1


@pytest.mark.parametrize("name", providers.names())
def test_the_transports_are_known_and_the_login_one_is_among_them(name):
    p = providers.get(name)
    assert p.transports and set(p.transports) <= {"http", "browser"}
    assert p.login_transport is None or p.login_transport in p.transports
    assert not p.needs_headed or "browser" in p.transports


def test_the_logins_are_what_the_research_found():
    assert set(FOUND) == set(LOGIN_PROVIDERS)
    for name, (auth, method, domain, rule, value) in FOUND.items():
        p = providers.get(name)
        assert (p.auth, p.login.method, p.login.domain) == (auth, method, domain), name
        assert getattr(p.login, rule) == value, name


def test_where_a_logged_in_session_changes_the_endpoint_it_says_which_transport():
    assert {n: providers.get(n).login_transport for n in LOGIN_PROVIDERS} == {
        "boss": None, "zhaopin": "browser", "job51": None, "liepin": None, "kq36": None}   # 前程无忧: see F38
    assert providers.get("boss").transports == ("browser",) and providers.get("boss").needs_headed
    assert providers.get("kq36").transports == ("http",)           # the saved cookie goes on the http client
