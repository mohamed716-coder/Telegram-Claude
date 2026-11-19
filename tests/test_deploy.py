"""Deployment helpers: the single-instance lock, backups/restore and the exit codes
that tell a service manager (systemd, Docker) whether restarting makes sense."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from telegram.error import InvalidToken, NetworkError

from shopbot import app as app_module
from shopbot import backup, config, lock
from shopbot.db import Database

ROOT = Path(__file__).resolve().parent.parent
GOOD_TOKEN = "123456789:AAEexampleexampleexampleexample-_x"


# ======================================================================== lock
def test_only_one_process_can_hold_the_lock(tmp_path):
    path = tmp_path / "data" / lock.LOCK_NAME
    first = lock.acquire(path)
    assert first is not None
    assert lock.acquire(path) is None
    code = "import sys; from pathlib import Path; from shopbot import lock; " \
           f"sys.exit(0 if lock.acquire(Path({str(path)!r})) is None else 1)"
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0  # another process
    first.close()
    again = lock.acquire(path)
    assert again is not None
    again.close()


def test_the_lock_file_names_the_holder(tmp_path):
    path = tmp_path / lock.LOCK_NAME
    held = lock.acquire(path)
    assert lock.holder(path) == str(os.getpid())
    held.close()
    assert lock.holder(tmp_path / "missing.lock") == ""


# ===================================================================== backups
def make_shop(path: Path, deposits: int = 3) -> Database:
    db = Database(str(path), 1)
    for i in range(deposits):
        db.manual_deposit(1000 + i, 500, 10**9)
    return db


def balances(path: Path) -> dict[int, int]:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return dict(conn.execute("SELECT id, balance FROM users WHERE balance > 0").fetchall())
    finally:
        conn.close()


def test_a_backup_is_complete_checked_and_self_contained(tmp_path):
    db = make_shop(tmp_path / "shop.db")  # still open: changes may sit in shop.db-wal
    copy = backup.make_backup(tmp_path / "shop.db", tmp_path / "backups")
    assert copy.parent == tmp_path / "backups" and copy.name.startswith("shop-")
    assert balances(copy) == {1000: 500, 1001: 500, 1002: 500}
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
    assert sorted(p.name for p in copy.parent.iterdir()) == [copy.name]  # no -wal/-shm/partial
    db.close()


def test_old_backups_are_pruned_newest_kept(tmp_path):
    db = make_shop(tmp_path / "shop.db")
    made = [backup.make_backup(tmp_path / "shop.db", tmp_path / "b", keep=3) for _ in range(5)]
    assert backup.list_backups(tmp_path / "b") == made[-3:]
    made.append(backup.make_backup(tmp_path / "shop.db", tmp_path / "b", keep=0))  # 0 = keep all
    assert len(backup.list_backups(tmp_path / "b")) == 4
    db.close()


def test_backing_up_a_missing_database_is_a_clear_error(tmp_path):
    with pytest.raises(backup.BackupError, match="no database"):
        backup.make_backup(tmp_path / "shop.db", tmp_path / "b")


def test_restore_puts_a_backup_back_and_keeps_the_current_data(tmp_path):
    db_path = tmp_path / "shop.db"
    db = make_shop(db_path, deposits=1)
    good = backup.make_backup(db_path, tmp_path / "b")
    db.manual_deposit(2000, 999, 10**9)  # a later change, then the shop is stopped
    db.close()
    Path(f"{db_path}-wal").write_bytes(b"stale")  # leftovers must not survive a restore
    safety = backup.restore(good, db_path, tmp_path / "b")
    assert balances(db_path) == {1000: 500}
    assert safety is not None and balances(safety) == {1000: 500, 2000: 999}
    assert not Path(f"{db_path}-wal").exists()
    reopened = Database(str(db_path), 1)  # the bot can use the restored file
    assert reopened.get_user(1000)["balance"] == 500
    reopened.close()


def test_restore_refuses_while_the_bot_is_running(tmp_path):
    db_path = tmp_path / "shop.db"
    make_shop(db_path).close()
    good = backup.make_backup(db_path, tmp_path / "b")
    running = lock.acquire(tmp_path / lock.LOCK_NAME)
    with pytest.raises(backup.BackupError, match="The bot is running"):
        backup.restore(good, db_path, tmp_path / "b")
    running.close()


def test_restore_refuses_a_file_that_is_not_a_shop_database(tmp_path):
    db_path = tmp_path / "shop.db"
    make_shop(db_path).close()
    junk = tmp_path / "junk.db"
    junk.write_text("not a database")
    with pytest.raises(backup.BackupError):
        backup.restore(junk, db_path, tmp_path / "b")
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x (y)").connection.close()
    with pytest.raises(backup.BackupError, match="not a shop database"):
        backup.restore(other, db_path, tmp_path / "b")
    assert balances(db_path)  # untouched


def test_backup_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "shop.db"))
    assert backup.main([]) == 1
    assert "no database" in capsys.readouterr().err
    make_shop(tmp_path / "data" / "shop.db").close()
    assert backup.main(["--list"]) == 0
    assert "No backups" in capsys.readouterr().out
    assert backup.main([]) == 0
    made = Path(capsys.readouterr().out.strip())
    assert made.parent == tmp_path / "data" / "backups" and made.exists()
    assert backup.main(["--restore", str(made)]) == 0
    assert "Restored" in capsys.readouterr().out


# ================================================================ exit codes
@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """No .env and no settings from the real environment (restored afterwards, including
    anything a test's .env file put there)."""
    saved = dict(os.environ)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "OWNER_ID", "SELLER_DISCOUNT_PERCENT",
                 "SELLER_AUTO_THRESHOLD", "BINANCE_PAY_ID", "SUPPORT_CONTACT",
                 "RAILWAY_VOLUME_MOUNT_PATH"):
        os.environ.pop(name, None)
    os.environ["DB_PATH"] = str(tmp_path / "data" / "shop.db")
    yield tmp_path
    os.environ.clear()
    os.environ.update(saved)


