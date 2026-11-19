"""Deployment helpers: the single-instance lock, backups/restore and the exit codes
that tell a service manager (systemd, Docker) whether restarting makes sense."""

from __future__ import annotations

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
    import os
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
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
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
    """No .env and no settings from the real environment."""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "OWNER_ID", "SELLER_DISCOUNT_PERCENT",
                 "SELLER_AUTO_THRESHOLD", "BINANCE_PAY_ID", "SUPPORT_CONTACT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "shop.db"))
    return tmp_path


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
