"""Build and run the bot."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import NoReturn, Optional

from telegram import Update
from telegram.error import InvalidToken, NetworkError, TelegramError
from telegram.ext import Application
from telegram.request import BaseRequest

from . import lock
from .config import ConfigError, Settings
from .db import Database
from .handlers import register_handlers
from .binance import BinancePay
from .handlers.autopay import check_at_start
from .handlers.broadcast import resume_broadcasts
from .handlers.common import setup_bot_profile
from .ratelimit import ShopRateLimiter

log = logging.getLogger("shopbot")

# Exit codes, so a service manager knows when restarting is pointless
# (deploy/install.sh tells systemd not to restart after EXIT_CONFIG).
EXIT_CONFIG = 78  # wrong settings, e.g. a missing or wrong BOT_TOKEN
EXIT_RUNNING = 75  # another copy is already running with the same data folder

TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]{30,}")
OFFLINE_NOTICE_EVERY = 30  # seconds between "still trying" log lines while starting
TOKEN_CHECK_EVERY = 120  # seconds between checks that the token still works
CHECKS_TIMEOUT = 60  # seconds a stop waits for payment checks (systemd allows 300)

_connected = threading.Event()


async def _watch_token(app: Application) -> None:
    """A token revoked while the bot runs only makes polling stop quietly. Notice it and
    stop the bot, so it exits with EXIT_CONFIG and the service shows that it failed."""
    while True:
        await asyncio.sleep(TOKEN_CHECK_EVERY)
        try:
            await app.bot.get_me()
        except InvalidToken:
            app.bot_data["token_rejected"] = True
            app.stop_running()
            return
        except TelegramError:
            continue  # network trouble: polling copes with that by itself


async def _post_stop(app: Application) -> None:
    task = app.bot_data.pop("token_watch", None)
    if task is not None:
        task.cancel()
    # A payment check started while stopping isn't awaited by Application.stop(): let it
    # finish (it usually takes seconds) before its connection to Binance is closed.
    checks = [t for t in asyncio.all_tasks()
              if t.get_name().startswith("shopbot:binance:") and not t.done()]
    if checks:
        log.info("Finishing %s payment check(s) before stopping ...", len(checks))
        await asyncio.wait(checks, timeout=CHECKS_TIMEOUT)
    client = app.bot_data.get("binance")
    if client is not None:
        await client.close()  # a check still running now ends as if Binance couldn't be reached
    late = [t for t in checks if not t.done()]
    if late:
        log.warning("%s payment check(s) were cut off by the stop", len(late))
        await asyncio.wait(late, timeout=5)  # their answer, while the bot can still send it
    # Broadcasts notice the stop within a second and save how far they got.
    busy = [t for t in app.bot_data.get("broadcast_tasks", ()) if not t.done()]
    if busy:
        await asyncio.wait(busy, timeout=10)
    # A purchase confirmed while stopping starts its delivery after Application.stop()
    # stopped waiting for tasks: wait for those too, or the buyer pays and gets nothing.
    late = [t for t in asyncio.all_tasks()
            if t.get_name().startswith("shopbot:delivery:") and not t.done()]
    if late:
        log.info("Finishing %s delivery(ies) before stopping ...", len(late))
        await asyncio.gather(*late, return_exceptions=True)


async def _post_init(app: Application) -> None:
    _connected.set()  # Telegram answered: the start-up connection works
    await setup_bot_profile(app)
    app.bot_data["token_watch"] = asyncio.create_task(_watch_token(app))
    resume_broadcasts(app)  # a broadcast (or noted new stock) that a restart interrupted
    if app.bot_data.get("binance") is not None:  # tell the owner if the key doesn't work
        app.bot_data["binance_check"] = asyncio.create_task(check_at_start(app), name="binance-check")
    log.info("Bot @%s is running. Owner ID: %s", app.bot.username, app.bot_data["settings"].owner_id)


def build_application(
    settings: Settings,
    *,
    request: Optional[BaseRequest] = None,
    rate_limit: bool = True,
) -> Application:
    builder = (
        Application.builder().token(settings.bot_token).post_init(_post_init).post_stop(_post_stop)
    )
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    if rate_limit:
        # Waits and retries automatically when Telegram says "too many requests"
        # (short waits only; see ratelimit.py).
        builder = builder.rate_limiter(ShopRateLimiter(max_retries=5))
    app = builder.build()
    app.bot_data["db"] = Database(settings.db_path, settings.owner_id)
    app.bot_data["settings"] = settings
    # Deposits are checked automatically with a read-only Binance key, if there is one.
    app.bot_data["db"].start_binance_auto(settings.binance_auto)  # payments before don't count
    app.bot_data["binance"] = (
        BinancePay(settings.binance_api_key, settings.binance_api_secret)
        if settings.binance_auto else None
    )
    register_handlers(app)
    return app


def _report_while_offline() -> None:
    """While the bot can't reach Telegram at start (it keeps retrying quietly), say so."""
    waited = 0
    while not _connected.wait(OFFLINE_NOTICE_EVERY):
        waited += OFFLINE_NOTICE_EVERY
        log.warning("Still trying to reach Telegram (%ss so far): no internet, or Telegram is "
                    "down. The bot keeps trying by itself.", waited)


