"""Owner-only commands.

Every command that needs a user accepts the ID right away (``/ban 123``) or asks
for it when it's missing (``/ban`` → "Send the user's Telegram ID").
"""

from __future__ import annotations

import csv
import functools
import io
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from telegram import Update
from telegram.constants import ParseMode

from ..db import BalanceChanged, ShopError
from .common import (
    Ctx,
    cancel_kb,
    cfg_of,
    clear_state,
    csv_safe,
    db_of,
    esc,
    is_owner,
    markup,
    menu_btn,
    money,
    need_text,
    notify,
    parse_amount,
    parse_int,
    set_state,
    state_handler,
    sync_commands,
    user_html,
    user_name,
)

Handler = Callable[[Update, Ctx], Awaitable[None]]
STATUS_TEXT_LIMIT = 50  # users listed in the /status message; the CSV has everyone


def owner_command(func: Handler) -> Handler:
    @functools.wraps(func)
    async def wrapper(update: Update, context: Ctx) -> None:
        if not is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
            await update.effective_message.reply_text("⛔ Only the owner can use this command.")  # type: ignore[union-attr]
            return
        await func(update, context)

    return wrapper


async def reply(update: Update, text: str, keyboard: Any = None) -> None:
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


def _not_started_note(row: Any) -> str:
    if row is not None and row["started"]:
        return ""
    return "\nℹ️ This user hasn't started the bot yet."


def _who(row: Any, user_id: int) -> str:
    """A user for a reply, without adding unknown IDs to the database."""
    return user_html(row) if row is not None else f"User <code>{user_id}</code>"


# ------------------------------------------------------------ user actions
async def do_add_admin(update: Update, context: Ctx, target: int) -> None:
    db = db_of(context)
    if is_owner(context, target):
        await reply(update, "👑 That's you — the owner already has every permission.")
        return
    if db.is_admin(target):
        await reply(update, f"👮 {user_html(db.get_user(target))} is already an admin.")
        return
    row = db.set_admin(target, True)
    await sync_commands(context, target)
    await reply(update, f"✅ {user_html(row)} is now an <b>admin</b>.{_not_started_note(row)}")
    await notify(context, target,
                 "👮 You're now an <b>admin</b> of this shop.\nSend /start to open the admin panel.")


async def do_remove_admin(update: Update, context: Ctx, target: int) -> None:
    db = db_of(context)
    if not db.is_admin(target):
        await reply(update, f"ℹ️ {_who(db.get_user(target), target)} isn't an admin.")
        return
    row = db.set_admin(target, False)
    await sync_commands(context, target)
    await reply(
        update,
        f"✅ {user_html(row)} is no longer an admin.\n"
        "Their products stay in the shop; you can remove or pull them in 📦 My Products.",
    )
    text = "ℹ️ Your admin access to this shop was removed."
    if row["earnings"] > 0:
        text += (
            f"\n💼 You still have <b>{money(row['earnings'])}</b> in earnings: send /withdraw "
            "to withdraw them, or use 📊 Earnings in the menu (/start)."
        )
    await notify(context, target, text)


async def do_add_seller(update: Update, context: Ctx, target: int) -> None:
    db, cfg = db_of(context), cfg_of(context)
    current = db.get_user(target)
    if current is not None and current["is_seller"]:
        await reply(update, f"⭐ {user_html(current)} is already a seller.")
        return
    row = db.set_seller(target, True)
    await reply(
        update,
        f"✅ {user_html(row)} is now a <b>seller</b> "
        f"({cfg.seller_discount_percent}% off all products).{_not_started_note(row)}",
    )
    await notify(context, target,
                 f"⭐ You're now a <b>Seller</b>! You get <b>{cfg.seller_discount_percent}% off</b> "
                 "every product in the shop.")


async def do_remove_seller(update: Update, context: Ctx, target: int) -> None:
    db = db_of(context)
    current = db.get_user(target)
    if current is None or not current["is_seller"]:
        await reply(update, f"ℹ️ {_who(current, target)} isn't a seller.")
        return
    row = db.set_seller(target, False)
    await reply(
        update,
        f"✅ {user_html(row)} is no longer a seller.\n"
        "They won't become a seller automatically again — use /seller to bring it back.",
    )
    await notify(context, target, "ℹ️ Your seller discount was removed.")


