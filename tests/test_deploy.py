"""Deployment helpers: the single-instance lock, backups/restore and the exit codes
that tell a service manager (systemd, Docker) whether restarting makes sense."""

from __future__ import annotations

import os
import re
import shutil
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
                                           ("deploy/ci-deploy.sh", "bash"),
                                           ("deploy/ci-remote.sh", "bash"),
                                           ("deploy/docker-entrypoint.sh", "sh")])
def test_shell_scripts_parse_and_use_unix_line_endings(script, shell):
    path = ROOT / script
    assert b"\r\n" not in path.read_bytes()
    assert subprocess.run([shell, "-n", str(path)]).returncode == 0


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() != 0, reason="needs root on Linux/macOS")
def test_backups_and_restores_made_as_root_belong_to_the_bot_user(tmp_path):
    """With sudo, files must still belong to the user the bot runs as, or the automatic
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


def test_restore_with_too_little_disk_space_changes_nothing(tmp_path, monkeypatch):
    db_path = tmp_path / "shop.db"
    make_shop(db_path).close()
    good = backup.make_backup(db_path, tmp_path / "b")
    before = db_path.read_bytes()
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: type("U", (), {"free": 1024})())
    with pytest.raises(backup.DiskSpaceError):
        backup.restore(good, db_path, tmp_path / "b")
    assert db_path.read_bytes() == before
    assert [p.name for p in (tmp_path / "b").iterdir()] == [good.name]  # no "damaged" copy


def test_copies_kept_by_a_restore_do_not_push_out_regular_backups(tmp_path):
    db_path = tmp_path / "shop.db"
    make_shop(db_path).close()
    regular = [backup.make_backup(db_path, tmp_path / "b", keep=3) for _ in range(3)]
    for _ in range(3):
        backup.restore(regular[-1], db_path, tmp_path / "b")
    assert backup.list_backups(tmp_path / "b") == regular
    assert len(list((tmp_path / "b").glob("before-restore-*.db"))) == 3


def test_a_damaged_database_at_start_says_how_to_fix_it_and_is_not_restarted(clean_env, monkeypatch, caplog):
    monkeypatch.setenv("BOT_TOKEN", GOOD_TOKEN)
    (clean_env / "data").mkdir()
    (clean_env / "data" / "shop.db").write_bytes(b"\x13garbage" * 1024)
    assert run_main() == 78
    assert "Can't open the database" in caplog.text and "restore the newest backup" in caplog.text


@pytest.mark.parametrize("error", ["attempt to write a readonly database",
                                   "database or disk is full", "unable to open database file"])
def test_a_database_it_cannot_write_is_retried_not_called_damaged(clean_env, monkeypatch, caplog, error):
    # A wrong owner or a full disk can be fixed without a restore, and once it is (e.g. by
    # freeing space) the service must come back by itself: exit 1, not 78.
    monkeypatch.setenv("BOT_TOKEN", GOOD_TOKEN)
    monkeypatch.setattr(app_module, "build_application",
                        lambda s: FakeApp(sqlite3.OperationalError(error)))
    assert run_main() == 1
    assert "Can't use the database" in caplog.text and "disk isn't full" in caplog.text
    assert "restore" not in caplog.text


@pytest.mark.parametrize("error, code, expected", [
    (PermissionError(13, "Permission denied"), 78, "Run the bot as the user who owns"),
    (OSError(28, "No space left on device"), 1, "Is the disk full?"),
])
def test_a_data_folder_problem_at_start_exits_the_right_way(clean_env, monkeypatch, caplog, error, code, expected):
    monkeypatch.setenv("BOT_TOKEN", GOOD_TOKEN)

    def fail(path):
        raise error

    monkeypatch.setattr(lock, "acquire", fail)
    assert run_main() == code
    assert "Can't use the data folder" in caplog.text and expected in caplog.text


@pytest.mark.parametrize("script", ["install.sh", "ci-deploy.sh", "ci-remote.sh"])
def test_the_deploy_scripts_never_stop_reading_a_pipe_early(script):
    # Under "set -o pipefail", "producer | grep -q" fails when grep exits on the first
    # match and the producer gets SIGPIPE: the check then says "not found" by mistake.
    text = (ROOT / "deploy" / script).read_text()
    assert "pipefail" in text
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if re.search(r"\|\s*grep\s+(-\w*q|--quiet)", line)]


def test_a_later_line_in_dotenv_wins_like_in_a_shell(clean_env, monkeypatch):
    (clean_env / ".env").write_text("BOT_TOKEN=x\nSUPPORT_CONTACT=\nSUPPORT_CONTACT=@my_support\n")
    monkeypatch.delenv("DB_PATH", raising=False)
    assert config.Settings.from_env().support_contact == "@my_support"


# ================================================== deploying from GitHub Actions
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
needs_ssh = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("ssh"),
                               reason="needs bash and an ssh client")


def run_ci_deploy(tmp_path, **env):
    """deploy/ci-deploy.sh with only these settings (and not in GitHub's log format)."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(("VPS_", "GITHUB_"))
            and k not in ("BOT_TOKEN", "DEPLOY_BRANCH", "KNOWN_HOSTS")}
    base.update(KNOWN_HOSTS=str(tmp_path / "known_hosts"), DEPLOY_BRANCH="test-branch")
    base.update(env)
    return subprocess.run(["bash", str(ROOT / "deploy" / "ci-deploy.sh")], env=base,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
def test_the_deploy_job_says_which_secrets_are_missing(tmp_path):
    done = run_ci_deploy(tmp_path)
    assert done.returncode == 1
    assert "Add the repository secret(s) VPS_HOST VPS_PASSWORD BOT_TOKEN:" in done.stderr
    done = run_ci_deploy(tmp_path, VPS_HOST="203.0.113.25", BOT_TOKEN=GOOD_TOKEN)
    assert "Add the repository secret(s) VPS_PASSWORD:" in done.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
def test_the_deploy_job_wants_just_an_address(tmp_path):
    done = run_ci_deploy(tmp_path, VPS_HOST="http://my server", VPS_PASSWORD="pw")
    assert done.returncode == 1
    assert "VPS_HOST must be just the server's IP address" in done.stderr


@needs_ssh
def test_the_deploy_job_understands_root_at_address_colon_port(tmp_path):
    # Port 1 refuses at once: the job must have read the port out of VPS_HOST.
    done = run_ci_deploy(tmp_path, VPS_HOST=" root@127.0.0.1:1 ", VPS_PASSWORD="pw")
    assert done.returncode == 1
    assert "Can't reach the server at VPS_HOST on port 1." in done.stderr
    assert not (tmp_path / "known_hosts").exists()


@needs_ssh
def test_a_malformed_token_secret_is_not_used_but_does_not_stop_the_deploy(tmp_path):
    done = run_ci_deploy(tmp_path, VPS_HOST="127.0.0.1", VPS_PORT="1", VPS_PASSWORD="pw",
                         BOT_TOKEN="hello")
    assert "doesn't look like a bot token" in done.stderr and "so it is not used" in done.stderr
    assert "Can't reach the server" in done.stderr  # it went on to log in


@needs_ssh
def test_on_github_errors_are_annotations_and_edited_secrets_stay_hidden(tmp_path):
    done = run_ci_deploy(tmp_path, GITHUB_ACTIONS="true", VPS_HOST=" 127.0.0.1 ",
                         VPS_PORT="1", VPS_PASSWORD="pw-with-newline\n", BOT_TOKEN=f" {GOOD_TOKEN}\n")
    masks = {line.split("::", 2)[2] for line in done.stdout.splitlines()
             if line.startswith("::add-mask::")}
    assert {"127.0.0.1", "pw-with-newline", GOOD_TOKEN} <= masks  # trimmed copies too
    assert done.stderr.splitlines()[-1].startswith("::error::Can't reach the server")


def test_install_sh_takes_an_offered_token_only_when_telegram_accepts_it():
    # The block in install.sh that decides, run with a stand-in for Telegram:
    # tokens starting with "good" work, "down" means Telegram can't be reached.
    text = (ROOT / "deploy" / "install.sh").read_text()
    block = text[text.index('offered="${OFFERED_BOT_TOKEN:-}"'):text.index('bot_name=""')]

    def decide(file_token, offered, env_token="", seen=False):
        script = (
            'warn() { echo "WARN: $*"; }\n'
            'check_token() { case "$1" in good*) return 0 ;; down*) return 3 ;; *) return 2 ;; esac; }\n'
            'file_token="$FILE_TOKEN"; token="${BOT_TOKEN:-$file_token}"; where=""\n'
            + block + 'echo "USE=$token"\n'
        )
        env = {"PATH": os.environ["PATH"], "FILE_TOKEN": file_token, "OFFERED_BOT_TOKEN": offered}
        if env_token:
            env["BOT_TOKEN"] = env_token
        if seen:  # the secret is the one the last deploy already offered
            env["OFFERED_BOT_TOKEN_SEEN"] = "yes"
        out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True).stdout
        return out.splitlines()[-1].removeprefix("USE="), "WARN" in out

    assert decide("", "good-new") == ("good-new", False)  # first install
    assert decide("good-old", "good-new") == ("good-new", False)  # updated secret wins
    assert decide("good-old", "revoked") == ("good-old", True)  # outdated secret: keep, warn
    assert decide("good-old", "down-new") == ("good-old", False)  # can't tell: keep
    assert decide("", "revoked") == ("revoked", False)  # nothing else: the check rejects it
    assert decide("good-old", "good-old") == ("good-old", False)  # same: nothing to do
    assert decide("good-old", "good-new", env_token="good-typed") == ("good-typed", False)
    # An unchanged secret doesn't undo a token changed on the server, while that one works.
    assert decide("good-server", "good-secret", seen=True) == ("good-server", True)
    assert decide("revoked-server", "good-secret", seen=True) == ("good-secret", False)


