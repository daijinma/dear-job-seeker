"""The updater: a fake release API and fake downloads (httpx.MockTransport), a tiny stand-in build per platform, no
network, no waiting."""
import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from djs import update

OURS, NEW = "1.2.6", "1.4.0"
TARGETS = ("macos-arm64", "macos-x86_64", "windows-x64")
needs_sh = pytest.mark.skipif(os.name == "nt", reason="the stand-in build is a shell script")

# What a build's console program prints for `--selftest`
STUBS = {"ok": '#!/bin/sh\n[ "$1" = "--selftest" ] && echo "selftest ok"\n',
         "no": '#!/bin/sh\necho "something is missing"\n',
         "fail": "#!/bin/sh\necho oops >&2\nexit 1\n",
         "hang": "#!/bin/sh\nexec sleep 5\n"}


# ---- a release, as GitHub shows it
class Utf8NoFlag(str):
    """A file name that zipfile writes as raw UTF-8 without the "this is UTF-8" flag, as `ditto` does."""

    def encode(self, encoding="utf-8", errors="strict"):
        return str.encode(self, "utf-8")


def put(z, name, data=b"", mode=0o644, kind=stat.S_IFREG, noflag=False):
    info = zipfile.ZipInfo(Utf8NoFlag(name) if noflag else name)
    info.create_system = 3
    info.external_attr = (kind | mode) << 16
    z.writestr(info, data)


