"""One run per out directory: a second process that starts meanwhile stops instead of racing."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".run.lock"
# sysexits EX_TEMPFAIL: the caller may try again later.
EXIT_BUSY = 75


class RunBusy(Exception):
    """Another process holds the lock for this out directory."""


@contextmanager
def hold_run_lock(out_dir: Path, enabled: bool = True) -> Iterator[None]:
    """Hold an exclusive, non-blocking ``flock`` for the duration of the block.

    The kernel drops the lock when the process dies, so a crash cannot leave the directory
    locked; the lock file itself stays and is never deleted.
    """
    if not enabled:
        yield
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    handle = (out_dir / LOCK_NAME).open("a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunBusy(f"another run holds {out_dir / LOCK_NAME}") from exc
        yield
    finally:
        handle.close()
