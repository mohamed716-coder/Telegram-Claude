"""Entry point: runs your bot and your account side by side."""

import asyncio
import logging
import os
import sys

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from .bot import BotApp
from .config import load_config
from .relay import Relay
from .store import Store

log = logging.getLogger("kdbot")


async def run(cfg) -> None:
    # Retry connections forever, so short network problems never stop the bot.
    net = dict(connection_retries=None, retry_delay=5, auto_reconnect=True)
    user = TelegramClient(StringSession(cfg.session_string), cfg.api_id, cfg.api_hash, **net)
    # The bot's login and the codes/users database are kept on disk.
    os.makedirs(cfg.data_dir, exist_ok=True)
    bot = TelegramClient(os.path.join(cfg.data_dir, "bot"), cfg.api_id, cfg.api_hash, **net)
    store = Store(os.path.join(cfg.data_dir, "kdbot.db"))
    relay = Relay(user, cfg.target_bot, cfg.reply_timeout, cfg.settle_seconds)

    await user.connect()
    if not await user.is_user_authorized():
        raise SystemExit("SESSION_STRING is invalid or expired; run `python login.py` again.")
    await bot.start(bot_token=cfg.bot_token)
    me = await user.get_me()
    bot_me = await bot.get_me()

    app = BotApp(cfg, store, relay, bot, user, owner_id=me.id, bot_username=bot_me.username)

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/"))
    async def on_command(event):
        try:
            await app.handle(event)
        except Exception:
            log.exception("error while handling %r", event.raw_text)

    log.info(
        "Running as @%s. Relaying as %s (admin) to %s",
        bot_me.username, me.username or me.id, cfg.target_bot,
    )
    await run_until_either_disconnects(user, bot)
    # Getting here means a connection was lost for good; exit with an error so
    # Docker/systemd restarts the bot from scratch.
    raise SystemExit("Lost connection to Telegram; exiting so the bot gets restarted.")


async def run_until_either_disconnects(*clients) -> None:
    tasks = [asyncio.ensure_future(c.run_until_disconnected()) for c in clients]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for client in clients:
            await client.disconnect()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(run(load_config()))


if __name__ == "__main__":
    main()
