"""Connecting the AI assistants: our one entry written into each assistant's own config file, and nothing else touched."""
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

from djs import connect

C = "/opt/app/bin/djs"   # what command() answers while `sys.executable` is /opt/app/bin/python
NAME = "dear-job-seeker"
PATHS = {
    "claude-code": ".claude.json",
    "claude-desktop": "Library/Application Support/Claude/claude_desktop_config.json",
    "codex": ".codex/config.toml",
    "gemini-cli": ".gemini/settings.json",
    "antigravity": ".gemini/antigravity/mcp_config.json",
    "cursor": ".cursor/mcp.json",
    "kiro": ".kiro/settings/mcp.json",
    "opencode": ".config/opencode/opencode.json",
    "workbuddy": ".workbuddy/mcp.json",
    "codebuddy": ".codebuddy/.mcp.json",
    "qwen-code": ".qwen/settings.json",
    "kimi-code": ".kimi-code/mcp.json",
}
JSON_KEYS = [k for k in PATHS if k != "codex"]
TYPED = {"claude-code", "workbuddy", "codebuddy"}   # these write "type": "stdio" as well


def entry(key, command=C):
    if key == "opencode":
        return {"type": "local", "command": [command], "enabled": True}
    return {**({"type": "stdio"} if key in TYPED else {}), "command": command, "args": []}


def container(key):
    return "mcp" if key == "opencode" else "mcpServers"


def dumped(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(h))
    monkeypatch.setenv("APPDATA", str(h / "AppData" / "Roaming"))
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", "/opt/app/bin/python")
    return h


def installed(home, key, text=None):
    """The assistant is on this computer (its folder; Claude Code: its file), with this text as its config if given."""
    path = home / PATHS[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None or key == "claude-code":
        path.write_text("{}" if text is None else text, encoding="utf-8")
    return path


def names(folder):
    return sorted(p.name for p in folder.iterdir())


def moved(monkeypatch):
    """The app now lives somewhere else: every entry written so far is out of date."""
    monkeypatch.setattr(sys, "executable", "/new/place/python")
    return "/new/place/djs"


# ---- the table
def test_command_is_the_console_program_next_to_the_interpreter_or_the_app(home, monkeypatch):
    assert connect.command() == C
    monkeypatch.setattr(sys, "executable", "/Applications/找岗位助手.app/Contents/MacOS/找岗位助手")
    assert connect.command() == "/Applications/找岗位助手.app/Contents/MacOS/djs"
    monkeypatch.setattr(sys, "executable", "/app/找岗位助手.exe")
    assert connect.command() == "/app/djs.exe"
    monkeypatch.setattr(sys, "executable", "/venv/bin/python3.12")   # `with_stem` would give djs.12
    assert connect.command() == "/venv/bin/djs"


def test_a_translocated_app_is_not_written_into_an_assistant(home, monkeypatch):
    """A downloaded app that was not moved runs from a random read-only copy: its path would not last."""
    key = "cursor"
    (home / ".cursor").mkdir()
    monkeypatch.setattr(connect, "command", lambda: "/private/var/folders/x/AppTranslocation/ABC/d/找岗位助手.app/Contents/MacOS/djs")
    with pytest.raises(ValueError, match="应用程序"):
        connect.connect(key)
    assert not (home / ".cursor" / "mcp.json").exists()


def test_targets_and_where_their_files_are(home):
    assert [(t.key, t.label, t.group) for t in connect.TARGETS] == [
        ("claude-code", "Claude Code", "intl"), ("claude-desktop", "Claude Desktop", "intl"), ("codex", "Codex", "intl"),
        ("gemini-cli", "Gemini CLI", "intl"), ("antigravity", "Antigravity", "intl"), ("cursor", "Cursor", "intl"),
        ("kiro", "Kiro", "intl"), ("opencode", "OpenCode", "intl"), ("workbuddy", "WorkBuddy", "cn"),
        ("codebuddy", "CodeBuddy", "cn"), ("qwen-code", "Qwen Code", "cn"), ("kimi-code", "Kimi Code", "cn")]
    assert {t.key: t.path() for t in connect.TARGETS} == {k: home / p for k, p in PATHS.items()}
    assert [(g.key, g.label) for g in connect.GUIDED] == [
        ("qoder", "Qoder"), ("trae", "Trae / Trae CN"), ("lingma", "通义灵码"), ("cherry-studio", "Cherry Studio")]
    assert all("粘贴" in g.where for g in connect.GUIDED)


def test_claude_desktop_lives_where_each_system_keeps_application_data(home, monkeypatch, tmp_path):
    target = connect.find("claude-desktop")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))   # not under the home folder: the variable is what counts
    assert target.path() == tmp_path / "roaming/Claude/claude_desktop_config.json"
    monkeypatch.delenv("APPDATA")
    assert target.path() == home / "AppData/Roaming/Claude/claude_desktop_config.json"   # no variable: the usual place
    monkeypatch.setattr(sys, "platform", "darwin")
    assert target.path() == home / "Library/Application Support/Claude/claude_desktop_config.json"
    monkeypatch.setattr(sys, "platform", "linux")
    assert target.path() == home / ".config/Claude/claude_desktop_config.json"


