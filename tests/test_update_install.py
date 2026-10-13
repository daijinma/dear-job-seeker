"""Installing a downloaded update: the real macOS helper (djs/apply.sh, with ditto) on plain folders named X.app under
paths with spaces and Chinese, with `open` and `xattr` shimmed so no app starts; what update.py hands the helpers
(argv of both platforms), the result file and its texts, the reasons not to install. The Windows helper only runs in CI
(packaging/update-check.sh). Offline, everything under tmp."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from djs import update

OURS, NEW = "1.2.6", "1.4.0"
APP_HELPER = Path(update.__file__).with_name("apply.sh")
mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="the helper uses macOS's ditto")


# ---- fake builds and the shims in front of `open` and `xattr`
def make_app(path: Path, mark: str) -> Path:
    """A stand-in for an app bundle: a folder with the program inside and a file saying which build it is."""
    (path / "Contents" / "MacOS").mkdir(parents=True)
    (path / "Contents" / "MacOS" / "djs").write_text("#!/bin/sh\n", encoding="utf-8")
    (path / "Contents" / "MacOS" / "找岗位助手").write_text("#!/bin/sh\n", encoding="utf-8")
    (path / "mark.txt").write_text(mark, encoding="utf-8")
    return path


def mark_of(path: Path) -> str:
    return (path / "mark.txt").read_text(encoding="utf-8")


def shim(directory: Path, name: str, body: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text("#!/bin/sh\n" + body, encoding="utf-8")
    (directory / name).chmod(0o755)


@pytest.fixture
def mac(tmp_path):
    """Folders with awkward names, and the environment a helper runs in: `open` and `xattr` log their arguments."""
    odd = tmp_path / "my apps 应用's $x"
    log, bin_ = tmp_path / "shims.log", tmp_path / "bin"
    for name in ("open", "xattr"):
        shim(bin_, name, f'echo "{name} $*" >> "$SHIM_LOG"\n')
    env = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "SHIM_LOG": str(log)}
    updates = tmp_path / "数据 data" / "updates"
    updates.mkdir(parents=True)
    return SimpleNamespace(
        env=env, bin=bin_, updates=updates, previous=updates / "previous", result=updates / "last.json",
        app=make_app(odd / "找岗位助手.app", "old"), new=make_app(updates / NEW / "stage" / "找岗位助手.app", "new"),
        calls=lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def run_helper(mac, pid, *, new=None, ticks=None, version=NEW):
    env = {**mac.env, **({"APPLY_TICKS": str(ticks)} if ticks else {})}
    return subprocess.run(["/bin/sh", str(APP_HELPER), str(pid), str(mac.app), str(new or mac.new), str(mac.previous),
                           str(mac.result), version], env=env, capture_output=True, text=True, timeout=60)


def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def reported(mac) -> dict:
    return json.loads(mac.result.read_text(encoding="utf-8"))


# ---- the macOS helper itself
@mac_only
def test_the_swap_puts_the_new_build_in_place_keeps_the_old_one_and_starts_it(mac):
    r = run_helper(mac, dead_pid())
    assert r.returncode == 0, r.stderr
    assert mark_of(mac.app) == "new" and (mac.app / "Contents" / "MacOS" / "找岗位助手").is_file()
    assert mark_of(mac.previous / "找岗位助手.app") == "old"
    assert [p.name for p in mac.previous.iterdir()] == ["找岗位助手.app"]
    assert mac.calls() == [f"xattr -dr com.apple.quarantine {mac.app}", f"open {mac.app}"]
    assert reported(mac) == {"ok": True, "code": "ok", "version": NEW, "who": []}
    assert mark_of(mac.new) == "new"   # the staged build is copied, not consumed


@mac_only
def test_the_helper_waits_for_the_app_to_quit(mac):
    # a process that is not our child (a child of ours would stay a zombie, and a zombie still answers kill -0)
    pid = int(subprocess.run(["/bin/sh", "-c", "sleep 1 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True).stdout)
    start = time.monotonic()
    r = run_helper(mac, pid)
    assert r.returncode == 0 and reported(mac)["ok"] is True
    assert time.monotonic() - start >= 0.5 and mark_of(mac.app) == "new"


@mac_only
def test_a_failed_copy_puts_the_old_build_back_and_says_so(mac):
    shim(mac.bin, "ditto", "exit 1\n")
    r = run_helper(mac, dead_pid())
    assert r.returncode == 1
    assert mark_of(mac.app) == "old" and (mac.app / "Contents" / "MacOS" / "djs").is_file()
    assert list(mac.previous.iterdir()) == []
    assert mac.calls() == [f"open {mac.app}"]   # the old one is started again
    assert reported(mac) == {"ok": False, "code": "swap_failed", "version": NEW, "who": []}


@mac_only
def test_a_half_copy_is_removed_before_the_old_build_comes_back(mac):
    shim(mac.bin, "ditto", 'mkdir -p "$2/half"\nexit 1\n')   # copies some of it, then fails
    run_helper(mac, dead_pid())
    assert mark_of(mac.app) == "old" and not (mac.app / "half").exists()


@mac_only
def test_an_app_that_does_not_quit_is_left_alone(mac):
    r = run_helper(mac, os.getpid(), ticks=2)
    assert r.returncode == 1
    assert mark_of(mac.app) == "old" and not mac.previous.exists()
    assert mac.calls() == []   # nothing is started: the app is still there
    assert reported(mac) == {"ok": False, "code": "still_running", "version": NEW, "who": []}


@mac_only
def test_the_next_update_replaces_the_previous_build(mac):
    run_helper(mac, dead_pid(), version="1.3.0", new=make_app(mac.updates / "1.3.0" / "stage" / "找岗位助手.app", "mid"))
    assert mark_of(mac.previous / "找岗位助手.app") == "old"
    run_helper(mac, dead_pid())
    assert mark_of(mac.app) == "new" and mark_of(mac.previous / "找岗位助手.app") == "mid"
    assert [p.name for p in mac.previous.iterdir()] == ["找岗位助手.app"]


@mac_only
def test_install_now_starts_the_helper_with_the_arguments_it_expects(mac, installed, monkeypatch):
    """Through update.install_now() into the real helper: the arguments agree in number and order."""
    real = subprocess.Popen
    gone = dead_pid()

    def start(cmd, **kw):   # the app's own pid never ends in a test: hand over one that has
        assert kw["start_new_session"] is True
        real([*cmd[:2], str(gone), *cmd[3:]], env=mac.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait()
    monkeypatch.setattr(subprocess, "Popen", start)
    update.later()
    update.install_now()
    assert mark_of(installed.root) == "new" and mark_of(installed.updates / "previous" / "找岗位助手.app") == "old"
    assert installed.last()["text"] == f"已更新到 {NEW}"
    assert mac.calls()[-1] == f"open {installed.root}"


# ---- what update.py hands the helper
@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A packaged app 1.2.6 under tmp whose update 1.4.0 is downloaded; the running program is inside `root`."""
    home = tmp_path / "data"
    root = make_app(tmp_path / "应用 apps" / "找岗位助手.app", "old")
    new = make_app(home / "updates" / NEW / "stage" / "找岗位助手.app", "new")
    monkeypatch.setenv("DJS_HOME", str(home))
    monkeypatch.delenv("DJS_UPDATE", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(root / "Contents" / "MacOS" / "找岗位助手"))
    monkeypatch.setattr(update, "_version", lambda: OURS)
    monkeypatch.setattr(update, "_mem", {"state": "ready", "latest": NEW, "note": ""})
    monkeypatch.setattr(update, "_swept", False)

    def disk():
        return json.loads((home / "updates" / "state.json").read_text(encoding="utf-8"))
    return SimpleNamespace(home=home, updates=home / "updates", root=root, new=new, disk=disk, last=update.last_result)


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    return calls


