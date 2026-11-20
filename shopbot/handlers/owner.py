"""Owner-only commands.

Every command that needs a user accepts the ID right away (``/ban 123``) or asks
for it when it's missing (``/ban`` → "Send the user's Telegram ID").
"""

from __future__ import annotations

import asyncio
import csv
import functools
import io
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError

from ..binance import clean_order_id
from ..db import BalanceChanged, PaymentUsed, PaymentWaiting, ShopError
from .autopay import client_of, find_for_owner, paid_text, payment_record
from .money import doubt_text, listed_text, own_count
from .common import (
    GREEN,
    RED,
    Ctx,
    answer,
    btn,
    cancel_kb,
    cfg_of,
    clear_state,
    csv_safe,
    db_of,
    esc,
    get_state,
    is_owner,
    markup,
    menu_btn,
    money,
    need_text,
    notify,
    parse_amount,
    parse_int,
    set_state,
    short,
    state_handler,
    sync_commands,
    user_html,
    user_name,
)

Handler = Callable[[Update, Ctx], Awaitable[None]]
log = logging.getLogger("shopbot.owner")
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
        f"{_not_started_note(row)}\n\nSend the amount to add, e.g. <code>50</code> or <code>12.50</code>."
        + ("\nNext you can give the Binance Pay Order ID of the payment, or skip it."
           if client_of(context) is not None else ""),
        cancel_kb(),
    )


def single_order_id(text: str) -> Optional[str]:
    """An Order ID sent on its own (also as '#…' or 'Order ID: …'), else None: an amount or a
    word next to it must never become part of the ID (clean_order_id joins what it's given)."""
    value = re.sub(r"(?i)^\s*order\s*id\s*:?\s*", "", text or "").strip()
    return clean_order_id(value) if len(value.split()) == 1 else None


def _skip_kb(context: Ctx, state: dict) -> Any:
    if not state.get("skip", True):  # after a refused Order ID: another one, or Cancel
        return cancel_kb()
    who = short(user_name(db_of(context).get_user(state["target"])), 16)
    return markup([[btn(f"⏭ Skip: add {money(state['amount'])} to {who}", f"abk:{state['nonce']}", GREEN)],
                   [btn("❌ Cancel", "cancel", RED)]])


async def ask_order_id(update: Update, context: Ctx, target: int, amount: int, note: str = "",
                       *, skip: bool = True, refused: Optional[str] = None) -> None:
    """The amount is known: ask for the Binance Pay Order ID it's for. It's optional:
    ⏭ Skip adds the balance without one, but not right after an Order ID was refused
    (that payment may be the one this money is for), unless Binance can't be asked about
    it at all (``refused``: that Order ID, checked again when Skip is pressed)."""
    # The Skip button is valid for exactly this question, not for a later one.
    state = set_state(context, "owner_order_id", target=target, amount=amount, nonce=secrets.token_hex(4),
                      skip=skip, refused=refused)
    who = user_html(db_of(context).get_user(target))
    if note:  # the Order ID sent was refused
        text = note + ("\n\nSend another Order ID, or press Cancel." if not skip else
                       f"\n\nSend another Order ID, or press ⏭ Skip to add {money(amount)} to {who} "
                       "without one.")
    else:
        text = (f"🧾 <b>Order ID (optional)</b>\n👤 {who}\n💵 Amount: <b>{money(amount)}</b> (not added yet)"
                "\n\nIf this is for a Binance Pay payment, send its Order ID: then that payment can't be "
                "added again. If it isn't one, press ⏭ Skip.")
    await reply(update, text, _skip_kb(context, state))


@state_handler("owner_order_id")
async def on_owner_order_id(update: Update, context: Ctx, state: dict) -> None:
    if not is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
        clear_state(context)
        return
    text = await need_text(update)
    if text is None:
        return
    order_id = single_order_id(text)
    parts = text.split()
    if order_id is None and len(parts) == 2:  # the amount and the Order ID, either way round
        for amount_text, id_text in ((parts[0], parts[1]), (parts[1], parts[0])):
            amount, found = parse_amount(amount_text), single_order_id(id_text)
            if amount is None or found is None:
                continue
            if amount != state["amount"]:
                await reply(update, f"⚠️ The amount here is <b>{money(state['amount'])}</b>, not "
                                    f"{money(amount)}. Send only the Order ID, or press Cancel and send "
                                    "/addbal again for another amount.", _skip_kb(context, state))
                return
            order_id = found
    if order_id is None:
        await reply(update, f"⚠️ <code>{esc(short(text.strip(), 40))}</code> isn't a Binance Pay Order ID. "
                            "Send only the Order ID (e.g. <code>412345678901234567</code>)"
                            + (f", or press ⏭ Skip to add {money(state['amount'])} without one."
                               if state.get("skip", True) else "."), _skip_kb(context, state))
        return
    clear_state(context)
    await check_order_id(update, context, "addbal", order_id, state["target"], state["amount"], prompt=True)