class _HideToken(logging.Formatter):
    """Keep the bot token (and the Binance key) out of the logs: Telegram's error messages
    can contain the token."""

    def __init__(self, fmt: str, token: str, *others: str) -> None:
        super().__init__(fmt)
        self.secrets = [(token, "<BOT_TOKEN>")] + [(s, "<SECRET>") for s in others if s]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret, mask in self.secrets:
            if secret:
                text = text.replace(secret, mask)
        return text


def _fail(code: int, message: str) -> NoReturn:
    log.error(message)
    sys.exit(code)


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def main() -> None:
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        _fail(EXIT_CONFIG, f"{exc}. Fix it in the .env file (or your host's variables).")
    if not settings.bot_token:
        _fail(
            EXIT_CONFIG,
            "BOT_TOKEN is not set. Create a bot with @BotFather, then put the token in a "
            ".env file (BOT_TOKEN=123456:ABC...) or in the BOT_TOKEN environment variable.",
        )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(_HideToken(LOG_FORMAT, settings.bot_token,
                                        settings.binance_api_key, settings.binance_api_secret))
    if not TOKEN_RE.fullmatch(settings.bot_token):
        _fail(
            EXIT_CONFIG,
            "BOT_TOKEN doesn't look like a bot token (it looks like 123456789:AAH...). "
            "Copy it again from @BotFather and put it in .env (or your host's variables).",
        )
    lock_path = Path(settings.db_path).parent / lock.LOCK_NAME
    try:
        instance = lock.acquire(lock_path)
    except PermissionError as exc:
        _fail(EXIT_CONFIG, f"Can't use the data folder {lock_path.parent}: {exc}. "
                           "Run the bot as the user who owns that folder.")
    except OSError as exc:  # e.g. a full disk: may go away, so let the service retry
        _fail(1, f"Can't use the data folder {lock_path.parent}: {exc}. Is the disk full? "
                 "The bot is started again automatically if it runs as a service.")
    if instance is None:
        pid = lock.holder(lock_path)
        _fail(
            EXIT_RUNNING,
            "Another copy of the bot is already running"
            + (f" (process {pid})" if pid else "")
            + " with the same data folder. Only one copy may run at a time: stop the other "
            "one first (on a server: sudo systemctl stop shopbot; with Docker: docker compose stop).",
        )
    _connected.clear()
    threading.Thread(target=_report_while_offline, daemon=True).start()
    try:
        app = build_application(settings)
        app.run_polling(
            allowed_updates=[Update.MESSAGE, Update.EDITED_MESSAGE, Update.CALLBACK_QUERY],
            # Keep trying to reach Telegram while starting (e.g. right after a reboot or
            # during an outage) instead of exiting; a rejected token still stops at once.
            bootstrap_retries=-1,
        )
        if app.bot_data.get("token_rejected"):
            raise InvalidToken("revoked while running")
    except InvalidToken:
        _fail(
            EXIT_CONFIG,
            "Telegram rejected BOT_TOKEN. Copy the token again from @BotFather "
            "(/mybots → your bot → API Token) and put it in .env (or your host's variables).",
        )
    except sqlite3.OperationalError as exc:  # can't open or write it (owner, full disk...)
        _fail(1, f"Can't use the database {settings.db_path} ({exc}). Check that the disk isn't "
                 "full and that the bot's user owns the data folder (on a server, running "
                 "deploy/install.sh again fixes the owner). The bot is started again "
                 "automatically if it runs as a service.")
    except sqlite3.DatabaseError as exc:  # the file itself is damaged or not a database
        _fail(
            EXIT_CONFIG,
            f"Can't open the database {settings.db_path} ({exc}). It looks damaged: restore the "
            "newest backup (DEPLOY.md: Restore a backup).",
        )
    except NetworkError as exc:  # only reaches here while starting; polling retries itself
        _fail(1, f"Couldn't reach Telegram ({exc}). Check the internet connection; "
                 "the bot will be started again automatically if it runs as a service.")
    finally:
        _connected.set()  # ends the "still trying" reports
        instance.close()
