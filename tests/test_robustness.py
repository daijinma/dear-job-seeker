"""Regressions found by driving the real MCP server from Claude Code: a platform silently stopped working."""
import threading
import time

from djs import db, engine, providers, taskqueue


def test_concurrent_first_connects_on_a_fresh_db_never_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    errors, start = [], threading.Barrier(24)

    def connect():
        start.wait()
        try:
            db.connect().close()
        except Exception as e:  # the old code raised 'database is locked' from PRAGMA journal_mode=WAL
            errors.append(e)

    threads = [threading.Thread(target=connect) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_worker_loop_survives_errors_and_still_runs_its_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "ERROR_BACKOFF", 0.05)
    monkeypatch.setattr(engine, "POLL_SECONDS", 0.05)
    ran, real_claim, failures = [], taskqueue.claim, [RuntimeError("database is locked")]

    def flaky_claim(conn, provider=None):
        if failures:
            raise failures.pop()
        return real_claim(conn, provider)

    monkeypatch.setattr(taskqueue, "claim", flaky_claim)
    monkeypatch.setattr(engine, "run_task", lambda conn, task: (ran.append(task["id"]), taskqueue.done(conn, task["id"], {})))
    monkeypatch.setattr(providers, "names", lambda: ["nowcoder"])
    conn = db.connect()
    tid = taskqueue.create(conn, "nowcoder", {"keyword": "python"})
    engine._threads.clear()
    engine.start_worker()
    try:
        deadline = time.monotonic() + 5
        while tid not in ran and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ran == [tid]
        assert engine._threads["nowcoder"].is_alive()
    finally:
        engine.stop_worker(2)
        engine._threads.clear()