def test_the_macos_helper_is_started_detached_with_every_path_as_an_argument(installed, spawned):
    update.later()
    update.install_now()
    [(cmd, kw)] = spawned
    updates = installed.updates
    assert cmd == ["/bin/sh", str(updates / "apply.sh"), str(os.getpid()), str(installed.root), str(installed.new),
                   str(updates / "previous"), str(updates / "last.json"), NEW]
    assert kw == {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "close_fds": True, "start_new_session": True}
    assert (updates / "apply.sh").read_text(encoding="utf-8") == APP_HELPER.read_text(encoding="utf-8")   # a copy
    assert installed.disk()["install_on_quit"] is False   # used up


def test_the_windows_helper_is_started_detached_with_named_parameters(installed, spawned, monkeypatch, tmp_path):
    folder = tmp_path / "程序 folder" / "dear-job-seeker"
    build = installed.updates / NEW / "stage" / "dear-job-seeker"
    build.mkdir(parents=True)
    (build / "djs.exe").write_bytes(b"MZ")
    folder.mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(folder / "找岗位助手.exe"))
    update.install_now()
    [(cmd, kw)] = spawned
    updates = installed.updates
    assert cmd == ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                   "-File", str(updates / "apply.ps1"), "-ProcessId", str(os.getpid()), "-AppDir", str(folder),
                   "-NewDir", str(build), "-PreviousDir", str(updates / "previous"), "-Result", str(updates / "last.json"),
                   "-Version", NEW, "-Exe", "找岗位助手.exe"]
    assert kw == {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "close_fds": True, "creationflags": 0x8 | 0x200 | 0x08000000}
    assert (updates / "apply.ps1").read_bytes() == Path(update.__file__).with_name("apply.ps1").read_bytes()


