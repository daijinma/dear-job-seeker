"""The login window of djs.browser against a stand-in for the browser's DevTools endpoint (tests/fake_devtools.py): the
commands it sends, what it does when the browser quits or does not answer, the user's own browser (DJS_CDP_URL), a browser
left over by a crashed run, and a browser process, with the processes it started, that must be gone within one deadline
whatever it does or whatever happens to the caller (and the Windows way of ending the tree). No real browser;
test_auth_browser.py and test_plain_window_real.py have one."""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import fake_devtools
from djs import browser, db
from fake_devtools import DevTools

process = pytest.mark.skipif(sys.platform == "win32", reason="the fake browser is a script")
SIGKILL = getattr(signal, "SIGKILL", 9)   # the parametrize lists below are read on Windows too, for its own tests
URL = "https://x.test/login"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DJS_CDP_URL", raising=False)
    monkeypatch.delenv("FAKE_BROWSER", raising=False)
    return tmp_path / "home"


@pytest.fixture
def dev():
    fake = DevTools("https://site.test/login", noise=True)
    yield fake
    fake.quit()


def profile():
    return db.profile_dir("boss", create=True) / "browser"


def cookie(name, value="v", domain=".site.test", path="/", **more):
    return {"name": name, "value": value, "domain": domain, "path": path, "expires": -1, "secure": False, "httpOnly": False, **more}


def lock_is_free(name="boss"):
    with browser.profile_lock(name, wait=0):   # BrowserBusy while someone holds it
        return True


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def goes_away(pid, seconds=10):
    """The process is gone, zombie included: one whose parent died first is reaped by init a moment later."""
    deadline = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not alive(pid)


def helper():
    """Pid of the helper process the fake browser started (FAKE_BROWSER=helper)."""
    return int((profile() / "helper-pid").read_text())


def refuse(*args, **kwargs):
    raise AssertionError("nothing is started or signalled here")


# ============================================================ the protocol helper
def test_events_are_skipped_and_the_answer_is_the_one_asked_for(dev):
    cdp = browser._Cdp(dev.address)
    assert cdp.call("Target.getTargets")["targetInfos"][0]["url"] == "https://site.test/login"   # an event came first
    cdp.close()


def test_a_refused_command_a_silent_browser_a_browser_that_quit_and_no_browser_are_told_apart(dev):
    cdp = browser._Cdp(dev.address)
    with pytest.raises(browser.BrowserUnavailable, match="No.such: 'No.such' wasn't found"):
        cdp.call("No.such")
    with pytest.raises(browser.BrowserUnavailable, match="did not answer Test.slow") as slow:
        cdp.call("Test.slow", timeout=0.2, seconds=1)
    assert not isinstance(slow.value, browser.BrowserGone)
    assert cdp.call("Target.getTargets")["targetInfos"]   # the late answer of the slow call is skipped, not mistaken
    dev.quit()
    with pytest.raises(browser.BrowserGone):
        cdp.call("Target.getTargets")
    with pytest.raises(browser.BrowserUnavailable, match="Cannot talk to the browser at ws://127.0.0.1:1/x"):
        browser._Cdp("ws://127.0.0.1:1/x")


# ============================================================ what the login asks of the window
def test_the_window_reads_cookies_and_the_address_and_notices_the_page_was_closed(dev):
    dev.cookies = [cookie("wt2", "SECRET")]
    window = browser.Window(browser._Cdp(dev.address), "page-1")
    assert (window.url(), window.closed()) == ("https://site.test/login", False)
    assert window.cookies() == dev.cookies
    dev.pages.clear()   # the user closed the page
    assert (window.url(), window.closed()) == ("", True)


def test_a_browser_that_quit_has_no_window_cookies_or_address(dev):
    dev.cookies = [cookie("wt2")]
    window = browser.Window(browser._Cdp(dev.address), "page-1")
    dev.quit()
    assert (window.closed(), window.url(), window.cookies()) == (True, "", [])
    window.delete_cookies(("wt2",), "site.test")   # nothing left to delete, nothing to complain about


