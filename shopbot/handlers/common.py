"""Shared helpers and the always-available handlers (/start, /help, menu, router)."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Conflict, NetworkError, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ..config import Settings
from ..db import Database

log = logging.getLogger("shopbot")

Ctx = ContextTypes.DEFAULT_TYPE

# Telegram button colours (Bot API 9.4+).
GREEN, BLUE, RED = "success", "primary", "danger"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
OWNER, ADMIN, USER = "owner", "admin", "user"

# The largest number SQLite can store. Bigger ids in callback data are invalid.
MAX_DB_INT = 2**63 - 1

# Callback prefixes that must NOT end the action in progress (see pre_handler).
KEEP_STATE_PREFIXES = {"apd", "apc", "dsend", "wall", "dok", "dno", "wok", "wno", "noop"}

BOT_DESCRIPTION = "🛍 Welcome to the shop!\n\nPress /start to start the shop."
BOT_SHORT_DESCRIPTION = "🛍 Send /start to start the shop."
EDIT_HINT = "✏️ Edited messages are ignored. Please send a new message instead."

USER_COMMANDS = [
    ("start", "Start the shop"),
    ("help", "Show all commands"),
    ("cancel", "Cancel the current action"),
]
ADMIN_COMMANDS = [
    ("start", "Open the admin panel"),
    ("withdraw", "Withdraw your earnings"),
    ("help", "Show all commands"),
    ("cancel", "Cancel the current action"),
]
OWNER_COMMANDS = [
    ("start", "Open the owner panel"),
    ("help", "Show all commands"),
    ("addbal", "Add balance to a user by ID"),
    ("modifybal", "See and set a user's balance"),
    ("addadmin", "Make a user an admin by ID"),
    ("removeadmin", "Remove an admin by ID"),
    ("seller", "Make a user a seller (discount)"),
    ("removeseller", "Remove a seller by ID"),
    ("ban", "Ban a user by ID"),
    ("unban", "Unban a user by ID"),
    ("withdraw", "Withdraw your earnings"),
    ("close", "Close the shop"),
    ("open", "Open the shop"),
    ("status", "All users, balances and deposits"),
    ("pending", "Deposits and withdrawals to review"),
    ("announce", "Send a message to all users"),
    ("cancel", "Cancel the current action"),
]


# ------------------------------------------------------------------ accessors
def db_of(context: Ctx) -> Database:
    return context.bot_data["db"]


def cfg_of(context: Ctx) -> Settings:
    return context.bot_data["settings"]


def role_of(context: Ctx, user_id: int) -> str:
    if user_id == cfg_of(context).owner_id:
        return OWNER
    if db_of(context).is_admin(user_id):
        return ADMIN
    return USER


def is_owner(context: Ctx, user_id: int) -> bool:
    return user_id == cfg_of(context).owner_id


def is_staff(context: Ctx, user_id: int) -> bool:
    return role_of(context, user_id) in (OWNER, ADMIN)


def support_contact(context: Ctx) -> str:
    """Who customers should write to: SUPPORT_CONTACT, else the owner's @username."""
    cfg = cfg_of(context)
    value = (cfg.support_contact or "").strip()
    if value.lower() in ("none", "off", "no", "-"):
        return ""
    if value:
        return value
    row = db_of(context).get_user(cfg.owner_id)
    if row is not None and row["username"]:
        return f"@{row['username']}"
    return ""


def contact_owner(context: Ctx) -> str:
    """'contact @support' when a contact is known, else 'contact the owner' (HTML)."""
    contact = support_contact(context)
    return f"contact {esc(contact)}" if contact else "contact the owner"


# ----------------------------------------------------------------- formatting
def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}$"


_AMOUNT_RE = re.compile(r"^\$?\s*([0-9]{1,9})(?:[.,]([0-9]{1,2}))?\s*\$?$")
_INT_RE = re.compile(r"^\$?\s*([0-9]{1,15})\s*\$?$")


def parse_amount(text: Optional[str]) -> Optional[int]:
    """'12', '12.5', '12,50', '$12', '12$' -> cents. None when invalid."""
    match = _AMOUNT_RE.match((text or "").strip())
    if not match:
        return None
    cents = int(match.group(1)) * 100
    if match.group(2):
        cents += int(match.group(2).ljust(2, "0"))
    return cents


