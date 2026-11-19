"""A lock file that stops two copies of the bot from using the same data at once.

Two copies with the same token would fight over Telegram updates (409 Conflict) and
both write to the database, so the second copy refuses to start instead.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Optional

LOCK_NAME = "bot.lock"


def acquire(path: Path) -> Optional[IO[str]]:
    """Lock ``path``; return the open file (keep it open while running) or None if
    another process holds the lock. The lock is released when the process exits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    writable = True
    try:
        handle = open(path, "a+", encoding="utf-8")
    except PermissionError:  # made by another user (e.g. root): locking works read-only too
        handle = open(path, "r", encoding="utf-8")
        writable = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    if writable and os.name != "nt":
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    return handle


def holder(path: Path) -> str:
    """The process id written by the lock holder, or '' if unknown."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
