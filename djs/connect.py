"""Connect the app's MCP server (the `djs` console program) to the AI assistants on this computer: write, refresh and
remove our one entry, `dear-job-seeker`, in each assistant's own config file. Nothing else in those files is touched;
a file we cannot read is never written."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

NAME = "dear-job-seeker"
BACKUP = ".djs-bak"
ENV_KEYS = ("env", "environment")   # what a user adds to our entry: OpenCode says `environment`


def command() -> str:
    """Absolute path of the console program: next to the interpreter, or next to the app's own executable. (`with_name`,
    not `with_stem`: a venv's interpreter may be called python3.12.)"""
    exe = Path(sys.executable)
    return str(exe.with_name("djs.exe" if exe.suffix.lower() == ".exe" else "djs"))


# ---- the assistants
def _plain(c: str) -> dict:
    return {"command": c, "args": []}


def _typed(c: str) -> dict:
    return {"type": "stdio", "command": c, "args": []}


def _opencode(c: str) -> dict:
    return {"type": "local", "command": [c], "enabled": True}


def _home(*parts: str) -> Callable[[], Path]:
    return lambda: Path.home().joinpath(*parts)   # resolved per call: the home folder is not fixed at import


def _claude_desktop() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    return base / "Claude" / "claude_desktop_config.json"


def _kimi() -> Path:
    folder = os.environ.get("KIMI_CODE_HOME")
    return (Path(folder) if folder else Path.home() / ".kimi-code") / "mcp.json"


@dataclass(frozen=True)
class Target:
    key: str
    label: str
    group: str                    # "intl" | "cn"
    path: Callable[[], Path]      # the config file
    entry: Callable[[str], dict]  # our entry, for the given command
    container: str = "mcpServers"  # the key (TOML: table) that holds the servers
    toml: bool = False
    by_file: bool = False         # installed = the config file itself exists, not just its folder


@dataclass(frozen=True)
class Guided:
    key: str
    label: str
    where: str                    # where in the assistant the config is pasted


TARGETS = (
    Target("claude-code", "Claude Code", "intl", _home(".claude.json"), _typed, by_file=True),
    Target("claude-desktop", "Claude Desktop", "intl", _claude_desktop, _plain),
    Target("codex", "Codex", "intl", _home(".codex", "config.toml"), _plain, container="mcp_servers", toml=True),
    Target("gemini-cli", "Gemini CLI", "intl", _home(".gemini", "settings.json"), _plain),
    Target("antigravity", "Antigravity", "intl", _home(".gemini", "antigravity", "mcp_config.json"), _plain),
    Target("cursor", "Cursor", "intl", _home(".cursor", "mcp.json"), _plain),
    Target("kiro", "Kiro", "intl", _home(".kiro", "settings", "mcp.json"), _plain),
    Target("opencode", "OpenCode", "intl", _home(".config", "opencode", "opencode.json"), _opencode, container="mcp"),
    Target("workbuddy", "WorkBuddy", "cn", _home(".workbuddy", "mcp.json"), _typed),
    Target("codebuddy", "CodeBuddy", "cn", _home(".codebuddy", ".mcp.json"), _typed),
    Target("qwen-code", "Qwen Code", "cn", _home(".qwen", "settings.json"), _plain),
    Target("kimi-code", "Kimi Code", "cn", _kimi, _plain),
)
GUIDED = (
    Guided("qoder", "Qoder", "设置 → MCP → 我的服务 → 添加，粘贴配置"),
    Guided("trae", "Trae / Trae CN", "设置 → MCP → 添加 → 手动配置，粘贴配置"),
    Guided("lingma", "通义灵码", "设置 → MCP 服务 → 添加，粘贴配置"),
    Guided("cherry-studio", "Cherry Studio", "设置 → MCP 服务器 → 添加 → 从 JSON 导入，粘贴配置"),
)


def find(key: str) -> Target:
    for t in TARGETS:
        if t.key == key:
            return t
    raise ValueError("未知的应用")


def _path(t: Target) -> Path:
    """The file to read and write: through a symlink (a dotfiles folder), so the link itself stays."""
    return Path(os.path.realpath(t.path()))


def detected(t: Target) -> bool:
    p = t.path()
    try:
        return p.is_file() if t.by_file else p.parent.is_dir()
    except OSError:   # a folder we may not look into (one made with sudo): not ours to connect
        return False


def _shown(p: Path) -> str:
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(p)