def test_claude_desktop_is_written_under_appdata_on_windows(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    folder = home / "AppData" / "Roaming" / "Claude"
    assert not connect.detected(connect.find("claude-desktop"))
    folder.mkdir(parents=True)
    assert connect.connect("claude-desktop") is True
    assert json.loads((folder / "claude_desktop_config.json").read_text()) == {"mcpServers": {NAME: {"command": C, "args": []}}}


def test_kimi_code_follows_its_home_variable(home, monkeypatch, tmp_path):
    (home / ".kimi-code").mkdir()
    target = connect.find("kimi-code")
    assert target.path() == home / ".kimi-code" / "mcp.json" and connect.detected(target)
    elsewhere = tmp_path / "kimi"
    monkeypatch.setenv("KIMI_CODE_HOME", str(elsewhere))
    assert target.path() == elsewhere / "mcp.json" and not connect.detected(target)   # its folder is the variable's
    elsewhere.mkdir()
    assert connect.connect("kimi-code") is True
    assert json.loads((elsewhere / "mcp.json").read_text())["mcpServers"][NAME]["command"] == C
    assert names(home / ".kimi-code") == []


def test_an_assistant_is_found_by_its_own_folder_and_claude_code_by_its_file(home):
    detected = lambda: {t.key for t in connect.TARGETS if connect.detected(t)}   # noqa: E731
    assert detected() == set()   # a home folder alone is no assistant
    (home / ".gemini").mkdir()
    assert detected() == {"gemini-cli"}   # antigravity has its own folder inside
    (home / ".gemini" / "antigravity").mkdir()
    (home / ".kiro").mkdir()
    assert detected() == {"gemini-cli", "antigravity"}   # Kiro's config sits one folder deeper
    (home / ".kiro" / "settings").mkdir()
    for folder in (".codex", ".cursor", ".config/opencode", ".workbuddy", ".codebuddy", ".qwen", ".kimi-code",
                   "Library/Application Support/Claude"):
        (home / folder).mkdir(parents=True)
    assert detected() == set(PATHS) - {"claude-code"}
    (home / ".claude").mkdir()
    assert "claude-code" not in detected()   # its folder is not its file
    (home / ".claude.json").write_text("{}")
    assert "claude-code" in detected()


def test_a_folder_we_may_not_look_into_is_not_an_assistant_and_does_not_break_the_list(home, monkeypatch):
    def denied(self):
        raise PermissionError("denied")   # what Python 3.12 does for a folder made with sudo
    monkeypatch.setattr(Path, "is_dir", denied)
    monkeypatch.setattr(Path, "is_file", denied)
    assert not any(connect.detected(t) for t in connect.TARGETS)
    assert not any(t["detected"] for t in connect.status()["targets"])


def test_nothing_is_written_for_an_assistant_that_is_not_there(home):
    with pytest.raises(ValueError, match="没有检测到 Cursor"):
        connect.connect("cursor")
    assert os.listdir(home) == []   # not even its folder
    with pytest.raises(ValueError, match="未知的应用"):
        connect.connect("nope")
    assert connect.disconnect("cursor") is False and os.listdir(home) == []   # nothing to take out of a file that is not there


def test_config_text_is_what_a_guided_assistant_pastes(home):
    assert connect.config_text() == json.dumps({"mcpServers": {NAME: {"command": C}}}, indent=2, ensure_ascii=False)
    assert json.loads(connect.status()["config"]) == {"mcpServers": {NAME: {"command": C}}}


# ---- JSON assistants
@pytest.mark.parametrize("key", JSON_KEYS)
def test_connect_creates_the_entry_and_the_file(home, key):
    path = installed(home, key)
    existed = path.exists()   # only Claude Code is found by its file
    assert connect.connect(key) is True
    assert path.read_text(encoding="utf-8") == dumped({container(key): {NAME: entry(key)}})
    assert names(path.parent) == sorted({path.name, *([path.name + ".djs-bak"] if existed else [])})   # no backup of nothing, no temp file
    assert (path.parent / (path.name + ".djs-bak")).exists() is existed


@pytest.mark.parametrize("key", JSON_KEYS)
def test_connect_keeps_everything_else_in_the_file(home, key):
    other = {"command": "npx", "args": ["-y", "天气"], "env": {"K": "v"}}
    before = {"theme": "暗色", container(key): {"other": other, "again": {"url": "http://x"}}, "tail": [1, {"a": None}]}
    original = json.dumps(before, indent=4, ensure_ascii=False)   # the user's own layout
    path = installed(home, key, original)
    connect.connect(key)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data) == ["theme", container(key), "tail"] and list(data[container(key)]) == ["other", "again", NAME]
    assert data == {**before, container(key): {**before[container(key)], NAME: entry(key)}}
    assert path.read_text(encoding="utf-8") == dumped(data) and "天气" in path.read_text(encoding="utf-8")   # indent 2, no \u escapes
    assert (path.parent / (path.name + ".djs-bak")).read_text(encoding="utf-8") == original   # the user's file as it was