def run_main() -> int:
    with pytest.raises(SystemExit) as stopped:
        app_module.main()
    return stopped.value.code


class FakeApp:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def run_polling(self, **kwargs) -> None:
        raise self.error


@pytest.mark.parametrize("env, expected", [
    ({}, "BOT_TOKEN is not set"),
    ({"BOT_TOKEN": "hello"}, "doesn't look like a bot token"),
    ({"BOT_TOKEN": GOOD_TOKEN, "OWNER_ID": "abc"}, "OWNER_ID must be a whole number"),
    ({"BOT_TOKEN": GOOD_TOKEN, "SELLER_DISCOUNT_PERCENT": "150"}, "between 0 and 100"),
])
def test_bad_settings_exit_with_78_so_the_service_is_not_restarted(clean_env, monkeypatch, caplog, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert run_main() == app_module.EXIT_CONFIG == 78
    assert expected in caplog.text


def test_a_token_telegram_rejects_exits_with_78(clean_env, monkeypatch, caplog):
    monkeypatch.setenv("BOT_TOKEN", GOOD_TOKEN)
    monkeypatch.setattr(app_module, "build_application", lambda s: FakeApp(InvalidToken("Unauthorized")))
    assert run_main() == 78
    assert "Telegram rejected BOT_TOKEN" in caplog.text


def test_no_internet_at_start_exits_with_1_so_it_is_retried(clean_env, monkeypatch, caplog):
    monkeypatch.setenv("BOT_TOKEN", GOOD_TOKEN)
    monkeypatch.setattr(app_module, "build_application", lambda s: FakeApp(NetworkError("down")))
    assert run_main() == 1
    assert "Couldn't reach Telegram" in caplog.text
    assert lock.acquire(clean_env / "data" / lock.LOCK_NAME) is not None  # released on exit


def test_a_second_copy_exits_with_75(clean_env, monkeypatch, caplog):
    monkeypatch.setenv("BOT_TOKEN", GOOD_TOKEN)
    held = lock.acquire(clean_env / "data" / lock.LOCK_NAME)
    built = []
    monkeypatch.setattr(app_module, "build_application", lambda s: built.append(s))
    assert run_main() == app_module.EXIT_RUNNING == 75
    assert "Another copy of the bot is already running" in caplog.text
    assert built == []  # never touched the database or Telegram
    held.close()


# ============================================================== shell scripts
@pytest.mark.parametrize("script, shell", [("deploy/install.sh", "bash"),
                                           ("deploy/docker-entrypoint.sh", "sh")])
def test_shell_scripts_parse_and_use_unix_line_endings(script, shell):
    path = ROOT / script
    assert b"\r\n" not in path.read_bytes()
    assert subprocess.run([shell, "-n", str(path)]).returncode == 0


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() != 0, reason="needs root on Linux/macOS")
def test_backups_and_restores_made_as_root_belong_to_the_bot_user(tmp_path):
    """With sudo, files must still belong to the user the bot runs as, or the nightly
    backup can't prune them and the bot can't write the restored database."""
    data = tmp_path / "data"
    data.mkdir()
    make_shop(data / "shop.db").close()
    os.chown(data, 1000, 1000)
    os.chown(data / "shop.db", 1000, 1000)
    made = backup.make_backup(data / "shop.db", data / "backups")
    assert (made.stat().st_uid, (data / "backups").stat().st_uid) == (1000, 1000)
    backup.restore(made, data / "shop.db", data / "backups")
    assert (data / "shop.db").stat().st_uid == 1000
    assert all(p.stat().st_uid == 1000 for p in (data / "backups").iterdir())


