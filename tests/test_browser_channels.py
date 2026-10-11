"""Which browser the persistent profile starts in: installed Chrome, then Edge, then Playwright's own Chromium, and where
the login window finds the same browser on each OS. A fake `pw` stands in for Playwright, so no browser is needed."""
import shutil
import subprocess
import sys
from contextlib import nullcontext
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


# ============================================================ the browser a login window starts in
@pytest.fixture
def machine(monkeypatch):
    """Describes a machine: the OS, the files that exist, the commands on the PATH, environment variables, and where
    Playwright says its own Chromium would be."""
    def describe(platform, files=(), commands=(), env=None, own="/nowhere/chromium"):
        present = {str(f) for f in files} | {f"/usr/bin/{c}" for c in commands}
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(Path, "is_file", lambda self: str(self) in present)
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name in commands else None)
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            monkeypatch.delenv(var, raising=False)
        for var, value in (env or {}).items():
            monkeypatch.setenv(var, value)
        monkeypatch.setenv("HOME", "/home/me")
        monkeypatch.setenv("USERPROFILE", "/home/me")
        chromium = SimpleNamespace(executable_path=own)
        monkeypatch.setattr(browser, "_pw", lambda: SimpleNamespace(sync_playwright=lambda: nullcontext(SimpleNamespace(chromium=chromium))))
    return describe


MAC = "/Applications/{0}.app/Contents/MacOS/{0}"


def test_on_a_mac_chrome_comes_first_then_edge_in_applications_and_in_the_users_own_folder(machine):
    chrome, edge = Path(MAC.format("Google Chrome")), Path(MAC.format("Microsoft Edge"))
    machine("darwin", files=[edge, chrome])
    assert browser.find_browser() == ("chrome", chrome) and browser.executable() == chrome
    machine("darwin", files=[edge])
    assert browser.find_browser() == ("msedge", edge)
    mine = Path("/home/me") / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    machine("darwin", files=[edge, mine])
    assert browser.executable() == mine   # Chrome in the user's own folder still beats Edge
    machine("darwin", files=[Path("/Applications/Safari.app/Contents/MacOS/Safari")])
    assert browser.find_browser() is None


def test_on_windows_the_program_folders_then_local_app_data_are_searched_for_chrome_then_edge(machine):
    env = {"ProgramFiles": "/pf", "ProgramFiles(x86)": "/pf86", "LocalAppData": "/local"}
    chrome = lambda base: Path(base, "Google", "Chrome", "Application", "chrome.exe")   # noqa: E731
    edge = lambda base: Path(base, "Microsoft", "Edge", "Application", "msedge.exe")   # noqa: E731
    machine("win32", files=[edge("/pf"), chrome("/local"), chrome("/pf86")], env=env)
    assert browser.find_browser() == ("chrome", chrome("/pf86"))   # Program Files (x86) comes before Local AppData
    machine("win32", files=[edge("/pf"), chrome("/local")], env=env)
    assert browser.executable() == chrome("/local")   # Chrome anywhere beats Edge
    machine("win32", files=[edge("/local")], env=env)
    assert browser.find_browser() == ("msedge", edge("/local"))
    machine("win32", files=[chrome("/pf")], env={"LocalAppData": "/local"})   # ProgramFiles is not even set
    assert browser.find_browser() is None


def test_on_linux_the_commands_are_tried_chrome_then_edge_then_chromium(machine):
    machine("linux", commands=["chromium-browser", "chromium", "microsoft-edge", "google-chrome-stable", "google-chrome"])
    assert browser.find_browser() == ("chrome", Path("/usr/bin/google-chrome"))
    machine("linux", commands=["chromium-browser", "chromium", "microsoft-edge", "google-chrome-stable"])
    assert browser.executable() == Path("/usr/bin/google-chrome-stable")
    machine("linux", commands=["chromium-browser", "chromium", "microsoft-edge"])
    assert browser.find_browser() == ("msedge", Path("/usr/bin/microsoft-edge"))
    machine("linux", commands=["chromium-browser", "chromium"])
    assert browser.find_browser() == ("chromium", Path("/usr/bin/chromium"))
    machine("linux", commands=["chromium-browser"])
    assert browser.find_browser() == ("chromium", Path("/usr/bin/chromium-browser"))


def test_playwrights_own_chromium_is_the_last_resort_and_only_when_it_is_there(machine):
    machine("linux", files=["/cache/chromium-1/chrome"], own="/cache/chromium-1/chrome")
    assert browser.find_browser() == ("chromium", Path("/cache/chromium-1/chrome"))
    machine("linux", commands=["google-chrome"], files=["/cache/chromium-1/chrome"], own="/cache/chromium-1/chrome")
    assert browser.find_browser()[0] == "chrome"   # a browser the machine already has comes first
    machine("linux", own="/cache/chromium-1/chrome")   # Playwright knows where it would put it, but it never did
    assert browser.find_browser() is None
    with pytest.raises(browser.BrowserUnavailable) as e:
        browser.executable()
    assert "Google Chrome or Microsoft Edge" in str(e.value) and "playwright install chromium" in str(e.value)


def test_the_order_follows_the_channels_the_persistent_profile_uses(machine, monkeypatch):
    machine("linux", commands=["google-chrome", "microsoft-edge"])
    monkeypatch.setattr(browser, "CHANNELS", ("msedge", "chrome", None))
    assert browser.find_browser() == ("msedge", Path("/usr/bin/microsoft-edge"))


def test_without_chrome_or_edge_and_without_playwright_the_error_says_what_to_install(machine, monkeypatch):
    machine("linux")

    def missing():
        raise browser.BrowserUnavailable(browser.INSTALL_HINT)

    monkeypatch.setattr(browser, "_pw", missing)
    with pytest.raises(browser.BrowserUnavailable, match="playwright install chromium"):
        browser.executable()
    machine("linux", commands=["google-chrome"])
    monkeypatch.setattr(browser, "_pw", missing)
    assert browser.executable() == Path("/usr/bin/google-chrome")   # Chrome does not need Playwright at all
