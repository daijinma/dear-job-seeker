"""The login window of djs.browser against a stand-in for the browser's DevTools endpoint (tests/fake_devtools.py): the
commands it sends, what it does when the browser quits or does not answer, the user's own browser (DJS_CDP_URL), a browser
left over by a crashed run, and a browser process that must be gone whatever it does or whatever happens to the caller.
No real browser; test_auth_browser.py has one."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import fake_devtools
from djs import browser, db
from fake_devtools import DevTools

process = pytest.mark.skipif(sys.platform == "win32", reason="the fake browser is a script")
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
    return started


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
@pytest.mark.parametrize("mode, ended_by", [("stuck", signal.SIGTERM), ("deaf", signal.SIGKILL)])
def test_a_browser_that_does_not_quit_when_asked_is_terminated_and_then_killed(fake_chrome, monkeypatch, mode, ended_by):
    monkeypatch.setenv("FAKE_BROWSER", mode)
    with browser.plain_window("boss", URL, headless=True):
        pass
    assert fake_chrome[0].returncode == -ended_by and lock_is_free() and not browser._alive


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
