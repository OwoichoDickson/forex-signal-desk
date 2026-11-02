"""Exclusive file locks that work on macOS/Linux (fcntl) and Windows (msvcrt)."""
from __future__ import annotations

import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def lock(f, blocking: bool = True) -> None:
    """Lock an open file; raises BlockingIOError when blocking=False and another process holds it."""
    if fcntl is not None:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    f.seek(0)
    while True:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if not blocking:
                raise BlockingIOError("file is locked")
            time.sleep(0.1)


def unlock(f) -> None:
    if fcntl is not None:
        fcntl.flock(f, fcntl.LOCK_UN)
        return
    f.seek(0)
    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