@pytest.mark.parametrize("key", JSON_KEYS)
def test_a_file_without_the_servers_key_gets_it_last(home, key):
    path = installed(home, key, '{"a": 1, "b": {"c": 2}}')
    connect.connect(key)
    assert list(json.loads(path.read_text())) == ["a", "b", container(key)]


@pytest.mark.parametrize("key", JSON_KEYS)
def test_connecting_twice_changes_nothing_the_second_time(home, key):
    path = installed(home, key, '{"mcpServers": {}, "mcp": {}}')
    assert connect.connect(key) is True
    stat, data = path.stat(), path.read_bytes()
    listing = names(path.parent)
    assert connect.connect(key) is False
    assert (path.stat().st_mtime_ns, path.read_bytes(), names(path.parent)) == (stat.st_mtime_ns, data, listing)   # no rewrite, no new backup


@pytest.mark.parametrize("key", JSON_KEYS)
def test_an_out_of_date_entry_is_replaced_and_the_first_backup_stays(home, monkeypatch, key):
    original = dumped({"keep": 1, container(key): {NAME: {"command": "djs", "args": []}, "other": {"command": "x"}}})   # the README's old line
    path = installed(home, key, original)
    assert connect.status()["targets"][list(PATHS).index(key)]["state"] == "stale"
    assert connect.connect(key) is True
    data = json.loads(path.read_text())
    assert data[container(key)][NAME] == entry(key) and data[container(key)]["other"] == {"command": "x"} and list(data[container(key)]) == [NAME, "other"]
    new = moved(monkeypatch)
    assert connect.connect(key) is True   # and again, when the app moves
    assert json.loads(path.read_text())[container(key)][NAME] == entry(key, new)
    assert (path.parent / (path.name + ".djs-bak")).read_text() == original   # the first one, never overwritten


@pytest.mark.parametrize("key", JSON_KEYS)
def test_an_out_of_date_entry_keeps_the_environment_variables_the_user_put_in_it(home, key):
    env = "environment" if key == "opencode" else "env"
    mine = {"command": "uvx", "args": ["--from", "git+https://x", "djs"], env: {"DJS_JOB51_HMAC_KEY": "k"}}   # the README's other way, plus the keys
    path = installed(home, key, dumped({container(key): {"first": {}, NAME: mine, "last": {}}}))
    connect.connect(key)
    servers = json.loads(path.read_text())[container(key)]
    assert list(servers) == ["first", NAME, "last"]   # in place
    assert servers[NAME] == {**entry(key), env: {"DJS_JOB51_HMAC_KEY": "k"}}   # ours wins; no argument of the old command is left over
    assert connect.connect(key) is False and connect.status()["targets"][list(PATHS).index(key)]["state"] == "connected"


@pytest.mark.parametrize("key", JSON_KEYS)
def test_disconnect_removes_only_our_entry(home, key):
    path = installed(home, key, dumped({"keep": 1, container(key): {"other": {"command": "x"}}}))
    connect.connect(key)
    assert connect.disconnect(key) is True
    assert json.loads(path.read_text()) == {"keep": 1, container(key): {"other": {"command": "x"}}}
    assert connect.disconnect(key) is False   # nothing left to remove
    path.write_text(dumped({container(key): {NAME: entry(key)}}))
    assert connect.disconnect(key) is True
    assert json.loads(path.read_text()) == {container(key): {}}   # the emptied container is the assistant's own key: it stays


