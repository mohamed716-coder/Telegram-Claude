"""Build and run the bot."""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Update
from telegram.ext import AIORateLimiter, Application
from telegram.request import BaseRequest

from .config import Settings
from .db import Database
from .handlers import register_handlers
from .handlers.common import setup_bot_profile

log = logging.getLogger("shopbot")


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
        # Waits and retries automatically when Telegram says "too many requests".
        builder = builder.rate_limiter(AIORateLimiter(max_retries=5))
    app = builder.build()
    app.bot_data["db"] = Database(settings.db_path, settings.owner_id)
    app.bot_data["settings"] = settings
    register_handlers(app)
    return app


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    if not settings.bot_token:
        raise SystemExit(
            "BOT_TOKEN is not set. Create a bot with @BotFather, then put the token in a "
            ".env file (BOT_TOKEN=123456:ABC...) or in the BOT_TOKEN environment variable."
        )
    app = build_application(settings)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