async def cb_skip_order_id(update: Update, context: Ctx) -> None:
    """⏭ Skip: add the balance asked about without an Order ID."""
    query = update.callback_query
    if not is_owner(context, query.from_user.id):  # type: ignore[union-attr]
        await answer(query, "Only the owner can do that.", show_alert=True)
        return
    nonce = (query.data or "").split(":", 1)[-1]  # type: ignore[union-attr]
    state = get_state(context)
    if not (isinstance(state, dict) and state.get("name") == "owner_order_id" and state.get("nonce") == nonce
            and state.get("skip", True)):
        await answer(query, "This has expired. Before adding again, check the balance with /modifybal.",
                     show_alert=True)
        return
    clear_state(context)  # before adding: a second press finds nothing to add
    refused, db = state.get("refused"), db_of(context)
    if refused and (db.payment_problem([refused], state["target"]) is not None or db.unlinked_payments(refused)
                    or db.waiting_request_for(refused) is not None):
        # Offered while nothing was known about that Order ID; something is now (added, or a
        # request with it): this money may be that payment, so nothing is added.
        await answer(query, "⚠️ Nothing was added: that Order ID is known to the bot now. Send /addbal "
                     "again to see it.", show_alert=True)
        return
    await answer(query)
    try:
        await query.edit_message_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except TelegramError as exc:
        log.info("Could not remove the Skip button: %s", exc)
    await do_add_balance(update, context, state["target"], state["amount"], None, prompt=True)


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


def _not_registrable(context: Ctx, exc: ShopError, order_id: str, target: Optional[int]) -> str:
    """Why this payment can't be registered as added by hand (PaymentUsed, PaymentWaiting)."""
    db = db_of(context)
    if isinstance(exc, PaymentUsed):
        return f"Order ID <code>{esc(order_id)}</code> was used already.\n{payment_record(context, exc.row)}"
    if isinstance(exc, PaymentWaiting):
        text = (f"Order ID <code>{esc(order_id)}</code> is waiting with a request: decide that "
                f"first.\n{payment_record(context, exc.row)}")
        request = exc.request
        alone = [p["order_id"] for p in db.deposit_payments(request["id"]) if p["status"] == "owner"]
        if target is None and alone == [exc.row["order_id"]]:
            text += (f"\nIf it's their payment and you added it by hand, send "
                     f"/regbin {esc(order_id)} {request['user_id']}: that closes the request.")
        return text
    return esc(str(exc))


async def _tell_closed(context: Ctx, closed: Any) -> None:
    """Customers whose waiting request was settled by the owner by hand."""
    db = db_of(context)
    for request_id in closed:
        request = db.get_deposit(request_id)
        if request is not None:
            await notify(context, request["user_id"],
                         f"🧾 Your deposit request #{request_id} ({money(request['amount'])}) is closed: "
                         "the owner added that payment by hand.", markup([[menu_btn()]]))