def test_disconnect_with_no_file_or_no_entry_writes_nothing(home):
    path = installed(home, "cursor")
    assert connect.disconnect("cursor") is False and names(path.parent) == []
    path.write_text('{"mcpServers": {"other": {}}}')
    stat = path.stat()
    assert connect.disconnect("cursor") is False
    assert (path.stat().st_mtime_ns, names(path.parent)) == (stat.st_mtime_ns, ["mcp.json"])   # untouched, and not backed up


UNREADABLE = {
    "comments": '// my servers\n{\n  "mcpServers": {}\n}\n',
    "broken": '{"mcpServers": {',
    "not an object": '["mcpServers"]',
    "servers not an object": '{"mcpServers": ["a"], "mcp": ["a"]}',
    "not text": b"\xff\xfe{\x00",
}


@pytest.mark.parametrize("what", UNREADABLE)
@pytest.mark.parametrize("key", JSON_KEYS)
def test_a_file_we_cannot_read_is_refused_and_stays_byte_for_byte_as_it_was(home, key, what):
    path = installed(home, key)
    body = UNREADABLE[what]
    path.write_bytes(body if isinstance(body, bytes) else body.encode())
    before, stat = path.read_bytes(), path.stat()
    expected = f"配置文件读不懂（可能带有注释）：{path}，请改用“复制配置”手动添加"
    for action in (connect.connect, connect.disconnect):
        with pytest.raises(ValueError) as error:
            action(key)
        assert str(error.value) == expected
    assert (path.read_bytes(), path.stat().st_mtime_ns, names(path.parent)) == (before, stat.st_mtime_ns, [path.name])   # no backup, no temp file
    assert connect.status()["targets"][list(PATHS).index(key)]["state"] == "unreadable"


def test_an_empty_file_is_an_empty_config_and_a_byte_order_mark_is_not_a_problem(home):
    path = installed(home, "cursor", "  \n")
    assert connect.status()["targets"][5]["state"] == "off"
    connect.connect("cursor")
    assert json.loads(path.read_text()) == {"mcpServers": {NAME: entry("cursor")}}
    path.write_bytes(b"\xef\xbb\xbf" + b'{"mcpServers": {"other": {"command": "x"}}}')
    connect.connect("cursor")
    assert path.read_bytes().startswith(b"{") and list(json.loads(path.read_text())["mcpServers"]) == ["other", NAME]


def test_a_file_with_windows_line_endings_keeps_them_all_the_way_through(home):
    path = installed(home, "cursor", '{\r\n  "mcpServers": {\r\n    "other": {}\r\n  }\r\n}\r\n')
    connect.connect("cursor")
    raw = path.read_bytes()
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0 and raw.endswith(b"}\r\n")   # no line ending of the other kind
    assert list(json.loads(raw)["mcpServers"]) == ["other", NAME]
    connect.disconnect("cursor")
    assert path.read_bytes() == b'{\r\n  "mcpServers": {\r\n    "other": {}\r\n  }\r\n}\r\n'
    path.write_bytes(b'{"mcpServers": {}}')   # no final line ending at all: ours is the Unix one
    connect.connect("cursor")
    assert b"\r" not in path.read_bytes() and path.read_bytes().endswith(b"}\n")


def test_a_lone_surrogate_the_file_carries_as_an_escape_does_not_stop_the_write(home):
    path = installed(home, "claude-code", '{"history": ["cut \\ud83d"], "name": "天气", "mcpServers": {}}')   # JSON.stringify writes these
    assert connect.connect("claude-code") is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["history"] == ["cut \ud83d"] and data["mcpServers"][NAME] == entry("claude-code")   # nothing lost
    assert names(path.parent) == [".claude.json", ".claude.json.djs-bak"]


CMD = 'C:\\Users\\x "y"\\找岗位助手\\djs.exe'   # backslashes, quotes and the app's own name


@pytest.mark.parametrize("key", ["cursor", "opencode", "codex"])
def test_a_command_with_backslashes_quotes_and_chinese_letters_is_written_exactly(home, monkeypatch, key):
    monkeypatch.setattr(connect, "command", lambda: CMD)
    path = installed(home, key)
    connect.connect(key)
    text = path.read_text(encoding="utf-8")
    if key == "codex":
        assert tomllib.loads(text)["mcp_servers"][NAME] == {"command": CMD, "args": []}
        assert 'command = "C:\\\\Users\\\\x \\"y\\"\\\\找岗位助手\\\\djs.exe"' in text   # a basic string: each backslash and quote escaped
    else:
        assert json.loads(text)[container(key)][NAME] == entry(key, CMD)
        assert "找岗位助手" in text and "\\u" not in text   # the letters themselves, not escapes
    assert connect.status()["targets"][list(PATHS).index(key)]["state"] == "connected"