def test_cookies_are_deleted_by_name_and_only_on_the_platforms_domain(dev):
    dev.cookies = [cookie("wt2", "a", ".site.test", secure=True, httpOnly=True), cookie("wt2", "b", "www.site.test", "/x"),
                   cookie("zp_at", "c", "a.b.site.test"), cookie("keep", "d"), cookie("wt2", "e", ".other.test"),
                   cookie("wt2", "f", "notsite.test")]
    window = browser.Window(browser._Cdp(dev.address), "page-1")
    window.delete_cookies(("wt2", "zp_at"), "site.test")
    assert sorted((c["name"], c["domain"]) for c in dev.cookies) == [("keep", ".site.test"), ("wt2", ".other.test"), ("wt2", "notsite.test")]
    [sent] = [params["cookies"] for method, params in dev.calls if method == "Storage.setCookies"]
    assert all(c["value"] == "" and c["expires"] < time.time() for c in sent)   # an expiry long past is how a browser drops one
    assert {(c["domain"], c["path"], c["secure"], c["httpOnly"]) for c in sent} == {
        (".site.test", "/", True, True), ("www.site.test", "/x", False, False), ("a.b.site.test", "/", False, False)}
    dev.calls.clear()
    window.delete_cookies(("wt2",), "nowhere.test")
    assert dev.methods() == ["Storage.getCookies"]   # nothing matched: nothing is sent


# ============================================================ the user's own browser (DJS_CDP_URL)
def test_in_the_users_browser_the_window_is_one_new_tab_and_only_that_tab_is_closed(dev, monkeypatch, home):
    monkeypatch.setenv("DJS_CDP_URL", dev.http_url)
    with browser.plain_window("boss", URL) as window:
        assert dev.pages == {"page-1": "https://site.test/login", "page-2": URL}   # theirs stays where it is
        assert (window.url(), window.closed()) == (URL, False)
        assert not (home / "profiles").exists()   # no profile of ours, so no lock either
    assert dev.pages == {"page-1": "https://site.test/login"}
    assert "Browser.close" not in dev.methods() and ("Target.closeTarget", {"targetId": "page-2"}) in dev.calls


def test_in_the_users_browser_no_process_is_started_or_signalled(dev, monkeypatch):
    monkeypatch.setenv("DJS_CDP_URL", dev.http_url)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(browser, "_Browser", refuse)
    monkeypatch.setattr(browser, "_signal_tree", refuse)
    monkeypatch.setattr(os, "killpg", refuse, raising=False)
    with browser.plain_window("boss", URL) as window:
        assert window.url() == URL
    assert dev.pages == {"page-1": "https://site.test/login"} and not dev.gone.is_set() and not browser._alive


def test_the_users_browser_stays_when_the_user_already_closed_the_tab(dev, monkeypatch):
    monkeypatch.setenv("DJS_CDP_URL", dev.http_url)
    with browser.plain_window("boss", URL) as window:
        dev.pages.pop("page-2")
        assert window.closed()
    assert not dev.gone.is_set()


def test_the_users_browser_may_be_named_by_its_websocket(dev, monkeypatch):
    monkeypatch.setenv("DJS_CDP_URL", dev.address)
    with browser.plain_window("boss", URL) as window:
        assert window.url() == URL


@pytest.mark.parametrize("address", ["http://127.0.0.1:1", "not a url"])
def test_an_unreachable_user_browser_says_how_to_start_one(monkeypatch, address):
    monkeypatch.setenv("DJS_CDP_URL", address)
    with pytest.raises(browser.BrowserUnavailable, match=r"remote-debugging-port=9222.*DJS_CDP_URL") as e:
        with browser.plain_window("boss", URL):
            pass
    assert address in str(e.value)


# ============================================================ a browser of ours left by a crashed run
def leave(fake, guid=None):
    """The DevToolsActivePort file a crashed run left in the profile, naming `fake` (or another browser run's id)."""
    profile().mkdir(parents=True, exist_ok=True)
    (profile() / "DevToolsActivePort").write_text(f"{fake.port}\n/devtools/browser/{guid or fake.guid}\n")


def test_a_browser_left_by_a_crashed_run_is_closed_over_its_own_socket(dev):
    leave(dev)
    browser._clear_leftovers(profile())
    assert dev.methods() == ["Browser.close"] and dev.gone.is_set()
    assert not (profile() / "DevToolsActivePort").exists()


