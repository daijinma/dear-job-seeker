#!/usr/bin/env bash
# Smoke-test a PyInstaller build: packaging/smoke.sh [dist-dir]
# Run it with the build environment's Python on PATH (it needs the project's dependencies for the MCP client) and
# with network access (one real crawl). The checks themselves are in smoke.py, which also runs on Windows.
exec "$(command -v python || command -v python3)" "$(dirname "$0")/smoke.py" "$@"