def test_each_state_is_told_apart(home):
    assert {t["state"] for t in connect.status()["targets"]} == {"off"}   # no file at all is off, found or not
    installed(home, "cursor", dumped({"mcpServers": {NAME: entry("cursor")}}))
    installed(home, "kiro", dumped({"mcpServers": {NAME: {"command": "/old/djs", "args": []}}}))
    installed(home, "qwen-code", dumped({"mcpServers": {"other": {}}}))
    installed(home, "workbuddy", "{not json")
    installed(home, "opencode", dumped({"mcp": {NAME: {"type": "local", "command": ["/old/djs"], "enabled": True}}}))
    installed(home, "codex", f'[mcp_servers.{NAME}]\ncommand = "{C}"\n')
    states = {t["key"]: t["state"] for t in connect.status()["targets"]}
    assert states["cursor"] == "connected" and states["kiro"] == "stale" and states["qwen-code"] == "off"
    assert states["workbuddy"] == "unreadable" and states["opencode"] == "stale" and states["codex"] == "connected"
    assert states["gemini-cli"] == "off"


def test_opencode_gets_its_own_shape_under_its_own_key(home):
    path = installed(home, "opencode", dumped({"$schema": "https://opencode.ai/config.json", "mcp": {"x": {"type": "remote", "url": "u"}}}))
    connect.connect("opencode")
    data = json.loads(path.read_text())
    assert data["mcp"][NAME] == {"type": "local", "command": [C], "enabled": True}
    assert "mcpServers" not in data and data["$schema"] and data["mcp"]["x"] == {"type": "remote", "url": "u"}


# ---- TOML (Codex)
TABLE = f'[mcp_servers.{NAME}]\ncommand = "/old/djs"\nargs = []\n'
CODEX = f'''# my codex
model = "gpt-5"   # the default

[mcp_servers.other]
command = "npx"
args = ["-y", "thing"]

{TABLE}
# fast runs
[profiles.fast]
model = "mini"
'''
OURS = f'[mcp_servers.{NAME}]\ncommand = "{C}"\nargs = []\n'


def test_codex_gets_a_new_table_and_a_missing_file_is_created(home):
    path = installed(home, "codex")
    assert connect.connect("codex") is True
    assert path.read_text() == OURS and names(path.parent) == ["config.toml"]
    assert tomllib.loads(path.read_text())["mcp_servers"][NAME] == {"command": C, "args": []}


def test_codex_keeps_comments_and_other_tables_and_replaces_only_our_table(home):
    path = installed(home, "codex", CODEX)
    assert connect.connect("codex") is True
    text = path.read_text()
    assert text == CODEX.replace(TABLE, "") + "\n" + OURS   # nothing re-serialised: the old table cut out, the new one last
    assert "# my codex" in text and "# the default" in text and "# fast runs" in text   # the comment above the next table stays with it
    data = tomllib.loads(text)
    assert data["mcp_servers"]["other"]["args"] == ["-y", "thing"] and data["profiles"]["fast"] == {"model": "mini"}
    assert text.count(NAME) == 1 and data["mcp_servers"][NAME]["command"] == C
    assert (path.parent / "config.toml.djs-bak").read_text() == CODEX


def test_codex_twice_changes_nothing_and_disconnect_cuts_only_our_table(home):
    path = installed(home, "codex", CODEX)
    connect.connect("codex")
    stat, data = path.stat(), path.read_bytes()
    assert connect.connect("codex") is False
    assert (path.stat().st_mtime_ns, path.read_bytes(), names(path.parent)) == (stat.st_mtime_ns, data, ["config.toml", "config.toml.djs-bak"])
    assert connect.disconnect("codex") is True
    assert path.read_text() == CODEX.replace(TABLE, "") + "\n"   # our table is gone, the blank line that set it apart is not worth a rewrite
    assert NAME not in path.read_text() and tomllib.loads(path.read_text())["profiles"]["fast"] == {"model": "mini"}
    assert connect.disconnect("codex") is False