def test_a_stale_file_is_just_removed():
    profile().mkdir(parents=True)
    (profile() / "DevToolsActivePort").write_text("1\n/devtools/browser/abc\n")   # nobody listens on port 1
    browser._clear_leftovers(profile())
    assert not (profile() / "DevToolsActivePort").exists()
    browser._clear_leftovers(profile())   # nothing there at all: no error


def test_whatever_took_over_the_port_since_is_not_touched(dev):
    leave(dev, guid="another-run")   # the file names a browser run other than the one that answers on that port now
    browser._clear_leftovers(profile())
    assert dev.calls == [] and not dev.gone.is_set()   # not asked to close, not even talked to
    assert not (profile() / "DevToolsActivePort").exists()


def test_a_leftover_that_will_not_close_keeps_the_profile_busy(dev, monkeypatch):
    monkeypatch.setattr(browser, "QUIT_SECONDS", 0.3)
    dev.deaf = True
    leave(dev)
    with pytest.raises(browser.BrowserBusy):
        browser._clear_leftovers(profile())
    assert dev.methods() == ["Browser.close"]   # asked politely, and nothing else was done to it


@pytest.mark.parametrize("text, address", [
    ("", None), ("5001", None), ("5001\n", None), ("five\n/devtools/browser/abc\n", None), ("5001\nnot-a-path\n", None),
    ("5001\n/devtools/browser/abc\n", "ws://127.0.0.1:5001/devtools/browser/abc")])
def test_the_address_comes_from_the_port_file_once_it_is_complete(text, address):
    profile().mkdir(parents=True)
    assert browser._devtools_address(profile()) is None   # no file yet
    (profile() / "DevToolsActivePort").write_text(text)
    assert browser._devtools_address(profile()) == address


# ============================================================ a browser process of ours (a script that behaves like one)
class Started(list):
    """Every process started so far, and the script they run."""
    script: Path


@pytest.fixture
def fake_chrome(tmp_path, monkeypatch):
    """The browser the window starts is tests/fake_devtools.py."""
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
    for proc in started:   # a failed test must not leave a browser (or its helper) running on the developer's machine
        if browser._group_alive(proc.pid):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(10)


@process
def test_the_window_runs_a_browser_on_the_shared_profile_and_lets_it_quit(fake_chrome):
    with browser.plain_window("boss", URL, headless=True) as window:
        [proc] = fake_chrome
        assert proc.args[1:3] == [f"--user-data-dir={profile()}", "--remote-debugging-port=0"] and proc.args[-1] == URL
        assert "--headless=new" in proc.args and set(browser.WINDOW_FLAGS) <= set(proc.args)
        assert (window.url(), window.closed()) == (URL, False)
        with pytest.raises(browser.BrowserBusy):
            lock_is_free()   # while the window is open it owns the profile
    assert proc.returncode == 0 and lock_is_free() and not browser._alive   # it quit by itself when asked


@process
def test_a_window_for_a_person_is_not_headless(fake_chrome):
    with browser.plain_window("boss", URL):
        assert not any(a.startswith("--headless") for a in fake_chrome[0].args)


@process
@pytest.mark.parametrize("mode, ended_by", [("stuck", signal.SIGTERM), ("deaf", SIGKILL)])
def test_a_browser_that_does_not_quit_when_asked_is_terminated_and_then_killed(fake_chrome, monkeypatch, mode, ended_by):
    monkeypatch.setenv("FAKE_BROWSER", mode)
    with browser.plain_window("boss", URL, headless=True):
        pass
    assert fake_chrome[0].returncode == -ended_by and lock_is_free() and not browser._alive
    assert not (profile() / "DevToolsActivePort").exists()   # a killed browser cannot remove its port file; the window does


@process
def test_a_browser_that_never_listens_is_stopped_without_waiting_for_a_goodbye(fake_chrome, monkeypatch):
    monkeypatch.setenv("FAKE_BROWSER", "silent")
    monkeypatch.setattr(browser, "START_SECONDS", 0.5)
    monkeypatch.setattr(browser, "QUIT_SECONDS", 30)   # a wait for Browser.close would show: it never had a socket
    begun = time.monotonic()
    with pytest.raises(browser.BrowserUnavailable, match="did not start in time"):
        with browser.plain_window("boss", URL, headless=True):
            pass
    assert time.monotonic() - begun < 10 and fake_chrome[0].returncode == -signal.SIGTERM and lock_is_free()