def test_a_helper_that_cannot_start_leaves_the_choice_for_next_time(installed, monkeypatch):
    def refuse(*a, **kw):
        raise OSError("no /bin/sh")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    update.later()
    with pytest.raises(OSError):
        update.install_now()
    assert installed.disk()["install_on_quit"] is True


def test_the_app_root_is_the_bundle_on_a_mac_and_the_folder_on_windows(installed, monkeypatch, tmp_path):
    assert update.app_root() == installed.root
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python"))   # no bundle around it
    assert update.app_root() is None
    monkeypatch.setattr(sys, "platform", "win32")
    assert update.app_root() == tmp_path / "bin"
    monkeypatch.setattr(sys, "platform", "linux")
    assert update.app_root() is None
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delattr(sys, "frozen")
    assert update.app_root() is None


# ---- when installing is not possible
def test_it_can_install_a_downloaded_build_from_a_folder_it_may_write_to(installed):
    assert update.can_install() == (True, "")


def test_nothing_ready_means_nothing_to_install(installed, monkeypatch, spawned):
    monkeypatch.setattr(update, "_mem", {"state": "downloading", "latest": NEW, "note": ""})
    assert update.can_install() == (False, "还没有下载好的新版本")
    with pytest.raises(ValueError, match="还没有下载好的新版本"):
        update.install_now()
    assert spawned == [] and not (installed.updates / "apply.sh").exists()


def test_a_source_checkout_cannot_install(installed, monkeypatch):
    monkeypatch.delattr(sys, "frozen")
    assert update.can_install() == (False, "开发版不能自动更新")


def test_an_app_run_from_a_translocated_copy_must_be_moved_first(installed, monkeypatch, tmp_path, spawned):
    odd = make_app(tmp_path / "private" / "AppTranslocation" / "A1B2" / "d" / "找岗位助手.app", "old")
    monkeypatch.setattr(sys, "executable", str(odd / "Contents" / "MacOS" / "找岗位助手"))
    assert update.can_install() == (False, "请先把程序拖进“应用程序”文件夹再更新")
    with pytest.raises(ValueError, match="应用程序"):
        update.install_now()
    assert spawned == []


def deny_write(monkeypatch, folder):
    real = os.access
    monkeypatch.setattr(os, "access", lambda p, mode, **kw: False if Path(p) == folder and mode == os.W_OK
                        else real(p, mode, **kw))


def test_a_folder_that_cannot_be_written_cannot_be_updated(installed, monkeypatch, spawned):
    deny_write(monkeypatch, installed.root.parent)
    assert update.can_install() == (False, "没有权限替换这个位置的程序")
    with pytest.raises(ValueError, match="没有权限"):
        update.install_now()
    assert spawned == []