def test_codex_tables_below_ours_stay_when_it_is_replaced_and_go_when_it_is_removed(home):
    text = f'{TABLE}\n[mcp_servers.{NAME}.env]\nK = "v"\n\n[other]\na = 1\n'
    path = installed(home, "codex", text)
    connect.connect("codex")
    data = tomllib.loads(path.read_text())
    assert data["mcp_servers"][NAME] == {"command": C, "args": [], "env": {"K": "v"}} and data["other"] == {"a": 1}
    connect.disconnect("codex")
    assert tomllib.loads(path.read_text()) == {"other": {"a": 1}}


def test_codex_finds_our_table_however_its_header_is_spelled(home):
    path = installed(home, "codex", f'[mcp_servers."{NAME}"]  # mine\ncommand = "/old"\n\n[ x ]\na = 1\n')
    connect.connect("codex")
    assert path.read_text().count(NAME) == 1 and tomllib.loads(path.read_text())["mcp_servers"][NAME]["command"] == C


def test_codex_keeps_windows_line_endings(home):
    path = installed(home, "codex", '# c\r\n[a]\r\nx = 1\r\n')
    connect.connect("codex")
    raw = path.read_bytes()
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0 and raw.startswith(b"# c\r\n[a]\r\nx = 1\r\n\r\n[mcp_servers.")
    assert tomllib.loads(path.read_text())["mcp_servers"][NAME]["command"] == C
    connect.disconnect("codex")
    assert path.read_bytes() == b"# c\r\n[a]\r\nx = 1\r\n\r\n"   # still no bare line ending
    assert tomllib.loads(path.read_text()) == {"a": {"x": 1}}


def test_codex_without_a_final_newline_gets_one_before_our_table(home):
    path = installed(home, "codex", 'model = "x"')
    connect.connect("codex")
    assert path.read_text() == f'model = "x"\n\n{OURS}'
    path.write_text(f'model = "x"\n[mcp_servers.{NAME}]\ncommand = "/old"')   # ours is last and has no newline either
    connect.connect("codex")
    assert path.read_text() == f'model = "x"\n\n{OURS}'


def test_codex_with_a_file_that_is_not_toml_is_refused_untouched(home):
    path = installed(home, "codex", 'model = \n[x\n')
    for action in (connect.connect, connect.disconnect):
        with pytest.raises(ValueError) as error:
            action("codex")
        assert str(error.value) == f"配置文件读不懂：{path}，请改用“复制配置”手动添加"
    assert path.read_text() == 'model = \n[x\n' and names(path.parent) == ["config.toml"]
    assert connect.status()["targets"][2]["state"] == "unreadable"


def test_codex_with_our_entry_written_in_a_form_we_cannot_cut_is_refused_untouched(home):
    text = f'[mcp_servers]\n{NAME} = {{ command = "/old/djs" }}\n'
    path = installed(home, "codex", text)
    assert connect.status()["targets"][2]["state"] == "stale"
    for action in (connect.connect, connect.disconnect):
        with pytest.raises(ValueError):   # a second table would make the file invalid: it is never written
            action("codex")
    assert path.read_text() == text and names(path.parent) == ["config.toml"]


def test_codex_with_our_table_cut_short_by_a_line_that_starts_with_a_bracket_is_refused_untouched(home):
    text = f'[a]\nx = 1\n\n[mcp_servers.{NAME}]\ncommand = "/old/djs"\nmatrix = [\n  [1],\n  [2],\n]\n'   # the cut would leave `[2],` and `]`
    path = installed(home, "codex", text)
    for action in (connect.connect, connect.disconnect):
        with pytest.raises(ValueError, match="配置文件读不懂"):   # the result is checked with tomllib before it replaces anything
            action("codex")
    assert path.read_text() == text and names(path.parent) == ["config.toml"]


# ---- how the file is written
def test_the_file_keeps_its_permissions_and_so_does_its_backup(home):
    if os.name == "nt":
        pytest.skip("no permission bits")
    path = installed(home, "cursor", '{"token": "secret"}')
    path.chmod(0o640)
    connect.connect("cursor")
    assert path.stat().st_mode & 0o777 == 0o640
    assert (path.parent / "mcp.json.djs-bak").stat().st_mode & 0o777 == 0o640   # it holds the same secrets


def refuse(*args, **kw):
    raise PermissionError("denied")


def test_a_failed_replace_leaves_the_file_as_it_was_and_no_temp_file_behind(home, monkeypatch):
    path = installed(home, "cursor", '{"mcpServers": {"other": {}}}')
    before = path.read_bytes()
    monkeypatch.setattr(connect.os, "replace", refuse)
    with pytest.raises(ValueError, match=f"没能写入配置文件：{path}"):
        connect.connect("cursor")
    assert path.read_bytes() == before and set(names(path.parent)) <= {"mcp.json", "mcp.json.djs-bak"}   # no *.tmp