async def do_add_balance(
    update: Update, context: Ctx, target: int, amount: int, order_id: Optional[str] = None,
    *, prompt: bool = False, payment: Any = None,
) -> bool:
    """Add balance by hand (/addbal). ``payment``: what Binance has for ``order_id``. False
    if nothing was added; with ``prompt`` (from the Add balance prompt) the owner is asked
    for another Order ID."""
    db, cfg = db_of(context), cfg_of(context)
    try:
        done = db.manual_deposit(target, amount, cfg.seller_auto_threshold * 100, order_id, payment)
    except (PaymentUsed, PaymentWaiting) as exc:
        text = "⚠️ Nothing was added: " + _not_registrable(context, exc, order_id or "", target)
        if prompt and get_state(context) is None:  # asked again: another Order ID (no Skip)
            await ask_order_id(update, context, target, amount, note=text, skip=False)
        else:
            await reply(update, text)
        return False
    except ShopError as exc:
        await reply(update, f"⚠️ {esc(exc)}")
        return False
    row, became_seller = done.user, done.became_seller
    text = (
        f"✅ Added <b>{money(amount)}</b> to {user_html(row)}.\n"
        f"💳 New balance: <b>{money(row['balance'])}</b>\n"
        f"📥 Total deposits: {money(row['total_deposits'])}"
    )
    if became_seller:
        text += f"\n⭐ They reached {cfg.seller_auto_threshold}$ in deposits and are now a seller."
    if done.registered is not None:
        text += _registered_text(context, done.registered)
    elif client_of(context) is not None and not prompt:  # the prompt asked for it already
        text += (f"\n\n💡 For a Binance Pay payment, send <code>/regbin ORDER_ID {target}</code> "
                 "too: then it can't be added again automatically.")
    text += _not_started_note(row)
    try:  # added already: the customer is told whatever happens to this reply
        await reply(update, text)
    except TelegramError as exc:
        log.warning("Could not confirm the balance added to %s: %s", target, exc)
    if done.registered is not None:
        await _tell_closed(context, done.registered.closed)
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
    return True


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
        order_id = None
        amount = parse_amount(" ".join(args[1:])) if action == "addbal" else None  # e.g. "10 $"
        if amount is None:
            amount = parse_amount(args[1])
            if action == "addbal" and amount is not None and len(args) > 2:
                order_id = clean_order_id(args[2]) if len(args) == 3 else None
                if order_id is None:
                    await reply(update, f"⚠️ <code>{esc(' '.join(args[2:]))}</code> isn't a Binance Pay "
                                        f"Order ID. Example: /addbal {target} 10 412345678901234567")
                    return
        if amount is None or (action == "addbal" and amount <= 0):
            await reply(update, f"⚠️ <code>{esc(args[1])}</code> isn't a valid amount. Example: /{action} {target} 25")
            return
        if action == "addbal" and order_id:
            await check_order_id(update, context, "addbal", order_id, target, amount)
        elif action == "addbal":
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
    order_id = None
    amount = parse_amount(text)
    if amount is None and state["action"] == "addbal" and len(text.split()) > 1:
        first, rest = text.split(None, 1)  # the amount, then an Order ID
        amount = parse_amount(first)
        if amount is not None:
            order_id = single_order_id(rest)
            if order_id is None:
                await reply(update, f"⚠️ <code>{esc(rest)}</code> isn't a Binance Pay Order ID. Send the "
                                    "amount, then the Order ID, e.g. <code>10 412345678901234567</code>.")
                return
    if amount is None or (state["action"] == "addbal" and amount <= 0):
        await reply(update, "⚠️ Please send an amount like <code>25</code> or <code>25.50</code>.")
        return
    clear_state(context)
    if state["action"] == "addbal":
        if order_id is None and client_of(context) is not None:  # ask for it: it's optional
            await ask_order_id(update, context, state["target"], amount)
        elif order_id is not None:
            await check_order_id(update, context, "addbal", order_id, state["target"], amount, prompt=True)
        else:
            await do_add_balance(update, context, state["target"], amount, prompt=True)
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


# ------------------------------------------------------- Binance payments
REGBIN_HELP = (
    "🧾 <b>Register a Binance payment</b>\n\n"
    "For a Binance Pay payment you added by hand: the bot records it as added, so it can't be "
    "added again, automatically or with a request.\n\n"
    "<code>/regbin ORDER_ID</code>\n"
    "<code>/regbin ORDER_ID USER_ID</code> (whose payment it was)\n\n"
    "Adding the balance and registering the payment in one go: "
    "<code>/addbal USER_ID AMOUNT ORDER_ID</code>"
)


def _registered_text(context: Ctx, done: Any) -> str:
    text = (f"\n🧾 Order ID <code>{esc(done.payment['order_id'])}</code> is registered as added: "
            "it can't be added again.")
    text += "".join(f"\n🧾 Request #{request_id} was waiting with it: closed." for request_id in done.closed)
    return text


async def do_register(update: Update, context: Ctx, order_id: str, user_id: Optional[int],
                      payment: Any = None) -> None:
    """/regbin: record a payment as added by hand (``payment``: what Binance has for it)."""
    db = db_of(context)
    try:
        done = db.register_payment(order_id, user_id, payment)
    except PaymentUsed as exc:
        await reply(update, f"ℹ️ Order ID <code>{esc(order_id)}</code> can't be added again already, "
                            f"nothing changed.\n{payment_record(context, exc.row)}")
        return
    except PaymentWaiting as exc:
        await reply(update, "⚠️ Nothing was registered: " + _not_registrable(context, exc, order_id, user_id))
        return
    except ShopError as exc:
        await reply(update, f"⚠️ {esc(exc)}")
        return
    whose = ""
    if user_id is not None and user_id != cfg_of(context).owner_id:
        whose = f"\n👤 Paid by {user_html(db.get_user(user_id))}"
    try:
        await reply(update, "✅ Done." + whose + _registered_text(context, done))
    except TelegramError as exc:
        log.warning("Could not confirm the registered payment %s: %s", order_id, exc)
    await _tell_closed(context, done.closed)


