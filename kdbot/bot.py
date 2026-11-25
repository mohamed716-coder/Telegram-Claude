"""All bot commands: /start, /status, /redeem, the relay commands and admin commands."""

import functools
import html
import io
import logging
import re
import secrets
import time
from datetime import datetime, timezone

from telethon import Button
from telethon.errors import MessageNotModifiedError
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

# Shown at the end of every edited answer.
OWNER_TAG = "@anony6916"
# When line 6 of the target bot's answer is exactly this, the "0" texts are used.
ZERO_LINE = "• PROCESSED: 0 + 10 / 10"
# In a /setresponse... text, <line_4>, <line_6> etc. are replaced by that line of the
# target bot's answer.
LINE_TAG = re.compile(r"<line_(\d+)>")
# Admin commands that set what users see. Each text is saved per relay command,
# under "<admin command>:<relay command>", e.g. "setresponseaft:kd".
RESPONSE_COMMANDS = ("setresponsebef", "setresponseaft", "setresponse0bef", "setresponse0aft")
# Callback data of the "which command?" buttons: "setresp:<token>:<relay command or *>".
BUTTON_PREFIX = "setresp"

ADMIN_HELP = (
    "<b>Admin</b>\n"
    "/code &lt;days&gt; &lt;antispam_seconds&gt; [count]\n"
    "/codes – unused codes\n"
    "/delcode &lt;code&gt;\n"
    "/users – premium users\n"
    "/removepremium &lt;user_id&gt;\n"
    "\n"
    "<b>What users see</b> (write your text on the lines below the command; "
    "the bot then asks which command it's for)\n"
    "/setresponsebef – the first answer\n"
    "/setresponseaft – after the target bot edits its answer\n"
    "/setresponse0bef, /setresponse0aft – the same, when line 6 is "
    f"<code>{html.escape(ZERO_LINE)}</code>\n"
    "&lt;line_4&gt;, &lt;line_6&gt;… in your text = that line of the target bot's answer. "
    "Send a command with no text to reset it."
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


def line_of(text: str, n: int) -> str:
    """Line `n` (counting from 1) of `text`, or "" if it has fewer lines."""
    lines = text.split("\n")
    return lines[n - 1] if 1 <= n <= len(lines) else ""


def is_zero(text: str) -> bool:
    """True when line 6 says nothing was processed."""
    return line_of(text, 6).strip() == ZERO_LINE


def render(template: str, source: str) -> str:
    """Fill a /setresponse... text in: <line_N> becomes line N of the target bot's answer."""
    return LINE_TAG.sub(lambda m: line_of(source, int(m.group(1))), template)


def user_tag(username: str | None) -> str:
    return f"@{username}" if username else "Unknown"


def footer(username: str | None) -> str:
    return f"\n\nUSER: {user_tag(username)}\nStatus: Premium\nOwner: {OWNER_TAG}"


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
            **{name: functools.partial(self.cmd_setresponse, name) for name in RESPONSE_COMMANDS},
        }
        # /setresponse... texts waiting for the admin to tap which command they're for.
        self.pending: dict[str, tuple[str, str]] = {}  # token -> (admin command, text)

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

    async def notify_owner(self, text: str) -> None:
        """Send `text` to the owner as plain text (exactly as written)."""
        try:
            await self.bot.send_message(self.owner_id, text, parse_mode=None)
        except Exception:  # e.g. the owner never opened the bot
            log.warning("could not notify the owner")

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

        relay_cmd = command.lstrip("/").lower()  # "kd", "zz" or "ko": picks which texts are used
        username = getattr(await event.get_sender(), "username", None)
        if uid != self.owner_id:
            # Tell the owner who used it and what they typed after the command.
            await self.notify_owner(f"USER: {user_tag(username)}\n{text.split(maxsplit=1)[1]}")
        status = await event.reply("In queue…" if self.relay.busy else "Working on it…")
        log.info("user %s -> %r", uid, text)
        answered = False
        shown: dict[int, object] = {}  # target bot's message id -> our message showing it

        async def deliver(replies, is_update: bool) -> None:
            # Called with the first answer, then again each time the target bot
            # edits its answer; every call is for this user's request only.
            nonlocal answered
            if not answered:
                answered = True
                await status.delete()
            for msg in replies:
                try:
                    content = self.response_for(msg, is_update, username, relay_cmd)
                    ours = shown.get(msg.id)
                    if ours is None:
                        shown[msg.id] = await self.show(event.chat_id, msg, *content, reply_to=event.id)
                    else:
                        shown[msg.id] = await self.update_shown(
                            event.chat_id, ours, msg, *content, reply_to=event.id
                        )
                except Exception:
                    log.exception("failed to forward %s %s", "update" if is_update else "reply", msg.id)

        try:
            await self.relay.ask(text, deliver)
        except Exception:
            log.exception("relay failed")
            if not answered:
                await status.edit("Something went wrong, please try again later.")
            return
        if not answered:
            await status.edit(f"No answer within {self.cfg.reply_timeout:.0f}s, please try again.")

    def response_for(self, msg: Message, is_update: bool, username: str | None, relay_cmd: str):
        """What the user sees for one of the target bot's messages: (text, entities, with_media).

        A text set with /setresponse... for this relay command replaces the message
        entirely (only its <line_N> lines are taken from it). Without one, it's shown
        as the target bot sent it. Edited versions always end with the footer.
        """
        source = msg.message or ""
        name = "setresponse" + ("0" if is_zero(source) else "") + ("aft" if is_update else "bef")
        template = self.store.get_setting(f"{name}:{relay_cmd}")
        if template is not None:
            text, entities, with_media = render(template, source), None, False
        else:
            text, entities, with_media = source, msg.entities, True
        if is_update:
            text += footer(username)
        return text, entities, with_media

    async def show(self, chat, msg: Message, text: str, entities, with_media: bool, reply_to):
        """Send a new message to the user. Returns the message holding the text (to edit later)."""
        if with_media and msg.media and msg.file and not msg.web_preview:
            data = io.BytesIO(await self.user.download_media(msg, file=bytes))
            data.name = msg.file.name or f"file{msg.file.ext or ''}"
            caption_fits = len(text) <= CAPTION_LIMIT
            sent = await self.bot.send_file(
                chat,
                data,
                caption=text if caption_fits else None,
                formatting_entities=entities if caption_fits else None,
                parse_mode=None,
                voice_note=bool(msg.voice),
                video_note=bool(msg.video_note),
                reply_to=reply_to,
            )
            if caption_fits or not text:
                return sent
        if not text:
            return None
        # parse_mode=None: the text is shown exactly as written, never read as markdown.
        return await self.bot.send_message(
            chat, text, formatting_entities=entities, parse_mode=None, reply_to=reply_to
        )

    async def update_shown(self, chat, ours, msg: Message, text: str, entities, with_media: bool,
                           reply_to):
        """Edit the message the user already has; send a new one only if it can't be edited."""
        try:
            return await self.bot.edit_message(
                chat, ours, text, formatting_entities=entities, parse_mode=None
            )
        except MessageNotModifiedError:  # what the user sees didn't change
            return ours
        except Exception:
            log.warning("could not edit message, sending a new one", exc_info=True)
            return await self.show(chat, msg, text, entities, with_media, reply_to)

    # ---- admin -------------------------------------------------------------

    async def cmd_setresponse(self, name: str, event, args: str) -> None:
        """/setresponse(0)bef|aft with the text on the next lines (or none, to reset).

        Asks with buttons which relay command the text is for; handle_button saves it.
        """
        token = secrets.token_hex(4)
        self.pending[token] = (name, args)
        buttons = [
            [Button.inline(f"/{c}", f"{BUTTON_PREFIX}:{token}:{c.lower()}".encode())
             for c in self.cfg.commands],
            [Button.inline("All commands", f"{BUTTON_PREFIX}:{token}:*".encode())],
        ]
        what = "text" if args else "reset"
        await event.reply(f"Which command is this /{name} {what} for?", buttons=buttons)

    async def handle_button(self, event) -> None:
        """An admin tapped /kd, /zz, /ko or All commands under a /setresponse... question."""
        if not self.is_admin(event.sender_id):
            return await event.answer("Only admins can do this.", alert=True)
        try:
            _, token, choice = event.data.decode().split(":", 2)
        except ValueError:
            return await event.answer()
        if choice != "*" and choice not in self.relay_commands:
            return await event.answer("Unknown command.", alert=True)
        pending = self.pending.pop(token, None)
        if pending is None:
            return await event.answer("This has expired, please send the command again.", alert=True)
        name, text = pending
        targets = [c.lower() for c in self.cfg.commands] if choice == "*" else [choice]
        for c in targets:
            if text:
                self.store.set_setting(f"{name}:{c}", text)
            else:
                self.store.delete_setting(f"{name}:{c}")
        commands = " ".join(f"/{c}" for c in targets)
        if text:
            preview = text
            if name.endswith("aft"):
                preview += footer(getattr(await event.get_sender(), "username", None))
            reply = f"✅ Saved /{name} for {commands}. Users will see:\n\n{esc(preview)}"
        else:
            reply = f"↩️ /{name} reset for {commands}: users see the target bot's message as it is."
        await event.edit(reply, parse_mode="html", buttons=None)

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