def _unreadable(t: Target) -> str:
    hint = "" if t.toml else "（可能带有注释）"
    return f"配置文件读不懂{hint}：{t.path()}，请改用“复制配置”手动添加"


# ---- reading
def _stamp(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


def _read(t: Target, p: Path) -> tuple[tuple[int, int] | None, bytes | None]:
    """The file's (mtime, size) taken before its bytes, so a change while reading shows up at the check; (None, None) when
    there is no file."""
    try:
        stamp = _stamp(p)
        return (stamp, p.read_bytes()) if stamp else (None, None)
    except FileNotFoundError:
        return None, None
    except OSError:
        raise ValueError(_unreadable(t)) from None


def _parse(t: Target, raw: bytes | None) -> tuple[str, dict]:
    """(text, document); a missing or empty file is an empty document."""
    try:
        text = (raw or b"").decode("utf-8-sig")   # editors on Windows put a BOM in front
        if t.toml:
            doc = tomllib.loads(text)
        else:
            doc = json.loads(text) if text.strip() else {}
        if not isinstance(doc, dict):
            raise ValueError
    except ValueError:   # json, tomllib and the decoder all raise subclasses of it
        raise ValueError(_unreadable(t)) from None
    return text, doc


def _servers(t: Target, doc: dict) -> dict:
    servers = doc.get(t.container, {})
    if not isinstance(servers, dict):
        raise ValueError(_unreadable(t))
    return servers


def _entry_state(t: Target, doc: dict, c: str) -> str:
    servers = _servers(t, doc)
    if NAME not in servers:
        return "off"
    entry = servers[NAME]
    return "connected" if isinstance(entry, dict) and entry.get("command") == t.entry(c)["command"] else "stale"


def _state(t: Target, c: str) -> str:
    try:
        return _entry_state(t, _parse(t, _read(t, _path(t))[1])[1], c)
    except ValueError:
        return "unreadable"


# ---- editing: each returns the file's new text, or None when there is nothing to change
def _key(name: str) -> str:
    return rf"""(?:{name}|"{name}"|'{name}')"""   # a TOML key may be quoted


_TABLE = re.compile(rf"^\s*\[\s*{_key('mcp_servers')}\s*\.\s*{_key(re.escape(NAME))}\s*(?P<sub>\.[^\]]*)?\]\s*(#.*)?$")


def _toml_cut(text: str, subtables: bool) -> str:
    """The text without our table (and, with `subtables`, the tables below it): from its header to the next line that
    starts with `[`. The blank and comment lines just above that next table belong to it and stay."""
    lines, kept, i = text.splitlines(keepends=True), [], 0
    while i < len(lines):
        m = _TABLE.match(lines[i].rstrip("\r\n"))
        if not m or (m["sub"] and not subtables):
            kept.append(lines[i])
            i += 1
            continue
        head, i = i, i + 1
        while i < len(lines) and not lines[i].lstrip().startswith("["):
            i += 1
        while i > head + 1 and (not lines[i - 1].strip() or lines[i - 1].lstrip().startswith("#")):
            i -= 1
    return "".join(kept)


def _toml_set(text: str, c: str) -> str:
    # ponytail: our table is rewritten whole (sub-tables such as .env stay); inline `env = {...}` or startup_timeout_sec
    # lines the user added to it are lost on a refresh. TOML has no serializer here: keeping them needs line-level edits.
    kept, nl = _toml_cut(text, subtables=False), "\r\n" if "\r\n" in text else "\n"
    if kept and not kept.endswith("\n"):
        kept += nl
    if kept.strip() and not kept.endswith(("\n\n", "\r\n\r\n")):
        kept += nl
    block = (f"[mcp_servers.{NAME}]", f"command = {json.dumps(c, ensure_ascii=False)}", "args = []")
    return kept + nl.join(block) + nl


def _verified(t: Target, new: str, ok: Callable[[dict], bool]) -> str:
    """New TOML text that parses and says what was meant; anything else is refused and the file stays as it is."""
    try:
        servers = _servers(t, tomllib.loads(new))
    except ValueError:
        raise ValueError(_unreadable(t)) from None
    if not ok(servers):
        raise ValueError(_unreadable(t))
    return new


def _dump(doc: dict, text: str) -> str:
    """Two-space JSON with a final newline, in the line endings the file had."""
    body = json.dumps(doc, indent=2, ensure_ascii=False)
    try:
        body.encode("utf-8")
    except UnicodeEncodeError:   # the file carries a lone surrogate as an escape (\ud83d): it stays one
        body = json.dumps(doc, indent=2)
    nl = "\r\n" if "\r\n" in text else "\n"
    return body.replace("\n", nl) + nl


def _with_entry(t: Target, text: str, doc: dict, c: str) -> str | None:
    if _entry_state(t, doc, c) == "connected":
        return None
    if t.toml:
        return _verified(t, _toml_set(text, c), lambda s: s.get(NAME, {}).get("command") == c)
    servers = doc.setdefault(t.container, {})
    old = servers.get(NAME)   # a replaced entry keeps its place and its environment variables (the README sends keys there); a new one goes last
    servers[NAME] = {**t.entry(c), **{k: v for k, v in old.items() if k in ENV_KEYS}} if isinstance(old, dict) else t.entry(c)
    return _dump(doc, text)


def _without_entry(t: Target, text: str, doc: dict, c: str) -> str | None:
    servers = _servers(t, doc)
    if NAME not in servers:
        return None
    if t.toml:
        return _verified(t, _toml_cut(text, subtables=True), lambda s: NAME not in s)
    del servers[NAME]   # an emptied container stays: it is the assistant's own key
    return _dump(doc, text)


# ---- writing
def _backup(p: Path, raw: bytes) -> None:
    """A copy of the file as it was before we first changed it, with its permissions; an existing one is never touched."""
    bak = p.with_name(p.name + BACKUP)
    try:
        fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # private until it has the original's permissions
    except FileExistsError:
        return
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        shutil.copymode(p, bak)
    except OSError:
        bak.unlink(missing_ok=True)   # half a copy would stand for the original from now on
        raise


def _apply(t: Target, edit: Callable[[Target, str, dict, str], str | None]) -> bool:
    """Read, edit, write through a temp file in the same folder and replace. True when the file was changed.
    If the file changed under us between reading and replacing, read again and redo it once."""
    c, p = command(), _path(t)
    for _ in range(2):
        stamp, raw = _read(t, p)
        text, doc = _parse(t, raw)
        new = edit(t, text, doc, c)
        if new is None:
            return False
        tmp = None
        try:
            fd, name = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
            tmp = Path(name)
            with os.fdopen(fd, "wb") as f:
                f.write(new.encode("utf-8"))
            if stamp:
                shutil.copymode(p, tmp)
            if _stamp(p) != stamp:
                continue
            if raw is not None:
                _backup(p, raw)
            os.replace(tmp, p)
            return True
        except OSError:
            raise ValueError(f"没能写入配置文件：{t.path()}，请检查它的权限，或改用“复制配置”手动添加") from None
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)
    raise ValueError("配置文件正在被其他程序修改，请稍后再试")


