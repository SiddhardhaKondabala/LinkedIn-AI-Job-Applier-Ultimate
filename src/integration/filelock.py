"""A tiny advisory file lock for cross-process serialization (Phase 8).

Used to make the result writer's append and the claim store's create safe when
two executor processes run against the same output directory. Advisory locking
(``fcntl.flock``) is honored on macOS and Linux — the platforms the bot runs on.

The lock is held only for the brief critical section around a read-check-write,
never across browser work.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

try:  # pragma: no cover - platform guard
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]


@contextlib.contextmanager
def file_lock(lock_path):
    """Hold an exclusive advisory lock on ``<lock_path>`` for the with-block.

    A dedicated lock file is used so the data file it guards can be freely
    truncated/replaced. When fcntl is unavailable the context is a no-op, so the
    caller still works single-process.
    """
    p = Path(lock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a+")
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
