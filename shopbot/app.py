"""Build and run the bot."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import NoReturn, Optional

from telegram import Update
from telegram.error import InvalidToken, NetworkError
from telegram.ext import Application
from telegram.request import BaseRequest

from . import lock
from .config import ConfigError, Settings
from .db import Database
from .handlers import register_handlers
from .handlers.common import setup_bot_profile
from .ratelimit import ShopRateLimiter

log = logging.getLogger("shopbot")

# Exit codes, so a service manager knows when restarting is pointless
# (deploy/install.sh tells systemd not to restart after EXIT_CONFIG).
EXIT_CONFIG = 78  # wrong settings, e.g. a missing or wrong BOT_TOKEN
EXIT_RUNNING = 75  # another copy is already running with the same data folder

TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]{30,}")


async def _post_init(app: Application) -> None:
    await setup_bot_profile(app)
    me = await app.bot.get_me()
    log.info("Bot @%s is running. Owner ID: %s", me.username, app.bot_data["settings"].owner_id)


def build_application(
    settings: Settings,
    *,
    request: Optional[BaseRequest] = None,
    rate_limit: bool = True,
) -> Application:
    builder = Application.builder().token(settings.bot_token).post_init(_post_init)
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    if rate_limit:
        # Waits and retries automatically when Telegram says "too many requests"
        # (short waits only; see ratelimit.py).
        builder = builder.rate_limiter(ShopRateLimiter(max_retries=5))
    app = builder.build()
    app.bot_data["db"] = Database(settings.db_path, settings.owner_id)
    app.bot_data["settings"] = settings
    register_handlers(app)
    return app


def _fail(code: int, message: str) -> NoReturn:
    log.error(message)
    sys.exit(code)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        _fail(EXIT_CONFIG, f"{exc}. Fix it in the .env file.")
    if not settings.bot_token:
        _fail(
            EXIT_CONFIG,
            "BOT_TOKEN is not set. Create a bot with @BotFather, then put the token in a "
            ".env file (BOT_TOKEN=123456:ABC...) or in the BOT_TOKEN environment variable.",
        )
    if not TOKEN_RE.fullmatch(settings.bot_token):
        _fail(
            EXIT_CONFIG,
            "BOT_TOKEN doesn't look like a bot token (it looks like 123456789:AAH...). "
            "Copy it again from @BotFather and put it in .env.",
        )
    lock_path = Path(settings.db_path).parent / lock.LOCK_NAME
    instance = lock.acquire(lock_path)
    if instance is None:
        pid = lock.holder(lock_path)
        _fail(
            EXIT_RUNNING,
            "Another copy of the bot is already running"
            + (f" (process {pid})" if pid else "")
            + " with the same data folder. Only one copy may run at a time: stop the other "
            "one first (on a server: sudo systemctl stop shopbot).",
        )
    try:
        app = build_application(settings)
        app.run_polling(
            allowed_updates=[Update.MESSAGE, Update.EDITED_MESSAGE, Update.CALLBACK_QUERY]
        )
    except InvalidToken:
        _fail(
            EXIT_CONFIG,
            "Telegram rejected BOT_TOKEN. Copy the token again from @BotFather "
            "(/mybots → your bot → API Token) and put it in .env.",
        )
    except NetworkError as exc:  # only reaches here while starting; polling retries itself
        _fail(1, f"Couldn't reach Telegram ({exc}). Check the internet connection; "
                 "the bot will be started again automatically if it runs as a service.")
    finally:
        instance.close()