# ---- public
def connect(key: str) -> bool:
    """Write our entry for the assistant, or refresh it; False when it was there already. Only for an assistant found
    on this computer. Raises ValueError, in Chinese, when it cannot."""
    t = find(key)
    if not detected(t):
        raise ValueError(f"没有检测到 {t.label}")
    if "/AppTranslocation/" in command():   # macOS runs a downloaded app that was not moved from a random read-only copy
        raise ValueError("请先把“找岗位助手”拖进「应用程序」文件夹，再从那里打开并接入：现在这个位置是系统临时放的，路径每次都会变")
    _path(t).parent.mkdir(parents=True, exist_ok=True)
    return _apply(t, _with_entry)


def disconnect(key: str) -> bool:
    """Remove our entry (and nothing else); False when there was none."""
    return _apply(find(key), _without_entry)


def sync(keys) -> dict[str, str]:
    """Refresh our entry wherever it points elsewhere now (the app moved or was updated). Best effort: returns the
    messages of the ones that failed, by key."""
    errors, c = {}, command()
    for key in keys:
        try:
            if _state(find(key), c) == "stale":
                connect(key)
        except Exception as e:
            errors[key] = str(e)
    return errors


def config_text(c: str | None = None) -> str:
    """What to paste into an assistant that has no config file we know of."""
    return json.dumps({"mcpServers": {NAME: {"command": c or command()}}}, indent=2, ensure_ascii=False)


def status() -> dict:
    c = command()
    return {"command": c, "config": config_text(c),
            "targets": [{"key": t.key, "label": t.label, "group": t.group, "detected": detected(t),
                         "state": _state(t, c), "path": _shown(t.path())} for t in TARGETS],
            "guided": [{"key": g.key, "label": g.label, "where": g.where} for g in GUIDED]}
