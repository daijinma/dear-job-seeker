"""`.env` autoload. A GUI app started from Finder has no shell environment, so keys (see README) come from a file."""
from __future__ import annotations

import os
from pathlib import Path

from djs import db


def _pairs(text: str):
    for line in text.splitlines():
        line = line.strip().removeprefix("export ")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            yield key, value


def load() -> None:
    """Read $DJS_HOME/.env, then ./.env. Variables that are already set (shell, MCP client env block, or the file
    read first) are never overridden. Empty values are skipped, so a copied .env.example masks nothing."""
    for path in (db.home() / ".env", Path(".env")):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for key, value in _pairs(text):
            if value and key not in os.environ:
                os.environ[key] = value