@needs_ssh
def test_the_binance_secrets_are_hidden_checked_and_never_stop_the_deploy(tmp_path):
    key, secret = "K" * 64, "S" * 64
    done = run_ci_deploy(tmp_path, GITHUB_ACTIONS="true", VPS_HOST="127.0.0.1", VPS_PORT="1",
                         VPS_PASSWORD="pw", BINANCE_API_KEY=f" {key}\n", BINANCE_API_SECRET=f"'{secret}'")
    masks = {line.split("::", 2)[2] for line in done.stdout.splitlines()
             if line.startswith("::add-mask::")}
    assert {key, secret} <= masks  # the trimmed copies are hidden too
    assert "Binance" not in done.stderr and "Can't reach the server" in done.stderr
    done = run_ci_deploy(tmp_path, VPS_HOST="127.0.0.1", VPS_PORT="1", VPS_PASSWORD="pw",
                         BINANCE_API_KEY="my key", BINANCE_API_SECRET=secret)
    assert "don't look like a Binance API key" in done.stderr and "Can't reach the server" in done.stderr
    done = run_ci_deploy(tmp_path, VPS_HOST="127.0.0.1", VPS_PORT="1", VPS_PASSWORD="pw",
                         BINANCE_API_SECRET=secret)
    assert "BINANCE_API_KEY isn't, so neither is used" in done.stderr
    done = run_ci_deploy(tmp_path, VPS_HOST="127.0.0.1", VPS_PORT="1", VPS_PASSWORD="pw",
                         BINANCE_API_KEY="OFF")
    assert "Binance" not in done.stderr  # "off" is a valid choice: automatic deposits off


