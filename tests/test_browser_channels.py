"""Which browser we start, login window and crawl alike: an installed Chrome, else Edge, and where each is found on each OS. No
browser is needed, the machine is described to the code."""
import shutil
import sys
from pathlib import Path

import pytest

from djs import browser


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    return tmp_path


@pytest.fixture
def machine(monkeypatch):
    """Describes a machine: the OS, the files that exist, the commands on the PATH and environment variables."""
    def describe(platform, files=(), commands=(), env=None):
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
    return describe


MAC = "/Applications/{0}.app/Contents/MacOS/{0}"


def test_chrome_then_edge_are_the_only_browsers():
    assert browser.CHANNELS == ("chrome", "msedge")   # Playwright's own Chromium is not a fallback any more


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


def test_on_linux_the_commands_are_tried_chrome_then_edge(machine):
    machine("linux", commands=["microsoft-edge", "google-chrome-stable", "google-chrome"])
    assert browser.find_browser() == ("chrome", Path("/usr/bin/google-chrome"))
    machine("linux", commands=["microsoft-edge", "google-chrome-stable"])
    assert browser.executable() == Path("/usr/bin/google-chrome-stable")
    machine("linux", commands=["microsoft-edge"])
    assert browser.find_browser() == ("msedge", Path("/usr/bin/microsoft-edge"))


def test_chromium_alone_is_not_a_browser_we_start(machine):
    machine("linux", commands=["chromium", "chromium-browser"])
    assert browser.find_browser() is None


def test_the_order_follows_the_channels(machine, monkeypatch):
    machine("linux", commands=["google-chrome", "microsoft-edge"])
    monkeypatch.setattr(browser, "CHANNELS", ("msedge", "chrome"))
    assert browser.find_browser() == ("msedge", Path("/usr/bin/microsoft-edge"))


def test_no_browser_at_all_says_what_to_install(machine):
    machine("linux")
    assert browser.find_browser() is None and not browser.ready()
    with pytest.raises(browser.BrowserUnavailable) as e:
        browser.executable()
    assert str(e.value) == browser.no_browser_hint()
    assert "Google Chrome or Microsoft Edge" in str(e.value)
    assert not any(word in str(e.value).lower() for word in ("playwright", "install-browser", "pip "))   # nothing to run, nothing to download
    machine("linux", commands=["google-chrome"])
    assert browser.executable() == Path("/usr/bin/google-chrome") and browser.ready()
