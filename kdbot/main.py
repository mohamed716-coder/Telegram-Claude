"""Entry point: runs your bot and your account side by side."""

import asyncio
import io
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom import Message

from .config import load_config
from .core import Cooldown, is_valid_arg, parse_command, render_message
from .relay import Relay

log = logging.getLogger("kdbot")

USAGE = "Usage: /kd <value>\nExample: /kd 12345"
CAPTION_LIMIT = 1024


async def forward_reply(bot: TelegramClient, user: TelegramClient, chat, msg: Message, reply_to):
    """Re-send one of the target bot's messages to the user, through our bot."""
    text, entities = msg.message or "", msg.entities
    if msg.media and msg.file and not msg.web_preview:
        data = io.BytesIO(await user.download_media(msg, file=bytes))
        data.name = msg.file.name or f"file{msg.file.ext or ''}"
        caption_fits = len(text) <= CAPTION_LIMIT
        await bot.send_file(
            chat,
            data,
            caption=text if caption_fits else None,
            formatting_entities=entities if caption_fits else None,
            voice_note=bool(msg.voice),
            video_note=bool(msg.video_note),
            reply_to=reply_to,
        )
        if caption_fits:
            return
    if text:
        await bot.send_message(chat, text, formatting_entities=entities, reply_to=reply_to)


async def run(cfg) -> None:
    user = TelegramClient(StringSession(cfg.session_string), cfg.api_id, cfg.api_hash)
    bot = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
    relay = Relay(user, cfg.target_bot, cfg.reply_timeout, cfg.settle_seconds)
    cooldown = Cooldown(cfg.cooldown_seconds)

    @bot.on(events.NewMessage(pattern=r"(?i)^/(start|help)\b"))
    async def help_cmd(event):
        await event.reply(USAGE)

    @bot.on(events.NewMessage(pattern=r"(?i)^/kd\b"))
    async def kd_cmd(event):
        arg = parse_command(event.raw_text)
        if arg is None:
            return
        uid = event.sender_id
        if cfg.allowed_users and uid not in cfg.allowed_users:
            return await event.reply("Sorry, you are not allowed to use this bot.")
        if not arg:
            return await event.reply(USAGE)
        if not is_valid_arg(arg, cfg.arg_pattern):
            return await event.reply("That value is not valid.")
        wait = cooldown.remaining(uid)
        if wait:
            return await event.reply(f"Please wait {wait:.0f}s before the next request.")
        cooldown.mark(uid)

        status = await event.reply("In queue…" if relay.busy else "Working on it…")
        log.info("user %s -> %r", uid, arg)
        try:
            replies = await relay.ask(render_message(cfg.message_template, arg))
        except Exception:
            log.exception("relay failed")
            return await status.edit("Something went wrong, please try again later.")

        if not replies:
            return await status.edit(f"No answer from the bot within {cfg.reply_timeout:.0f}s.")
        await status.delete()
        for msg in replies:
            try:
                await forward_reply(bot, user, event.chat_id, msg, reply_to=event.id)
            except Exception:
                log.exception("failed to forward reply %s", msg.id)

    await user.connect()
    if not await user.is_user_authorized():
        raise SystemExit("SESSION_STRING is invalid or expired; run `python login.py` again.")
    await bot.start(bot_token=cfg.bot_token)
    me = await user.get_me()
    log.info("Running. Relaying as %s to %s", me.username or me.id, cfg.target_bot)
    try:
        await bot.run_until_disconnected()
    finally:
        await user.disconnect()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(load_config()))


if __name__ == "__main__":
    main()