def test_the_binance_key_travels_to_dotenv_on_the_server():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "BINANCE_API_KEY: ${{ secrets.BINANCE_API_KEY }}" in workflow
    assert "BINANCE_API_SECRET: ${{ secrets.BINANCE_API_SECRET }}" in workflow
    upload = (ROOT / "deploy" / "ci-deploy.sh").read_text()
    assert "code.bundle remote.sh token binance |" in upload and 'rm -f "$work/token" "$work/binance"' in upload
    remote = (ROOT / "deploy" / "ci-remote.sh").read_text()
    assert '"$here/token" "$here/binance"; rmdir' in remote  # removed however it ends
    assert remote.index('rm -f "$here/token" "$here/binance"') < remote.index("git init")  # at once
    assert 'export OFFERED_BINANCE_API_KEY="$binance_key"' in remote
    assert "-m shopbot.binance --check" in remote


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash")
def test_install_sh_saves_the_offered_binance_key_in_dotenv(tmp_path):
    text = (ROOT / "deploy" / "install.sh").read_text()
    function = text[text.index("save_binance_key() ("):text.index("ask_token() {")]
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    env_file = tmp_path / ".env"
    key, secret = "A" * 64, "B" * 64

    def save(offered_key, offered_secret=""):
        script = function + 'save_binance_key "$1" "$2"\n'
        env = {"PATH": os.environ["PATH"], "ENV_FILE": str(env_file), "VENV": str(tmp_path / "venv"),
               "APP_DIR": str(ROOT)}
        done = subprocess.run(["bash", "-c", script, "save", offered_key, offered_secret], env=env,
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    env_file.write_text("BOT_TOKEN=123:abc\nBINANCE_API_KEY=old\nOWNER_ID=1\nexport BINANCE_API_KEY=older\n")
    assert "Saved the Binance API key" in save(key, secret)
    lines = env_file.read_text().splitlines()
    assert lines == ["BOT_TOKEN=123:abc", "OWNER_ID=1", f"BINANCE_API_KEY={key}", f"BINANCE_API_SECRET={secret}"]
    assert save(key, secret) == ""  # the same key again: nothing to do
    assert "doesn't look right" in save("short", secret)
    assert env_file.read_text().splitlines()[-2:] == [f"BINANCE_API_KEY={key}", f"BINANCE_API_SECRET={secret}"]
    assert "Took the Binance API key out" in save("off")
    assert env_file.read_text().splitlines() == ["BOT_TOKEN=123:abc", "OWNER_ID=1"]
    assert save("off") == ""  # already off


def test_the_deploy_log_turns_the_servers_binance_check_into_warnings(tmp_path):
    text = (ROOT / "deploy" / "ci-deploy.sh").read_text()
    block = text[text.index("check=\"$(sed -n"):text.index("running=\"$(sed -n")]

    def warnings(*lines):
        (tmp_path / "install.log").write_text("".join(f"{line}\n" for line in lines))
        script = ('note() { echo "NOTE: $*"; }\nBINANCE_GUIDE=guide\nwork="$WORK"\n' + block)
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "WORK": str(tmp_path)})
        return [line[6:] for line in done.stdout.splitlines() if line.startswith("NOTE: ")]

    assert warnings("==> On the server now:", "  Binance check: Binance: the key works from this server; 2 Pay payment(s) received in the last 7 days are visible.") == []
    assert warnings("  Binance check: Binance: no API key set (BINANCE_API_KEY and BINANCE_API_SECRET), so deposits are checked by hand.") == []
    assert warnings() == []  # an older server script: no check at all
    (refused,) = warnings("  Binance check: Binance: Binance refused the API key (Invalid API-key).")
    assert refused.startswith("Automatic deposits don't work: Binance refused the API key (Invalid API-key).")
    assert refused.endswith("every deposit comes to you to approve (guide).")
    assert warnings("  Binance check: Binance: the key works from this server; 0 Pay payment(s).",
                    "  Binance check: WARNING: this key belongs to Binance ID 1, but ...") == [
        "Binance key: this key belongs to Binance ID 1, but ..."]


