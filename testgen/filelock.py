"""An exclusive lock between processes on one machine, by a lock file (msvcrt on Windows, fcntl elsewhere):
the audit log's chain in data/audit/ and migrations of a SQLite database."""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path


@contextlib.contextmanager
def locked(path: Path | str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:          # LK_LOCK gives up after ~10 s: keep waiting
                    time.sleep(0.05)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
