"""Which browser the persistent profile starts in: installed Chrome, then Edge, then Playwright's own Chromium.
A fake `pw` stands in for Playwright, so no browser is needed."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from djs import browser

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    return tmp_path


def missing(channel):
    """The text Playwright raises when a browser is not on the machine."""
    if channel:
        return f"Chromium distribution '{channel}' is not found at /x\nRun \"playwright install {channel}\""
    return "BrowserType.launch_persistent_context: Executable doesn't exist at /x"


class Ctx:
    def __init__(self, channel):
        self.channel, self.scripts = channel, []

    def add_init_script(self, js):
        self.scripts.append(js)


class Chromium:
    """Starts only the browsers in `installed` (channel names; None = Playwright's Chromium), and records each try."""

    def __init__(self, installed, failure=None):
        self.installed, self.failure, self.tries = installed, failure, []

    def launch_persistent_context(self, path, channel=None, **kw):
        self.tries.append((channel, path, kw))
        if self.failure:
            raise sync_api.Error(self.failure)
        if channel not in self.installed:
            raise sync_api.Error(missing(channel))
        return Ctx(channel)


def launch(*installed, failure=None):
    chromium = Chromium(installed, failure)
    return chromium, browser.launch_context(SimpleNamespace(chromium=chromium), "boss", True)


def test_installed_chrome_is_used_first_and_alone():
    chromium, ctx = launch("chrome", "msedge", None)
    assert ctx.channel == "chrome" and [t[0] for t in chromium.tries] == ["chrome"]


def test_edge_follows_chrome():
    chromium, ctx = launch("msedge", None)
    assert ctx.channel == "msedge" and [t[0] for t in chromium.tries] == ["chrome", "msedge"]


def test_playwright_chromium_is_the_last_resort():
    chromium, ctx = launch(None)
    assert ctx.channel is None and [t[0] for t in chromium.tries] == ["chrome", "msedge", None]


def test_every_try_uses_the_same_profile_flags_and_stealth():
    chromium, ctx = launch(None)
    assert len({(path, repr(sorted(kw.items()))) for _, path, kw in chromium.tries}) == 1
    _, path, kw = chromium.tries[0]
    assert path.endswith(str(Path("profiles") / "boss" / "browser"))
    assert (kw["headless"], kw["locale"]) == (True, "zh-CN")
    assert kw["args"] == ["--disable-blink-features=AutomationControlled"]
    assert ctx.scripts == [browser.STEALTH_JS]


def test_no_browser_at_all_says_what_to_install():
    with pytest.raises(browser.BrowserUnavailable) as e:
        launch()
    assert "Google Chrome or Microsoft Edge" in str(e.value) and "playwright install chromium" in str(e.value)


def test_a_real_launch_failure_is_not_mistaken_for_a_missing_browser():
    chromium = Chromium({"chrome"}, failure="Target page, context or browser has been closed")
    with pytest.raises(sync_api.Error, match="has been closed"):
        browser.launch_context(SimpleNamespace(chromium=chromium), "boss", True)
    assert [t[0] for t in chromium.tries] == ["chrome"]  # no silent switch to another browser


def test_packaged_app_points_at_its_own_installer(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(Path("/apps/找岗位助手.app/Contents/MacOS/找岗位助手")))
    hint = browser.no_browser_hint()
    assert hint.endswith(f'{Path("/apps/找岗位助手.app/Contents/MacOS/djs")}" install-browser')
    assert "playwright install" not in hint


def test_install_chromium_runs_the_driver_that_ships_with_playwright(monkeypatch):
    from playwright._impl._driver import compute_driver_executable
    ran = {}

    def fake_run(cmd, env):
        ran.update(cmd=cmd, env=env)
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert browser.install_chromium() == 3
    assert ran["cmd"] == [*compute_driver_executable(), "install", "chromium"]
    assert ran["env"]["PW_LANG_NAME"] == "python"
