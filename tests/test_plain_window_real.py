"""The browser process of the login window against a real headless browser. What the fake browser of test_plain_window.py
cannot show: that nothing a real browser starts (renderers, GPU and network processes, not only the one the window
started) survives the window, however it is ended, and that the profile opens again each time. Skipped where no browser can
be started."""
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from djs import browser

URL = "about:blank"
posix = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("ps"), reason="looks at process groups with ps")
# How the window is ended: the polite way (Browser.close), after Browser.close went unanswered (SIGTERM ends it), after that
# and SIGTERM went unheeded (SIGKILL), with the whole browser frozen, or with the browser already killed from outside.
HOWS = ["asked to quit", "terminated", "killed", "frozen", "killed from outside"]


@pytest.fixture(scope="module")
def browser_works(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DJS_HOME", str(tmp_path_factory.mktemp("probe")))
        mp.delenv("DJS_CDP_URL", raising=False)
        try:
            with browser.plain_window("probe", URL, headless=True):
                pass
        except Exception as e:   # no browser, no permission to start one, a sandbox...
            pytest.skip(f"No browser can be started here ({type(e).__name__}: {e})")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch, browser_works):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.setattr(browser, "QUIT_SECONDS", 1.0)   # the terminate and kill paths wait a second at each step
    yield
    for leftover in list(browser._alive):   # a failed test must not leave a browser running on the developer's machine
        leftover.close()


def members(pgid):
    """The live processes of a process group; zombies, which hold nothing, are left out."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, check=True).stdout
    rows = [line.split() for line in out.splitlines()]
    return sorted(int(pid) for pid, group, stat in (r for r in rows if len(r) == 3)
                  if int(group) == pgid and not stat.startswith("Z"))


def wait_until(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "gave up waiting"
        time.sleep(0.05)


def arrange(how, monkeypatch):
    """Make the window impossible to end the polite way, as far as `how` says."""
    if how in ("terminated", "killed"):
        real_call = browser._Cdp.call

        def call(self, method, timeout=10, **params):
            if method == "Browser.close":
                raise browser.BrowserUnavailable("The window did not answer Browser.close")   # as if it never did
            return real_call(self, method, timeout, **params)

        monkeypatch.setattr(browser._Cdp, "call", call)
    if how == "killed":
        real_signal = browser._signal_tree

        def kill_only(proc, kill, timeout):
            if kill:
                real_signal(proc, kill, timeout)

        monkeypatch.setattr(browser, "_signal_tree", kill_only)


def test_the_profile_opens_again_straight_after_a_window():
    for _ in range(3):
        with browser.plain_window("site", URL, headless=True) as window:
            assert window.url() == URL
    assert not browser._alive


@posix
@pytest.mark.parametrize("how", HOWS)
def test_nothing_of_a_real_browser_survives_the_window_and_the_profile_opens_again(monkeypatch, how):
    arrange(how, monkeypatch)
    with browser.plain_window("site", URL, headless=True) as window:
        pgid = next(iter(browser._alive)).proc.pid
        wait_until(lambda: len(members(pgid)) > 1)   # a real browser is more than the process that was started
        assert window.url() == URL
        if how == "frozen":
            os.killpg(pgid, signal.SIGSTOP)
        if how == "killed from outside":
            os.killpg(pgid, signal.SIGKILL)
        begun = time.monotonic()
    assert time.monotonic() - begun < 3 * browser.QUIT_SECONDS + 1.5
    assert members(pgid) == [] and not browser._alive
    with browser.plain_window("site", URL, headless=True) as window:   # the very profile the first one left
        assert window.url() == URL
        second = next(iter(browser._alive)).proc.pid
    assert members(second) == [] and not browser._alive