@process
def test_a_browser_that_quits_at_once_is_reported_not_waited_for(fake_chrome, monkeypatch):
    monkeypatch.setenv("FAKE_BROWSER", "exit")   # e.g. the profile is open elsewhere and that window took the job over
    begun = time.monotonic()
    with pytest.raises(browser.BrowserUnavailable, match="quit right after it started"):
        with browser.plain_window("boss", URL, headless=True):
            pass
    assert time.monotonic() - begun < 10 and lock_is_free()


@process
@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_the_process_is_gone_after_any_exception_in_the_block(fake_chrome, error):
    with pytest.raises(error):
        with browser.plain_window("boss", URL, headless=True):
            raise error("stop")
    assert fake_chrome[0].poll() is not None and lock_is_free() and not browser._alive


@process
def test_no_browser_means_no_lock_taken_and_no_process(monkeypatch):
    monkeypatch.setattr(browser, "find_browser", lambda: None)
    with pytest.raises(browser.BrowserUnavailable, match="No browser found"):
        with browser.plain_window("boss", URL):
            pass
    assert lock_is_free() and not browser._alive


@process
def test_a_browser_of_ours_left_on_the_profile_is_closed_before_a_new_one_starts(fake_chrome):
    orphan = subprocess.Popen([str(fake_chrome.script), f"--user-data-dir={profile()}", "--remote-debugging-port=0", "about:blank"])
    deadline = time.monotonic() + 10
    while browser._devtools_address(profile()) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert orphan.poll() is None   # the crashed run's browser is up
    with browser.plain_window("boss", URL, headless=True) as window:
        assert orphan.wait(5) == 0   # it quit when asked, before the new one started
        assert window.url() == URL and fake_chrome[1].poll() is None


@process
def test_an_interpreter_that_exits_with_a_window_open_closes_it(fake_chrome, tmp_path):
    code = f"""
import threading, time
from pathlib import Path
from djs import browser
browser.find_browser = lambda: ('chrome', Path({str(fake_chrome.script)!r}))

def hold():
    with browser.plain_window('boss', {URL!r}, headless=True):
        time.sleep(60)

threading.Thread(target=hold, daemon=True).start()
while not (browser._alive and next(iter(browser._alive)).cdp):
    time.sleep(0.05)
print(next(iter(browser._alive)).proc.pid, flush=True)
"""   # the main thread ends: the interpreter exits while the window is still open in a daemon thread
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "DJS_HOME": str(tmp_path / "other-home")})
    assert out.returncode == 0, out.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(int(out.stdout.split()[-1]), 0)   # the browser went with the interpreter


# ============================================================ the whole tree, within one deadline
def start_browser(fake_chrome):
    """A `_Browser` on the fake browser with its DevTools socket, as _own_window builds one."""
    command = [str(fake_chrome.script), f"--user-data-dir={profile()}", "--remote-debugging-port=0", URL]
    started = browser._Browser(command, profile())
    deadline = time.monotonic() + 15
    while (address := browser._devtools_address(profile())) is None:
        assert time.monotonic() < deadline, "the fake browser did not start"
        time.sleep(0.05)
    started.cdp = browser._Cdp(address)
    return started


def nothing_reaches_it(monkeypatch):
    """No signal gets to the browser from now on, as if it sat in an uninterruptible sleep; returns the real sender, for the
    test to clean up with."""
    real = browser._signal_tree
    monkeypatch.setattr(browser, "_signal_tree", lambda *args: None)
    return real


@process
def test_the_browser_leads_a_group_of_its_own(fake_chrome):
    with browser.plain_window("boss", URL, headless=True):
        pid = fake_chrome[0].pid
        assert os.getpgid(pid) == os.getsid(pid) == pid != os.getpgrp()   # signalling it never reaches this process