# ============================================================ restore, hardened
def test_restore_works_when_the_current_database_is_damaged(tmp_path):
    db_path = tmp_path / "shop.db"
    make_shop(db_path).close()
    good = backup.make_backup(db_path, tmp_path / "b")
    with open(db_path, "r+b") as fh:  # wreck the live database
        fh.seek(0)
        fh.write(b"\0" * 4096)
    saved = backup.restore(good, db_path, tmp_path / "b")
    assert balances(db_path) == {1000: 500, 1001: 500, 1002: 500}
    assert saved is not None and saved.name.startswith("damaged-")  # kept for a specialist


def test_restoring_a_raw_copy_keeps_what_is_only_in_its_wal_file(tmp_path):
    source = tmp_path / "pc" / "shop.db"
    source.parent.mkdir()
    pc = make_shop(source, deposits=5)  # still open: the data sits in shop.db-wal
    assert Path(f"{source}-wal").exists()
    copies = tmp_path / "copied"
    copies.mkdir()
    for suffix in ("", "-wal"):  # what someone copies from the data folder
        Path(f"{copies / 'shop.db'}{suffix}").write_bytes(Path(f"{source}{suffix}").read_bytes())
    pc.close()
    db_path = tmp_path / "server" / "shop.db"
    db_path.parent.mkdir()
    backup.restore(copies / "shop.db", db_path, tmp_path / "server" / "backups")
    assert len(balances(db_path)) == 5
    assert Path(f"{copies / 'shop.db'}-wal").exists()  # the original copy is left alone


def test_restore_refuses_the_live_database_and_leaves_nothing_behind(tmp_path):
    db_path = tmp_path / "shop.db"
    make_shop(db_path).close()
    with pytest.raises(backup.BackupError, match="live database itself"):
        backup.restore(db_path, db_path, tmp_path / "b")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"x" * 5000)
    with pytest.raises(backup.BackupError):
        backup.restore(junk, db_path, tmp_path / "b")
    assert balances(db_path) and not list(tmp_path.glob("shop.db.restoring*"))


def test_a_failed_backup_leaves_no_half_written_files(tmp_path):
    db_path = tmp_path / "shop.db"
    db_path.write_bytes(b"not a database at all" * 300)
    with pytest.raises(backup.BackupError, match="Couldn't back up"):
        backup.make_backup(db_path, tmp_path / "b")
    assert list((tmp_path / "b").iterdir()) == []


def test_old_leftovers_are_cleaned_up_and_fresh_ones_kept(tmp_path):
    db = make_shop(tmp_path / "shop.db")
    folder = tmp_path / "b"
    folder.mkdir()
    old = folder / "shop-2020-01-01_00-00-00_000000.db.partial"
    fresh = folder / "shop-2099-01-01_00-00-00_000000.db.partial"  # another backup running now
    old.write_text("x")
    fresh.write_text("x")
    os.utime(old, (0, 0))
    backup.make_backup(tmp_path / "shop.db", folder)
    assert not old.exists() and fresh.exists()
    db.close()