def test_a_temp_file_that_cannot_be_made_leaves_the_file_as_it_was(home, monkeypatch):
    path = installed(home, "cursor", '{"mcpServers": {"other": {}}}')
    monkeypatch.setattr(connect.tempfile, "mkstemp", refuse)
    with pytest.raises(ValueError, match="没能写入配置文件"):
        connect.connect("cursor")
    assert path.read_text() == '{"mcpServers": {"other": {}}}' and names(path.parent) == ["mcp.json"]   # nothing was made, so no backup


def test_a_failure_while_writing_the_temp_file_removes_it(home, monkeypatch):
    path = installed(home, "cursor", "{}")

    def broken(fd, *args):
        os.close(fd)
        raise OSError("disk full")
    monkeypatch.setattr(connect.os, "fdopen", broken)
    with pytest.raises(ValueError):
        connect.connect("cursor")
    assert names(path.parent) == ["mcp.json"] and path.read_text() == "{}"


def test_half_a_backup_is_not_left_behind_to_stand_for_the_original(home, monkeypatch):
    path = installed(home, "cursor", '{"first": true}')
    real = connect.shutil.copymode

    def copymode(src, dst):
        if str(dst).endswith(".djs-bak"):
            raise OSError("no space left")
        real(src, dst)
    monkeypatch.setattr(connect.shutil, "copymode", copymode)
    with pytest.raises(ValueError, match="没能写入配置文件"):
        connect.connect("cursor")
    assert names(path.parent) == ["mcp.json"] and path.read_text() == '{"first": true}'
    monkeypatch.setattr(connect.shutil, "copymode", real)
    connect.connect("cursor")   # the next try makes a whole one
    assert (path.parent / "mcp.json.djs-bak").read_text() == '{"first": true}'


def test_the_backup_is_made_once_and_never_over_an_older_one(home):
    path = installed(home, "cursor", '{"first": true}')
    connect.connect("cursor")
    connect.disconnect("cursor")
    path.write_text('{"second": true}')
    connect.connect("cursor")
    assert (path.parent / "mcp.json.djs-bak").read_text() == '{"first": true}'
    (path.parent / "mcp.json.djs-bak").write_text("the user's own copy")
    connect.disconnect("cursor")
    assert (path.parent / "mcp.json.djs-bak").read_text() == "the user's own copy"   # never overwritten, whoever made it


def test_a_symlinked_config_is_written_through_and_the_link_stays(home):
    if os.name == "nt":
        pytest.skip("symlinks need a privilege")
    real = home / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text('{"mcpServers": {"other": {}}}')
    link = installed(home, "cursor")
    link.symlink_to(real)
    connect.connect("cursor")
    assert link.is_symlink() and list(json.loads(real.read_text())["mcpServers"]) == ["other", NAME]
    assert names(link.parent) == ["mcp.json"] and (real.parent / "mcp.json.djs-bak").exists()
    assert connect.status()["targets"][5]["state"] == "connected"


def test_a_link_to_a_folder_that_is_not_there_yet_gets_the_folder_made_for_the_file(home):
    if os.name == "nt":
        pytest.skip("symlinks need a privilege")
    link = installed(home, "cursor")
    link.symlink_to(home / "dotfiles" / "cursor" / "mcp.json")   # a dotfiles folder that was not set up on this computer yet
    assert connect.connect("cursor") is True
    assert link.is_symlink() and json.loads(link.read_text())["mcpServers"][NAME] == entry("cursor")
    assert names(home / "dotfiles" / "cursor") == ["mcp.json"]


def meddle(monkeypatch, path, times):
    """Someone else changes the file after we read it: on the first `times` writes, just before the check."""
    real, calls = tempfile.mkstemp, []

    def mkstemp(*args, **kw):
        calls.append(1)
        if len(calls) <= times:
            data = json.loads(path.read_text()) if path.exists() else {}
            data[f"other{len(calls)}"] = len(calls)
            path.write_text(json.dumps(data))
        return real(*args, **kw)
    monkeypatch.setattr(connect.tempfile, "mkstemp", mkstemp)
    return calls


