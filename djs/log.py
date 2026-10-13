"""The app's log file, <data>/logs/djs.log: rotating, UTF-8, and cleaned of what must not be kept (query strings of URLs,
cookies, tokens, keys) before a line is written. Every part logs under `djs.<part>` (get); setup() is called once by
whoever starts the process and never raises: a folder that cannot be written leaves the log on stderr."""
from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from djs import db

NAME = "djs"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUPS = 3

_QUERY = re.compile(r"""(https?://[^\s?#'"<>)\]]*)[?#][^\s'"<>)\]]*""", re.I)
_USER = re.compile(r"""([a-z][a-z0-9+.-]*://)[^/\s@'"<>]+@""", re.I)   # `http://name:password@proxy:7890`
# a name ending in cookie / token / key / secret / password / authorization (`__zp_stoken__` too), then `=`, `:` or
# `": "`; a quoted value is masked by itself, an unquoted one up to the next separator
_NAME = r"""[\w.-]{0,30}(?:cookie|token|key|secret|password|authorization)s?[_-]*["']?\s*[:=]\s*"""
_VALUE = re.compile(rf"""({_NAME})("[^"]*"|'[^']*'|[^\s,;&"'}}\]]+)""", re.I)
# the rest of the line for the two that carry several words (`Authorization: Bearer abc`, `Cookie: a=1; b=2`)
_LINE = re.compile(r"""([\w.-]{0,30}(?:cookie|authorization)s?[_-]*["']?\s*[:=]\s*+)(?!["'])[^\r\n]+""", re.I)


def redact(text: str) -> str:
    """`text` without the query and fragment of any http(s) URL, without the name and password in front of a host and with
    the values of secret-looking names masked."""
    text = _LINE.sub(lambda m: m[1] + "***", _USER.sub(r"\1***@", _QUERY.sub(r"\1", text)))
    return _VALUE.sub(lambda m: m[1] + (m[2][0] + "***" + m[2][0] if m[2][0] in "\"'" else "***"), text)


class _Redact(logging.Filter):
    """On the handler (not the logger), so it covers every `djs.*` logger and the tracebacks too."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = redact(record.getMessage()), None
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:   # a line that cannot be cleaned is not written, and the caller of log.info() never hears of it
            return False
        return True


def path() -> Path:
    """The log file (for 「打开日志文件夹」: its folder). Not created by asking."""
    try:
        base = db.home()
    except OSError:
        base = Path(os.environ.get("DJS_HOME") or Path.home() / ".dear-job-seeker")
    return base / "logs" / "djs.log"


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"{NAME}.{name}")


def _ours(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if getattr(h, "djs_handler", False)]


def setup() -> None:
    """Send `djs.*` to the log file (level INFO, DEBUG when DJS_DEBUG=1). Calling it again changes nothing, unless the
    data folder is another one now (the file handler moves with it)."""
    try:
        logger = logging.getLogger(NAME)
        logger.setLevel(logging.DEBUG if os.environ.get("DJS_DEBUG") == "1" else logging.INFO)
        # mcp configures the root logger at INFO with a stderr handler: our lines belong in the file only, and httpx's
        # per-request INFO line carries the signed URL of a download
        logger.propagate = False
        logging.getLogger("httpx").setLevel(logging.WARNING)
        file = path()
        current = _ours(logger)
        if len(current) == 1 and current[0].djs_path == os.path.abspath(file):  # type: ignore[attr-defined]
            return
        for h in current:
            logger.removeHandler(h)
            h.close()
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = RotatingFileHandler(file, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        except OSError as e:
            if sys.stderr is None:  # a windowed app has none: then the log is simply off
                return
            handler = logging.StreamHandler(sys.stderr)
            print(f"djs: no log file at {file} ({e}), logging to stderr", file=sys.stderr)
        handler.setFormatter(logging.Formatter(FORMAT))
        handler.addFilter(_Redact())
        handler.djs_handler, handler.djs_path = True, os.path.abspath(file)  # type: ignore[attr-defined]
        logger.addHandler(handler)
    except Exception:  # logging must never be the reason the app does not start
        pass
