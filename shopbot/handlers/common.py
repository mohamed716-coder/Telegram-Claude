"""Shared helpers and the always-available handlers (/start, /help, menu, router)."""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ..config import Settings
from ..db import Database

log = logging.getLogger("shopbot")

Ctx = ContextTypes.DEFAULT_TYPE

# Telegram button colours (Bot API 9.4+).
GREEN, BLUE, RED = "success", "primary", "danger"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
OWNER, ADMIN, USER = "owner", "admin", "user"

# Callback prefixes that must NOT end the action in progress (see pre_handler).
KEEP_STATE_PREFIXES = {"apd", "apc", "dsend", "wall", "dok", "dno", "wok", "wno", "noop"}

BOT_DESCRIPTION = "🛍 Welcome to the shop!\n\nPress /start to start the shop."
BOT_SHORT_DESCRIPTION = "🛍 Send /start to start the shop."

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


def callback_ints(data: str, count: int) -> list[int]:
    """'xx:1:2' -> [1, 2]. Raises ValueError for malformed data."""
    parts = data.split(":")[1:]
    if len(parts) < count:
        raise ValueError(data)
    values = [int(p) for p in parts[:count]]
    if any(v < 0 for v in values):
        raise ValueError(data)
    return values


# ---------------------------------------------------------------------- state
StateHandler = Callable[[Update, Ctx, dict], Awaitable[None]]
STATE_HANDLERS: dict[str, StateHandler] = {}


def state_handler(name: str) -> Callable[[StateHandler], StateHandler]:
    def decorator(func: StateHandler) -> StateHandler:
        STATE_HANDLERS[name] = func
        return func

    return decorator


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
    if role in (OWNER, ADMIN):
        lines.append(f"💼 Earnings: <b>{money(row['earnings'])}</b>")
    if role == OWNER:
        lines.append("🏪 Shop: " + ("🟢 Open" if db.shop_is_open() else "🔴 Closed"))
    return "\n".join(lines)


def panel_keyboard(role: str) -> InlineKeyboardMarkup:
    shop = btn("🛍 Shop", "sh:0:0", GREEN)
    add_balance = btn("💳 Add Balance", "dp", GREEN)
    if role == OWNER:
        rows = [
            [shop, btn("📦 My Products", "mp:0:0", BLUE)],
            [btn("➕ Add Product", "ap:0:0", GREEN), btn("🏷 Add Label", "lb:0:0", GREEN)],
            [btn("📊 Earnings", "ea", BLUE), add_balance],
        ]
    elif role == ADMIN:
        rows = [
            [shop, btn("📦 My Products", "mp:0:0", BLUE)],
            [btn("➕ Add Product", "ap:0:0", GREEN), btn("📊 Earnings", "ea", BLUE)],
            [add_balance],
        ]
    else:
        rows = [[shop, add_balance], [btn("🧾 My Orders", "mo:0", BLUE)]]
    return markup(rows)


async def send_panel(update: Update, context: Ctx, *, new: bool = False, note: str = "") -> None:
    user_id = update.effective_user.id  # type: ignore[union-attr]
    text = panel_text(context, user_id)
    if note:
        text = f"{note}\n\n{text}"
    await show(update, context, text, panel_keyboard(role_of(context, user_id)), new=new)


def help_text(role: str, cfg: Settings) -> str:
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
        lines.append("Buttons: 🛍 Shop · 💳 Add Balance · 🧾 My Orders")
    lines += [
        "",
        f"⭐ Sellers get {cfg.seller_discount_percent}% off every product. You become a seller "
        f"automatically once your total deposits reach {cfg.seller_auto_threshold}$.",
    ]
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


async def setup_bot_profile(application: Any) -> None:
    """Bot description, default command list and per-chat lists for staff."""
    bot = application.bot
    db: Database = application.bot_data["db"]
    cfg: Settings = application.bot_data["settings"]
    try:
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
        await bot.set_my_commands(_commands(USER_COMMANDS))
    except TelegramError as exc:
        log.warning("Could not set the bot description/commands: %s", exc)
    staff = [(cfg.owner_id, OWNER_COMMANDS)] + [
        (row["id"], ADMIN_COMMANDS) for row in db.list_admins() if row["id"] != cfg.owner_id
    ]
    for chat_id, commands in staff:
        try:
            await bot.set_my_commands(_commands(commands), scope=BotCommandScopeChat(chat_id))
        except TelegramError as exc:  # e.g. that user never opened the bot yet
            log.info("Could not set commands for %s: %s", chat_id, exc)


# ------------------------------------------------------------------- handlers
def _is_command(update: Update) -> bool:
    message = update.effective_message
    if message is None or not message.text:
        return False
    return any(e.type == "bot_command" and e.offset == 0 for e in message.entities or ())


async def pre_handler(update: Update, context: Ctx) -> None:
    """Runs before every other handler: private chats only, register user, bans, flows."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or chat.type != ChatType.PRIVATE or user is None or user.is_bot:
        raise ApplicationHandlerStop
    db = db_of(context)
    db.touch_user(user.id, user.first_name, user.last_name, user.username)

    if db.is_banned(user.id):
        clear_state(context)
        if update.callback_query is not None:
            await update.callback_query.answer("🚫 You are banned from this bot.", show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text("🚫 You are banned from this bot.")
        raise ApplicationHandlerStop

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
            await context.bot.send_message(
                chat.id,
                f"⚠️ Add Product stopped — {len(old['items'])} unsaved product(s) were discarded.",
            )


async def cmd_start(update: Update, context: Ctx) -> None:
    user_id = update.effective_user.id  # type: ignore[union-attr]
    synced: set[int] = context.bot_data.setdefault("commands_synced", set())
    if user_id not in synced and role_of(context, user_id) != USER:
        await sync_commands(context, user_id)
        synced.add(user_id)
    await send_panel(update, context, new=True)


async def cmd_help(update: Update, context: Ctx) -> None:
    user_id = update.effective_user.id  # type: ignore[union-attr]
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        help_text(role_of(context, user_id), cfg_of(context)), parse_mode=ParseMode.HTML
    )


async def cmd_cancel(update: Update, context: Ctx) -> None:
    clear_state(context)
    await send_panel(update, context, new=True, note="❌ Cancelled.")


async def cb_menu(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()  # type: ignore[union-attr]
    await send_panel(update, context)


async def cb_cancel(update: Update, context: Ctx) -> None:
    clear_state(context)
    await update.callback_query.answer("Cancelled")  # type: ignore[union-attr]
    await send_panel(update, context, note="❌ Cancelled.")


async def cb_noop(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()  # type: ignore[union-attr]


async def cb_unknown(update: Update, context: Ctx) -> None:
    await update.callback_query.answer(  # type: ignore[union-attr]
        "This button is no longer active. Send /start.", show_alert=True
    )


async def stale_button(update: Update) -> None:
    await update.callback_query.answer(  # type: ignore[union-attr]
        "This button is no longer valid. Send /start.", show_alert=True
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


async def on_error(update: object, context: Ctx) -> None:
    log.error("Error while handling an update", exc_info=context.error)
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