def make_zip(layout="mac", selftest="ok", mark="", pad=0) -> bytes:
    """A stand-in for a release zip: the mac one holds the .app (names without the UTF-8 flag, a symlink, executable
    programs) and a helper beside it; the Windows one the folder."""
    stub = STUBS[selftest].encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if layout == "mac":
            put(z, "找岗位助手.app/", kind=stat.S_IFDIR, mode=0o755, noflag=True)
            put(z, "找岗位助手.app/Contents/MacOS/djs", stub, 0o755, noflag=True)
            put(z, "找岗位助手.app/Contents/MacOS/找岗位助手", b"#!/bin/sh\n", 0o755, noflag=True)
            put(z, "找岗位助手.app/Contents/Frameworks/Lib/x.dylib", b"lib", noflag=True)
            put(z, "找岗位助手.app/Contents/Resources/Lib", b"../Frameworks/Lib", 0o755, stat.S_IFLNK, noflag=True)
            put(z, "安装.command", b"#!/bin/sh\n", 0o755, noflag=True)
        else:
            put(z, "dear-job-seeker/djs.exe", stub, 0o755)
            put(z, "dear-job-seeker/找岗位助手.exe", b"MZ", 0o755)
            put(z, "dear-job-seeker/_internal/x.dll", b"dll")
        put(z, "mark.txt", mark.encode())
        if pad:
            put(z, "pad.bin", bytes(range(256)) * (pad // 256))
    return buf.getvalue()


def asset(name, data, **over):
    return {"name": name, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "browser_download_url": f"https://github.test/dl/{name}", **over}


class Cut(httpx.SyncByteStream):
    """A body that breaks off after `n` bytes, as a dropped connection does."""

    def __init__(self, body, n):
        self.body, self.n = body, n

    def __iter__(self):
        yield self.body[:self.n]
        raise httpx.ReadError("connection reset")


class Fake:
    """The release API and the file server in one MockTransport handler; it records what it was asked."""

    def __init__(self):
        self.api, self.downloads = [], []
        self.release, self.files, self.n = None, {}, 0
        self.etag = None
        self.honor_range, self.refuse_range = True, False
        self.cut_at = None        # the next download breaks off after this many bytes
        self.api_reply = None     # a function(request) -> Response that replaces the API's answer
        self.offline = False

    def publish(self, ver, layout=None, selftest="ok", pad=0, assets=None, **release):
        """Make `ver` the latest release with a zip for every target (or the given assets)."""
        self.n += 1
        self.etag = f'"r{self.n}"'
        listed = []
        for t in TARGETS:
            data = make_zip(layout or ("windows" if t.startswith("windows") else "mac"), selftest, mark=t, pad=pad)
            name = f"dear-job-seeker-{ver}-{t}.zip"
            self.files[name] = data
            listed.append(asset(name, data))
        self.release = {"tag_name": f"v{ver}", "draft": False, "prerelease": False,
                        "assets": listed if assets is None else assets, **release}
        return self.release

    def asset(self, target):
        return next(a for a in self.release["assets"] if a["name"].endswith(f"-{target}.zip"))

    def __call__(self, request):
        if self.offline:
            raise httpx.ConnectError("offline", request=request)
        path = request.url.path
        if path.endswith("/releases/latest"):
            self.api.append(request)
            if self.api_reply:
                return self.api_reply(request)
            if self.etag and request.headers.get("If-None-Match") == self.etag:
                return httpx.Response(304)
            return httpx.Response(200, json=self.release, headers={"ETag": self.etag} if self.etag else {})
        self.downloads.append(request)
        if path.startswith("/redirect/"):
            return httpx.Response(302, headers={"Location": "https://cdn.test/dl/" + path.rsplit("/", 1)[-1]})
        data = self.files.get(path.rsplit("/", 1)[-1])
        if data is None:
            return httpx.Response(404)
        rng = request.headers.get("Range")
        if rng and self.refuse_range:
            return httpx.Response(416, headers={"Content-Range": f"bytes */{len(data)}"})
        start = int(re.match(r"bytes=(\d+)-", rng)[1]) if rng and self.honor_range else 0
        status, headers = (206, {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"}) if start else (200, {})
        if self.cut_at is not None:
            n, self.cut_at = self.cut_at, None
            return httpx.Response(status, headers=headers, stream=Cut(data[start:], n))
        return httpx.Response(status, headers=headers, content=data[start:])


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A packaged app 1.2.6 on an Apple-silicon Mac, its data folder under tmp, the fake GitHub behind httpx."""
    home = tmp_path / "data"
    fake, clock = Fake(), SimpleNamespace(t=1_000_000.0)
    monkeypatch.setenv("DJS_HOME", str(home))
    monkeypatch.delenv("DJS_UPDATE", raising=False)
    monkeypatch.delenv("DJS_UPDATE_API", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(update, "_version", lambda: OURS)
    monkeypatch.setattr(update, "_now", lambda: clock.t)
    monkeypatch.setattr(update, "_spawn", lambda job: job())   # the download runs inside check()
    monkeypatch.setattr(update, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(update, "_mem", {"state": "none", "latest": None, "note": ""})
    monkeypatch.setattr(update, "_job", None)
    monkeypatch.setattr(update, "_resume_at", 0.0)
    monkeypatch.setattr(update, "_thread", None)
    monkeypatch.setattr(update, "_swept", False)

    def disk():
        try:
            return json.loads((home / "updates" / "state.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    return SimpleNamespace(fake=fake, clock=clock, home=home, updates=home / "updates", disk=disk)


def stage_of(app, ver=NEW):
    return app.updates / ver / "stage"


# ---- names and the machine
def test_asset_pattern_matches_the_three_names_of_1_2_6_and_nothing_else():
    for target in TARGETS:
        assert re.fullmatch(update.ASSET_PATTERN, f"dear-job-seeker-1.2.6-{target}.zip")
    assert re.fullmatch(update.ASSET_PATTERN, "dear-job-seeker-1.10.0-windows-x64.zip")
    for name in ("dear-job-seeker-1.2.6-linux-x64.zip", "dear-job-seeker-1.2.6-windows-arm64.zip",
                 "dear-job-seeker-v1.2.6-macos-arm64.zip", "dear-job-seeker-1.2.6-macos-arm64.zip.sha256",
                 "dear-job-seeker-1.2.6-macos-arm64.dmg", "Source code (zip)", "xdear-job-seeker-1.2.6-windows-x64.zip",
                 "dear-job-seeker-macos-arm64.zip"):
        assert not re.fullmatch(update.ASSET_PATTERN, name), name


def test_the_release_workflow_builds_and_checks_the_names_the_updater_looks_for():
    workflow = Path(update.__file__).parents[1] / ".github" / "workflows" / "release.yml"
    if not workflow.is_file():
        pytest.skip("no .github folder next to the package")
    text = workflow.read_text(encoding="utf-8")
    assert re.findall(r"- target: (\S+)", text) == list(TARGETS)   # the three builds
    assert 'name=dear-job-seeker-$version-${{ matrix.target }}.zip' in text   # named as the updater asks for them
    assert 'for t in ("macos-arm64", "macos-x86_64", "windows-x64")' in text   # and the release is checked for the same three
    assert all(re.fullmatch(update.ASSET_PATTERN, f"dear-job-seeker-1.4.0-{t}.zip") for t in TARGETS)


@pytest.mark.skipif(not shutil.which("grep"), reason="needs grep")
def test_asset_pattern_works_as_grep_extended_regex():
    names = "\n".join(["dear-job-seeker-1.2.6-macos-arm64.zip", "dear-job-seeker-1.2.6-linux-x64.zip", "other.zip"])
    out = subprocess.run(["grep", "-Ex", update.ASSET_PATTERN], input=names, capture_output=True, text=True).stdout
    assert out.split() == ["dear-job-seeker-1.2.6-macos-arm64.zip"]


@pytest.mark.parametrize("plat, machine, want", [
    ("darwin", "arm64", "macos-arm64"), ("darwin", "x86_64", "macos-x86_64"),
    ("win32", "AMD64", "windows-x64"), ("win32", "x86_64", "windows-x64")])
def test_target_comes_from_this_process(app, monkeypatch, plat, machine, want):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert update.target() == want
    assert re.fullmatch(update.ASSET_PATTERN, f"dear-job-seeker-1.0.0-{want}.zip")


def test_disabled_for_a_source_checkout_and_by_env(app, monkeypatch):
    assert not update.disabled()
    monkeypatch.setenv("DJS_UPDATE", "0")
    assert update.disabled()
    monkeypatch.setenv("DJS_UPDATE", "1")
    assert not update.disabled()
    monkeypatch.delattr(sys, "frozen")   # run from source: Python, not the packaged program
    assert update.disabled()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(update, "_version", lambda: None)   # the package was never installed
    assert update.disabled()


def test_a_disabled_updater_checks_nothing(app, monkeypatch):
    app.fake.publish(NEW)
    monkeypatch.setenv("DJS_UPDATE", "0")
    out = update.check(manual=True)
    assert out["found"] is False and out["state"] == "none" and app.fake.api == []
    monkeypatch.setattr(update, "_loop", lambda: pytest.fail("the loop must not start"))
    update.start()
    assert update._thread is None


# ---- checking
@needs_sh
def test_a_newer_release_is_downloaded_unzipped_and_tested(app):
    app.fake.publish(NEW)
    out = update.check()
    assert out == {"found": True, "current": OURS, "latest": NEW, "state": "ready", "note": "", "install_on_quit": False}
    assert update.current() == {k: v for k, v in out.items() if k != "found"}
    build = update.staged()
    assert build == stage_of(app) / "找岗位助手.app"
    assert os.access(build / "Contents" / "MacOS" / "djs", os.X_OK)
    assert os.access(build / "Contents" / "MacOS" / "找岗位助手", os.X_OK)
    assert os.readlink(build / "Contents" / "Resources" / "Lib") == "../Frameworks/Lib"
    assert (build / "Contents" / "Resources" / "Lib" / "x.dylib").read_bytes() == b"lib"   # through the link
    assert (stage_of(app) / "安装.command").is_file()
    assert [p.name for p in (app.updates / NEW).iterdir()] == ["stage"]   # the zip is not kept
    assert app.disk()["ready_version"] == NEW and app.disk()["etag"] == app.fake.etag
    [download] = app.fake.downloads
    assert download.headers["User-Agent"] == f"dear-job-seeker/{OURS}" and download.headers["Accept-Encoding"] == "identity"


def test_the_request_says_who_asks(app):
    app.fake.publish(OURS)
    update.check()
    [req] = app.fake.api
    assert str(req.url) == "https://api.github.com/repos/daijinma/dear-job-seeker/releases/latest"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["User-Agent"] == f"dear-job-seeker/{OURS}"
    assert "If-None-Match" not in req.headers


def test_the_api_address_can_be_overridden(app, monkeypatch):
    monkeypatch.setenv("DJS_UPDATE_API", "http://127.0.0.1:8123/repos/me/app/")
    app.fake.publish(OURS)
    update.check()
    assert str(app.fake.api[0].url) == "http://127.0.0.1:8123/repos/me/app/releases/latest"


@pytest.mark.parametrize("tag, kind", [(OURS, "same"), ("1.2.5", "older"), ("1.2.6.0", "same with more digits"),
                                       ("1.4.0rc1", "not a number"), ("nightly", "not a version")])
def test_a_release_that_is_not_newer_changes_nothing(app, tag, kind):
    app.fake.publish(tag)
    out = update.check(manual=True)
    assert out["found"] is False and out["state"] == "none" and out["latest"] is None, kind
    assert app.fake.downloads == []
    assert app.disk()["etag"] == app.fake.etag   # nothing more to do about this answer


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_drafts_and_prereleases_never_count(app, flag):
    app.fake.publish(NEW, **{flag: True})
    assert update.check()["found"] is False
    assert app.fake.downloads == []


@pytest.mark.parametrize("ours, tag, newer", [("1.2.9", "1.2.10", True), ("1.10.0", "1.9.0", False),
                                              ("1.9.0", "1.10.0", True), ("1.4", "1.4.0", False), ("1.4", "1.4.1", True)])
def test_versions_compare_as_numbers_not_as_text(app, monkeypatch, ours, tag, newer):
    monkeypatch.setattr(update, "_version", lambda: ours)
    monkeypatch.setattr(update, "_spawn", lambda job: None)   # only the decision matters here
    app.fake.publish(tag)
    assert (update.check()["state"] == "downloading") is newer


@needs_sh
def test_an_unchanged_answer_is_a_304_that_keeps_the_state(app):
    app.fake.publish(NEW)
    update.check()
    assert update.check()["state"] == "ready"
    first, second = app.fake.api
    assert "If-None-Match" not in first.headers and second.headers["If-None-Match"] == app.fake.etag
    assert len(app.fake.downloads) == 1   # the 304 did not start another download


@needs_sh
def test_a_manual_check_asks_without_the_etag(app):
    app.fake.publish(OURS)
    update.check()
    update.check(manual=True)
    assert "If-None-Match" not in app.fake.api[1].headers
    update.check()
    assert app.fake.api[2].headers["If-None-Match"] == app.fake.etag


@pytest.mark.parametrize("target_, want", [("macos-arm64", "macos-arm64"), ("macos-x86_64", "macos-x86_64"),
                                           ("windows-x64", "windows-x64")])
@needs_sh
def test_only_the_zip_for_this_build_is_downloaded(app, monkeypatch, target_, want):
    plat, machine = {"macos-arm64": ("darwin", "arm64"), "macos-x86_64": ("darwin", "x86_64"),
                     "windows-x64": ("win32", "AMD64")}[target_]
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    app.fake.publish(NEW)
    out = update.check()
    assert [r.url.path.rsplit("/", 1)[-1] for r in app.fake.downloads] == [f"dear-job-seeker-{NEW}-{want}.zip"]
    assert out["state"] == "ready"
    assert update.staged().name == ("dear-job-seeker" if want.startswith("windows") else "找岗位助手.app")


def test_a_release_without_our_zip_is_not_an_update_yet(app, caplog):
    caplog.set_level(logging.INFO, logger="djs")
    release = app.fake.publish(NEW)
    release["assets"] = [a for a in release["assets"] if "macos-arm64" not in a["name"]]
    out = update.check()
    assert out["state"] == "none" and out["note"] == "" and app.fake.downloads == []
    assert "has no dear-job-seeker-1.4.0-macos-arm64.zip" in caplog.text
    assert app.disk().get("etag") is None   # the release may still get its files: ask again in full


# ---- the download
def test_a_wrong_digest_is_refused_and_nothing_is_offered(app):
    release = app.fake.publish(NEW)
    app.fake.asset("macos-arm64")["digest"] = "sha256:" + "0" * 64
    out = update.check()
    assert (out["state"], out["note"], out["found"]) == ("failed", "下载的文件没通过校验", False)
    assert update.staged() is None and not (app.updates / NEW).exists()
    assert app.disk().get("ready_version") is None


def test_a_wrong_size_is_refused(app):
    app.fake.publish(NEW)
    app.fake.asset("macos-arm64")["size"] -= 1
    out = update.check()
    assert (out["state"], out["note"]) == ("failed", "下载的文件没通过校验")
    assert not (app.updates / NEW).exists()


@pytest.mark.parametrize("drop", ["digest", "size"])
def test_a_release_without_checksum_is_refused_before_downloading(app, drop):
    app.fake.publish(NEW)
    del app.fake.asset("macos-arm64")[drop]
    out = update.check()
    assert (out["state"], out["note"]) == ("failed", "发布页没有校验值")
    assert app.fake.downloads == [] and update.staged() is None


def test_another_kind_of_digest_is_not_accepted(app):
    app.fake.publish(NEW)
    app.fake.asset("macos-arm64")["digest"] = "md5:" + "0" * 32
    assert update.check()["note"] == "发布页没有校验值"


@needs_sh
def test_a_refused_release_is_tried_again_only_when_asked(app):
    app.fake.publish(NEW)
    app.fake.asset("macos-arm64")["digest"] = "sha256:" + "0" * 64
    app.fake.etag = None   # a server that sends no ETag asks the full question every time
    update.check()
    update.check()
    assert update.current()["state"] == "failed" and len(app.fake.downloads) == 1
    app.fake.publish(NEW)   # the release is repaired
    assert update.check()["state"] == "failed" and len(app.fake.downloads) == 1
    out = update.check(manual=True)
    assert out["state"] == "ready" and len(app.fake.downloads) == 2


@needs_sh
def test_an_interrupted_download_resumes_from_where_it_broke(app):
    app.fake.publish(NEW, pad=20_000)
    app.fake.cut_at = 5000
    out = update.check()
    assert (out["state"], out["latest"], out["note"]) == ("none", None, "")   # a dropped line says nothing
    part = app.updates / NEW / f"dear-job-seeker-{NEW}-macos-arm64.zip.part"
    assert part.stat().st_size == 5000
    assert app.disk().get("etag") is None   # so the next check is a full one and resumes
    out = update.check()
    assert out["state"] == "ready"
    first, second = app.fake.downloads
    assert "Range" not in first.headers and second.headers["Range"] == "bytes=5000-"
    assert not part.exists()


@needs_sh
def test_a_server_that_ignores_range_starts_again(app):
    app.fake.publish(NEW, pad=20_000)
    app.fake.honor_range = False
    app.fake.cut_at = 5000
    update.check()
    assert update.check()["state"] == "ready"
    assert app.fake.downloads[1].headers["Range"] == "bytes=5000-"   # asked, was answered with the whole file


def leave_part(app, size):
    part = app.updates / NEW / f"dear-job-seeker-{NEW}-macos-arm64.zip.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"junk" * (size // 4))
    return part


@needs_sh
def test_a_part_the_server_cannot_continue_is_dropped(app):
    app.fake.publish(NEW, pad=20_000)
    part = leave_part(app, 4000)
    app.fake.refuse_range = True   # 416: the server cannot serve from byte 4000
    assert update.check()["state"] == "ready"
    assert [r.headers.get("Range") for r in app.fake.downloads] == ["bytes=4000-", None]
    assert not part.exists()


@needs_sh
def test_a_part_bigger_than_the_file_is_dropped_without_asking(app):
    app.fake.publish(NEW)
    leave_part(app, 40_000)   # the real zip is a few KB: the part is bigger than the whole file
    assert update.check()["state"] == "ready"
    assert [r.headers.get("Range") for r in app.fake.downloads] == [None]


@needs_sh
def test_the_download_follows_redirects(app):
    app.fake.publish(NEW)
    for a in app.fake.release["assets"]:
        a["browser_download_url"] = a["browser_download_url"].replace("/dl/", "/redirect/")
    assert update.check()["state"] == "ready"
    assert [r.url.host for r in app.fake.downloads] == ["github.test", "cdn.test"]


def test_a_failed_request_for_the_file_leaves_the_state_alone(app):
    app.fake.publish(NEW)
    del app.fake.files[f"dear-job-seeker-{NEW}-macos-arm64.zip"]   # the file server answers 404
    out = update.check()
    assert (out["state"], out["note"], out["found"]) == ("none", "", False)


# ---- when the network or GitHub says no
def test_offline_is_silent(app, caplog):
    caplog.set_level(logging.INFO, logger="djs")
    app.fake.publish(NEW)
    app.fake.offline = True
    out = update.check(manual=True)
    assert out == {"found": False, "current": OURS, "latest": None, "state": "none", "note": "", "install_on_quit": False}
    assert "update check failed" in caplog.text and not (app.home / "updates").exists()


@pytest.mark.parametrize("status", [404, 500, 502])
def test_other_http_errors_are_silent_too(app, status):
    app.fake.api_reply = lambda request: httpx.Response(status)
    assert update.check()["state"] == "none"


def test_rubbish_from_the_api_is_silent(app):
    for reply in (httpx.Response(200, content=b"<html>"), httpx.Response(200, json=["not", "a", "release"])):
        app.fake.api_reply = lambda request, reply=reply: reply
        assert update.check()["state"] == "none"


@pytest.mark.parametrize("status, headers", [
    (429, {"Retry-After": "120"}),
    (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000120"}),
    (403, {"Retry-After": "120", "X-RateLimit-Reset": "1000060"})])
def test_a_rate_limit_is_waited_out(app, status, headers):
    app.fake.api_reply = lambda request: httpx.Response(status, headers=headers)
    out = update.check()
    assert out["state"] == "none" and len(app.fake.api) == 1
    app.clock.t += 60
    update.check()
    update.check(manual=True)   # a click does not break the wait either
    assert len(app.fake.api) == 1
    app.clock.t += 61   # past the reset time
    update.check()
    assert len(app.fake.api) == 2


def test_a_rate_limit_without_a_time_waits_one_interval(app):
    app.fake.api_reply = lambda request: httpx.Response(429)
    update.check()
    app.clock.t += update.INTERVAL - 1
    update.check()
    assert len(app.fake.api) == 1
    app.clock.t += 2
    update.check()
    assert len(app.fake.api) == 2


# ---- the staged build
@needs_sh
@pytest.mark.parametrize("selftest", ["no", "fail"])
def test_a_build_that_fails_its_selftest_is_not_ready(app, selftest):
    app.fake.publish(NEW, selftest=selftest)
    out = update.check()
    assert (out["state"], out["note"], out["found"], out["latest"]) == ("failed", "新版本没通过自检", False, NEW)
    assert update.staged() is None and not (app.updates / NEW).exists()
    assert app.disk().get("ready_version") is None
    assert update.check()["state"] == "failed" and len(app.fake.downloads) == 1   # the 304: not fetched again


@needs_sh
def test_a_build_that_hangs_in_its_selftest_is_not_ready(app, monkeypatch):
    monkeypatch.setattr(update, "SELFTEST_TIMEOUT", 0.2)
    app.fake.publish(NEW, selftest="hang")
    assert update.check()["note"] == "新版本没通过自检"
    assert not (app.updates / NEW).exists()


def test_a_zip_without_the_app_is_not_ready(app):
    app.fake.publish(NEW, layout="windows")   # the Windows folder where a macOS build is needed
    out = update.check()
    assert (out["state"], out["note"]) == ("failed", "新版本没通过自检")


@needs_sh
def test_the_selftest_is_asked_with_the_flag_and_must_say_ok(tmp_path):
    program = tmp_path / "build" / "Contents" / "MacOS" / "djs"
    program.parent.mkdir(parents=True)
    program.write_text(STUBS["ok"])
    program.chmod(0o755)
    assert update._selftest(tmp_path / "build") is True   # the stub says ok only when called with --selftest
    program.write_text(STUBS["no"])
    assert update._selftest(tmp_path / "build") is False
    program.unlink()
    assert update._selftest(tmp_path / "build") is False   # cannot even start


@needs_sh
def test_the_selftest_runs_as_a_program_of_its_own(tmp_path):
    program = tmp_path / "build" / "Contents" / "MacOS" / "djs"
    program.parent.mkdir(parents=True)
    program.write_text('#!/bin/sh\n[ "$PYINSTALLER_RESET_ENVIRONMENT" = 1 ] && echo "selftest ok"\n')   # a packaged program told so
    program.chmod(0o755)
    assert update._selftest(tmp_path / "build") is True


def bad_zip(tmp_path, *entries):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as z:
        put(z, "fine.txt", b"ok")
        for name, data, kind in entries:
            put(z, name, data, 0o755, kind)
    return path


@pytest.mark.parametrize("entry", [
    ("../evil.txt", b"x", stat.S_IFREG),
    ("a/../../evil.txt", b"x", stat.S_IFREG),
    ("/tmp/djs-evil.txt", b"x", stat.S_IFREG),
    ("link", b"../../outside", stat.S_IFLNK),
    ("link", b"/etc", stat.S_IFLNK)])
def test_an_entry_that_leaves_the_folder_is_rejected(tmp_path, entry):
    dest = tmp_path / "in" / "stage"
    with pytest.raises(ValueError):
        update._unzip(bad_zip(tmp_path, entry), dest)
    assert not (tmp_path / "in" / "evil.txt").exists() and not (tmp_path / "evil.txt").exists()
    assert not os.path.lexists("/tmp/djs-evil.txt")


@needs_sh
def test_a_file_cannot_be_written_through_links_that_add_up_to_an_escape(tmp_path):
    # each link looks inside on its own; l2 is the parent of the folder in the end
    entries = [("a/l1", b"..", stat.S_IFLNK), ("b/l2", b"../a/l1/..", stat.S_IFLNK), ("b/l2/evil.txt", b"x", stat.S_IFREG)]
    with pytest.raises(ValueError):
        update._unzip(bad_zip(tmp_path, *entries), tmp_path / "in" / "stage")
    assert not (tmp_path / "in" / "evil.txt").exists()


@needs_sh
def test_a_link_that_leads_out_in_the_end_is_not_made(tmp_path):
    # the text of "m" stays inside the folder step by step; followed through l1 (the folder itself) it ends beside it
    entries = [("a/l1", b"..", stat.S_IFLNK), ("m", b"a/l1/../outside.txt", stat.S_IFLNK)]
    with pytest.raises(ValueError):
        update._unzip(bad_zip(tmp_path, *entries), tmp_path / "in" / "stage")
    assert not os.path.lexists(tmp_path / "in" / "stage" / "m")


@needs_sh
@pytest.mark.filterwarnings("ignore:Duplicate name")   # the zip of this test holds the name twice, on purpose
def test_a_file_replaces_a_link_of_its_name_it_is_not_written_through_it(tmp_path):
    entries = [("keep.txt", b"original", stat.S_IFREG), ("m", b"keep.txt", stat.S_IFLNK), ("m", b"new", stat.S_IFREG)]
    update._unzip(bad_zip(tmp_path, *entries), tmp_path / "ok")
    assert not (tmp_path / "ok" / "m").is_symlink() and (tmp_path / "ok" / "m").read_bytes() == b"new"
    assert (tmp_path / "ok" / "keep.txt").read_bytes() == b"original"


@needs_sh
def test_links_that_stay_inside_are_kept(tmp_path):
    update._unzip(bad_zip(tmp_path, ("a/b", b"x", stat.S_IFREG), ("up/sub", b"../a", stat.S_IFLNK)), tmp_path / "ok")
    assert os.readlink(tmp_path / "ok" / "up" / "sub") == "../a"
    assert (tmp_path / "ok" / "up" / "sub" / "b").read_bytes() == b"x"


def test_a_zip_that_escapes_is_refused_as_a_download(app):
    data = bad_zip(app.home.parent, ("../evil.txt", b"x", stat.S_IFREG)).read_bytes()
    name = f"dear-job-seeker-{NEW}-macos-arm64.zip"
    app.fake.publish(NEW, assets=[asset(name, data)])
    app.fake.files[name] = data
    out = update.check()
    assert (out["state"], out["note"]) == ("failed", "下载的文件没通过校验")
    assert not (app.updates / "evil.txt").exists() and not (app.updates / NEW).exists()


def test_a_zip_that_is_not_a_zip_is_refused(app):
    data = b"this is not a zip" * 50
    name = f"dear-job-seeker-{NEW}-macos-arm64.zip"
    app.fake.publish(NEW, assets=[asset(name, data)])
    app.fake.files[name] = data
    assert update.check()["note"] == "下载的文件没通过校验"


@needs_sh
def test_names_without_the_utf8_flag_come_out_as_utf8(tmp_path):
    with zipfile.ZipFile(io.BytesIO(make_zip())) as z:
        flagged = [i.filename for i in z.infolist() if "找" in i.filename]
    assert flagged == []   # read back as cp437 mojibake: the flag is not set, like the real release
    path = tmp_path / "b.zip"
    path.write_bytes(make_zip())
    update._unzip(path, tmp_path / "out")
    assert {p.name for p in (tmp_path / "out").iterdir()} == {"mark.txt", "找岗位助手.app", "安装.command"}


# ---- later, replacing, restoring
def test_later_is_remembered_across_restarts(app, monkeypatch):
    assert update.current()["install_on_quit"] is False
    update.later()
    assert app.disk()["install_on_quit"] is True
    monkeypatch.setattr(update, "_mem", {"state": "none", "latest": None, "note": ""})   # a new process
    assert update.current()["install_on_quit"] is True


@needs_sh
def test_install_on_exit_needs_a_ready_update_and_the_flag(app):
    update.later()
    assert update.should_install_on_exit() is False   # nothing downloaded
    app.fake.publish(NEW)
    update.check()
    assert update.should_install_on_exit() is True
    update._save(install_on_quit=False)
    assert update.should_install_on_exit() is False


@needs_sh
def test_a_newer_release_replaces_the_one_that_is_ready(app):
    app.fake.publish("1.3.0")
    update.check()
    assert update.current()["latest"] == "1.3.0" and stage_of(app, "1.3.0").is_dir()
    app.fake.publish(NEW)
    out = update.check()
    assert (out["state"], out["latest"]) == ("ready", NEW)
    assert not (app.updates / "1.3.0").exists() and stage_of(app).is_dir()
    assert app.disk()["ready_version"] == NEW


@needs_sh
def test_a_newer_release_stops_an_older_download_in_flight(app, monkeypatch):
    jobs = []
    monkeypatch.setattr(update, "_spawn", jobs.append)
    app.fake.publish("1.3.0")
    assert update.check()["state"] == "downloading"
    app.fake.publish(NEW)
    assert update.check()["latest"] == NEW
    old, new = jobs
    old()   # gives up at its first chunk: nothing of it stays
    assert update.current()["state"] == "downloading" and not (app.updates / "1.3.0").exists()
    new()
    assert update.current()["state"] == "ready" and update.current()["latest"] == NEW


@needs_sh
def test_a_ready_update_is_still_there_after_a_restart(app, monkeypatch):
    app.fake.publish(NEW)
    update.check()
    monkeypatch.setattr(update, "_mem", {"state": "none", "latest": None, "note": ""})
    monkeypatch.setattr(update, "_swept", False)
    out = update.current()
    assert (out["state"], out["latest"]) == ("ready", NEW) and update.staged() == stage_of(app) / "找岗位助手.app"
    assert update.check()["state"] == "ready" and len(app.fake.downloads) == 1   # 304: it was not fetched again


@needs_sh
def test_what_is_not_newer_than_the_running_build_is_swept(app, monkeypatch):
    app.fake.publish(NEW)
    update.check()
    (app.updates / "1.0.0").mkdir()
    (app.updates / "previous").mkdir()
    monkeypatch.setattr(update, "_version", lambda: NEW)   # the update was installed
    monkeypatch.setattr(update, "_mem", {"state": "none", "latest": None, "note": ""})
    monkeypatch.setattr(update, "_swept", False)
    assert update.current()["state"] == "none"
    assert sorted(p.name for p in app.updates.iterdir() if p.is_dir()) == ["previous"]
    assert app.disk()["ready_version"] is None


# ---- state.json
def test_state_is_written_whole_or_not_at_all(app, monkeypatch):
    update.later()
    before = (app.updates / "state.json").read_text(encoding="utf-8")
    assert json.loads(before) == {"install_on_quit": True, "etag": None, "ready_version": None}
    assert [p.name for p in app.updates.iterdir()] == ["state.json"]   # no half-written leftovers
    real = os.replace

    def broken(src, dst):
        raise OSError("disk went away")
    monkeypatch.setattr(os, "replace", broken)
    update._save(etag='"x"')
    monkeypatch.setattr(os, "replace", real)
    assert (app.updates / "state.json").read_text(encoding="utf-8") == before
    assert [p.name for p in app.updates.iterdir()] == ["state.json"]


def test_a_damaged_state_file_reads_as_empty(app):
    app.updates.mkdir(parents=True)
    for junk in ("not json", "[1, 2]", ""):
        (app.updates / "state.json").write_text(junk, encoding="utf-8")
        assert update._load() == {"install_on_quit": False, "etag": None, "ready_version": None}
    update.later()
    assert app.disk()["install_on_quit"] is True


# ---- the loop
def test_the_loop_checks_after_a_jitter_and_then_every_half_hour(app, monkeypatch):
    waits, checks = [], []
    answers = iter([False, False, False, True])
    monkeypatch.setattr(update.random, "uniform", lambda a, b: 3.5)
    monkeypatch.setattr(update, "_sleep", lambda s: waits.append(s) or next(answers))
    monkeypatch.setattr(update, "check", lambda: checks.append(1))
    update._loop()
    assert waits == [3.5, update.INTERVAL, update.INTERVAL, update.INTERVAL] and len(checks) == 3


def test_the_jitter_is_between_zero_and_five_seconds(app, monkeypatch):
    seen = []
    monkeypatch.setattr(update.random, "uniform", lambda a, b: seen.append((a, b)) or 0)
    monkeypatch.setattr(update, "_sleep", lambda s: True)
    update._loop()
    assert seen == [(0, 5)] and update.INTERVAL == 1800


def test_a_crashing_check_does_not_end_the_loop(app, monkeypatch, caplog):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("bug")
    answers = iter([False, False, True])
    monkeypatch.setattr(update, "_sleep", lambda s: next(answers))
    monkeypatch.setattr(update, "check", boom)
    update._loop()
    assert len(calls) == 2 and "update loop" in caplog.text


def test_start_never_raises_into_the_app(app, monkeypatch, caplog):
    monkeypatch.setattr(update, "disabled", lambda: 1 / 0)
    update.start()
    assert update._thread is None and "did not start" in caplog.text


def test_start_runs_one_daemon_thread(app, monkeypatch):
    started, release, runs = threading.Event(), threading.Event(), []

    def loop():
        runs.append(1)
        started.set()
        release.wait(5)
    monkeypatch.setattr(update, "_loop", loop)
    update.start()
    started.wait(5)
    thread = update._thread
    update.start()
    assert update._thread is thread and thread.daemon and len(runs) == 1
    release.set()
    thread.join(5)
    update.start()   # the thread ended: a new one may start
    assert update._thread is not thread
    update._thread.join(5)