def test_the_workflow_deploys_only_the_servers_branch_one_at_a_time():
    yaml = pytest.importorskip("yaml")
    flow = yaml.safe_load(WORKFLOW.read_text())
    triggers = flow.get("on", flow.get(True))  # YAML 1.1 reads a bare "on" as true
    assert "claude/telegram-bot-vphwhx" in triggers["push"]["branches"]
    assert {"shopbot/**", "deploy/**", "bot.py", "requirements.txt"} <= set(triggers["push"]["paths"])
    assert flow["permissions"] == {"contents": "read"}
    job = flow["jobs"]["deploy"]
    # On the job: a skipped run (another branch) must not cancel a deploy that waits.
    assert "concurrency" not in flow and job["concurrency"]["cancel-in-progress"] is False
    assert "vars.DEPLOY_BRANCH" in job["if"] and "claude/telegram-bot-vphwhx" in job["if"]
    steps = {step.get("id"): step for step in job["steps"]}
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["fetch-depth"] == 0
    deploy = steps["deploy"]
    assert deploy["run"] == "bash deploy/ci-deploy.sh"
    for name in ("VPS_HOST", "VPS_PASSWORD", "BOT_TOKEN"):
        assert deploy["env"][name] == "${{ secrets.%s }}" % name
    remember = job["steps"][-1]
    assert remember["uses"].startswith("actions/cache/save@")
    assert "steps.deploy.outputs.remember-server == 'true'" in remember["if"]
    # The remembered server key is a file the deploy script uses and git never commits.
    path = remember["with"]["path"]
    assert path == steps["known"]["with"]["path"]
    assert f'KNOWN_HOSTS:-{path}' in (ROOT / "deploy" / "ci-deploy.sh").read_text()
    assert path in (ROOT / ".gitignore").read_text().splitlines()


