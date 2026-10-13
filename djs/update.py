"""Self-update, the part that needs no restart: ask GitHub whether the latest release is newer than this build, download
the zip made for this computer in the background, check it (size, sha256, the build's own `--selftest`) and keep it
unzipped in <data>/updates/<version>/stage until the user installs it. Nothing touches the running app until install_now():
that starts a helper script (apply.sh, apply.ps1) which swaps the build after the app has quit and starts the new one.

State for the window and the `status` tool comes from current(); the daemon loop is start(). A source checkout never
checks, and DJS_UPDATE=0 switches it off. Text for the user (`note`) is Chinese, the log gets the English detail."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from djs import db, log

API = "https://api.github.com/repos/daijinma/dear-job-seeker"
# The name of this build's zip in a release (also what the release job checks; works as a regex and as grep -E)
ASSET_PATTERN = r"dear-job-seeker-[0-9]+(\.[0-9]+)*-(macos-arm64|macos-x86_64|windows-x64)\.zip"
APP, FOLDER = "找岗位助手.app", "dear-job-seeker"   # what the zip holds: the macOS bundle, the Windows folder
EXE = "找岗位助手.exe"   # the window's program in that folder
INTERVAL = 30 * 60
JITTER = 5            # seconds: the first check comes 0-5 s after start()
TIMEOUT = 20
SELFTEST_TIMEOUT = 30
MAX_BACKOFF = 24 * 3600
CHUNK = 1 << 16
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # a console program started from the window must not flash one
NOTE_VERIFY = "下载的文件没通过校验"
NOTE_NO_DIGEST = "发布页没有校验值"
NOTE_SELFTEST = "新版本没通过自检"
WHY_NOT_READY = "还没有下载好的新版本"
WHY_SOURCE = "开发版不能自动更新"
WHY_MOVE_FIRST = "请先把程序拖进“应用程序”文件夹再更新"
WHY_READ_ONLY = "没有权限替换这个位置的程序"
# What the helper's result file (its `code`) says at the next start
RESULT_TEXT = {"ok": "已更新到 {version}", "swap_failed": "更新没有完成，已恢复原来的版本",
               "locked": "更新没有完成：{who} 还在使用本程序，请先关闭后再试", "still_running": "更新没有完成：程序没能及时退出"}
NOTE_NOT_DONE = "更新没有完成"
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW: the helper outlives the app and shows no window
DETACHED = 0x00000008 | 0x00000200 | 0x08000000

LOG = log.get("update")
_mem = {"state": "none", "latest": None, "note": ""}   # none | downloading | ready | failed; the rest is in state.json
_guard = threading.RLock()       # the memory state and state.json (never held across the network)
_check_lock = threading.Lock()   # one check at a time
_job: str | None = None          # the version being downloaded: a newer release replaces it
_resume_at = 0.0                 # no request before this (epoch seconds): GitHub said 403/429
_thread: threading.Thread | None = None
_swept = False
_sleep = threading.Event().wait  # seconds -> True when told to stop (never: the thread is a daemon)
_now = time.time


def _version() -> str | None:
    from djs import service   # late: service reports our state in `status`
    return service.app_version()


def disabled() -> bool:
    """A source checkout (no installed package, or not the packaged app) never updates; DJS_UPDATE=0 turns it off."""
    return os.environ.get("DJS_UPDATE") == "0" or _version() is None or not getattr(sys, "frozen", False)


def target() -> str:
    """The kind of build this process is: the suffix of its zip's name in a release."""
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else f"macos-{machine}"
    if sys.platform == "win32":
        return "windows-x64" if machine in ("amd64", "x86_64") else f"windows-{machine}"
    return f"{sys.platform}-{machine}"


def _api() -> str:
    return (os.environ.get("DJS_UPDATE_API") or API).rstrip("/")


def _client() -> httpx.Client:
    """The one place the HTTP transport is made (tests swap it); the user's system proxy stays in use."""
    return httpx.Client(trust_env=True)


def _spawn(job) -> None:
    threading.Thread(target=job, name="djs-update-download", daemon=True).start()


# ---- versions
def _parse(v) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in str(v).strip().lstrip("vV").split("."))
    except ValueError:
        return None