def parse_int(text: Optional[str]) -> Optional[int]:
    """A whole number such as '25' or '25$'. None when invalid."""
    match = _INT_RE.match((text or "").strip())
    return int(match.group(1)) if match else None


def utf16_len(text: Optional[str]) -> int:
    """Length as Telegram counts it (UTF-16 code units)."""
    return len((text or "").encode("utf-16-le")) // 2


def user_name(row: Any) -> str:
    """Plain-text display name for a users row."""
    if row is None:
        return "Unknown user"
    name = " ".join(p for p in (row["first_name"], row["last_name"]) if p).strip()
    if name:
        return name
    if row["username"]:
        return f"@{row['username']}"
    return f"User {row['id']}"


def user_html(row: Any) -> str:
    """HTML: Name (@username) · ID."""
    if row is None:
        return "Unknown user"
    parts = [f"<b>{esc(user_name(row))}</b>"]
    if row["username"]:
        parts.append(f"@{esc(row['username'])}")
    parts.append(f"<code>{row['id']}</code>")
    return " · ".join(parts)


def off_names(rows: list[Any]) -> str:
    """'“A” is' or '“A” and “B” are': for messages about disabled labels."""
    names = " and ".join(f"“{esc(row['name'])}”" for row in rows)
    return f"{names} {'is' if len(rows) == 1 else 'are'}"


def hidden_note(db: Database, label_id: int) -> str:
    """For the owner and admins: why customers don't see this label because of a
    disabled label (itself, or one above it), or "" if that isn't the case."""
    off = db.disabled_on_path(label_id)
    if not off:
        return ""
    above = [row for row in off if row["id"] != label_id]
    if len(above) == len(off):
        return f"🚫 <i>Customers don't see it: {off_names(above)} disabled.</i>"
    if above:
        return f"🚫 <i>Disabled, and {off_names(above)} disabled too: customers don't see it.</i>"
    return "🚫 <i>Disabled: customers don't see it.</i>"


