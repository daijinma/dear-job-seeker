"""The packaged app's entry: the self-test the build runs on itself needs no browser driver."""
import sys

import pytest

from djs import app


def test_selftest_passes_without_playwright(monkeypatch, tmp_path, capsys):
    pytest.importorskip("webview")   # the window's backend: part of what the self-test proves
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    for name in ("playwright", "playwright.sync_api"):
        monkeypatch.setitem(sys.modules, name, None)   # importing either now fails
    app.selftest()
    assert "selftest ok" in capsys.readouterr().out