def _newer(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# ---- what is kept on disk
def _dir() -> Path:
    return db.home() / "updates"


def _load() -> dict:
    try:
        data = json.loads((_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"install_on_quit": bool(data.get("install_on_quit")), "etag": data.get("etag") or None,
            "ready_version": data.get("ready_version") or None}


def _save(**changes) -> None:
    """Change keys of state.json: written beside it, then moved over it, so a reader never sees half a file."""
    with _guard:
        try:
            _dir().mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=_dir(), prefix="state.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({**_load(), **changes}, f)
                os.replace(tmp, _dir() / "state.json")
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as e:
            LOG.warning("cannot write the update state: %s", e)


def _versions() -> list[Path]:
    """The version folders in <data>/updates (`previous` and the files there are not)."""
    try:
        return [d for d in _dir().iterdir() if d.is_dir() and _parse(d.name) is not None]
    except OSError:
        return []


def _console(build: Path) -> Path:
    return build / "djs.exe" if sys.platform == "win32" else build / "Contents" / "MacOS" / "djs"


def _build(stage: Path) -> Path | None:
    """The build inside an unzipped release (the .app, the Windows folder) if it has its console program."""
    name = FOLDER if sys.platform == "win32" else APP
    try:
        found = [p for p in (stage / name, *stage.glob(f"*/{name}")) if _console(p).is_file()]
    except OSError:
        return None
    return found[0] if found else None


def _restore() -> None:
    """Take up what an earlier run left ready and sweep what is not newer than this build. Cheap, called often: the
    `djs` program of an assistant has no download of its own and learns the state the app left here."""
    global _swept
    cur = _parse(_version())
    if cur is None:
        return
    with _guard:
        if not _swept:
            _swept = True
            for d in _versions():
                if not _newer(_parse(d.name), cur):
                    shutil.rmtree(d, ignore_errors=True)
        ready = _load()["ready_version"]
        if ready is None or _mem["state"] != "none":
            return
        v = _parse(ready)
        if v is not None and _newer(v, cur) and _build(_dir() / ready / "stage"):
            _mem.update(state="ready", latest=ready, note="")
        else:
            _save(ready_version=None)
            shutil.rmtree(_dir() / ready, ignore_errors=True)


# ---- the public state
def current() -> dict:
    """What the window and `status` show. Never touches the network."""
    if not disabled():
        _restore()
    with _guard:
        return {"current": _version(), "latest": _mem["latest"], "state": _mem["state"], "note": _mem["note"],
                "install_on_quit": _load()["install_on_quit"]}


def later() -> None:
    """The user chose to install when the app next quits."""
    _save(install_on_quit=True)


def should_install_on_exit() -> bool:
    return current()["state"] == "ready" and _load()["install_on_quit"]


def staged() -> Path | None:
    """The new build (the .app, the Windows folder) when it is downloaded, verified and has passed its self-test."""
    if current()["state"] != "ready":
        return None
    return _build(_dir() / _mem["latest"] / "stage")


# ---- checking
def check(manual: bool = False) -> dict:
    """One check now. A newer release starts its download (in a thread) unless that one is already downloading or
    ready. `manual` ignores the ETag and tries a release again whose download failed verification. Offline, rate
    limited or any other failure: nothing changes and nothing is said (the log has it). `found` tells whether an update
    is downloading or ready."""
    if not disabled():
        _restore()
        with _check_lock:
            try:
                _check(manual)
            except Exception:
                LOG.exception("update check crashed")
    cur = current()
    return {"found": cur["state"] in ("downloading", "ready"), **cur}


def _reset_time(r: httpx.Response) -> float:
    """When a refused request may be repeated: Retry-After (seconds or a date) or X-RateLimit-Reset (epoch seconds)."""
    now, at = _now(), []
    after = r.headers.get("Retry-After")
    if after:
        try:
            at.append(now + float(after))
        except ValueError:
            try:
                at.append(parsedate_to_datetime(after).timestamp())
            except (TypeError, ValueError):
                pass
    try:
        at.append(float(r.headers["X-RateLimit-Reset"]))
    except (KeyError, ValueError):
        pass
    return min(max(at, default=now + INTERVAL), now + MAX_BACKOFF)


def _check(manual: bool) -> None:
    global _resume_at
    if _now() < _resume_at:
        LOG.info("update check skipped: GitHub asked to wait another %d s", _resume_at - _now())
        return
    ours = _version()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"dear-job-seeker/{ours}"}
    etag = _load()["etag"]
    if etag and not manual:
        headers["If-None-Match"] = etag
    try:
        with _client() as client:
            r = client.get(f"{_api()}/releases/latest", headers=headers, timeout=TIMEOUT, follow_redirects=True)
    except (httpx.HTTPError, OSError) as e:
        LOG.info("update check failed: %s", e)
        return
    if r.status_code == 304:
        LOG.info("update check: nothing new since the last one")
    elif r.status_code in (403, 429):
        _resume_at = _reset_time(r)
        LOG.info("update check refused (HTTP %s): no more requests for %d s", r.status_code, _resume_at - _now())
    elif r.status_code != 200:
        LOG.info("update check: HTTP %s", r.status_code)
    else:
        try:
            release = r.json()
        except ValueError:
            LOG.info("update check: the answer is not JSON")
            return
        _found(release if isinstance(release, dict) else {}, r.headers.get("ETag"), manual, ours)


def _found(release: dict, etag: str | None, manual: bool, ours: str | None) -> None:
    """Decide on the latest release. The ETag is kept only for an answer that needs no more work (so that a 304 never
    hides a download that still has to happen)."""
    global _job
    ver = str(release.get("tag_name") or "").lstrip("vV")
    new, old = _parse(ver), _parse(ours)
    if release.get("draft") or release.get("prerelease") or new is None or old is None or not _newer(new, old):
        LOG.info("update check: %s is not newer than %s", ver or "?", ours)
        _save(etag=etag)
        return
    name = f"dear-job-seeker-{ver}-{target()}.zip"
    asset = next((a for a in release.get("assets") or [] if isinstance(a, dict) and a.get("name") == name), None)
    if asset is None or not asset.get("browser_download_url"):
        LOG.info("release %s has no %s (yet)", ver, name)
        _save(etag=None)
        return
    with _guard:
        if _mem["latest"] == ver and (_mem["state"] in ("downloading", "ready") or _mem["state"] == "failed" and not manual):
            return
        _mem.update(state="downloading", latest=ver, note="")
        _job = ver
    _save(etag=None, ready_version=None)
    for d in _versions():   # an older download or ready build is not wanted any more
        if d.name != ver:
            shutil.rmtree(d, ignore_errors=True)
    LOG.info("update %s found, downloading %s", ver, name)
    _spawn(lambda: _fetch(ver, asset, etag))


# ---- downloading
def _fail(ver: str, note: str, etag: str | None, why: str) -> None:
    """The release is refused, for good: it is not tried again until the user asks or a newer one comes."""
    LOG.warning("update %s refused: %s", ver, why)
    with _guard:
        if _job == ver:
            _mem.update(state="failed", latest=ver, note=note)
    _save(etag=etag)


def _idle(ver: str) -> None:
    """Back to nothing (the download resumes at the next check)."""
    with _guard:
        if _job == ver:
            _mem.update(state="none", latest=None, note="")


def _fetch(ver: str, asset: dict, etag: str | None) -> None:
    """Download, verify and stage one release asset; the body of the download thread."""
    try:
        size, digest = asset.get("size"), str(asset.get("digest") or "")
        if not isinstance(size, int) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            return _fail(ver, NOTE_NO_DIGEST, etag, "the release lists no size or sha256 for the asset")
        folder = _dir() / ver
        zipped = folder / asset["name"]
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if not _download(ver, asset["browser_download_url"], zipped, size):
                shutil.rmtree(folder, ignore_errors=True)   # a newer release took over
                return
        except (httpx.HTTPError, OSError) as e:
            LOG.info("download of %s interrupted, it resumes at the next check: %s", ver, e)
            return _idle(ver)
        sha = hashlib.sha256()
        with zipped.open("rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                sha.update(block)
        if zipped.stat().st_size != size or sha.hexdigest() != digest[7:].lower():
            shutil.rmtree(folder, ignore_errors=True)
            return _fail(ver, NOTE_VERIFY, etag, f"size or sha256 of {zipped.name} differ from the release's")
        stage = folder / "stage"
        shutil.rmtree(stage, ignore_errors=True)
        try:
            _unzip(zipped, stage)
        except (zipfile.BadZipFile, ValueError) as e:
            shutil.rmtree(folder, ignore_errors=True)
            return _fail(ver, NOTE_VERIFY, etag, f"cannot unzip {zipped.name}: {e}")
        build = _build(stage)
        if build is None or not _selftest(build):
            shutil.rmtree(folder, ignore_errors=True)
            return _fail(ver, NOTE_SELFTEST, etag, "the staged build did not pass its self-test")
        zipped.unlink(missing_ok=True)
        with _guard:
            if _job != ver:
                return
            _save(ready_version=ver, etag=etag)
            _mem.update(state="ready", latest=ver, note="")
        LOG.info("update %s is ready in %s", ver, stage)
    except Exception:
        LOG.exception("update %s failed", ver)
        _idle(ver)


def _download(ver: str, url: str, zipped: Path, size: int) -> bool:
    """Stream `url` into <zipped>.part (resuming one that is there) and move it to `zipped`. False when a newer release
    took over meanwhile."""
    part = zipped.with_name(zipped.name + ".part")
    if zipped.is_file():   # complete, but an earlier run ended before it was checked
        return True
    for _ in range(2):   # a part that does not fit the file gets one clean restart
        have = part.stat().st_size if part.is_file() else 0
        if have > size:
            part.unlink()
            have = 0
        if have == size:
            break
        headers = {"User-Agent": f"dear-job-seeker/{_version()}", "Accept-Encoding": "identity"}
        if have:
            headers["Range"] = f"bytes={have}-"
        with _client() as client, client.stream("GET", url, headers=headers, timeout=TIMEOUT, follow_redirects=True) as r:
            start = re.match(r"bytes (\d+)-", r.headers.get("Content-Range", ""))
            if have and (r.status_code == 416 or r.status_code == 206 and not (start and int(start[1]) == have)):
                part.unlink()
                continue
            r.raise_for_status()
            with part.open("ab" if have and r.status_code == 206 else "wb") as f:   # 200: the server ignored Range
                for chunk in r.iter_bytes():   # as they arrive: what came before a break is kept
                    if _job != ver:
                        return False
                    f.write(chunk)
        break
    os.replace(part, zipped)
    return True


# ---- staging
def _inside(root: Path, base: Path, name: str) -> Path:
    path = Path(os.path.normpath(base / name))
    if path != root and root not in path.parents:
        raise ValueError(f"{name!r} leaves the folder")
    return path


def _unzip(zipped: Path, dest: Path) -> None:
    """Unzip as `ditto` would: names are UTF-8 (a zip made on macOS does not say so), symlinks and file modes are
    kept. ValueError for an entry that would land outside `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zipped) as z:
        for info in z.infolist():
            name = info.filename
            if not info.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("utf-8")
                except UnicodeError:
                    pass
            path = _inside(root, root, name)
            real = Path(os.path.realpath(path.parent))
            if real != root and root not in real.parents:   # through a symlink of the zip itself
                raise ValueError(f"{name!r} leaves the folder")
            mode = info.external_attr >> 16
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                if os.name == "posix" and mode:
                    path.chmod(mode & 0o777 | 0o700)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link = z.read(info).decode("utf-8")
                _inside(root, path.parent, link)
                path.unlink(missing_ok=True)
                os.symlink(link, path)
                real = Path(os.path.realpath(path))   # links that look inside one by one can add up to a way out
                if real != root and root not in real.parents:
                    path.unlink()
                    raise ValueError(f"{name!r} leads out of the folder")
                continue
            if path.is_symlink():   # a file of that name replaces the link, it is not written through it
                path.unlink()
            with z.open(info) as src, path.open("wb") as out:
                shutil.copyfileobj(src, out)
            if os.name == "posix" and mode:
                path.chmod(mode & 0o777 | 0o600)


def _selftest(build: Path) -> bool:
    """Run the new build's own check, as the release job does: it must say `selftest ok`."""
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}   # another packaged program, not a child of this one
    try:
        r = subprocess.run([str(_console(build)), "--selftest"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=SELFTEST_TIMEOUT, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                           env=env)
    except (OSError, subprocess.SubprocessError) as e:
        LOG.warning("self-test of %s did not run: %s", build, e)
        return False
    if "selftest ok" not in r.stdout:
        LOG.warning("self-test of %s said no (exit %s): %s", build, r.returncode, (r.stdout + r.stderr)[-500:])
    return "selftest ok" in r.stdout


# ---- installing
def app_root() -> Path | None:
    """The installed build that runs this process: the .app bundle (macOS) or the folder of the window's program
    (Windows). None for a source checkout."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    if sys.platform == "darwin":
        return next((p for p in exe.parents if p.suffix == ".app"), None)
    return exe.parent if sys.platform == "win32" else None


def _plan() -> tuple[Path, Path, str]:
    """What install_now() swaps: (the staged build, the installed one, the new version). ValueError with the reason (for
    the user, in Chinese) when it cannot."""
    new = staged()
    if new is None:
        raise ValueError(WHY_NOT_READY)
    root = app_root()
    if root is None:
        raise ValueError(WHY_SOURCE)
    if "/AppTranslocation/" in root.as_posix():   # a downloaded app run from where it was unzipped: macOS runs a copy
        raise ValueError(WHY_MOVE_FIRST)
    if not os.access(root.parent, os.W_OK):       # installed by an administrator
        raise ValueError(WHY_READ_ONLY)
    return new, root, _mem["latest"]


def can_install() -> tuple[bool, str]:
    """Whether install_now() can work and, when not, why."""
    try:
        _plan()
    except ValueError as e:
        return False, str(e)
    return True, ""


def install_now() -> None:
    """Start the swap and return at once; the caller quits the app right after. The helper runs from a copy in
    <data>/updates (the bundle it lives in is about to be replaced), detached, and waits for the app's process to end.
    It leaves the old build in <data>/updates/previous and its result in last.json (see last_result)."""
    new, root, version = _plan()
    win, folder = sys.platform == "win32", _dir()
    folder.mkdir(parents=True, exist_ok=True)
    helper = folder / ("apply.ps1" if win else "apply.sh")
    shutil.copyfile(Path(__file__).with_name(helper.name), helper)
    args = [str(os.getpid()), str(root), str(new), str(folder / "previous"), str(folder / "last.json"), version]
    if win:
        names = ("-ProcessId", "-AppDir", "-NewDir", "-PreviousDir", "-Result", "-Version")
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(helper),
               *(x for pair in zip(names, args) for x in pair), "-Exe", EXE]
        how = {"creationflags": DETACHED}
    else:
        cmd, how = ["/bin/sh", str(helper), *args], {"start_new_session": True}
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, **how)
    _save(install_on_quit=False)
    LOG.info("installing %s over %s: the helper takes over", version, root)


def apply_on_exit() -> None:
    """At quit: install now if the user chose "next time". Quitting never fails because of an update."""
    try:
        if should_install_on_exit():
            install_now()
    except Exception:
        LOG.exception("the update was not installed at quit")
        _save(install_on_quit=False)   # the choice is used up, whether or not the helper started


def last_result() -> dict | None:
    """What the helper did, as {ok, version, text} (text for the user, Chinese). The file is read once and deleted;
    None when there is none."""
    path = _dir() / "last.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = None
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        LOG.warning("cannot remove %s: %s", path, e)
    if not isinstance(data, dict):
        return None
    ok, version, who = data.get("ok") is True, str(data.get("version") or ""), data.get("who")
    who = [w for w in who if isinstance(w, str) and w] if isinstance(who, list) else []
    text = RESULT_TEXT.get(str(data.get("code")), RESULT_TEXT["ok"] if ok else NOTE_NOT_DONE)
    return {"ok": ok, "version": version, "text": text.format(version=version, who="、".join(who) or "别的程序")}


# ---- the loop
def _loop() -> None:
    delay = random.uniform(0, JITTER)
    while not _sleep(delay):
        try:
            check()
        except Exception:
            LOG.exception("update loop")
        delay = INTERVAL


def start() -> None:
    """Check shortly from now and every 30 minutes after, in a daemon thread. Once; nothing when disabled()."""
    global _thread
    try:
        if disabled():
            return
        with _guard:
            if _thread is None or not _thread.is_alive():
                _thread = threading.Thread(target=_loop, name="djs-update", daemon=True)
                _thread.start()
    except Exception:   # the app opens its window whatever the updater does
        LOG.exception("the update loop did not start")