def short(text: str, limit: int = 28) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def csv_safe(value: Any) -> Any:
    """Stop spreadsheet apps from running text such as '=HYPERLINK(...)' as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# ------------------------------------------------------------------ keyboards
def btn(text: str, data: str, style: Optional[str] = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data, style=style)


def markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def grid(buttons: list[InlineKeyboardButton], cols: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + cols] for i in range(0, len(buttons), cols)]


def menu_btn() -> InlineKeyboardButton:
    return btn("🏠 Menu", "menu")


def cancel_kb() -> InlineKeyboardMarkup:
    return markup([[btn("❌ Cancel", "cancel", RED)]])


def pager(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    """◀️ 2/5 ▶️ row; ``prefix`` is the callback data without the page number."""
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(btn("▶️", f"{prefix}{page + 1}"))
    return row


def page_count(total: int, size: int) -> int:
    return max(1, (total + size - 1) // size)


def callback_ints(data: Optional[str], count: int) -> list[int]:
    """'xx:1:2' -> [1, 2]. Raises ValueError for malformed or out-of-range data."""
    parts = (data or "").split(":")[1:]
    if len(parts) < count:
        raise ValueError(data)
    values = [int(p) for p in parts[:count]]
    if any(v < 0 or v > MAX_DB_INT for v in values):
        raise ValueError(data)
    return values


# ---------------------------------------------------------------------- state
StateHandler = Callable[[Update, Ctx, dict], Awaitable[None]]
STATE_HANDLERS: dict[str, StateHandler] = {}

# Called for edited messages, in order, until one returns True (it handled the edit).
EditHook = Callable[[Update, Ctx], Awaitable[bool]]
EDIT_HOOKS: list[EditHook] = []


def state_handler(name: str) -> Callable[[StateHandler], StateHandler]:
    def decorator(func: StateHandler) -> StateHandler:
        STATE_HANDLERS[name] = func
        return func

    return decorator


def edit_hook(func: EditHook) -> EditHook:
    EDIT_HOOKS.append(func)
    return func


def get_state(context: Ctx) -> Optional[dict]:
    if context.user_data is None:
        return None
    return context.user_data.get("state")


def clear_state(context: Ctx) -> Optional[dict]:
    if context.user_data is None:
        return None
    task = context.user_data.pop("status_task", None)
    if task is not None and not task.done():
        task.cancel()
    return context.user_data.pop("state", None)


def set_state(context: Ctx, name: str, **data: Any) -> dict:
    clear_state(context)
    state = {"name": name, **data}
    context.user_data["state"] = state  # type: ignore[index]
    return state


# --------------------------------------------------------------------- output
async def answer(
    query: Optional[CallbackQuery], text: Optional[str] = None, show_alert: bool = False
) -> None:
    """Answer a button press. Never raises.

    Telegram refuses to answer a press that waited too long ("query is too old"), and
    the network can time out. Neither may stop the action the press asked for.
    """
    if query is None:
        return
    try:
        await query.answer(text, show_alert=show_alert)
    except TelegramError as exc:
        log.info("Could not answer a button press: %s", exc)


async def show(
    update: Update,
    context: Ctx,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    new: bool = False,
) -> None:
    """Edit the message whose button was pressed, or send a new message."""
    query = update.callback_query
    if (
        query is not None
        and not new
        and query.message is not None
        and getattr(query.message, "text", None) is not None
    ):
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                link_preview_options=NO_PREVIEW,
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.debug("Could not edit message, sending a new one: %s", exc)
    await context.bot.send_message(
        update.effective_chat.id,  # type: ignore[union-attr]
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        link_preview_options=NO_PREVIEW,
    )


async def try_show(
    update: Update,
    context: Ctx,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    new: bool = False,
) -> bool:
    """show() for use after a change was saved: a Telegram error is logged, not raised,
    so the steps that follow (deliveries, notifications) still run."""
    try:
        await show(update, context, text, reply_markup, new=new)
        return True
    except TelegramError as exc:
        log.warning("Could not show a message to %s: %s", update.effective_chat.id, exc)  # type: ignore[union-attr]
        return False


async def notify(
    context: Ctx, chat_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None
) -> bool:
    """Message another user; False if they blocked the bot or never started it."""
    try:
        await context.bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            link_preview_options=NO_PREVIEW,
        )
        return True
    except TelegramError as exc:
        log.info("Could not notify %s: %s", chat_id, exc)
        return False


async def tell(update: Update, text: str, *, alert: bool = False) -> None:
    """Best-effort short reply: an alert for a button press, else a message."""
    if update.callback_query is not None:
        await answer(update.callback_query, text, show_alert=alert)
        return
    message = update.effective_message
    if message is None:
        return
    try:
        await message.reply_text(text)
    except TelegramError as exc:
        log.info("Could not reply to %s: %s", update.effective_chat.id, exc)  # type: ignore[union-attr]


async def need_text(update: Update) -> Optional[str]:
    """The text of the incoming message, or None after asking for text."""
    message = update.effective_message
    if message is None or message.text is None:
        if message is not None:
            await message.reply_text("✍️ Please send this as a text message.")
        return None
    return message.text


# ---------------------------------------------------------------------- panel
def panel_text(context: Ctx, user_id: int) -> str:
    db, cfg = db_of(context), cfg_of(context)
    row = db.get_user(user_id) or db.ensure_user(user_id)
    role = role_of(context, user_id)
    if role == OWNER:
        title = "Welcome Owner"
    elif role == ADMIN:
        title = "Welcome Admin"
    else:
        title = f"Welcome {esc(row['first_name'] or 'to the shop')}"
    lines = [f"👋 <b>{title}</b>", "", f"💰 Balance: <b>{money(row['balance'])}</b>"]
    if row["is_seller"]:
        lines.append(f"⭐ Seller — <b>{cfg.seller_discount_percent}% off</b> all products")
    if role in (OWNER, ADMIN) or row["earnings"] > 0:
        lines.append(f"💼 Earnings: <b>{money(row['earnings'])}</b>")
    if role == OWNER:
        lines.append("🏪 Shop: " + ("🟢 Open" if db.shop_is_open() else "🔴 Closed"))
        pending = db.count_pending_requests()
        if pending:
            lines.append(f"📥 Requests waiting for you: <b>{pending}</b>")
    return "\n".join(lines)


def panel_keyboard(role: str, *, earnings: int = 0, pending: int = 0) -> InlineKeyboardMarkup:
    shop = btn("🛍 Shop", "sh:0:0", GREEN)
    add_balance = btn("💳 Add Balance", "dp", GREEN)
    if role == OWNER:
        rows = [
            [shop, btn("📦 My Products", "mp:0:0", BLUE)],
            [btn("➕ Add Product", "ap:0:0", GREEN), btn("🏷 Add Label", "lb:0:0", GREEN)],
            [btn("📊 Earnings", "ea", BLUE), add_balance],
        ]
        if pending:
            rows.append([btn(f"📥 Pending requests ({pending})", "pq", BLUE)])
    elif role == ADMIN:
        rows = [
            [shop, btn("📦 My Products", "mp:0:0", BLUE)],
            [btn("➕ Add Product", "ap:0:0", GREEN), btn("📊 Earnings", "ea", BLUE)],
            [add_balance],
        ]
    else:
        rows = [[shop, add_balance], [btn("🧾 My Orders", "mo:0", BLUE)]]
        if earnings > 0:  # e.g. a removed admin who still has money to withdraw
            rows[1].append(btn("📊 Earnings", "ea", BLUE))
    return markup(rows)


async def send_panel(update: Update, context: Ctx, *, new: bool = False, note: str = "") -> None:
    user_id = update.effective_user.id  # type: ignore[union-attr]
    db = db_of(context)
    text = panel_text(context, user_id)
    if note:
        text = f"{note}\n\n{text}"
    role = role_of(context, user_id)
    row = db.get_user(user_id)
    keyboard = panel_keyboard(
        role,
        earnings=row["earnings"] if row is not None else 0,
        pending=db.count_pending_requests() if role == OWNER else 0,
    )
    await show(update, context, text, keyboard, new=new)


def help_text(role: str, cfg: Settings, *, earnings: int = 0, support: str = "") -> str:
    lines = ["📖 <b>Commands</b>"]
    if role == OWNER:
        lines += [
            "",
            "👑 <b>Owner panel</b>",
            "/addbal <code>ID AMOUNT</code> — add balance to a user",
            "/modifybal <code>ID</code> — see a user's balance and set a new one",
            "/addadmin <code>ID</code> — make a user an admin",
            "/removeadmin <code>ID</code> — remove an admin",
            f"/seller <code>ID</code> — make a user a seller ({cfg.seller_discount_percent}% off)",
            "/removeseller <code>ID</code> — remove a seller",
            "/ban <code>ID</code> — ban a user from the bot",
            "/unban <code>ID</code> — unban a user",
            "/close — close the shop",
            "/open — open the shop",
            "/status — all users with ID, name, balance and deposits",
            "/pending — deposits and withdrawals waiting for your answer",
            "/announce — send a message to all users (or <code>/announce your text</code>)",
            "",
            "Buttons: 🛍 Shop · 📦 My Products · ➕ Add Product · 🏷 Add Label · "
            "📊 Earnings · 💳 Add Balance",
        ]
    if role in (OWNER, ADMIN):
        lines += [
            "",
            "👮 <b>Admin panel</b>",
            "/withdraw — withdraw your earnings to Binance"
            + (" (recorded instantly for the owner)" if role == OWNER else ""),
        ]
        if role == ADMIN:
            lines.append(
                "Buttons: 🛍 Shop · 📦 My Products · ➕ Add Product · 📊 Earnings · 💳 Add Balance"
            )
    lines += [
        "",
        "👤 <b>Everyone</b>",
        "/start — open the shop menu",
        "/help — show this list",
        "/cancel — cancel the current action",
    ]
    if role == USER:
        if earnings > 0:
            lines.append(f"/withdraw — withdraw your earnings ({money(earnings)} available)")
        lines.append("Buttons: 🛍 Shop · 💳 Add Balance · 🧾 My Orders")
    lines += [
        "",
        f"⭐ Sellers get {cfg.seller_discount_percent}% off every product. You become a seller "
        f"automatically once your total deposits reach {cfg.seller_auto_threshold}$.",
    ]
    if support and role != OWNER:
        lines += ["", f"💬 Questions or problems? Contact {esc(support)}"]
    return "\n".join(lines)


# ----------------------------------------------------------- command menus
def _commands(pairs: list[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(name, description) for name, description in pairs]


async def sync_commands(context: Ctx, user_id: int) -> None:
    """Show the right command list in this user's chat menu."""
    role = role_of(context, user_id)
    try:
        if role == OWNER:
            await context.bot.set_my_commands(
                _commands(OWNER_COMMANDS), scope=BotCommandScopeChat(user_id)
            )
        elif role == ADMIN:
            await context.bot.set_my_commands(
                _commands(ADMIN_COMMANDS), scope=BotCommandScopeChat(user_id)
            )
        else:
            await context.bot.delete_my_commands(scope=BotCommandScopeChat(user_id))
    except TelegramError as exc:
        log.info("Could not set commands for %s: %s", user_id, exc)