def test_a_file_changed_while_we_write_is_read_again_and_done_once_more(home, monkeypatch):
    path = installed(home, "cursor", '{"mcpServers": {"other": {}}}')
    calls = meddle(monkeypatch, path, times=1)
    assert connect.connect("cursor") is True
    data = json.loads(path.read_text())
    assert data["other1"] == 1 and list(data["mcpServers"]) == ["other", NAME]   # their change is in, ours on top of it
    assert len(calls) == 2 and names(path.parent) == ["mcp.json", "mcp.json.djs-bak"]
    assert json.loads((path.parent / "mcp.json.djs-bak").read_text()).get("other1") == 1   # the backup is of what we replaced


def test_a_file_that_keeps_changing_is_given_up_untouched(home, monkeypatch):
    path = installed(home, "cursor", '{"mcpServers": {}}')
    calls = meddle(monkeypatch, path, times=9)
    with pytest.raises(ValueError, match="^配置文件正在被其他程序修改，请稍后再试$"):
        connect.connect("cursor")
    assert len(calls) == 2   # read, redo once, then stop
    assert json.loads(path.read_text()) == {"mcpServers": {}, "other1": 1, "other2": 2}   # only their changes
    assert names(path.parent) == ["mcp.json"]   # no temp file, no backup of a file we did not replace


def test_a_file_that_appears_while_we_write_is_not_overwritten(home, monkeypatch):
    path = installed(home, "cursor")
    meddle(monkeypatch, path, times=1)
    assert connect.connect("cursor") is True
    assert json.loads(path.read_text()) == {"other1": 1, "mcpServers": {NAME: entry("cursor")}}


# ---- every start
def test_sync_refreshes_what_points_elsewhere_and_nothing_else(home, monkeypatch):
    cursor = installed(home, "cursor", dumped({"mcpServers": {NAME: entry("cursor")}}))
    codex = installed(home, "codex", OURS)
    kiro = installed(home, "kiro", dumped({"mcpServers": {"other": {}}}))
    qwen = installed(home, "qwen-code", dumped({"mcpServers": {NAME: entry("qwen-code", "/old/djs")}}))
    new = moved(monkeypatch)
    assert connect.sync(["cursor", "codex", "kiro"]) == {}
    assert json.loads(cursor.read_text())["mcpServers"][NAME]["command"] == new
    assert tomllib.loads(codex.read_text())["mcp_servers"][NAME]["command"] == new
    assert json.loads(kiro.read_text()) == {"mcpServers": {"other": {}}}   # the user did not switch it on: no entry is added
    assert json.loads(qwen.read_text())["mcpServers"][NAME]["command"] == "/old/djs"   # not in the list: not touched
    before = (cursor.read_bytes(), cursor.stat().st_mtime_ns)
    assert connect.sync(["cursor"]) == {} and (cursor.read_bytes(), cursor.stat().st_mtime_ns) == before   # up to date


def test_sync_swallows_failures_and_says_which_assistant_and_why(home, monkeypatch):
    cursor = installed(home, "cursor", dumped({"mcpServers": {NAME: entry("cursor")}}))
    kiro = installed(home, "kiro", dumped({"mcpServers": {NAME: entry("kiro")}}))
    moved(monkeypatch)
    real = os.replace

    def replace(src, dst):
        if Path(dst) == cursor:
            raise PermissionError("denied")
        real(src, dst)
    monkeypatch.setattr(connect.os, "replace", replace)
    errors = connect.sync(["cursor", "nope", "kiro"])
    assert set(errors) == {"cursor", "nope"} and str(cursor) in errors["cursor"] and errors["nope"] == "未知的应用"
    assert json.loads(kiro.read_text())["mcpServers"][NAME]["command"] == "/new/place/djs"   # the others went on


def test_status_lists_every_assistant_with_the_path_shown_from_home(home):
    installed(home, "cursor")
    body = connect.status()
    assert set(body) == {"command", "config", "targets", "guided"} and body["command"] == C
    assert [t["key"] for t in body["targets"]] == list(PATHS)
    assert {t["key"]: t["detected"] for t in body["targets"]} == {k: k == "cursor" for k in PATHS}
    assert {t["key"]: t["path"] for t in body["targets"]}["cursor"] == "~/.cursor/mcp.json"
    assert {t["key"]: t["path"] for t in body["targets"]}["claude-desktop"] == "~/Library/Application Support/Claude/claude_desktop_config.json"
    assert body["targets"][0] == {"key": "claude-code", "label": "Claude Code", "group": "intl", "detected": False, "state": "off",
                                  "path": "~/.claude.json"}
    assert body["guided"][0] == {"key": "qoder", "label": "Qoder", "where": "设置 → MCP → 我的服务 → 添加，粘贴配置"}
    assert len(body["guided"]) == 4
