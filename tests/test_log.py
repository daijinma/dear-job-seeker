"""The log file: where it is, how it rotates, and what never reaches it."""
import logging
import re

import pytest

from djs import log


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("DJS_DEBUG", raising=False)
    logger, httpx = logging.getLogger("djs"), logging.getLogger("httpx")
    level, propagate, httpx_level = logger.level, logger.propagate, httpx.level
    yield tmp_path / "data"
    for h in log._ours(logger):
        logger.removeHandler(h)
        h.close()
    logger.setLevel(level)
    logger.propagate = propagate
    httpx.setLevel(httpx_level)


def written(logger_name="djs") -> str:
    for h in log._ours(logging.getLogger(logger_name)):
        h.flush()
    return log.path().read_text(encoding="utf-8")


def test_setup_writes_the_rotating_utf8_file_once(data):
    log.setup()
    log.setup()
    [handler] = log._ours(logging.getLogger("djs"))
    assert log.path() == data / "logs" / "djs.log" and log.path().exists()
    assert (handler.maxBytes, handler.backupCount, handler.encoding) == (1_000_000, 3, "utf-8")
    assert logging.getLogger("djs").level == logging.INFO
    log.get("update").info("找到新版本 %s", "1.4.0")
    log.get("update").debug("not at INFO")
    assert re.search(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO djs\.update: 找到新版本 1\.4\.0$", written(), re.M)
    assert "not at INFO" not in written()


def test_our_lines_stay_in_the_file_and_off_stderr(capsys):
    """mcp puts a stderr handler on the root logger: what we log must not travel up to it, nor httpx's request lines."""
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        log.setup()
        log.get("update").info("a line for the file")
        logging.getLogger("httpx").info("HTTP Request: GET https://example.test/x?sig=secret")
    finally:
        root.removeHandler(handler)
    assert capsys.readouterr().err == ""
    assert "a line for the file" in written()
    assert logging.getLogger("httpx").level >= logging.WARNING


def test_get_is_a_child_of_djs():
    assert log.get("update").name == "djs.update"


def test_debug_on_request(monkeypatch):
    monkeypatch.setenv("DJS_DEBUG", "1")
    log.setup()
    log.get("x").debug("fine detail")
    assert "DEBUG djs.x: fine detail" in written()


def test_a_new_data_folder_moves_the_file(tmp_path, monkeypatch):
    log.setup()
    first = log.path()
    monkeypatch.setenv("DJS_HOME", str(tmp_path / "other"))
    log.setup()
    assert len(log._ours(logging.getLogger("djs"))) == 1
    log.get("x").info("moved")
    assert "moved" in written() and first.read_text(encoding="utf-8") == ""


def test_rotation_keeps_three_old_files():
    log.setup()
    log._ours(logging.getLogger("djs"))[0].maxBytes = 20_000   # the real 1 MB is checked above; this only shows the rotation
    for _ in range(100):
        log.get("x").info("x" * 1000)
    files = sorted(p.name for p in log.path().parent.iterdir())
    assert files == ["djs.log", "djs.log.1", "djs.log.2", "djs.log.3"]


def test_url_query_and_fragment_are_cut():
    assert log.redact("GET https://api.github.com/repos/a/b?per_page=1&x=2#top done") == "GET https://api.github.com/repos/a/b done"
    assert log.redact("(see http://127.0.0.1:8000/x?y=1) and 'https://h/p?q'") == "(see http://127.0.0.1:8000/x) and 'https://h/p'"
    assert log.redact("no url, a ? mark") == "no url, a ? mark"


@pytest.mark.parametrize("text, want", [
    ("token=abc123 next", "token=*** next"),
    ("Token: abc123 next", "Token: *** next"),
    ("api_key = k-1 next", "api_key = *** next"),
    ("PASSWORD=hunter2&x=1", "PASSWORD=***&x=1"),
    ("secret: s3", "secret: ***"),
    ('{"access_token": "a b c", "keyword": "python"}', '{"access_token": "***", "keyword": "python"}'),
    ("{'Cookie': 'a=1; b=2', 'n': 3}", "{'Cookie': '***', 'n': 3}"),
    ("Authorization: Bearer abc.def", "Authorization: ***"),
    ("Cookie: a=1; b=2; c=3", "Cookie: ***"),
    ("keyword=python and no key word", "keyword=python and no key word"),
    ("__zp_stoken__=abc123 next", "__zp_stoken__=*** next"),   # the name of BOSS's cookie ends in underscores
    ("proxy http://name:pa55@127.0.0.1:7890 failed", "proxy http://***@127.0.0.1:7890 failed"),   # a proxy's login
    ("socks5://u:p@h:1 and https://a.b/c?d=e", "socks5://***@h:1 and https://a.b/c"),
    ("write to me@example.com, see http://h/x", "write to me@example.com, see http://h/x"),
])
def test_secret_values_are_masked(text, want):
    assert log.redact(text) == want


def test_what_reaches_the_file_is_clean():
    log.setup()
    log.get("update").info("fetch https://h/p?token=abc failed, cookie=%s", "sess=1; other=2")
    try:
        raise RuntimeError("refused with password=hunter2")
    except RuntimeError:
        log.get("update").exception("crashed")
    text = written()
    assert "https://h/p failed, cookie=***" in text
    assert "password=***" in text
    assert not any(leak in text for leak in ("abc", "sess=1", "hunter2"))


def test_a_line_that_cannot_be_formatted_is_dropped_and_never_raises_into_the_caller(monkeypatch):
    monkeypatch.setattr(logging.getLogger("djs"), "propagate", False)   # pytest's own handler would raise: not the app's
    log.setup()
    log.get("x").info("two holes %s %s", "one value")   # the arguments do not fit the message
    log.get("x").info("fine")
    text = written()
    assert "fine" in text and "two holes" not in text


def test_an_unwritable_folder_does_not_raise(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    monkeypatch.setenv("DJS_HOME", str(blocker / "data"))
    log.setup()
    log.setup()
    assert log.path() == blocker / "data" / "logs" / "djs.log"
    log.get("x").info("still heard")
    assert "still heard" in capsys.readouterr().err
