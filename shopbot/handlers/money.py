"""💳 Add Balance (deposits), 📊 Earnings and 💸 /withdraw."""

from __future__ import annotations

import logging
import time
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError

from ..db import ShopError
from .common import (
    BLUE,
    GREEN,
    OWNER,
    RED,
    USER,
    Ctx,
    answer,
    btn,
    callback_ints,
    cancel_kb,
    cfg_of,
    clear_state,
    contact_owner,
    db_of,
    esc,
    get_state,
    grid,
    is_owner,
    markup,
    menu_btn,
    money,
    need_text,
    notify,
    parse_amount,
    parse_int,
    role_of,
    set_state,
    show,
    stale_button,
    state_handler,
    try_show,
    user_html,
    user_name,
    utf16_len,
)

log = logging.getLogger("shopbot.money")

MAX_ADMINS_LISTED = 25
NOTE_LINES = 6  # lines of a request's notes shown, each up to NOTE_LINE_CHARS: the message
NOTE_LINE_CHARS = 160  # must stay within Telegram's 4096 characters
LISTED = 3  # deposits named in one warning line, then "and N more"
OWN_LISTED = 10  # or more if they're the customer's own (when the message has room)
NAME_CHARS = 24  # of another customer's name in a warning line
TEXT_LIMIT = 4000  # a request's text, in UTF-16 units: Telegram takes 4096 (after HTML)
CAPTION_LIMIT = 1000  # a receipt's caption: Telegram takes 1024
CONFIRM_FOR = 120.0  # seconds in which a second ✅ Approve approves a request with a warning


# ================================================================ deposits
async def cb_deposit_menu(update: Update, context: Ctx) -> None:
    await answer(update.callback_query)
    await show_deposit_menu(update, context)


async def show_deposit_menu(update: Update, context: Ctx, *, new: bool = False) -> None:
    """Choose an amount, then pay it (see ask_payment)."""
    cfg = cfg_of(context)
    clear_state(context)
    buttons = [btn(f"{amount}$", f"da:{amount}", GREEN) for amount in cfg.deposit_presets]
    rows = grid(buttons, 3)
    rows.append([btn("✏️ Custom amount", "dc", BLUE)])
    rows.append([btn("❌ Cancel", "menu", RED)])
    balance = db_of(context).get_user(update.effective_user.id)["balance"]  # type: ignore[index, union-attr]
    await show(
        update,
        context,
        f"💳 <b>Add Balance</b>\n\n💰 Current balance: <b>{money(balance)}</b>\n\n"
        "Choose how much you want to add:",
        markup(rows),
        new=new,
    )