async def do_ban(update: Update, context: Ctx, target: int) -> None:
    db = db_of(context)
    if is_owner(context, target):
        await reply(update, "👑 You can't ban yourself.")
        return
    current = db.get_user(target)
    if current is not None and current["is_banned"]:
        await reply(update, f"🚫 {user_html(current)} is already banned.")
        return
    try:
        row = db.set_banned(target, True)
    except ShopError as exc:
        await reply(update, f"⚠️ {esc(exc)}")
        return
    # Stop whatever they were doing in the bot.
    user_data = context.application.user_data.get(target)
    if user_data is not None:
        task = user_data.pop("status_task", None)
        if task is not None and not task.done():
            task.cancel()
        user_data.pop("state", None)
        user_data.pop("buy_nonce", None)
    extra = "\n👮 They're an admin: their access is blocked while banned." if row["is_admin"] else ""
    await reply(update, f"🚫 {user_html(row)} is now <b>banned</b>.{extra}{_not_started_note(row)}")
    await notify(context, target, "🚫 You have been banned from this bot.")


async def do_unban(update: Update, context: Ctx, target: int) -> None:
    db = db_of(context)
    current = db.get_user(target)
    if current is None or not current["is_banned"]:
        await reply(update, f"ℹ️ {_who(current, target)} isn't banned.")
        return
    row = db.set_banned(target, False)
    await reply(update, f"✅ {user_html(row)} is no longer banned.")
    await notify(context, target, "✅ You have been unbanned. Send /start to open the shop.")


async def ask_add_balance_amount(update: Update, context: Ctx, target: int) -> None:
    row = db_of(context).get_user(target) or db_of(context).ensure_user(target)
    set_state(context, "owner_amount", action="addbal", target=target)
    await reply(
        update,
        f"💰 <b>Add balance</b>\n👤 {user_html(row)}\n💳 Current balance: {money(row['balance'])}"
        f"{_not_started_note(row)}\n\nSend the amount to add, e.g. <code>50</code> or <code>12.50</code>.",
        cancel_kb(),
    )


async def ask_new_balance(update: Update, context: Ctx, target: int) -> None:
    row = db_of(context).get_user(target) or db_of(context).ensure_user(target)
    # Remember the balance shown: if it changes before the answer (a deposit is
    # approved, the user buys something), the owner is asked again instead.
    set_state(context, "owner_amount", action="modifybal", target=target, shown=row["balance"])
    await reply(
        update,
        f"💰 <b>Modify balance</b>\n👤 {user_html(row)}\n"
        f"💳 Balance: <b>{money(row['balance'])}</b>\n"
        f"📥 Total deposits: {money(row['total_deposits'])}{_not_started_note(row)}\n\n"
        "What should the balance be? Send the new balance, e.g. <code>25</code> or <code>25.50</code>.",
        cancel_kb(),
    )


async def do_add_balance(update: Update, context: Ctx, target: int, amount: int) -> None:
    db, cfg = db_of(context), cfg_of(context)
    try:
        row, became_seller = db.manual_deposit(target, amount, cfg.seller_auto_threshold * 100)
    except ShopError as exc:
        await reply(update, f"⚠️ {esc(exc)}")
        return
    text = (
        f"✅ Added <b>{money(amount)}</b> to {user_html(row)}.\n"
        f"💳 New balance: <b>{money(row['balance'])}</b>\n"
        f"📥 Total deposits: {money(row['total_deposits'])}"
    )
    if became_seller:
        text += f"\n⭐ They reached {cfg.seller_auto_threshold}$ in deposits and are now a seller."
    text += _not_started_note(row)
    await reply(update, text)
    message = (
        f"💰 <b>{money(amount)}</b> was added to your balance.\n"
        f"💳 New balance: <b>{money(row['balance'])}</b>"
    )
    if became_seller:
        message += (
            f"\n\n⭐ Your total deposits reached {cfg.seller_auto_threshold}$, so you're now a "
            f"<b>Seller</b> with <b>{cfg.seller_discount_percent}% off</b> all products."
        )
    await notify(context, target, message, markup([[menu_btn()]]))


