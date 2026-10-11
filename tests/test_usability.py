"""Usability features that need no fake platform: database migration, .env autoload, scripting CLI."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from djs import cli, db, engine, envfile, service, taskqueue

# tasks + jobs exactly as v1.1.0 created them: no task_jobs table, jobs only know their first task
OLD_SCHEMA = """
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'keyword',
  payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  locked_at REAL, next_run_at REAL NOT NULL DEFAULT 0, last_error TEXT, stats TEXT,
  created_at REAL NOT NULL DEFAULT (strftime('%s','now')));
CREATE INDEX tasks_claim ON tasks(status, next_run_at);
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, source_job_id TEXT NOT NULL,
  task_id INTEGER REFERENCES tasks(id), title TEXT, company TEXT, city TEXT, salary_text TEXT, description TEXT,
  source_url TEXT, published_at TEXT, published_at_kind TEXT, payload TEXT, detail TEXT, detail_error TEXT,
  first_seen_at REAL NOT NULL DEFAULT (strftime('%s','now')),
  last_seen_at REAL NOT NULL DEFAULT (strftime('%s','now')), UNIQUE(provider, source_job_id));
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # keeps the repo's own .env (cwd autoload) out of every test
    return tmp_path


# ---- database migration ----
def write_old_db(home: Path) -> None:
    conn = sqlite3.connect(home / "djs.db")
    conn.executescript(OLD_SCHEMA)
    conn.executemany("INSERT INTO tasks(id, provider, payload, status) VALUES (?,?,?,'done')",
                     [(1, "nowcoder", '{"keyword": "py"}'), (2, "nowcoder", '{"keyword": "py"}')])
    conn.executemany("INSERT INTO jobs(id, provider, source_job_id, task_id, title) VALUES (?,?,?,?,?)",
                     [(1, "nowcoder", "a", 1, "A"), (2, "nowcoder", "b", 1, "B"), (3, "nowcoder", "c", 2, "C"),
                      (4, "nowcoder", "d", None, "D")])
    conn.commit()
    conn.close()


def test_old_database_is_migrated_and_keeps_working(home):
    write_old_db(home)
    conn = db.connect()
    assert sorted(map(tuple, conn.execute("SELECT task_id, job_id FROM task_jobs"))) == [(1, 1), (1, 2), (2, 3)]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert [j["title"] for j in service.jobs_get(conn, task_id=1, complete_only=False)] == ["B", "A"]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 4
    conn.execute("INSERT INTO task_jobs VALUES (2, 1)")  # a link the engine made after the upgrade
    conn.execute("PRAGMA user_version = 0")  # as if another process were migrating right now
    db.connect().close()
    assert conn.execute("SELECT COUNT(*) FROM task_jobs").fetchone()[0] == 4  # nothing duplicated, nothing dropped
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert taskqueue.claim(conn, "nowcoder") is None  # old tasks are done; the queue itself works
    tid = taskqueue.create(conn, "nowcoder", {"keyword": "go"})
    assert taskqueue.claim(conn, "nowcoder")["id"] == tid


def test_fresh_database_is_at_the_current_version(home):
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM task_jobs").fetchone()[0] == 0


def test_add_column_if_missing_is_idempotent(home):
    conn = db.connect()
    conn.execute("INSERT INTO tasks(provider, payload) VALUES ('p', '{}')")
    for _ in range(2):
        db._add_column_if_missing(conn, "tasks", "note", "TEXT DEFAULT 'x'")
    assert [r["note"] for r in conn.execute("SELECT note FROM tasks")] == ["x"]


# ---- .env autoload ----
@pytest.fixture
def dotenv(home, monkeypatch):
    """(DJS_HOME/.env, ./.env); every os.environ change made by load() is undone afterwards."""
    cwd = home / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    with mock.patch.dict(os.environ):
        yield home / ".env", cwd / ".env"


def test_envfile_parses_comments_quotes_export_and_empty_values(dotenv):
    bom = "\ufeff"  # editors on Windows put one at the start of the file
    dotenv[0].write_text(bom + "DJS_T_PLAIN=abc\n# comment\n\nDJS_T_DQ=\"two words\"\nDJS_T_SQ='single'\n"
                         "export DJS_T_EXP = spaced \nDJS_T_EMPTY=\nnot a pair\n=novalue\nDJS_T_HASH=a#b=c\n",
                         encoding="utf-8")
    envfile.load()
    got = {k: v for k, v in os.environ.items() if k.startswith("DJS_T_")}
    assert got == {"DJS_T_PLAIN": "abc", "DJS_T_DQ": "two words", "DJS_T_SQ": "single",
                   "DJS_T_EXP": "spaced", "DJS_T_HASH": "a#b=c"}


