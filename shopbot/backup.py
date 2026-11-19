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
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import lock
from .config import ConfigError, Settings

DEFAULT_KEEP = 14
PREFIX = "shop-"
SIDECARS = ("-wal", "-shm")
SPARE_SPACE = 10 * 1024 * 1024  # bytes to leave free besides the copy itself
STALE_PARTIAL = 3600  # seconds after which a leftover half-written backup is removed
STOP_HINT = "on a server: sudo systemctl stop shopbot; with Docker: docker compose stop"


class BackupError(Exception):
    """Something the user can fix; the message says what."""


class DiskSpaceError(BackupError):
    """Not enough free disk space; nothing was changed."""


def _stamp() -> str:
    # UTC time with microseconds: names sort in the order the backups were made.
    return f"{datetime.now(timezone.utc):%Y-%m-%d_%H-%M-%S_%f}"


def _adopt_owner(path: Path, like: Path) -> None:
    """Run as root (e.g. with sudo)? Give ``path`` the owner of ``like`` (the data folder),
    so a bot that runs as a normal user can still read, replace and prune it."""
    if os.name == "nt" or os.geteuid() != 0 or not path.exists():
        return
    owner = like.stat()
    if (owner.st_uid, owner.st_gid) != (0, 0):
        os.chown(path, owner.st_uid, owner.st_gid)


def _remove_with_sidecars(path: Path) -> None:
    for suffix in ("", "-journal", *SIDECARS):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _check(path: Path) -> None:
    """Raise BackupError unless ``path`` is an intact shop database."""
    existed = {s for s in SIDECARS if Path(f"{path}{s}").exists()}
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
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


def _copy_database(source: Path, target: Path) -> None:
    """Write a checked, self-contained copy of ``source`` (including whatever is still
    only in its -wal file) to ``target``. Nothing is left behind if it fails."""
    try:
        src = sqlite3.connect(str(source), timeout=30)
        try:
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)  # one step: a consistent snapshot even while the bot writes
                dst.execute("PRAGMA journal_mode = DELETE")  # one file, no -wal
            finally:
                dst.close()
        finally:
            src.close()
        _check(target)
    except BaseException:
        _remove_with_sidecars(target)
        raise


def _clean_leftovers(backup_dir: Path) -> None:
    """Remove half-written backups left by a crash or reboot (not ones still in progress)."""
    cutoff = time.time() - STALE_PARTIAL
    for leftover in backup_dir.glob(f"{PREFIX}*.partial*"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
        except OSError:
            pass


def _db_size(db_path: Path) -> int:
    return sum(Path(f"{db_path}{s}").stat().st_size
               for s in ("", "-wal") if Path(f"{db_path}{s}").exists())


def _ensure_space(folder: Path, needed: int) -> None:
    free = shutil.disk_usage(folder).free
    if free < needed + SPARE_SPACE:
        raise DiskSpaceError(
            f"Not enough free disk space for a backup in {folder} "
            f"({free // 2**20} MB free, about {(needed + SPARE_SPACE) // 2**20} MB needed). "
            "Free some space first."
        )


def make_backup(
    db_path: Path, backup_dir: Path, keep: int = DEFAULT_KEEP, prefix: str = PREFIX
) -> Path:
    """Copy the database into ``backup_dir`` and drop all but the newest ``keep``
    regular backups. A different ``prefix`` makes a copy that is never pruned."""
    if not db_path.is_file():
        raise BackupError(f"There is no database at {db_path} yet.")
    if not backup_dir.is_dir():
        backup_dir.mkdir(parents=True)
        _adopt_owner(backup_dir, db_path.parent)
    _clean_leftovers(backup_dir)
    _ensure_space(backup_dir, _db_size(db_path))
    target = backup_dir / f"{prefix}{_stamp()}.db"
    while target.exists():  # practically never: same microsecond
        target = backup_dir / f"{prefix}{_stamp()}.db"
    partial = target.with_name(target.name + ".partial")
    _remove_with_sidecars(partial)
    try:
        _copy_database(db_path, partial)
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"Couldn't back up {db_path}: {exc}") from exc
    partial.replace(target)
    if os.name != "nt":
        os.chmod(target, 0o600)  # customer data: only the bot's user may read it
    _adopt_owner(target, db_path.parent)
    if keep > 0:
        for old in list_backups(backup_dir)[:-keep]:
            old.unlink(missing_ok=True)
    return target