async def do_set_balance(
    update: Update, context: Ctx, target: int, amount: int, expected: Optional[int] = None
) -> None:
    db = db_of(context)
    try:
        old, new = db.set_balance(target, amount, expected_old=expected)
    except BalanceChanged as exc:
        set_state(context, "owner_amount", action="modifybal", target=target, shown=exc.balance)
        await reply(
            update,
            f"⚠️ The balance of {user_html(db.get_user(target))} changed to "
            f"<b>{money(exc.balance)}</b> while you were typing (a deposit or a purchase), so "
            "nothing was changed.\n\nSend the new balance again, or press Cancel.",
            cancel_kb(),
        )
        return
    except ShopError as exc:
        await reply(update, f"⚠️ {esc(exc)}")
        return
    row = db.get_user(target)
    await reply(
        update,
        f"✅ Balance of {user_html(row)} changed from "
        f"{money(old)} to <b>{money(new)}</b>.{_not_started_note(row)}",
    )


TARGET_ACTIONS: dict[str, tuple[str, Callable[[Update, Ctx, int], Awaitable[None]]]] = {
    "addbal": ("💰 <b>Add balance</b>", ask_add_balance_amount),
    "modifybal": ("💰 <b>Modify balance</b>", ask_new_balance),
    "addadmin": ("👮 <b>Add admin</b>", do_add_admin),
    "removeadmin": ("👮 <b>Remove admin</b>", do_remove_admin),
    "seller": ("⭐ <b>Add seller</b>", do_add_seller),
    "removeseller": ("⭐ <b>Remove seller</b>", do_remove_seller),
    "ban": ("🚫 <b>Ban user</b>", do_ban),
    "unban": ("✅ <b>Unban user</b>", do_unban),
}


def _target_from_args(args: Optional[list[str]]) -> Optional[int]:
    if not args:
        return None
    return parse_int(args[0])


async def _run_target_command(update: Update, context: Ctx, action: str) -> None:
    title, func = TARGET_ACTIONS[action]
    args = context.args or []
    if not args:
        set_state(context, "owner_target", action=action)
        await reply(update, f"{title}\n\nSend the user's Telegram ID (numbers only).", cancel_kb())
        return
    target = _target_from_args(args)
    if target is None or target <= 0:
        await reply(update, f"⚠️ <code>{esc(args[0])}</code> isn't a valid user ID. Example: /{action} 123456789")
        return
    if action in ("addbal", "modifybal") and len(args) > 1:
        amount = parse_amount(args[1])
        if amount is None or (action == "addbal" and amount <= 0):
            await reply(update, f"⚠️ <code>{esc(args[1])}</code> isn't a valid amount. Example: /{action} {target} 25")
            return
        if action == "addbal":
            await do_add_balance(update, context, target, amount)
        else:
            await do_set_balance(update, context, target, amount)
        return
    await func(update, context, target)


@state_handler("owner_target")
async def on_owner_target(update: Update, context: Ctx, state: dict) -> None:
    if not is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
        clear_state(context)
        return
    text = await need_text(update)
    if text is None:
        return
    target = parse_int(text)
    if target is None or target <= 0:
        await reply(update, "⚠️ Please send a numeric Telegram user ID, e.g. <code>123456789</code>.")
        return
    clear_state(context)
    _, func = TARGET_ACTIONS[state["action"]]
    await func(update, context, target)


@state_handler("owner_amount")
async def on_owner_amount(update: Update, context: Ctx, state: dict) -> None:
    if not is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
        clear_state(context)
        return
    text = await need_text(update)
    if text is None:
        return
    amount = parse_amount(text)
    if amount is None or (state["action"] == "addbal" and amount <= 0):
        await reply(update, "⚠️ Please send an amount like <code>25</code> or <code>25.50</code>.")
        return
    clear_state(context)
    if state["action"] == "addbal":
        await do_add_balance(update, context, state["target"], amount)
    else:
        await do_set_balance(update, context, state["target"], amount, state.get("shown"))