@process
@pytest.mark.parametrize("modes, main_ended_by", [
    ("stuck,helper", -signal.SIGTERM),           # ignores Browser.close: SIGTERM to the group ends it and its helper
    ("deaf,stubborn-helper", -SIGKILL),          # ignores SIGTERM too, and so does the helper: only the kill ends them
    ("helper", 0),                               # quits when asked; its helper stays, with the main process already gone
    ("stubborn-helper", 0)])                     # ... one that ignores SIGTERM
def test_what_the_browser_started_goes_with_it(fake_chrome, monkeypatch, modes, main_ended_by):
    monkeypatch.setenv("FAKE_BROWSER", modes)
    with browser.plain_window("boss", URL, headless=True):
        child = helper()
        assert alive(child) and os.getpgid(child) == fake_chrome[0].pid   # in the browser's group
        begun = time.monotonic()
    assert time.monotonic() - begun < 3 * browser.QUIT_SECONDS + 1
    assert fake_chrome[0].returncode == main_ended_by
    assert goes_away(child) and lock_is_free() and not browser._alive


@process
def test_a_browser_that_ignores_browser_close_and_sigterm_is_killed_within_the_deadline(fake_chrome, monkeypatch):
    monkeypatch.setenv("FAKE_BROWSER", "deaf")
    with browser.plain_window("boss", URL, headless=True):
        begun = time.monotonic()
    assert time.monotonic() - begun < 3 * browser.QUIT_SECONDS + 1   # one deadline for the three steps
    assert fake_chrome[0].returncode == -signal.SIGKILL and lock_is_free() and not browser._alive


@process
def test_a_frozen_browser_that_never_answers_is_ended_within_the_deadline_too(fake_chrome, monkeypatch):
    monkeypatch.setenv("FAKE_BROWSER", "deaf")   # (BSD wakes a stopped process for SIGTERM, Linux does not: be deaf to it)
    with browser.plain_window("boss", URL, headless=True):
        os.kill(fake_chrome[0].pid, signal.SIGSTOP)   # no answer to Browser.close, none to the socket's goodbye
        begun = time.monotonic()
    assert time.monotonic() - begun < 3 * browser.QUIT_SECONDS + 1
    assert fake_chrome[0].returncode == -signal.SIGKILL and lock_is_free() and not browser._alive


@process
def test_close_is_safe_to_call_twice_and_from_two_threads(fake_chrome, monkeypatch):
    started = start_browser(fake_chrome)
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(started.close())) for _ in range(2)]   # owner and exit handler
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert answers == [True, True] and fake_chrome[0].returncode == 0 and not browser._alive
    monkeypatch.setattr(browser, "_signal_tree", refuse)   # what has gone is not signalled again: its ids may be reused
    assert started.close() is True


