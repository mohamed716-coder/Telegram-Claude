"""All bot commands: /start, /status, /redeem, the relay commands and admin commands."""

import html
import io
import logging
import time
from datetime import datetime, timezone

from telethon.tl.custom import Message

from .core import (
    Cooldown,
    command_pattern,
    format_duration,
    parse_code_args,
    parse_command,
    to_target_text,
)
from .store import CodeAlreadyUsed, CodeNotFound, Store

log = logging.getLogger("kdbot")

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4000

ADMIN_HELP = (
    "<b>Admin</b>\n"
    "/code &lt;days&gt; &lt;antispam_seconds&gt; [count]\n"
    "/codes – unused codes\n"
    "/delcode &lt;code&gt;\n"
    "/users – premium users\n"
    "/removepremium &lt;user_id&gt;"
)


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def chunks(lines: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Join lines into as few messages as possible without exceeding Telegram's limit."""
    out, current = [], ""
    for line in lines:
        if current and len(current) + 1 + len(line) > limit:
            out.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        out.append(current)
    return out


def display_name(sender) -> str:
    if sender is None:
        return ""
    name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
    username = getattr(sender, "username", None)
    if username:
        name = f"{name} (@{username})" if name else f"@{username}"
    return name


class BotApp:
    def __init__(self, cfg, store: Store, relay, bot, user, owner_id: int, bot_username: str,
                 clock=time.time):
        self.cfg = cfg
        self.store = store
        self.relay = relay
        self.bot = bot  # your bot (talks to users)
        self.user = user  # your account (talks to the target bot)
        self.owner_id = owner_id
        self.bot_username = (bot_username or "").lower()
        self.clock = clock
        self.cooldown = Cooldown()
        self.relay_pattern = command_pattern(cfg.commands)
        self.relay_commands = {c.lower() for c in cfg.commands}
        self.public = {
            "start": self.cmd_start,
            "help": self.cmd_start,
            "status": self.cmd_status,
            "redeem": self.cmd_redeem,
        }
        self.admin = {
            "code": self.cmd_code,
            "codes": self.cmd_codes,
            "delcode": self.cmd_delcode,
            "users": self.cmd_users,
            "removepremium": self.cmd_removepremium,
        }

    def is_admin(self, user_id: int) -> bool:
        return user_id == self.owner_id or user_id in self.cfg.admin_ids

    async def reply(self, event, text: str):
        return await event.reply(text, parse_mode="html", link_preview=False)

    # ---- dispatch ----------------------------------------------------------

    async def handle(self, event) -> None:
        parsed = parse_command(event.raw_text or "")
        if not parsed:
            return
        name, addressed_to, args = parsed
        if addressed_to and addressed_to.lower() != self.bot_username:
            return  # a command for another bot in the same group
        uid = event.sender_id
        self.store.touch_user(uid, display_name(await event.get_sender()))

        if name in self.relay_commands:
            await self.cmd_relay(event)
        elif name in self.public:
            await self.public[name](event, args)
        elif name in self.admin and self.is_admin(uid):
            await self.admin[name](event, args)

    # ---- everyone ----------------------------------------------------------

    def plan_name(self, uid: int) -> str:
        if self.is_admin(uid):
            return "Admin"
        return "Premium" if self.store.premium(uid) else "Free"

    async def cmd_start(self, event, args: str) -> None:
        uid = event.sender_id
        sender = await event.get_sender()
        first = getattr(sender, "first_name", None) or "there"
        plan = self.plan_name(uid)
        lines = [
            f"👋 Welcome, {esc(first)}!",
            "",
            f"<b>Plan:</b> {plan}",
            "",
            "<b>Commands:</b>",
            *(f"/{c}" for c in self.cfg.commands),
            "",
            "/status · /redeem &lt;code&gt;",
        ]
        if plan == "Free":
            lines += ["", "🔒 Redeem a code to unlock the commands."]
        if self.is_admin(uid):
            lines += ["", ADMIN_HELP]
        await self.reply(event, "\n".join(lines))

    async def cmd_status(self, event, args: str) -> None:
        uid = event.sender_id
        if self.is_admin(uid):
            return await self.reply(event, "<b>Plan:</b> Admin\nUnlimited access, no anti-spam.")
        row = self.store.premium(uid)
        if not row:
            note = "Your Premium has expired." if self.store.had_premium(uid) else "You don't have Premium."
            return await self.reply(
                event, f"<b>Plan:</b> Free\n{note}\nActivate it with /redeem &lt;code&gt;"
            )
        left = row["premium_until"] - self.clock()
        await self.reply(
            event,
            "<b>Plan:</b> Premium\n"
            f"<b>Time left:</b> {format_duration(left)}\n"
            f"<b>Expires:</b> {fmt_date(row['premium_until'])}\n"
            f"<b>Anti-spam:</b> {row['cooldown']}s between requests",
        )

    async def cmd_redeem(self, event, args: str) -> None:
        code = args.split()[0] if args.split() else ""
        if not code:
            return await self.reply(event, "Usage: /redeem &lt;code&gt;")
        uid = event.sender_id
        name = display_name(await event.get_sender())
        try:
            result = self.store.redeem(code, uid, name)
        except CodeNotFound:
            return await self.reply(event, "❌ This code is not valid.")
        except CodeAlreadyUsed:
            return await self.reply(event, "❌ This code has already been used.")
        log.info("user %s redeemed %s (%s days)", uid, code, result.days)
        await self.reply(
            event,
            "✅ <b>Premium activated!</b>\n"
            f"<b>Added:</b> {result.days} days\n"
            f"<b>Time left:</b> {format_duration(result.premium_until - self.clock())}\n"
            f"<b>Expires:</b> {fmt_date(result.premium_until)}\n\n"
            f"You can now use " + " ".join(f"/{c}" for c in self.cfg.commands),
        )
        await self.notify_admins(
            f"🎟 {esc(name or uid)} (<code>{uid}</code>) redeemed <code>{esc(code)}</code>"
            f" – {result.days} days, {result.cooldown}s anti-spam"
        )

    async def notify_admins(self, text: str) -> None:
        for admin_id in {self.owner_id, *self.cfg.admin_ids}:
            try:
                await self.bot.send_message(admin_id, text, parse_mode="html")
            except Exception:  # e.g. that admin never opened the bot
                log.warning("could not notify admin %s", admin_id)

    # ---- relay commands (/kd /ko /zz) ----------------------------------------

    async def cmd_relay(self, event) -> None:
        uid = event.sender_id
        text = to_target_text(event.raw_text, self.relay_pattern)
        if text is None:
            return
        if self.is_admin(uid):
            wait_s = 0
        else:
            row = self.store.premium(uid)
            if not row:
                note = "Your Premium has expired." if self.store.had_premium(uid) else "This command needs Premium."
                return await self.reply(event, f"🔒 {note}\nActivate it with /redeem &lt;code&gt;")
            wait_s = row["cooldown"]
        command = text.split(maxsplit=1)[0]
        if len(text.split(maxsplit=1)) < 2:
            return await self.reply(event, f"Usage: {command} &lt;your message&gt;")
        wait = self.cooldown.remaining(uid, wait_s)
        if wait:
            return await self.reply(event, f"⏳ Please wait {wait:.0f}s before the next request.")
        self.cooldown.mark(uid)

        status = await event.reply("In queue…" if self.relay.busy else "Working on it…")
        log.info("user %s -> %r", uid, text)
        try:
            replies = await self.relay.ask(text)
        except Exception:
            log.exception("relay failed")
            return await status.edit("Something went wrong, please try again later.")
        if not replies:
            return await status.edit(f"No answer within {self.cfg.reply_timeout:.0f}s, please try again.")
        await status.delete()
        for msg in replies:
            try:
                await self.forward_reply(event.chat_id, msg, reply_to=event.id)
            except Exception:
                log.exception("failed to forward reply %s", msg.id)

    async def forward_reply(self, chat, msg: Message, reply_to) -> None:
        """Re-send one of the target bot's messages to the user, through our bot."""
        text, entities = msg.message or "", msg.entities
        if msg.media and msg.file and not msg.web_preview:
            data = io.BytesIO(await self.user.download_media(msg, file=bytes))
            data.name = msg.file.name or f"file{msg.file.ext or ''}"
            caption_fits = len(text) <= CAPTION_LIMIT
            await self.bot.send_file(
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
            await self.bot.send_message(chat, text, formatting_entities=entities, reply_to=reply_to)

    # ---- admin -------------------------------------------------------------

    async def cmd_code(self, event, args: str) -> None:
        try:
            days, cooldown, count = parse_code_args(args)
        except ValueError as e:
            return await self.reply(event, esc(e))
        codes = [
            self.store.create_code(days, cooldown, event.sender_id, self.cfg.code_prefix)
            for _ in range(count)
        ]
        log.info("admin %s created %d code(s): %s days, %ss", event.sender_id, count, days, cooldown)
        title = "Code generated:" if count == 1 else f"{count} codes generated:"
        lines = [f"✅ {title}", *(f"<code>{c}</code>" for c in codes), "",
                 f"{days} days · {cooldown}s anti-spam · single use"]
        for part in chunks(lines):
            await self.reply(event, part)

    async def cmd_codes(self, event, args: str) -> None:
        rows = self.store.unused_codes()
        if not rows:
            return await self.reply(event, "No unused codes.")
        lines = [f"<b>Unused codes ({len(rows)}):</b>"] + [
            f"<code>{r['code']}</code> – {r['days']}d, {r['cooldown']}s" for r in rows
        ]
        for part in chunks(lines):
            await self.reply(event, part)

    async def cmd_delcode(self, event, args: str) -> None:
        code = args.split()[0] if args.split() else ""
        if not code:
            return await self.reply(event, "Usage: /delcode &lt;code&gt;")
        if self.store.delete_code(code):
            await self.reply(event, "🗑 Code deleted.")
        else:
            await self.reply(event, "No unused code with that value.")

    async def cmd_users(self, event, args: str) -> None:
        rows = self.store.premium_users()
        if not rows:
            return await self.reply(event, "No premium users.")
        now = self.clock()
        lines = [f"<b>Premium users ({len(rows)}):</b>"] + [
            f"{esc(r['name'] or '–')} <code>{r['user_id']}</code> – "
            f"{format_duration(r['premium_until'] - now)} left, {r['cooldown']}s"
            for r in rows
        ]
        for part in chunks(lines):
            await self.reply(event, part)

    async def cmd_removepremium(self, event, args: str) -> None:
        arg = args.split()[0] if args.split() else ""
        if not arg.lstrip("-").isdigit():
            return await self.reply(event, "Usage: /removepremium &lt;user_id&gt;\nSee /users for IDs.")
        if self.store.remove_premium(int(arg)):
            await self.reply(event, "Premium removed.")
        else:
            await self.reply(event, "That user doesn't have Premium.")
