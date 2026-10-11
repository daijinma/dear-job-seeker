"""The cross-platform file lock behind request pacing and the browser-profile lock."""
import os
import subprocess
import sys

import pytest

from djs import browser, filelock


def test_second_holder_times_out_and_gets_the_lock_once_it_is_released(tmp_path):
    path = tmp_path / "x.lock"
    with open(path, "a+") as a, open(path, "a+") as b:
        with filelock.locked(a):
            with pytest.raises(filelock.LockTimeout):
                with filelock.locked(b, timeout=0.1):
                    pass
        with filelock.locked(b, timeout=0):
            pass


def test_data_in_a_locked_file_stays_readable_and_writable(tmp_path):
    with open(tmp_path / "pace", "a+") as f, filelock.locked(f):
        f.seek(0)
        f.truncate()
        f.write("12.5")
        f.seek(0)
        assert f.read() == "12.5"


def test_busy_browser_profile_raises_browser_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    with browser.profile_lock("boss"):
        with pytest.raises(browser.BrowserBusy):
            with browser.profile_lock("boss", wait=0):
                pass
    with browser.profile_lock("boss", wait=0):  # free again once the first holder is done
        pass


def test_request_spacing_holds_across_processes(tmp_path):
    code = "import time; from djs import pacer; pacer.wait('p', 'direct', 0.3, 0); print(time.time())"
    env = {**os.environ, "DJS_HOME": str(tmp_path)}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True)
             for _ in range(3)]
    stamps = sorted(float(p.communicate()[0]) for p in procs)
    assert all(b - a >= 0.25 for a, b in zip(stamps, stamps[1:]))