def _make(action: str) -> Handler:
    @owner_command
    async def handler(update: Update, context: Ctx) -> None:
        await _run_target_command(update, context, action)

    handler.__name__ = f"cmd_{action}"
    return handler


cmd_addbal = _make("addbal")
cmd_modifybal = _make("modifybal")
cmd_addadmin = _make("addadmin")
cmd_removeadmin = _make("removeadmin")
cmd_seller = _make("seller")
cmd_removeseller = _make("removeseller")
cmd_ban = _make("ban")
cmd_unban = _make("unban")


# ------------------------------------------------------------- shop status
@owner_command
async def cmd_close(update: Update, context: Ctx) -> None:
    db = db_of(context)
    if not db.shop_is_open():
        await reply(update, "🔒 The shop is already closed. Send /open to open it.")
        return
    db.set_shop_open(False)
    await reply(update, "🔒 <b>The shop is now closed.</b>\nCustomers can't browse or buy until you send /open.")


@owner_command
async def cmd_open(update: Update, context: Ctx) -> None:
    db = db_of(context)
    if db.shop_is_open():
        await reply(update, "🟢 The shop is already open.")
        return
    db.set_shop_open(True)
    await reply(update, "🟢 <b>The shop is now open.</b>")


def _badges(context: Ctx, row: Any) -> str:
    badges = []
    if is_owner(context, row["id"]):
        badges.append("👑")
    elif row["is_admin"]:
        badges.append("👮")
    if row["is_seller"]:
        badges.append("⭐")
    if row["is_banned"]:
        badges.append("🚫")
    if not row["started"]:
        badges.append("⏳")
    return (" " + "".join(badges)) if badges else ""


def _role_name(context: Ctx, row: Any) -> str:
    if is_owner(context, row["id"]):
        return "owner"
    return "admin" if row["is_admin"] else "user"


@owner_command
async def cmd_status(update: Update, context: Ctx) -> None:
    db = db_of(context)
    users = db.list_users()
    totals = db.totals()
    header = (
        f"👥 <b>Users: {len(users)}</b>\n"
        f"💰 Balances held: {money(totals['balances'])} · 📥 Deposits: {money(totals['deposits'])}\n"
        "👑 owner · 👮 admin · ⭐ seller · 🚫 banned · ⏳ never started the bot\n"
    )
    entries = []
    for index, row in enumerate(users[:STATUS_TEXT_LIMIT], start=1):
        username = f" @{esc(row['username'])}" if row["username"] else ""
        entries.append(
            f"{index}. <b>{esc(user_name(row))}</b>{username} — <code>{row['id']}</code>"
            f"{_badges(context, row)}\n"
            f"    💰 {money(row['balance'])} · 📥 {money(row['total_deposits'])}"
        )
    chunks, current = [], header
    for entry in entries:
        if len(current) + len(entry) + 2 > 3900:
            chunks.append(current)
            current = ""
        current += "\n" + entry
    chunks.append(current)
    for chunk in chunks:
        await reply(update, chunk)
    if users:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "ID", "Name", "Username", "Role", "Seller", "Banned", "Started bot",
            "Balance ($)", "Total deposits ($)", "Earnings ($)", "Joined (UTC)", "Last seen (UTC)",
        ])
        for row in users:
            writer.writerow([
                row["id"], csv_safe(user_name(row)), csv_safe(row["username"]),
                _role_name(context, row),
                "yes" if row["is_seller"] else "no", "yes" if row["is_banned"] else "no",
                "yes" if row["started"] else "no",
                f"{row['balance'] / 100:.2f}", f"{row['total_deposits'] / 100:.2f}",
                f"{row['earnings'] / 100:.2f}", row["created_at"], row["last_seen"] or "",
            ])
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
        more = (
            f"Showing {STATUS_TEXT_LIMIT} of {len(users)} users above — the file has everyone."
            if len(users) > STATUS_TEXT_LIMIT
            else "All users as a spreadsheet."
        )
        await context.bot.send_document(
            update.effective_chat.id,  # type: ignore[union-attr]
            document=buffer.getvalue().encode("utf-8-sig"),
            filename=f"users_{stamp}.csv",
            caption=f"📄 {more}",
        )
