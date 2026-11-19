"""Back up or restore the shop database.

    python -m shopbot.backup                 make a backup now (keeps the newest 14)
    python -m shopbot.backup --list          show the backups
    python -m shopbot.backup --restore FILE  put a backup back (stop the bot first)

A backup is made with SQLite's online backup, so it is complete and consistent even
while the bot is running (copying shop.db by hand can miss recent changes, which may
still be in shop.db-wal). Every backup is checked before it is kept.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import lock
from .config import ConfigError, Settings

DEFAULT_KEEP = 14
PREFIX = "shop-"


class BackupError(Exception):
    """Something the user can fix; the message says what."""


SIDECARS = ("-wal", "-shm")


def _check(path: Path) -> None:
    """Raise BackupError unless ``path`` is an intact shop database."""
    existed = {s for s in SIDECARS if Path(f"{path}{s}").exists()}
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.execute("SELECT COUNT(*) FROM users").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{path.name} is not a shop database: {exc}") from exc
    finally:
        for suffix in set(SIDECARS) - existed:  # files a read of a WAL database leaves
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    if result != "ok":
        raise BackupError(f"{path.name} failed the integrity check: {result}")


def make_backup(db_path: Path, backup_dir: Path, keep: int = DEFAULT_KEEP) -> Path:
    """Copy the database into ``backup_dir`` and drop all but the newest ``keep``."""
    if not db_path.is_file():
        raise BackupError(f"There is no database at {db_path} yet.")
    backup_dir.mkdir(parents=True, exist_ok=True)
    # UTC time with microseconds: names sort in the order the backups were made.
    target = backup_dir / f"{PREFIX}{datetime.now(timezone.utc):%Y-%m-%d_%H-%M-%S_%f}.db"
    while target.exists():  # practically never: same microsecond
        target = backup_dir / f"{PREFIX}{datetime.now(timezone.utc):%Y-%m-%d_%H-%M-%S_%f}.db"
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    source = sqlite3.connect(str(db_path), timeout=30)
    try:
        copy = sqlite3.connect(str(partial))
        try:
            source.backup(copy)  # one step: a consistent snapshot even while the bot writes
            copy.execute("PRAGMA journal_mode = DELETE")  # one self-contained file
        finally:
            copy.close()
    finally:
        source.close()
    try:
        _check(partial)
    except BackupError:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(target)
    if keep > 0:
        for old in list_backups(backup_dir)[:-keep]:
            old.unlink(missing_ok=True)
    return target


def list_backups(backup_dir: Path) -> list[Path]:
    """Backups, oldest first."""
    if not backup_dir.is_dir():
        return []
    return sorted(p for p in backup_dir.glob(f"{PREFIX}*.db") if p.is_file())


def restore(backup_file: Path, db_path: Path, backup_dir: Path) -> Optional[Path]:
    """Replace the database with ``backup_file``. Refuses while the bot is running.

    The current database is backed up first; that backup's path is returned (None if
    there was no database).
    """
    if not backup_file.is_file():
        raise BackupError(f"There is no file {backup_file}.")
    _check(backup_file)
    held = lock.acquire(db_path.parent / lock.LOCK_NAME)
    if held is None:
        raise BackupError(
            "The bot is running. Stop it first (on a server: sudo systemctl stop shopbot), "
            "then restore, then start it again."
        )
    try:
        safety = None
        if db_path.exists():
            safety = make_backup(db_path, backup_dir, keep=0)
        for suffix in ("", "-wal", "-shm"):  # an old -wal file must not be applied to the copy
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        partial = db_path.with_name(db_path.name + ".restoring")
        shutil.copyfile(backup_file, partial)
        partial.replace(db_path)
        return safety
    finally:
        held.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shopbot.backup", description="Back up or restore the shop database."
    )
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"how many backups to keep (default {DEFAULT_KEEP}, 0 = all)")
    parser.add_argument("--dir", type=Path, help="backup folder (default: data/backups)")
    parser.add_argument("--list", action="store_true", help="list the backups")
    parser.add_argument("--restore", type=Path, metavar="FILE",
                        help="replace the database with this backup (stop the bot first)")
    args = parser.parse_args(argv)
    try:
        db_path = Path(Settings.from_env().db_path)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    backup_dir = args.dir or db_path.parent / "backups"
    try:
        if args.list:
            backups = list_backups(backup_dir)
            for path in backups:
                print(f"{path}  ({path.stat().st_size // 1024} KB)")
            if not backups:
                print(f"No backups in {backup_dir} yet.")
            return 0
        if args.restore:
            safety = restore(args.restore, db_path, backup_dir)
            print(f"Restored {args.restore} to {db_path}.")
            if safety is not None:
                print(f"The database you had before is saved as {safety}.")
            print("Start the bot again now.")
            return 0
        print(make_backup(db_path, backup_dir, args.keep))
        return 0
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