@process
def test_close_says_so_when_a_process_outlives_the_deadline(fake_chrome, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_BROWSER", "deaf")
    started = start_browser(fake_chrome)
    real_signal = nothing_reaches_it(monkeypatch)
    begun = time.monotonic()
    try:
        assert started.close() is False and started.running() and started in browser._alive
        assert time.monotonic() - begun < 3 * browser.QUIT_SECONDS + 1   # it gave up on time instead of waiting for ever
        said = capsys.readouterr().err
        assert f"pid {started.proc.pid}" in said and "still running" in said
    finally:
        real_signal(started.proc, True, 1)
    assert started.close() is True and not browser._alive   # once it has really gone, the next close() settles it


@process
def test_a_profile_whose_browser_could_not_be_ended_stays_busy_until_it_has_gone(fake_chrome, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_BROWSER", "deaf")
    real_signal = nothing_reaches_it(monkeypatch)
    with browser.plain_window("boss", URL, headless=True):
        pass   # the block itself ends normally: a login made in it is not thrown away over this
    proc = fake_chrome[0]
    try:
        assert "still running" in capsys.readouterr().err
        with pytest.raises(browser.BrowserBusy):
            lock_is_free()   # not free for a crawl in a browser either
        with pytest.raises(browser.BrowserBusy):
            with browser.plain_window("boss", URL, headless=True):
                pass   # and no second browser is started on it
        assert len(fake_chrome) == 1 and browser.profiles_in_use() == ["boss"]
    finally:
        real_signal(proc, True, 1)
        proc.wait(10)
    monkeypatch.delenv("FAKE_BROWSER")
    assert lock_is_free() and not browser._alive   # gone: free again, and the leftover is forgotten
    monkeypatch.setattr(browser, "_signal_tree", real_signal)
    with browser.plain_window("boss", URL, headless=True) as window:
        assert window.url() == URL


@process
def test_the_goodbye_to_a_frozen_browser_that_cannot_be_ended_is_bounded_too(fake_chrome, monkeypatch):
    started = start_browser(fake_chrome)
    os.kill(fake_chrome[0].pid, signal.SIGSTOP)
    real_signal = nothing_reaches_it(monkeypatch)
    begun = time.monotonic()
    try:
        assert started.close() is False
        assert time.monotonic() - begun < 3 * browser.QUIT_SECONDS + 4   # the socket's goodbye waits 2 s, not its default 10
    finally:
        real_signal(started.proc, True, 1)
    assert started.close() is True


# ============================================================ Windows: its own group, ended with taskkill
class WindowsProc:
    """The browser process as the Windows path sees it: alive until a kill reaches it."""
    pid = 4242

    def __init__(self, unkillable=False):
        self.returncode, self.killed, self.unkillable = None, False, unkillable

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("chrome.exe", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        if not self.unkillable:
            self.returncode = 1


@pytest.fixture
def windows(monkeypatch):
    """The Windows code path on any machine: the platform, subprocess's Windows constants, and a subprocess that only
    records what is started and what taskkill is asked. `outcome` is what taskkill does: works, fails, missing, hangs."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(browser, "QUIT_SECONDS", 0.2)
    world = SimpleNamespace(proc=WindowsProc(), started=[], taskkill=[], outcome="works")

    def popen(command, **kwargs):
        world.started.append((command, kwargs))
        return world.proc

    def run(command, **kwargs):
        world.taskkill.append((command, kwargs))
        if world.outcome == "missing":
            raise FileNotFoundError("taskkill")
        if world.outcome == "hangs":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if world.outcome == "works":
            world.proc.returncode = 1   # the tree is gone
        return subprocess.CompletedProcess(command, 0 if world.outcome == "works" else 1)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", run)
    yield world
    browser._alive.clear()


def test_on_windows_the_browser_starts_in_a_process_group_of_its_own(windows, tmp_path):
    browser._Browser(["chrome.exe", "--x"], tmp_path)
    [(command, kwargs)] = windows.started
    assert command == ["chrome.exe", "--x"]
    assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP and "start_new_session" not in kwargs


def test_on_windows_the_whole_tree_is_ended_with_taskkill(windows, tmp_path):
    started = browser._Browser(["chrome.exe"], tmp_path)
    assert started.close() is True and not browser._alive
    [(command, kwargs)] = windows.taskkill
    assert command == ["taskkill", "/T", "/F", "/PID", "4242"]   # /T: the children too
    assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW   # no console window flashing up in the app
    assert kwargs["timeout"] <= browser.QUIT_SECONDS   # a hung taskkill cannot eat the steps that follow
    assert windows.proc.killed is False


@pytest.mark.parametrize("outcome", ["fails", "missing", "hangs"])
def test_on_windows_the_browser_itself_is_killed_when_taskkill_cannot(windows, tmp_path, outcome):
    windows.outcome = outcome
    assert browser._Browser(["chrome.exe"], tmp_path).close() is True
    assert len(windows.taskkill) == 1 and windows.proc.killed is True


def test_on_windows_a_browser_that_nothing_ends_is_reported(windows, tmp_path, capsys):
    windows.outcome, windows.proc = "fails", WindowsProc(unkillable=True)
    started = browser._Browser(["chrome.exe"], profile())
    begun = time.monotonic()
    assert started.close() is False and started.running() and started in browser._alive
    assert time.monotonic() - begun < 3 * browser.QUIT_SECONDS + 1
    assert len(windows.taskkill) == 2   # the terminate step and the kill step
    assert "pid 4242" in capsys.readouterr().err
    with pytest.raises(browser.BrowserBusy):
        with browser.profile_lock("boss", wait=0):   # the profile of the stuck browser is not free
            pass