async def cb_deposit_amount(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        (amount,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    if not 1 <= amount <= cfg_of(context).max_custom_deposit:
        await stale_button(update)
        return
    await answer(query)
    await ask_payment(update, context, amount)


async def cb_deposit_custom(update: Update, context: Ctx) -> None:
    await answer(update.callback_query)
    set_state(context, "deposit_amount")
    await show(
        update,
        context,
        "✏️ Send the amount in $ you want to add.\nWhole numbers only, e.g. <code>25</code>",
        cancel_kb(),
    )


@state_handler("deposit_amount")
async def on_deposit_amount(update: Update, context: Ctx, state: dict) -> None:
    text = await need_text(update)
    if text is None:
        return
    amount = parse_int(text)
    limit = cfg_of(context).max_custom_deposit
    if amount is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "✏️ Please send a whole number only (no decimals), e.g. 25."
        )
        return
    if not 1 <= amount <= limit:
        await update.effective_message.reply_text(f"✏️ The amount must be between 1 and {limit}.")  # type: ignore[union-attr]
        return
    await ask_payment(update, context, amount, new=True)


async def ask_payment(update: Update, context: Ctx, amount: int, *, new: bool = False) -> None:
    """How to pay the chosen amount: with a Binance API key the customer sends the Order
    ID and the bot checks it (autopay.py); otherwise a receipt for the owner."""
    if context.bot_data.get("binance") is not None:
        from .autopay import ask_order_id  # local import: autopay imports this module

        await ask_order_id(update, context, amount, new=new)
        return
    await ask_receipt(update, context, amount, new=new)


async def ask_receipt(update: Update, context: Ctx, amount: int, *, new: bool = False) -> None:
    set_state(context, "deposit_receipt", amount=amount, receipt=None)
    binance_id = esc(cfg_of(context).binance_pay_id)
    await show(
        update,
        context,
        f"💳 <b>Add {amount}$ to your balance</b>\n\n"
        f"1️⃣ Send <b>{amount}$</b> with Binance Pay to Binance ID: <code>{binance_id}</code>\n"
        "2️⃣ Send a <b>photo of the payment receipt</b> here.\n"
        "3️⃣ Press <b>Confirm</b> to send your request to the owner.\n\n"
        "Your balance is added as soon as the owner approves the payment.",
        cancel_kb(),
        new=new,
    )


@state_handler("deposit_receipt")
async def on_deposit_receipt(update: Update, context: Ctx, state: dict) -> None:
    message = update.effective_message
    receipt = None
    if message.photo:  # type: ignore[union-attr]
        receipt = (message.photo[-1].file_id, "photo")  # type: ignore[union-attr]
    elif message.document and (message.document.mime_type or "").startswith("image/"):  # type: ignore[union-attr]
        receipt = (message.document.file_id, "document")  # type: ignore[union-attr]
    if receipt is None:
        await message.reply_text("📸 Please send the payment receipt as a photo.")  # type: ignore[union-attr]
        return
    state["receipt"] = receipt
    amount = state["amount"]
    await message.reply_text(  # type: ignore[union-attr]
        f"🧾 Receipt received for <b>{amount}$</b>.\n"
        "Press <b>Confirm</b> to send your request to the owner.\n"
        "(Sent the wrong photo? Just send the right one.)",
        parse_mode=ParseMode.HTML,
        reply_markup=markup([[btn("✅ Confirm", "dsend", GREEN), btn("❌ Cancel", "cancel", RED)]]),
    )


def deposit_caption(context: Ctx, deposit: Any) -> str:
    db = db_of(context)
    user = db.get_user(deposit["user_id"])
    order = deposit["order_id"]
    found = "".join(
        f"🔎 Binance has: <b>{esc(p['raw_amount'])} {esc(p['currency'])}</b>, Order ID "
        f"<code>{esc(p['order_id'])}</code>, paid {esc(p['paid_at'])} UTC\n"
        for p in db.deposit_payments(deposit["id"]) if p["currency"]
    )
    lines = [line for line in (deposit["note"] or "").splitlines() if line]
    if len(lines) > NOTE_LINES:
        lines = lines[:NOTE_LINES - 1] + [f"... and {len(lines) - NOTE_LINES + 1} more"]
    notes = "".join(f"• {esc(_short(line, NOTE_LINE_CHARS))}\n" for line in lines)
    head = (
        f"💳 <b>Deposit request #{deposit['id']}</b>\n"
        f"👤 {user_html(user)}\n"
        f"💵 Amount: <b>{money(deposit['amount'])}</b>\n"
        + (f"🧾 Binance Order ID: <code>{esc(order)}</code>\n" if order else "")
        + found
        + (f"⚠️ Not added automatically:\n{notes}" if notes else "")
    )
    tail = (f"💰 Current balance: {money(user['balance'])}\n"  # type: ignore[index]
            f"📥 Total deposits: {money(user['total_deposits'])}\n"  # type: ignore[index]
            f"🕒 {deposit['created_at']} UTC")
    limit = CAPTION_LIMIT if deposit["receipt_file_id"] else TEXT_LIMIT
    warnings = _request_warnings(context, deposit).splitlines(keepends=True)
    if utf16_len(head + "".join(warnings) + tail) > limit:  # fewer of the customer's own
        warnings = _request_warnings(context, deposit, compact=True).splitlines(keepends=True)
    cut = False
    while warnings and utf16_len(head + "".join(warnings) + tail) > limit:
        warnings.pop()  # never more than Telegram takes, or the owner gets nothing
        cut = True
    return head + "".join(warnings) + ("⚠️ (more warnings: too long to show)\n" if cut else "") + tail


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def listed_text(parts: list[str], sep: str = "; ", count: int = LISTED) -> str:
    """The first ``count`` parts (LISTED, or more), then "and N more"."""
    count = max(count, LISTED)
    shown = sep.join(parts[:count])
    return shown + (f"{sep}and {len(parts) - count} more" if len(parts) > count else "")


def own_count(rows: list[Any], user_id: int) -> int:
    """How many of these deposits are the customer's own (up to OWN_LISTED): those are all
    shown (they come first), as the one that may be this very payment is most likely among
    them; other customers' are cut to LISTED."""
    return min(OWN_LISTED, sum(1 for row in rows if row["user_id"] == user_id))


def _whose(context: Ctx, row: Any, user_id: int) -> str:
    if row["user_id"] == user_id:
        return ""
    return f" for {esc(_short(user_name(db_of(context).get_user(row['user_id'])), NAME_CHARS))}"


def doubt_text(context: Ctx, row: Any, user_id: int, held_before: Any = None) -> str:
    """A deposit that may be about a payment (see Database.request_warnings), as it is now."""
    whose = _whose(context, row, user_id)
    if row["method"] == "raise":
        return f"you raised a balance by {money(row['amount'])} with /modifybal{whose} ({row['created_at']} UTC)"
    if row["receipt_file_id"]:
        state = {"approved": "you approved it", "rejected": "you rejected it"}.get(row["status"], "it's waiting")
        return f"a receipt for {money(row['amount'])} came{whose} (request #{row['id']}, {state})"
    if row["id"] == held_before or row["method"] == "binance" and not row["unverified"]:
        verb = {"approved": "you approved", "rejected": "you rejected"}.get(row["status"], "was closed")
        return f"it was in request #{row['id']}{whose}, which {verb}"
    if row["method"] == "manual":
        return f"you added {money(row['amount'])} by hand{whose} ({row['created_at']} UTC)"
    if row["status"] == "pending":
        return f"request #{row['id']}{whose} is waiting, with an Order ID Binance didn't confirm"
    if row["status"] == "closed":  # its Order ID was added another way (by hand, or with a request)
        return f"request #{row['id']}{whose} was closed, with an Order ID Binance didn't confirm"
    return f"you {row['status']} request #{row['id']}{whose}, whose Order ID Binance didn't confirm"


def _added(context: Ctx, row: Any, user_id: int) -> str:
    """A deposit added since a request was made (see Database.request_warnings)."""
    whose = _whose(context, row, user_id)
    if row["method"] == "manual":
        return f"{money(row['amount'])} by hand{whose} ({row['created_at']} UTC)"
    if row["method"] == "raise":
        return f"{money(row['amount'])} raised with /modifybal{whose} ({row['created_at']} UTC)"
    if row["method"] == "binance_auto":
        return (f"{money(row['amount'])} automatically{whose}, Order ID "
                f"<code>{esc(row['order_id'] or '')}</code> ({row['decided_at']} UTC)")
    if row["receipt_file_id"]:
        return f"{money(row['amount'])}{whose} with a receipt, request #{row['id']} ({row['decided_at']} UTC)"
    paid = [p for p in db_of(context).deposit_payments(row["id"]) if p["status"] == "credited" and p["currency"]]
    payment = (f", Order ID <code>{esc(paid[0]['order_id'])}</code> of {esc(paid[0]['raw_amount'])} "
               f"{esc(paid[0]['currency'])}" if paid else "")
    return f"{money(row['amount'])}{whose} with request #{row['id']}{payment} ({row['decided_at']} UTC)"


def _used(context: Ctx, row: Any, user_id: int) -> str:
    """How a payment a request names was added already (see Database.request_used)."""
    added = db_of(context).get_deposit(row["deposit_id"]) if row["deposit_id"] else None
    if added is not None:
        return _added(context, added, user_id)
    whose = "" if row["user_id"] == cfg_of(context).owner_id else _whose(context, row, user_id)
    return f"you registered it as added by hand{whose} ({row['created_at']} UTC)"


def _request_warnings(context: Ctx, deposit: Any, compact: bool = False) -> str:
    """What the owner should know before approving a Binance request, as it is now.
    ``compact``: LISTED deposits a line, also of the customer's own (to fit a caption)."""
    text = ""
    held, since, elsewhere = db_of(context).request_warnings(deposit["id"])
    left = db_of(context).still_unconfirmed(deposit) if deposit["unverified"] else []
    if left:  # one Binance has, waiting with another request, gets its own line below
        left = [value for value in left if value not in {order_id for order_id, _ in elsewhere}]
    if left is None or left:
        ids = [f"<code>{esc(value)}</code>" for value in left or []]
        text += (f"❗ Binance didn't confirm Order ID {listed_text(ids, ', ')} as a new payment to you"
                 if ids else "❗ Binance didn't confirm this request's Order ID as a new payment to you")
        text += (". Approve only if you find the payment in your Binance app and haven't added it "
                 "already (each automatic deposit sent you a 💳 message).\n")
    if deposit["status"] != "pending":
        return text
    block = db_of(context).approval_block(deposit["id"])
    if block:
        text = f"⛔ <b>Can't be approved:</b> {esc(block)}\n" + text
    kept = [p for p in db_of(context).deposit_payments(deposit["id"]) if p["status"] == "owner"]
    if not block and len(kept) > 1:
        text += (f"ℹ️ ✅ Approve adds {money(deposit['amount'])} and marks all {len(kept)} payments it "
                 "shows as added.\n")
    for row in db_of(context).request_used(deposit["id"]):
        text += (f"⚠️ Order ID <code>{esc(row['order_id'])}</code> was added already: "
                 f"{_used(context, row, deposit['user_id'])}.\n")
    for order_id, rows in db_of(context).request_unlinked(deposit["id"]):
        recorded = listed_text([f"<code>{esc(row['order_id'])}</code> ({row['created_at']} UTC)"
                                for row in rows], ", ")
        text += (f"⚠️ May be added already: an older version recorded payments by Binance's transaction "
                 f"ID only ({recorded}). If one of them has Order ID <code>{esc(order_id)}</code> in your "
                 "Binance app, it was added already.\n")
    for order_id, other in elsewhere:
        text += (f"⚠️ Binance has Order ID <code>{esc(order_id)}</code> as a payment kept for request "
                 f"#{other['id']}{_whose(context, other, deposit['user_id'])}, which is waiting: this "
                 "request was sent with it too.\n")
    for payment, doubts, early in held:
        if early:
            text += (f"⚠️ May be added already: <code>{esc(payment['order_id'])}</code> was paid "
                     "before automatic checking started, when deposits came with receipts.\n")
        if doubts:
            holders = {payment["held_before"], payment["approved_in"], payment["deposit_id"]} - {deposit["id"], None}
            text += (f"⚠️ May be added already: after <code>{esc(payment['order_id'])}</code> was paid, "
                     + listed_text([doubt_text(context, row, deposit["user_id"],
                                               row["id"] if row["id"] in holders else None)
                                    for row in doubts],
                                   count=LISTED if compact else own_count(doubts, deposit["user_id"])) + ".\n")
    if since:  # what's shown is worked out only for those listed (a week of anyone's)
        count = LISTED if compact else max(LISTED, own_count(since, deposit["user_id"]))
        parts = [_added(context, row, deposit["user_id"]) for row in since[:count]]
        more = f"; and {len(since) - count} more" if len(since) > count else ""
        start = db_of(context).unverified_start(deposit)
        text += f"⚠️ Added since {start} UTC: " + "; ".join(parts) + more + ".\n"
    return text


async def cb_deposit_send(update: Update, context: Ctx) -> None:
    query = update.callback_query
    state = get_state(context)
    if state is None or state.get("name") != "deposit_receipt" or not state.get("receipt"):
        await answer(
            query, "This request has expired. Start again from 💳 Add Balance.", show_alert=True
        )
        return
    db = db_of(context)
    file_id, kind = state["receipt"]
    deposit = db.create_deposit(query.from_user.id, state["amount"] * 100, file_id, kind)
    # Saved: from here on Telegram errors are best-effort, and the owner hears first.
    clear_state(context)
    await answer(query, "✅ Sent")
    reached = await send_deposit_to_owner(context, deposit)
    text = (
        f"✅ <b>Request sent!</b>\n\n🧾 Deposit #{deposit['id']} · {money(deposit['amount'])}\n"
        "You'll get a message here as soon as the owner reviews it."
    )
    if not reached:
        text += "\n\nℹ️ The owner couldn't be notified right now, but your request is saved and waiting."
    await try_show(update, context, text, markup([[menu_btn()]]))


def _decision_keyboard(prefix_ok: str, prefix_no: str, request_id: int) -> Any:
    return markup([[
        btn("✅ Approve", f"{prefix_ok}:{request_id}", GREEN),
        btn("❌ Reject", f"{prefix_no}:{request_id}", RED),
    ]])


async def send_deposit_to_owner(context: Ctx, deposit: Any) -> bool:
    """The receipt with Approve/Reject buttons. False if the owner couldn't be reached."""
    cfg = cfg_of(context)
    keyboard = _decision_keyboard("dok", "dno", deposit["id"])
    caption = deposit_caption(context, deposit)
    file_id, kind = deposit["receipt_file_id"], deposit["receipt_kind"]
    if file_id:
        try:
            if kind == "photo":
                await context.bot.send_photo(
                    cfg.owner_id, file_id, caption=caption, parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            else:
                await context.bot.send_document(
                    cfg.owner_id, file_id, caption=caption, parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            return True
        except TelegramError as exc:
            log.warning("Could not send deposit #%s to the owner: %s", deposit["id"], exc)
    if not file_id and (deposit["order_id"] or deposit["note"]):  # checked with Binance: no receipt
        return await notify(context, cfg.owner_id, caption, keyboard)
    return await notify(
        context, cfg.owner_id, caption + "\n\n⚠️ The receipt couldn't be attached.", keyboard
    )


async def send_withdrawal_to_owner(context: Ctx, withdrawal: Any) -> bool:
    """The request with Approve/Reject buttons. False if the owner couldn't be reached."""
    return await notify(
        context,
        cfg_of(context).owner_id,
        withdrawal_text(context, withdrawal),
        _decision_keyboard("wok", "wno", withdrawal["id"]),
    )


async def _edit_request(update: Update, text: str, keyboard: Any = None) -> None:
    """Replace a request message's caption/text and its buttons (none by default;
    best-effort)."""
    query = update.callback_query
    message = query.message  # type: ignore[union-attr]
    try:
        if message is not None and getattr(message, "text", None) is None:
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)  # type: ignore[union-attr]
        else:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)  # type: ignore[union-attr]
    except TelegramError as exc:
        log.info("Could not edit request message: %s", exc)


async def cb_deposit_decide(update: Update, context: Ctx) -> None:
    query = update.callback_query
    approve = query.data.startswith(("dok:", "dokx:"))
    try:
        (deposit_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    if not is_owner(context, query.from_user.id):
        await answer(query, "Only the owner can do that.", show_alert=True)
        return
    db, cfg = db_of(context), cfg_of(context)
    if approve and db.approval_block(deposit_id):  # it would add a payment added already
        await answer(query, "⛔ This request can't be approved: its ⛔ line says why.", show_alert=True)
        deposit = db.get_deposit(deposit_id)
        if deposit is not None:  # ✅ stays: it checks again, and approves once nothing blocks it
            await _edit_request(update, deposit_caption(context, deposit),
                                markup([[btn("✅ Approve", f"dok:{deposit_id}", GREEN),
                                         btn("❌ Reject", f"dno:{deposit_id}", RED)]]))
        return
    warned = context.bot_data.setdefault("approve_warned", {})  # request id -> (when, what)
    shown = db.approval_warnings(deposit_id) if approve else set()
    if shown:
        # ✅ Approve never approves with a warning (a double tap must not confirm it), and
        # "Approve anyway" only what was shown (each warning, of its kind), not long ago:
        # anything new is shown first.
        at, seen = warned.get(deposit_id, (None, set()))
        if not (query.data.startswith("dokx:") and at is not None
                and time.monotonic() - at <= CONFIRM_FOR and shown <= seen):
            warned[deposit_id] = (time.monotonic(), shown)
            await answer(query, "⚠️ This may have been added already: read the ⚠️ lines of the "
                         "request, then press ⚠️ Approve anyway if you're sure.", show_alert=True)
            await _edit_request(update, deposit_caption(context, db.get_deposit(deposit_id)), markup([[
                btn("⚠️ Approve anyway", f"dokx:{deposit_id}", GREEN),
                btn("❌ Reject", f"dno:{deposit_id}", RED)]]))
            return
    warned.pop(deposit_id, None)
    try:
        decision = db.decide_deposit(deposit_id, approve, cfg.seller_auto_threshold * 100)
    except ShopError as exc:
        await answer(query, str(exc), show_alert=True)
        return
    deposit, user = decision.deposit, decision.user
    await answer(query, "✅ Approved" if approve else "❌ Rejected")
    if approve:
        status = f"✅ <b>Approved</b> — new balance {money(user['balance'])}"
        if decision.became_seller:
            status += "\n⭐ Reached the seller threshold: now a Seller."
        status += "".join(f"\n🧾 Request #{request_id} was waiting with this payment: closed."
                          for request_id in decision.closed)
    else:
        status = "❌ <b>Rejected</b>"
    await _edit_request(update, deposit_caption(context, deposit) + "\n\n" + status)
    if approve:
        text = (
            f"✅ <b>Deposit approved!</b>\n\n💵 +{money(deposit['amount'])}\n"
            f"💰 New balance: <b>{money(user['balance'])}</b>"
        )
        if decision.became_seller:
            text += (
                f"\n\n⭐ Congratulations! Your total deposits reached "
                f"{cfg.seller_auto_threshold}$, so you're now a <b>Seller</b> with "
                f"<b>{cfg.seller_discount_percent}% off</b> all products."
            )
    else:
        text = (
            f"❌ Your deposit request #{deposit['id']} of {money(deposit['amount'])} was "
            f"rejected.\nIf you think this is a mistake, please {contact_owner(context)}."
        )
    await notify(context, deposit["user_id"], text, markup([[menu_btn()]]))


# ================================================================ earnings
async def cb_earnings(update: Update, context: Ctx) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    db = db_of(context)
    role = role_of(context, user_id)
    row = db.get_user(user_id)
    if role == USER and not (row and row["earnings"]):
        await answer(query, "Earnings are for the owner and admins.", show_alert=True)
        return
    await answer(query)
    rows = [[btn("💸 Withdraw", "ew", GREEN)], [menu_btn()]]
    if role == OWNER:
        text = owner_earnings_text(context)
        pending = db.count_pending_requests()
        if pending:
            rows.insert(1, [btn(f"📥 Pending requests ({pending})", "pq", BLUE)])
    else:
        text = own_earnings_text(context, user_id)
    await show(update, context, text, markup(rows))


def owner_earnings_text(context: Ctx) -> str:
    db = db_of(context)
    stats = db.all_seller_stats()
    me, others = stats[0], stats[1:]
    totals = db.totals()
    lines = [
        "📊 <b>Earnings</b>",
        "",
        "👑 <b>You (Owner)</b>",
        f"• Earned: <b>{money(me['earned'])}</b> ({me['sold']} sold)",
        f"• Withdrawn: {money(me['withdrawn'])}",
        f"• Available: <b>{money(me['available'])}</b>",
        "",
        "👮 <b>Admins</b>",
    ]
    if not others:
        lines.append("• No admins yet. Add one with /addadmin ID")
    for stat in others[:MAX_ADMINS_LISTED]:
        user = stat["user"]
        tag = "" if user["is_admin"] else " (not admin anymore)"
        line = (
            f"• <b>{esc(user_name(user))}</b> <code>{user['id']}</code>{tag}\n"
            f"   Earned {money(stat['earned'])} ({stat['sold']} sold) · "
            f"Available {money(stat['available'])}"
        )
        if stat["pending"]:
            line += f" · Pending withdrawal {money(stat['pending'])}"
        lines.append(line)
    if len(others) > MAX_ADMINS_LISTED:
        lines.append(f"• …and {len(others) - MAX_ADMINS_LISTED} more")
    lines += [
        "",
        "🌐 <b>Whole shop</b>",
        f"• Total earnings: <b>{money(totals['revenue'])}</b>",
        f"• Total purchases: <b>{totals['orders']}</b> orders · {totals['items_sold']} products",
        f"• Approved deposits: {money(totals['deposits'])}",
        f"• Users: {totals['users']} · Balances held: {money(totals['balances'])}",
        f"• Products in stock: {totals['stock']}",
    ]
    return "\n".join(lines)


def own_earnings_text(context: Ctx, user_id: int) -> str:
    stat = db_of(context).seller_stats(user_id)
    return "\n".join([
        "📊 <b>Your Earnings</b>",
        "",
        f"• Earned: <b>{money(stat['earned'])}</b> ({stat['sold']} sold)",
        f"• Withdrawn: {money(stat['withdrawn'])}",
        f"• Waiting for approval: {money(stat['pending'])}",
        f"• Available: <b>{money(stat['available'])}</b>",
        f"• Your products in stock: {stat['stock']}",
        "",
        "Withdraw with /withdraw or the button below.",
    ])


# =============================================================== withdrawals
async def cmd_withdraw(update: Update, context: Ctx) -> None:
    await start_withdraw(update, context)


async def cb_withdraw(update: Update, context: Ctx) -> None:
    await answer(update.callback_query)
    await start_withdraw(update, context)


async def start_withdraw(update: Update, context: Ctx) -> None:
    user_id = update.effective_user.id  # type: ignore[union-attr]
    db = db_of(context)
    row = db.get_user(user_id)
    earnings = row["earnings"] if row else 0
    role = role_of(context, user_id)
    if role == USER and earnings <= 0:
        await show(update, context,
                   "💸 /withdraw is for the owner and admins — it withdraws money earned from "
                   "selling products.", markup([[menu_btn()]]))
        return
    if earnings <= 0:
        await show(update, context,
                   "💸 You have no earnings to withdraw right now.\nAvailable: <b>0.00$</b>",
                   markup([[menu_btn()]]))
        return
    set_state(context, "withdraw_amount")
    await show(
        update,
        context,
        f"💸 <b>Withdraw earnings</b>\n\nAvailable: <b>{money(earnings)}</b>\n\n"
        "Send the amount you want to withdraw (e.g. <code>25</code> or <code>25.50</code>), "
        "or tap the button below.",
        markup([
            [btn(f"💸 Withdraw all ({money(earnings)})", "wall", GREEN)],
            [btn("❌ Cancel", "cancel", RED)],
        ]),
    )


async def cb_withdraw_all(update: Update, context: Ctx) -> None:
    query = update.callback_query
    state = get_state(context)
    if state is None or state.get("name") != "withdraw_amount":
        await answer(query, "This request has expired. Send /withdraw again.", show_alert=True)
        return
    earnings = db_of(context).get_user(query.from_user.id)["earnings"]  # type: ignore[index]
    if earnings <= 0:
        clear_state(context)
        await answer(query, "You have no earnings to withdraw.", show_alert=True)
        return
    await answer(query)
    await ask_binance_id(update, context, earnings)


@state_handler("withdraw_amount")
async def on_withdraw_amount(update: Update, context: Ctx, state: dict) -> None:
    text = await need_text(update)
    if text is None:
        return
    amount = parse_amount(text)
    earnings = db_of(context).get_user(update.effective_user.id)["earnings"]  # type: ignore[union-attr, index]
    if amount is None or amount <= 0:
        await update.effective_message.reply_text("💸 Please send an amount like 25 or 25.50.")  # type: ignore[union-attr]
        return
    if amount > earnings:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"💸 You only have {money(earnings)} available. Send a smaller amount."
        )
        return
    await ask_binance_id(update, context, amount, new=True)


async def ask_binance_id(update: Update, context: Ctx, amount: int, *, new: bool = False) -> None:
    set_state(context, "withdraw_binance", amount=amount)
    await show(
        update,
        context,
        f"🏦 Send your <b>Binance ID</b> to receive <b>{money(amount)}</b>.",
        cancel_kb(),
        new=new,
    )


@state_handler("withdraw_binance")
async def on_withdraw_binance(update: Update, context: Ctx, state: dict) -> None:
    text = await need_text(update)
    if text is None:
        return
    binance_id = " ".join(text.split())
    if not binance_id or len(binance_id) > 64:
        await update.effective_message.reply_text("🏦 Please send a valid Binance ID.")  # type: ignore[union-attr]
        return
    user_id = update.effective_user.id  # type: ignore[union-attr]
    db = db_of(context)
    owner = is_owner(context, user_id)
    try:
        withdrawal = db.create_withdrawal(user_id, state["amount"], binance_id, auto_approve=owner)
    except ShopError as exc:
        clear_state(context)
        await update.effective_message.reply_text(f"⚠️ {exc}")  # type: ignore[union-attr]
        return
    # Saved (the earnings are reserved): Telegram errors from here on are best-effort,
    # and the owner hears first so the request can always be decided.
    clear_state(context)
    left = db.get_user(user_id)["earnings"]  # type: ignore[index]
    if owner:
        await try_show(
            update,
            context,
            f"✅ <b>Withdrawal recorded</b>\n\n💵 {money(withdrawal['amount'])}\n"
            f"🏦 Binance ID: <code>{esc(binance_id)}</code>\n💼 Earnings left: {money(left)}",
            markup([[menu_btn()]]),
            new=True,
        )
        return
    reached = await send_withdrawal_to_owner(context, withdrawal)
    text = (
        f"✅ <b>Withdrawal request sent</b>\n\n💵 {money(withdrawal['amount'])}\n"
        f"🏦 Binance ID: <code>{esc(binance_id)}</code>\n💼 Earnings left: {money(left)}\n\n"
        "The owner will review it. If it's rejected, the money goes back to your earnings."
    )
    if not reached:
        text += "\n\nℹ️ The owner couldn't be notified right now, but your request is saved and waiting."
    await try_show(update, context, text, markup([[menu_btn()]]), new=True)


def withdrawal_text(context: Ctx, withdrawal: Any) -> str:
    user = db_of(context).get_user(withdrawal["user_id"])
    return (
        f"💸 <b>Withdrawal request #{withdrawal['id']}</b>\n"
        f"👤 {user_html(user)}\n"
        f"💵 Amount: <b>{money(withdrawal['amount'])}</b>\n"
        f"🏦 Binance ID: <code>{esc(withdrawal['binance_id'])}</code>\n"
        f"💼 Earnings left: {money(user['earnings'])}\n"  # type: ignore[index]
        f"🕒 {withdrawal['created_at']} UTC"
    )


async def cb_withdraw_decide(update: Update, context: Ctx) -> None:
    query = update.callback_query
    approve = query.data.startswith("wok:")
    try:
        (withdrawal_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    if not is_owner(context, query.from_user.id):
        await answer(query, "Only the owner can do that.", show_alert=True)
        return
    db = db_of(context)
    try:
        withdrawal = db.decide_withdrawal(withdrawal_id, approve)
    except ShopError as exc:
        await answer(query, str(exc), show_alert=True)
        return
    await answer(query, "✅ Approved" if approve else "❌ Rejected")
    status = (
        "✅ <b>Approved</b> — remember to send the payment."
        if approve
        else "❌ <b>Rejected</b> — the amount went back to their earnings."
    )
    await _edit_request(update, withdrawal_text(context, withdrawal) + "\n\n" + status)
    if approve:
        text = (
            f"✅ <b>Withdrawal approved!</b>\n\n💵 {money(withdrawal['amount'])} is being sent "
            f"to Binance ID <code>{esc(withdrawal['binance_id'])}</code>."
        )
    else:
        left = db.get_user(withdrawal["user_id"])["earnings"]  # type: ignore[index]
        text = (
            f"❌ Your withdrawal request of {money(withdrawal['amount'])} was rejected.\n"
            f"The amount was returned to your earnings (available: {money(left)})."
        )
    await notify(context, withdrawal["user_id"], text, markup([[menu_btn()]]))


# ========================================================== pending requests
async def cmd_pending(update: Update, context: Ctx) -> None:
    if not is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
        await update.effective_message.reply_text("⛔ Only the owner can use this command.")  # type: ignore[union-attr]
        return
    await send_pending(context)


async def cb_pending(update: Update, context: Ctx) -> None:
    query = update.callback_query
    if not is_owner(context, query.from_user.id):  # type: ignore[union-attr]
        await answer(query, "Only the owner can do that.", show_alert=True)
        return
    await answer(query)
    await send_pending(context)


async def send_pending(context: Ctx) -> None:
    """Send every pending deposit and withdrawal again, each with Approve/Reject."""
    db, cfg = db_of(context), cfg_of(context)
    deposits, withdrawals = db.pending_deposits(), db.pending_withdrawals()
    total = db.count_pending_requests()
    if not total:
        await notify(context, cfg.owner_id, "✅ No deposits or withdrawals are waiting for you.",
                     markup([[menu_btn()]]))
        return
    head = (
        f"📥 <b>Pending requests: {total}</b>\n"
        f"💳 Deposits: {len(deposits)} · 💸 Withdrawals: {len(withdrawals)}\n"
        "Each one follows with its Approve / Reject buttons."
    )
    shown = len(deposits) + len(withdrawals)
    if shown < total:
        head += f"\nShowing the oldest {shown}; send /pending again after deciding these."
    await notify(context, cfg.owner_id, head)
    for deposit in deposits:
        await send_deposit_to_owner(context, deposit)
    for withdrawal in withdrawals:
        await send_withdrawal_to_owner(context, withdrawal)