def test_envfile_never_overrides_and_home_wins_over_cwd(dotenv):
    os.environ["DJS_T_SET"] = "real"
    dotenv[0].write_text("DJS_T_SET=home\nDJS_T_BOTH=home\n")
    dotenv[1].write_text("DJS_T_BOTH=cwd\nDJS_T_CWD=cwd\n")
    envfile.load()
    assert (os.environ["DJS_T_SET"], os.environ["DJS_T_BOTH"], os.environ["DJS_T_CWD"]) == ("real", "home", "cwd")


def test_envfile_without_files_is_a_noop(dotenv):
    before = dict(os.environ)
    envfile.load()
    assert dict(os.environ) == before


# ---- CLI: call ----
def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_call_round_trip(home, capsys):
    code, out, _ = run(capsys, "call", "crawl_start",
                       '{"provider": "nowcoder", "keyword": "python", "max_pages": 1, "detail": false}')
    assert (code, json.loads(out)) == (0, {"task_id": 1})
    code, out, _ = run(capsys, "call", "task_list")
    queued = json.loads(out)["result"][0]
    assert code == 0 and queued["payload"] == {"keyword": "python", "max_pages": 1, "detail": False}
    assert run(capsys, "call", "jobs_get", '{"task_id": 1, "limit": 3}')[:2] == (0, '{"result": []}\n')
    assert json.loads(run(capsys, "call", "search_list")[1])["result"][0]["per_site"][0]["provider"] == "nowcoder"


def test_call_reports_errors_as_json_with_exit_code_1(home, capsys):
    cases = {("nope",): "unknown tool 'nope'; available: provider_list, ",
             ("crawl_start", '{"keyword": "x"}'): "missing 1 required positional argument",
             ("crawl_start", "[1]"): "must be a JSON object",
             ("crawl_start", "{oops"): "bad arguments",
             ("crawl_start", '{"provider": "nope", "keyword": "x"}'): "KeyError",
             ("login", '{"provider": "nowcoder"}'): "does not need login"}  # login is an async tool
    for args, expected in cases.items():
        code, out, _ = run(capsys, "call", *args)
        assert code == 1 and expected in json.loads(out)["error"], args


def test_djs_script_dispatches_call_and_work(home):
    djs_bin = str(Path(sys.executable).with_name("djs"))
    env = {**os.environ, "DJS_HOME": str(home)}

    def djs(*argv):
        return subprocess.run([djs_bin, *argv], env=env, cwd=home, capture_output=True, text=True, timeout=60)

    r = djs("call", "provider_list")  # stdout must be pure JSON, whatever the libraries log to stderr
    assert r.returncode == 0 and "nowcoder" in {p["name"] for p in json.loads(r.stdout)["result"]}
    assert djs("call", "nope").returncode == 1
    assert djs("work", "--until-idle", "--timeout", "30").returncode == 0  # empty queue: nothing to wait for


# ---- CLI: work ----
def test_work_until_idle_prints_one_line_per_finished_task(home, capsys, monkeypatch):
    def pretend(conn, t):  # no network: nowcoder "works", the others die
        if t["provider"] == "nowcoder":
            taskqueue.done(conn, t["id"], {"pages": 1, "jobs_new": 3, "jobs_seen": 0})
        else:
            taskqueue.dead(conn, t["id"], "boom")

    monkeypatch.setattr(engine, "run_task", pretend)
    conn = db.connect()
    for provider in ("nowcoder", "iguopin"):
        taskqueue.create(conn, provider, {"keyword": "py"})
    code, out, err = run(capsys, "work", "--until-idle", "--timeout", "30")
    assert code == 0 and err == ""
    assert sorted(out.splitlines()) == ["task 1 nowcoder done pages=1 new=3 seen=0", "task 2 iguopin dead boom"]
    assert taskqueue.active_ids(conn) == set()


def test_work_until_idle_with_nothing_queued_returns_at_once(home, capsys):
    assert run(capsys, "work", "--until-idle") == (0, "", "")


def test_work_timeout_exits_2_with_until_idle_and_0_without(home, capsys, monkeypatch):
    monkeypatch.setattr(engine, "start_worker", lambda: None)  # nobody works the queue
    taskqueue.create(db.connect(), "nowcoder", {"keyword": "py"})
    code, out, err = run(capsys, "work", "--until-idle", "--timeout", "0.2")
    assert (code, out) == (2, "") and "timeout: 1 task(s) still active" in err
    assert run(capsys, "work", "--timeout", "0.2")[0] == 0