def list_backups(backup_dir: Path) -> list[Path]:
    """Backups, oldest first."""
    if not backup_dir.is_dir():
        return []
    return sorted(p for p in backup_dir.glob(f"{PREFIX}*.db") if p.is_file())


def _keep_damaged(db_path: Path, backup_dir: Path) -> Path:
    """Copy a database that can't be backed up normally (it's damaged) aside as it is."""
    if not backup_dir.is_dir():
        backup_dir.mkdir(parents=True)
        _adopt_owner(backup_dir, db_path.parent)
    target = backup_dir / f"damaged-{_stamp()}.db"
    for suffix in ("", *SIDECARS):
        side = Path(f"{db_path}{suffix}")
        if side.exists():
            shutil.copyfile(side, Path(f"{target}{suffix}"))
            _adopt_owner(Path(f"{target}{suffix}"), db_path.parent)
    return target


def restore(backup_file: Path, db_path: Path, backup_dir: Path) -> Optional[Path]:
    """Replace the database with ``backup_file``. Refuses while the bot is running.

    What was there before is saved first (as a normal backup, or as "damaged-...db"
    if it is too damaged for that); that file's path is returned (None if there was no
    database). The live database is only replaced once the new copy is complete.
    """
    if not backup_file.is_file():
        raise BackupError(f"There is no file {backup_file}.")
    if db_path.exists() and os.path.samefile(backup_file, db_path):
        raise BackupError("That is the live database itself. Choose a file from the backups folder.")
    lock_path = db_path.parent / lock.LOCK_NAME
    held = lock.acquire(lock_path)
    if held is None:
        raise BackupError(
            f"The bot is running. Stop it first ({STOP_HINT}), then restore, then start it again."
        )
    _adopt_owner(lock_path, db_path.parent)
    partial = db_path.with_name(db_path.name + ".restoring")
    try:
        # Room for the new copy and for keeping the current database, or change nothing.
        _ensure_space(db_path.parent, _db_size(backup_file) + (_db_size(db_path) if db_path.exists() else 0))
        # 1. A complete, checked copy of the backup, made from a private copy of it, so
        #    changes still in its -wal file come along and the original isn't touched.
        _remove_with_sidecars(partial)
        with tempfile.TemporaryDirectory(dir=db_path.parent) as tmp:
            staged = Path(tmp) / "source.db"
            for suffix in ("", *SIDECARS):
                side = Path(f"{backup_file}{suffix}")
                if side.exists():
                    shutil.copyfile(side, Path(f"{staged}{suffix}"))
            try:
                _copy_database(staged, partial)
            except sqlite3.DatabaseError as exc:
                raise BackupError(f"{backup_file.name} is not a usable shop database: {exc}") from exc
        # 2. Keep what is there now.
        safety = None
        if db_path.exists():
            try:
                safety = make_backup(db_path, backup_dir, keep=0, prefix="before-restore-")
            except DiskSpaceError:
                raise
            except BackupError:  # damaged: that's usually why someone restores
                safety = _keep_damaged(db_path, backup_dir)
        # 3. Swap the copy in. An old -wal file must not be applied to it.
        for suffix in SIDECARS:
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        _adopt_owner(partial, db_path.parent)
        partial.replace(db_path)
        return safety
    except BaseException:
        _remove_with_sidecars(partial)
        raise
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
            others = sorted(p for p in backup_dir.glob("*.db") if p.is_file() and p not in backups) \
                if backup_dir.is_dir() else []
            if others:
                print("Kept from restores (never deleted automatically):")
                for path in others:
                    print(f"  {path}")
            return 0
        if args.restore:
            safety = restore(args.restore, db_path, backup_dir)
            print(f"Restored {args.restore} to {db_path}.")
            print("Everything after that backup was made is undone: deposits, orders and products "
                  "added since then are gone, and products sold since then are for sale again.")
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