def test_the_server_side_deploy_never_deletes_the_shops_data():
    text = (ROOT / "deploy" / "ci-remote.sh").read_text()
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if "git clean" in line]  # -x would delete data/
    removals = [line for line in code if re.search(r"\brm\b", line)]
    # Only its own upload folder, or old leftover uploads with that exact name.
    assert removals and all('"$here/' in line or "'.shopbot-deploy.*'" in line for line in removals)


def test_backups_are_made_every_6_hours_and_a_week_of_them_is_kept():
    text = (ROOT / "deploy" / "install.sh").read_text()
    assert "OnCalendar=*-*-* 00/6:17:00" in text  # 00:17, 06:17, 12:17, 18:17
    assert "Persistent=true" in text  # missed while the server was off: made at boot
    assert backup.DEFAULT_KEEP == 28 == 7 * 24 // 6


def test_the_server_script_only_runs_from_its_own_upload_folder_and_deletes_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = (ROOT / "deploy" / "ci-remote.sh").read_text()
    for place in (home, tmp_path / "elsewhere", home / "Telegram-Claude" / "deploy"):
        place.mkdir(parents=True, exist_ok=True)
        (place / "remote.sh").write_text(script)
        (place / "code.bundle").write_text("x")
        (place / "keep.txt").write_text("shop data")
        done = subprocess.run(["bash", str(place / "remote.sh"), "branch"], capture_output=True,
                              text=True, env={**os.environ, "HOME": str(home)})
        assert done.returncode == 1 and "started by deploy/ci-deploy.sh only" in done.stderr, place
        assert sorted(p.name for p in place.iterdir()) == ["code.bundle", "keep.txt", "remote.sh"]


@needs_ssh
def test_a_pinned_server_key_that_does_not_match_is_refused(tmp_path):
    # A local sshd isn't needed: with deploy/server-fingerprint pinning a key, an address
    # that answers nothing is "can't reach", never "accepted".
    done = run_ci_deploy(tmp_path, VPS_HOST="127.0.0.1", VPS_PORT="1", VPS_PASSWORD="pw")
    assert done.returncode == 1 and "Can't reach the server at VPS_HOST on port 1" in done.stderr
    assert "Checking the server's identity against deploy/server-fingerprint" in done.stdout