PROFILE_SETTING = "bot_profile"


def _profile_signature(bot_id: int, owner_id: int, admin_ids: list[int]) -> str:
    payload = json.dumps(
        [BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION, USER_COMMANDS, ADMIN_COMMANDS,
         OWNER_COMMANDS, bot_id, owner_id, admin_ids],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def setup_bot_profile(application: Any) -> None:
    """Bot description, default command list and per-chat lists for staff.

    Skipped when nothing changed since the last successful run, so restarting the bot
    doesn't call Telegram's bot-settings methods (which have strict flood limits) again.
    """
    bot = application.bot
    db: Database = application.bot_data["db"]
    cfg: Settings = application.bot_data["settings"]
    admins = [row["id"] for row in db.list_admins() if row["id"] != cfg.owner_id]
    signature = _profile_signature(bot.id, cfg.owner_id, admins)  # a new token may be a new bot
    if db.get_setting(PROFILE_SETTING) == signature:
        log.info("Bot profile is up to date.")
        return

    profile_ok = True
    for what, call in (
        ("description", lambda: bot.set_my_description(BOT_DESCRIPTION)),
        ("short description", lambda: bot.set_my_short_description(BOT_SHORT_DESCRIPTION)),
        ("command menu", lambda: bot.set_my_commands(_commands(USER_COMMANDS))),
    ):
        try:
            await call()
        except TelegramError as exc:
            profile_ok = False
            log.warning("Could not set the bot %s: %s", what, exc)

    staff = [(cfg.owner_id, OWNER_COMMANDS)] + [(admin_id, ADMIN_COMMANDS) for admin_id in admins]
    for chat_id, commands in staff:
        try:
            await bot.set_my_commands(_commands(commands), scope=BotCommandScopeChat(chat_id))
        except TelegramError as exc:  # e.g. that user never opened the bot yet
            log.info("Could not set commands for %s: %s", chat_id, exc)
    if profile_ok:
        db.set_setting(PROFILE_SETTING, signature)


# ------------------------------------------------------------------- handlers
def _is_command(update: Update) -> bool:
    message = update.effective_message
    if message is None or not message.text:
        return False
    return any(e.type == "bot_command" and e.offset == 0 for e in message.entities or ())


async def pre_handler(update: Update, context: Ctx) -> None:
    """Runs before every other handler: private chats only, register user, bans, flows.

    Raising ApplicationHandlerStop is the only way to keep an update away from the
    other handlers, so every path that must block an update ends with it, even when
    the database or Telegram fails on the way.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or chat.type != ChatType.PRIVATE or user is None or user.is_bot:
        raise ApplicationHandlerStop
    db = db_of(context)
    try:
        db.touch_user(user.id, user.first_name, user.last_name, user.username)
        banned = db.is_banned(user.id)
    except Exception as exc:  # e.g. the database is locked: don't let the update through unchecked
        log.exception("Could not check user %s", user.id)
        await tell(update, "⚠️ Something went wrong. Please try again in a moment.", alert=True)
        raise ApplicationHandlerStop from exc

    edited = update.edited_message is not None
    if banned:
        clear_state(context)
        if not edited:
            await tell(update, "🚫 You are banned from this bot.", alert=True)
        raise ApplicationHandlerStop

    if edited:
        return  # an edit never ends the action in progress (see on_edited_message)

    # Pressing a menu button or sending a command ends the action in progress.
    ends_flow = False
    if update.callback_query is not None:
        prefix = (update.callback_query.data or "").split(":", 1)[0]
        ends_flow = prefix not in KEEP_STATE_PREFIXES
    elif _is_command(update):
        ends_flow = True
    if ends_flow:
        old = clear_state(context)
        if old and old.get("name") == "add_items" and old.get("items"):
            try:
                await context.bot.send_message(
                    chat.id,
                    f"⚠️ Add Product stopped — {len(old['items'])} unsaved product(s) were discarded.",
                )
            except TelegramError as exc:
                log.info("Could not send the discard warning to %s: %s", chat.id, exc)


async def cmd_start(update: Update, context: Ctx) -> None:
    user_id = update.effective_user.id  # type: ignore[union-attr]
    synced: set[int] = context.bot_data.setdefault("commands_synced", set())
    if user_id not in synced and role_of(context, user_id) != USER:
        await sync_commands(context, user_id)
        synced.add(user_id)
    await send_panel(update, context, new=True)


async def cmd_help(update: Update, context: Ctx) -> None:
    user_id = update.effective_user.id  # type: ignore[union-attr]
    row = db_of(context).get_user(user_id)
    text = help_text(
        role_of(context, user_id),
        cfg_of(context),
        earnings=row["earnings"] if row is not None else 0,
        support=support_contact(context),
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)  # type: ignore[union-attr]


async def cmd_cancel(update: Update, context: Ctx) -> None:
    clear_state(context)
    await send_panel(update, context, new=True, note="❌ Cancelled.")


async def cb_menu(update: Update, context: Ctx) -> None:
    await answer(update.callback_query)
    await send_panel(update, context)


async def cb_cancel(update: Update, context: Ctx) -> None:
    clear_state(context)
    await answer(update.callback_query, "Cancelled")
    await send_panel(update, context, note="❌ Cancelled.")


async def cb_noop(update: Update, context: Ctx) -> None:
    await answer(update.callback_query)


async def cb_unknown(update: Update, context: Ctx) -> None:
    await answer(
        update.callback_query, "This button is no longer active. Send /start.", show_alert=True
    )


async def stale_button(update: Update) -> None:
    await answer(
        update.callback_query, "This button is no longer valid. Send /start.", show_alert=True
    )


async def on_unknown_command(update: Update, context: Ctx) -> None:
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "🤔 Unknown command. Send /help to see the commands."
    )


async def on_message(update: Update, context: Ctx) -> None:
    """Route a plain message to the action the user is in the middle of."""
    state = get_state(context)
    if state is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "👋 Send /start to open the shop menu."
        )
        return
    handler = STATE_HANDLERS.get(state.get("name", ""))
    if handler is None:
        clear_state(context)
        await update.effective_message.reply_text("👋 Send /start to open the shop menu.")  # type: ignore[union-attr]
        return
    await handler(update, context, state)


async def on_edited_message(update: Update, context: Ctx) -> None:
    """An edited message is never new input.

    Running an edited command again would, for example, add an /addbal amount twice,
    and taking an edit as the answer to the current step could send money to the
    wrong user. Edit hooks may still handle an edit (Add Product replaces a product
    that isn't saved yet); otherwise the user is told to send a new message.
    """
    for hook in EDIT_HOOKS:
        if await hook(update, context):
            return
    if get_state(context) is not None or _is_command(update):
        await tell(update, EDIT_HINT)


async def on_error(update: object, context: Ctx) -> None:
    error = context.error
    if update is None and isinstance(error, Conflict):  # from polling, not from a user's action
        log.warning("Telegram says another copy of this bot is running (Telegram: %s). Only one "
                    "copy may run: stop the other one (on your PC, another server, Docker...). If "
                    "no other copy of yours runs anywhere, someone else may have your bot token: "
                    "send /revoke to @BotFather and install the new token.", error.message)
        return
    if update is None and isinstance(error, NetworkError):
        log.warning("Can't reach Telegram right now (%s). The bot keeps trying by itself.", error)
        return
    log.error("Error while handling an update", exc_info=error)
    if isinstance(update, Update) and update.effective_chat is not None:
        if update.effective_chat.type != ChatType.PRIVATE:
            return
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Something went wrong. Please try again, or send /start.",
            )
        except TelegramError:
            pass