def test_a_full_disk_is_reported_before_copying(tmp_path, monkeypatch):
    db = make_shop(tmp_path / "shop.db")
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: type("U", (), {"free": 1024})())
    with pytest.raises(backup.BackupError, match="Not enough free disk space"):
        backup.make_backup(tmp_path / "shop.db", tmp_path / "b")
    db.close()


def test_folders_with_special_characters_work(tmp_path):
    folder = tmp_path / "Bot #2 100%?"
    folder.mkdir()
    make_shop(folder / "shop.db").close()
    made = backup.make_backup(folder / "shop.db", folder / "backups")
    assert balances(made) and sorted(p.name for p in folder.iterdir()) == ["backups", "shop.db"]
    backup.restore(made, folder / "shop.db", folder / "backups")


# ======================================================== while the bot is running
def test_a_token_revoked_while_running_stops_the_bot_with_78(clean_env, monkeypatch, caplog):
    monkeypatch.setenv("BOT_TOKEN", GOOD_TOKEN)

    class RevokedApp:
        bot_data: dict = {}

        def run_polling(self, **kwargs) -> None:
            self.bot_data["token_rejected"] = True  # what _watch_token sets, then stops

    monkeypatch.setattr(app_module, "build_application", lambda s: RevokedApp())
    assert run_main() == 78
    assert "Telegram rejected BOT_TOKEN" in caplog.text


def test_the_token_watch_stops_the_app_when_telegram_rejects_the_token(monkeypatch):
    import asyncio

    calls = {"get_me": 0, "stopped": 0}

    class Bot:
        async def get_me(self):
            calls["get_me"] += 1
            if calls["get_me"] == 1:
                raise NetworkError("blip")  # a network problem is not a reason to stop
            raise InvalidToken("Unauthorized")

    class App:
        bot = Bot()
        bot_data: dict = {}

        def stop_running(self):
            calls["stopped"] += 1

    monkeypatch.setattr(app_module, "TOKEN_CHECK_EVERY", 0)
    app = App()
    asyncio.run(asyncio.wait_for(app_module._watch_token(app), 5))
    assert calls == {"get_me": 2, "stopped": 1} and app.bot_data["token_rejected"] is True


def test_polling_conflicts_and_outages_are_short_warnings(caplog):
    import asyncio
    from types import SimpleNamespace

    from telegram.error import Conflict

    from shopbot.handlers.common import on_error

    for error in (Conflict("terminated by other getUpdates request"), NetworkError("down")):
        caplog.clear()
        asyncio.run(on_error(None, SimpleNamespace(error=error)))
        assert "Error while handling" not in caplog.text and not caplog.records[0].exc_info
    assert "keeps trying" in caplog.text


def test_on_railway_the_database_goes_on_the_volume_wherever_it_is_mounted(clean_env, monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data/")
    assert config.Settings.from_env().db_path == "/data/shop.db"
    monkeypatch.setenv("DB_PATH", "/elsewhere/x.db")  # an explicit DB_PATH still wins
    assert config.Settings.from_env().db_path == "/elsewhere/x.db"


def test_the_token_never_appears_in_the_logs():
    import logging

    formatter = app_module._HideToken("%(message)s", GOOD_TOKEN)
    record = logging.LogRecord("telegram", logging.ERROR, __file__, 1,
                               "The token `%s` was rejected by the server.", (GOOD_TOKEN,), None)
    assert formatter.format(record) == "The token `<BOT_TOKEN>` was rejected by the server."


def test_a_dotenv_saved_with_a_windows_byte_order_mark_still_works(clean_env, monkeypatch):
    (clean_env / ".env").write_bytes("﻿BOT_TOKEN=abc123\r\nOWNER_ID=42\r\n".encode("utf-8"))
    monkeypatch.delenv("DB_PATH", raising=False)
    settings = config.Settings.from_env()
    assert (settings.bot_token, settings.owner_id) == ("abc123", 42)