# ---- quitting
def test_quitting_with_the_choice_made_installs(installed, spawned):
    update.later()
    update.apply_on_exit()
    assert len(spawned) == 1 and installed.disk()["install_on_quit"] is False


def test_quitting_without_the_choice_installs_nothing(installed, spawned):
    update.apply_on_exit()
    assert spawned == []


def test_a_failed_install_never_fails_the_quit_and_uses_up_the_choice(installed, spawned, monkeypatch, caplog):
    update.later()
    deny_write(monkeypatch, installed.root.parent)
    with caplog.at_level("ERROR", logger="djs.update"):
        update.apply_on_exit()   # raises nothing
    assert spawned == [] and installed.disk()["install_on_quit"] is False
    assert any("not installed at quit" in r.getMessage() for r in caplog.records)


def test_an_error_from_the_state_does_not_reach_the_caller(installed, monkeypatch):
    monkeypatch.setattr(update, "should_install_on_exit", lambda: 1 / 0)
    update.apply_on_exit()


# ---- what the next start says
@pytest.mark.parametrize("result, ok, text", [
    ({"ok": True, "code": "ok", "version": NEW, "who": []}, True, "已更新到 1.4.0"),
    ({"ok": False, "code": "swap_failed", "version": NEW, "who": []}, False, "更新没有完成，已恢复原来的版本"),
    ({"ok": False, "code": "locked", "version": NEW, "who": ["djs", "Claude"]}, False,
     "更新没有完成：djs、Claude 还在使用本程序，请先关闭后再试"),
    ({"ok": False, "code": "locked", "version": NEW, "who": []}, False, "更新没有完成：别的程序 还在使用本程序，请先关闭后再试"),
    ({"ok": False, "code": "locked", "version": NEW, "who": [None]}, False,   # what PowerShell writes for no name
     "更新没有完成：别的程序 还在使用本程序，请先关闭后再试"),
    ({"ok": False, "code": "still_running", "version": NEW, "who": []}, False, "更新没有完成：程序没能及时退出"),
    ({"ok": False, "code": "something new", "version": NEW}, False, "更新没有完成")])
def test_the_result_is_told_once_in_words(installed, result, ok, text):
    installed.updates.mkdir(parents=True, exist_ok=True)
    (installed.updates / "last.json").write_text(json.dumps(result), encoding="utf-8")
    assert update.last_result() == {"ok": ok, "version": NEW, "text": text}
    assert not (installed.updates / "last.json").exists()
    assert update.last_result() is None


def test_a_result_file_with_a_byte_order_mark_is_read(installed):
    installed.updates.mkdir(parents=True, exist_ok=True)
    (installed.updates / "last.json").write_bytes(b'\xef\xbb\xbf{"ok":true,"code":"ok","version":"1.4.0","who":[]}')
    assert update.last_result()["text"] == "已更新到 1.4.0"


@pytest.mark.parametrize("junk", ["", "not json", "[1, 2]", '"ok"', "null"])
def test_a_result_file_that_cannot_be_read_says_nothing_and_is_removed(installed, junk):
    installed.updates.mkdir(parents=True, exist_ok=True)
    (installed.updates / "last.json").write_text(junk, encoding="utf-8")
    assert update.last_result() is None
    assert not (installed.updates / "last.json").exists()


def test_no_result_file_says_nothing(installed):
    assert update.last_result() is None


def test_after_an_install_the_download_goes_and_the_previous_build_stays(installed, monkeypatch):
    previous = installed.updates / "previous" / "找岗位助手.app"
    make_app(previous, "old")
    (installed.updates / "last.json").write_text('{"ok":true,"code":"ok","version":"1.4.0","who":[]}', encoding="utf-8")
    update._save(ready_version=NEW)
    monkeypatch.setattr(update, "_version", lambda: NEW)   # the new build is running
    monkeypatch.setattr(update, "_mem", {"state": "none", "latest": None, "note": ""})
    assert update.last_result()["ok"] is True and update.current()["state"] == "none"
    assert not (installed.updates / NEW).exists() and mark_of(previous) == "old"
    assert installed.disk()["ready_version"] is None