@owner_command
async def cmd_regbin(update: Update, context: Ctx) -> None:
    """/regbin ORDER_ID [USER_ID]: a payment added by hand can't be added again."""
    args = context.args or []
    if not args or len(args) > 2:
        await reply(update, REGBIN_HELP)
        return
    order_id = clean_order_id(args[0])
    if order_id is None:
        await reply(update, f"⚠️ <code>{esc(args[0])}</code> isn't a Binance Pay Order ID. "
                            "Example: /regbin 412345678901234567")
        return
    user_id = None
    if len(args) == 2:
        user_id = parse_int(args[1])
        if user_id is None or user_id <= 0:
            await reply(update, f"⚠️ <code>{esc(args[1])}</code> isn't a valid user ID. "
                                f"Example: /regbin {order_id} 123456789")
            return
    await check_order_id(update, context, "regbin", order_id, user_id)


# ------------------------------------- an Order ID typed by the owner, checked first
CHECK_VALID_FOR = 600  # seconds a "this payment?" confirmation can be pressed


async def check_order_id(update: Update, context: Ctx, kind: str, order_id: str,
                         target: Optional[int], amount: int = 0, *, prompt: bool = False) -> None:
    """Before a payment the owner names is recorded as added by hand (``kind``: "addbal"
    adds ``amount`` to ``target`` for it, "regbin" only registers it): with automatic
    deposits on, Binance is asked for it first (in the background, like a customer's
    check), so it's recorded under both its IDs and the owner sees what it is (amount,
    time, payer, and why it may have been added already) before confirming. Without a key
    there's nothing to ask. ``prompt``: from the Add balance prompt, which waits meanwhile."""
    if client_of(context) is None:
        await _unchecked(update, context, kind, order_id, target, amount, prompt=prompt)
        return
    nonce = secrets.token_hex(4)
    if prompt:  # the question waits for the answer: Cancel or another command stops it
        set_state(context, "owner_checking", nonce=nonce, target=target, amount=amount)
    try:
        await reply(update, f"🔎 Checking Order ID <code>{esc(order_id)}</code> with Binance ...",
                    cancel_kb() if prompt else None)
    except TelegramError as exc:  # the check still runs: its answer is what counts
        log.info("Could not say an Order ID is being checked: %s", exc)
    # Not Application.create_task, like a payment check: a stop ends it (app.py).
    task = asyncio.get_running_loop().create_task(
        _check_for_owner(update, context, kind, order_id, target, amount, nonce if prompt else None),
        name=f"shopbot:binance:owner:{order_id}")
    tasks = context.bot_data.setdefault("binance_tasks", set())  # keep a reference
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _checking(context: Ctx, nonce: str) -> bool:
    """The Add balance prompt still waits for this check."""
    state = get_state(context)
    return isinstance(state, dict) and state.get("name") == "owner_checking" and state.get("nonce") == nonce


@state_handler("owner_checking")
async def on_owner_checking(update: Update, context: Ctx, state: dict) -> None:
    await reply(update, "🔎 Still checking that Order ID with Binance: one moment, or press Cancel.", cancel_kb())


async def _unchecked(update: Update, context: Ctx, kind: str, order_id: str, target: Optional[int],
                     amount: int, *, prompt: bool) -> None:
    """Without automatic deposits: recorded as typed (only an Order ID of digits is taken)."""
    if kind == "addbal":
        await do_add_balance(update, context, target, amount, order_id, prompt=prompt)  # type: ignore[arg-type]
    else:
        await do_register(update, context, order_id, target)


