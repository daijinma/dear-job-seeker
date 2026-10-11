"""`djs work` or the MCP server stopped from outside (Ctrl-C, SIGTERM from a supervisor, a client that closes the connection)
while a task runs: the task goes back to pending at once, with what it had done, instead of staying 'running' until its lock
expires. The programs are the real ones, each in its own process; the platform is a fake whose pages take a moment."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

FAKE_SITE = """
import signal
import time

from djs import db, providers, taskqueue
from djs.providers.base import Provider

# whatever started the test may have left Ctrl-C ignored: the program under test has to see the usual one
signal.signal(signal.SIGINT, signal.default_int_handler)


class Slow(Provider):
    name = label = "slow"
    interval = jitter = min_interval = 0.0
    first_page, page_size, page_cap = 1, 2, 6

    def fetch(self, client, payload, page):
        time.sleep(0.5)
        return [{"id": page * 10}, {"id": page * 10 + 1}]

    def job_id(self, job):
        return str(job["id"])

    def row(self, job, detail):
        return {"title": "t", "company": "c", "city": None, "salary_text": None, "description": None}


site = Slow()
providers.get = lambda n: site if n == "slow" else (_ for _ in ()).throw(KeyError(n))
providers.names = lambda: ["slow"]
taskqueue.create(db.connect(), "slow", {"keyword": "x", "max_pages": 6, "detail": "never"})
"""
WORK = FAKE_SITE + "from djs import cli\nraise SystemExit(cli.main(['work']))\n"
MCP = FAKE_SITE + "from djs import server\nserver.main()\n"


def task_row(home):
    """The only task of the data folder as the program left it, or None while there is none yet."""
    path = home / "djs.db"
    if not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM tasks").fetchone()
    except sqlite3.OperationalError:   # the tables are not there yet
        return None
    finally:
        conn.close()


def start(tmp_path, program, **popen):
    """The program in its own process, on a data folder of its own; returns (process, data folder)."""
    home, cwd = tmp_path / "home", tmp_path / "cwd"   # cwd: the program autoloads ./.env, which must not be a real one
    cwd.mkdir()
    script = tmp_path / "program.py"
    script.write_text(program)
    proc = subprocess.Popen([sys.executable, str(script)], cwd=cwd, env={**os.environ, "DJS_HOME": str(home)},
                            stderr=subprocess.PIPE, text=True, **popen)
    return proc, home


def wait_until_on_its_second_page(proc, home):
    """The worker has stored the first page of the task and is fetching the next."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        row = task_row(home)
        if row and row["status"] == "running" and json.loads(row["stats"] or "{}").get("pages", 0) >= 1:
            return
        time.sleep(0.05)
    pytest.fail(f"the task never got going: {proc.poll()}")


def assert_handed_back(home):
    row = task_row(home)
    assert (row["status"], row["locked_at"], row["attempts"], row["last_error"]) == ("pending", None, 0, None)
    assert json.loads(row["stats"])["pages"] >= 1   # what it had done stays on the task


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["Ctrl-C", "SIGTERM"])
def test_djs_work_stopped_by_a_signal_hands_its_running_task_back(tmp_path, sig):
    proc, home = start(tmp_path, WORK, stdout=subprocess.DEVNULL)
    try:
        wait_until_on_its_second_page(proc, home)
        began = time.monotonic()
        proc.send_signal(sig)
        code = proc.wait(30)
        left = time.monotonic() - began
        err = proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert code == 0, err
    assert left < 5   # the worker finished its request and left; nothing waited for the whole task
    assert_handed_back(home)


def send(server, message):
    server.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    server.stdin.flush()


@pytest.mark.parametrize("how", ["closes the connection", "sends SIGTERM"])
def test_a_client_that_goes_away_during_a_crawl_gets_its_task_handed_back(tmp_path, how):
    server, home = start(tmp_path, MCP, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        send(server, {"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        assert json.loads(server.stdout.readline())["id"] == 1
        send(server, {"method": "notifications/initialized"})
        wait_until_on_its_second_page(server, home)
        if how == "sends SIGTERM":
            server.send_signal(signal.SIGTERM)
        else:
            server.stdin.close()
        code = server.wait(30)
        err = server.stderr.read()
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
    assert code == 0, err
    assert_handed_back(home)
