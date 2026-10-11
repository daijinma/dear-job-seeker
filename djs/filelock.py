"""Exclusive lock on an open file, shared by threads and processes: flock on macOS/Linux, msvcrt on Windows."""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager

POLL = 0.05

if sys.platform == "win32":
    import msvcrt

    # Windows locks are mandatory: lock a byte far beyond the data so the holder's own reads and writes stay legal
    LOCK_AT = 1 << 30

    def _acquire(f) -> bool:
        f.seek(LOCK_AT)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _release(f) -> None:
        f.seek(LOCK_AT)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _acquire(f) -> bool:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _release(f) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)


class LockTimeout(TimeoutError):
    """The lock stayed with another holder for the whole wait."""


@contextmanager
def locked(f, timeout: float | None = None):
    """Hold the lock on the open file `f` for the block. Waits `timeout` seconds (None: as long as it takes)."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while not _acquire(f):
        if deadline is not None and time.monotonic() >= deadline:
            raise LockTimeout("locked by another holder")
        time.sleep(POLL)
    try:
        yield
    finally:
        try:
            f.flush()  # what the holder wrote has to be on disk before the next holder reads it
        finally:
            _release(f)