async def _check_for_owner(update: Update, context: Ctx, kind: str, order_id: str,
                           target: Optional[int], amount: int, nonce: Optional[str]) -> None:
    db, cfg = db_of(context), cfg_of(context)
    verb = "added" if kind == "addbal" else "registered"
    try:
        payment, why, lasting = await find_for_owner(context, order_id)
    except asyncio.CancelledError:  # the bot is stopping
        raise
    except Exception:
        log.exception("Checking an Order ID for the owner failed")
        payment, why, lasting = None, "something went wrong while asking Binance: send it again", False
    if nonce is not None:  # from the prompt: only if it still waits for this answer
        if not _checking(context, nonce):
            return  # cancelled, or the owner moved on (and was told nothing was added)
        clear_state(context)
    try:
        skip = False  # after a refusal, ⏭ Skip could add a payment added already
        ids = [order_id, *((payment.order_id, payment.transaction_id) if payment is not None else ())]
        problem = db.payment_problem(ids, target or cfg.owner_id)  # what the bot knows itself
        if problem is not None:
            text = f"⚠️ Nothing was {verb}: " + _not_registrable(context, problem, order_id, target)
        elif payment is not None:
            await _ask_to_confirm(update, context, kind, order_id, target, amount, payment)
            return
        else:
            text = f"⚠️ Nothing was {verb}: {esc(why)}."
            if lasting and kind == "addbal":
                text += _lasting_advice(context, order_id, target, amount)  # type: ignore[arg-type]
                skip = text.endswith(_WITHOUT)  # without the Order ID, earlier payments are held back
            elif lasting:
                text += "\nSend it again once Binance answers."
        if kind == "addbal" and nonce is not None:  # another Order ID, or Skip if that's safe
            await ask_order_id(update, context, target, amount, note=text, skip=skip,  # type: ignore[arg-type]
                               refused=order_id if skip else None)
        else:
            await reply(update, text)
    except Exception:  # a background task: nobody else would hear of it
        log.exception("Handling an Order ID for the owner failed")
        try:  # it may have been added, or not
            await reply(update, f"⚠️ Something went wrong with Order ID <code>{esc(order_id)}</code>. "
                                + ("Check the balance with /modifybal before you send it again."
                                   if kind == "addbal" else "Please send it again."))
        except TelegramError:
            pass


_WITHOUT = ("Meanwhile the balance can be added without the Order ID: payments made before it are "
            "then held back for you to decide.")


def _lasting_advice(context: Ctx, order_id: str, target: int, amount: int) -> str:
    """While Binance refuses the key: what the bot knows about this Order ID itself, and how
    to add the balance (ends with _WITHOUT only when adding it without the Order ID is safe)."""
    db = db_of(context)
    if not order_id.isdigit():  # the bot may know that payment by its Order ID only
        return ("\nThat's Binance's transaction ID: send the payment's Order ID (the digits in its "
                "details), which the bot can check against the payments it knows.")
    waiting = db.waiting_request_for(order_id)
    if waiting is not None and waiting["user_id"] == target:
        return (f"\nTheir request #{waiting['id']} ({money(waiting['amount'])}) was sent with this Order ID: "
                "if it's that payment, approve the request instead (/pending). That adds the balance and "
                "records the Order ID.")
    if waiting is not None:
        return (f"\nRequest #{waiting['id']} from {user_html(db.get_user(waiting['user_id']))} "
                f"({money(waiting['amount'])}) was sent with this Order ID: decide that one first (/pending).")
    unlinked = db.unlinked_payments(order_id)
    if unlinked:
        return ("\nThe bot knows some payments by Binance's transaction ID only ("
                + ", ".join(f"<code>{esc(row['order_id'])}</code> on {esc(row['created_at'])} UTC"
                            for row in unlinked[:3])
                + (f" and {len(unlinked) - 3} more" if len(unlinked) > 3 else "")
                + "): it may be one of them. Check in your Binance app: if it isn't, add the balance "
                  f"without the Order ID (<code>/addbal {target} {amount / 100:g}</code>); if it is, it was "
                  "added already.")
    return "\n" + _WITHOUT


def _doubt_lines(context: Ctx, payment: Any, target: int) -> tuple[list[str], set[tuple[str, Any]]]:
    """Why a payment about to be added by hand may have been added already (Database
    .hand_doubts), as lines, and what they are (to see if anything new turns up)."""
    db = db_of(context)
    doubts, early = db.hand_doubts(payment, target)
    held = set(db.held_by(db.payment_records(
        *[value for value in (payment.order_id, payment.transaction_id) if value])))
    lines = []
    if doubts:
        lines.append("⚠️ May be added already: after it was paid, " + listed_text(
            [doubt_text(context, row, target, row["id"] if row["id"] in held else None) for row in doubts],
            count=own_count(doubts, target)) + ".")
    if early:
        lines.append("⚠️ May be added already: it was paid before automatic checking started, "
                     "when deposits came with receipts.")
    # Every one, also those only counted ("and N more"): a new one is never confirmed unseen.
    # With its state: one waiting when shown and decided since is new again.
    keys = {("doubt", (row["id"], row["status"])) for row in doubts} | ({("early", 0)} if early else set())
    return lines, keys


async def _ask_to_confirm(update: Update, context: Ctx, kind: str, order_id: str,
                          target: Optional[int], amount: int, payment: Any) -> None:
    db, cfg = db_of(context), cfg_of(context)
    pending = context.user_data.setdefault("payment_checks", {})  # type: ignore[union-attr]
    for key in [key for key, entry in pending.items() if time.monotonic() - entry["at"] > CHECK_VALID_FOR]:
        pending.pop(key, None)
    lines = ["🔎 <b>Binance has this payment</b>", paid_text(payment)]
    warned: set[tuple[str, Any]] = set()
    whom = target if kind == "addbal" else (target if target and target != cfg.owner_id else None)
    user = db.get_user(whom) if whom else None
    if whom and (user is None or not user["started"]):
        lines.append(f"⚠️ User <code>{whom}</code> has never used the bot: is the ID right?")
    if kind == "addbal":
        if payment.currency not in cfg.binance_coins or payment.amount * 100 != amount:
            lines.append(f"⚠️ That's not the <b>{money(amount)}</b> you're adding.")
        doubts, warned = _doubt_lines(context, payment, target)  # type: ignore[arg-type]
        lines += doubts
        lines.append(f"\nAdd <b>{money(amount)}</b> to {_who(user, target)} for this payment?")  # type: ignore[arg-type]
        label = f"⚠️ Add {money(amount)} anyway" if warned else f"✅ Add {money(amount)}"
    else:
        whose = f" for {_who(user, whom)}" if whom else ""
        lines.append(f"\nRegister it as added by hand{whose}? Then it can't be added again.")
        label = "✅ Register"
    nonce = secrets.token_hex(4)
    pending[nonce] = {"kind": kind, "order_id": order_id, "target": target, "amount": amount,
                      "payment": payment, "warned": warned, "at": time.monotonic()}
    await reply(update, "\n".join(lines), markup([[btn(label, f"pyc:{nonce}", GREEN),
                                                   btn("❌ Cancel", f"pyx:{nonce}", RED)]]))


async def cb_payment_confirm(update: Update, context: Ctx) -> None:
    """✅ on "Binance has this payment": add (or register) it, as shown."""
    query = update.callback_query
    if not is_owner(context, query.from_user.id):  # type: ignore[union-attr]
        await answer(query, "Only the owner can do that.", show_alert=True)
        return
    confirm = (query.data or "").startswith("pyc:")  # type: ignore[union-attr]
    nonce = (query.data or "").split(":", 1)[-1]  # type: ignore[union-attr]
    entry = context.user_data.get("payment_checks", {}).pop(nonce, None)  # type: ignore[union-attr]
    if entry is None or time.monotonic() - entry["at"] > CHECK_VALID_FOR:
        await answer(query, "This has expired: send the Order ID again.", show_alert=True)
        return
    await answer(query)
    try:
        await query.edit_message_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except TelegramError as exc:
        log.info("Could not remove the confirm buttons: %s", exc)
    if not confirm:
        await reply(update, "❌ Cancelled: nothing was " + ("added." if entry["kind"] == "addbal" else "registered."))
        return
    if entry["kind"] == "addbal":
        # Nothing new may have turned up since it was shown: checked right before adding,
        # with nothing awaited in between.
        _, now = _doubt_lines(context, entry["payment"], entry["target"])
        if not now <= entry["warned"]:
            payment, db = entry["payment"], db_of(context)
            problem = db.payment_problem([entry["order_id"], payment.order_id, payment.transaction_id],
                                         entry["target"])
            if problem is not None:
                await reply(update, "⚠️ Nothing was added: "
                            + _not_registrable(context, problem, entry["order_id"], entry["target"]))
                return
            await reply(update, "⚠️ Nothing was added: something new may be about this payment, so here "
                                "it is again.")
            await _ask_to_confirm(update, context, entry["kind"], entry["order_id"], entry["target"],
                                  entry["amount"], payment)
            return
        await do_add_balance(update, context, entry["target"], entry["amount"], entry["order_id"],
                             payment=entry["payment"])
    else:
        await do_register(update, context, entry["order_id"], entry["target"], entry["payment"])


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
