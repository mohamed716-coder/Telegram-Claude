"""🛍 Shop: browse labels, buy products, get them delivered, 🧾 My Orders."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from ..content import send_content
from ..db import InsufficientBalance, PriceChanged, PurchaseResult, ShopError, seller_price
from ..ratelimit import FloodWait, wait_seconds
from .common import (
    BLUE,
    GREEN,
    RED,
    Ctx,
    answer,
    btn,
    callback_ints,
    cfg_of,
    clear_state,
    contact_owner,
    db_of,
    esc,
    grid,
    hidden_note,
    is_owner,
    markup,
    menu_btn,
    money,
    need_text,
    notify,
    off_names,
    page_count,
    pager,
    parse_int,
    set_state,
    show,
    short,
    stale_button,
    state_handler,
    try_show,
)

log = logging.getLogger("shopbot.shop")

PAGE_SIZE = 20
QUICK_QUANTITIES = (1, 2, 3, 5, 10)
MAX_QUANTITY = 200
ORDERS_PAGE_SIZE = 8
CLOSED_TEXT = "🔒 <b>The shop is closed right now.</b>\nPlease check back later."
UNAVAILABLE_TEXT = "⚠️ This isn't available right now."

# Telegram allows about one message per second in a chat, with short bursts. Products
# are sent in the background: the first few at once, then one every DELIVERY_INTERVAL
# seconds, so a big order neither hits flood limits nor holds up other customers.
DELIVERY_BURST = 5
DELIVERY_INTERVAL = 1.0
# A flood wait up to this long (seconds) is sat out once before a product counts as
# not sent. It only pauses this delivery, not the rest of the bot.
MAX_DELIVERY_WAIT = 60
# When the bot is stopping, deliveries go on for at most this long (seconds); the rest
# count as not sent (the buyer gets them from 🧾 My Orders), so a restart stays well
# inside the service manager's stop timeout (systemd TimeoutStopSec=300).
STOP_GRACE = 120


def shop_open_for(context: Ctx, user_id: int) -> bool:
    """The owner can always browse; everyone else only while the shop is open."""
    return db_of(context).shop_is_open() or is_owner(context, user_id)


def unit_price_for(context: Ctx, user_id: int, price: int) -> int:
    row = db_of(context).get_user(user_id)
    if row is not None and row["is_seller"]:
        return seller_price(price, cfg_of(context).seller_discount_percent)
    return price


def hidden_for(context: Ctx, user_id: int, label_id: int) -> bool:
    """True if this customer must not see the label: it, or a label above it, is
    disabled. The owner sees everything (marked as disabled)."""
    return not is_owner(context, user_id) and bool(db_of(context).disabled_on_path(label_id))


def disabled_note(context: Ctx, label_id: int) -> str:
    """For the owner: why customers don't see this, or "" if a disabled label isn't
    the reason."""
    return hidden_note(db_of(context), label_id)


def buyable(context: Ctx, label_id: int) -> bool:
    """A product customers can buy now (stock aside): it exists, is the last label,
    has a price, and neither it nor a label above it is disabled."""
    db = db_of(context)
    label = db.get_label(label_id)
    return (label is not None and label["price"] is not None
            and not db.has_children(label_id) and not db.disabled_on_path(label_id))


def visible_children(context: Ctx, parent_id: Optional[int], user_id: int) -> list[Any]:
    """Labels shown in the shop. Customers don't see disabled labels (nor anything
    inside them), nor products without a price."""
    db = db_of(context)
    children = db.children(parent_id)
    if is_owner(context, user_id):
        return children
    visible = []
    for child in children:
        if child["disabled"]:
            continue
        if db.has_children(child["id"]):
            if db.has_priced_product(child["id"]):
                visible.append(child)
        elif child["price"] is not None:
            visible.append(child)
    return visible


# --------------------------------------------------------------------- browse
async def cb_shop(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        label_id, page = callback_ints(query.data, 2)
    except ValueError:
        await stale_button(update)
        return
    await answer(query)
    if not shop_open_for(context, query.from_user.id):
        await show(update, context, CLOSED_TEXT, markup([[menu_btn()]]))
        return
    await render_shop(update, context, label_id, page)


async def render_shop(
    update: Update, context: Ctx, label_id: int, page: int = 0, *, new: bool = False, note: str = ""
) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    label = None
    if label_id:
        label = db.get_label(label_id)
        if label is None:
            # Customers get the same answer as for a disabled label, so they can't tell.
            gone = "⚠️ This category no longer exists." if is_owner(context, user_id) else UNAVAILABLE_TEXT
            await show(update, context, gone,
                       markup([[btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()]]), new=new)
            return
        if hidden_for(context, user_id, label_id):
            await show(update, context, UNAVAILABLE_TEXT,
                       markup([[btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()]]), new=new)
            return
        if not db.has_children(label_id):
            await render_product(update, context, label_id, new=new, note=note)
            return

    children = visible_children(context, label_id or None, user_id)
    pages = page_count(len(children), PAGE_SIZE)
    page = min(page, pages - 1)
    chunk = children[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [note, ""] if note else []
    lines.append("🛍 <b>Shop</b>")
    if label is not None:
        lines.append(f"📂 {esc(db.path_text(label_id))}")
    if is_owner(context, user_id) and not db.shop_is_open():
        lines.append("🔒 <i>The shop is closed for customers (/open to open it).</i>")
    if label is not None and is_owner(context, user_id) and (note_off := disabled_note(context, label_id)):
        lines.append(note_off)
    lines.append("")
    if not children:
        lines.append("Nothing here yet." if label else "The shop is empty right now. Check back soon!")
    else:
        lines.append("Choose a category or product:")

    categories, products = [], []
    for child in chunk:
        off = "🚫 " if child["disabled"] else ""  # only the owner sees disabled labels
        if db.has_children(child["id"]):
            categories.append(btn(f"{off}📂 {child['name']}", f"sh:{child['id']}:0"))
        elif child["price"] is None:
            products.append([btn(f"{off}{child['name']} · ⚠️ no price", f"sh:{child['id']}:0")])
        else:
            unit = unit_price_for(context, user_id, child["price"])
            sold_out = "" if db.count_items(child["id"]) else " · sold out"
            products.append(
                [btn(f"{off}🛒 {child['name']} · {money(unit)}{sold_out}", f"sh:{child['id']}:0")]
            )
    rows = grid(categories, 2) + products
    rows.append(pager(f"sh:{label_id}:", page, pages))
    back = f"sh:{label['parent_id'] or 0}:0" if label is not None else "menu"
    rows.append([btn("⬅️ Back", back), menu_btn()])
    await show(update, context, "\n".join(lines), markup(rows), new=new)


async def render_product(
    update: Update, context: Ctx, label_id: int, *, new: bool = False, note: str = ""
) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    label = db.get_label(label_id)
    if label is None:
        gone = "⚠️ This product no longer exists." if is_owner(context, user_id) else UNAVAILABLE_TEXT
        await show(update, context, gone,
                   markup([[btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()]]), new=new)
        return
    if hidden_for(context, user_id, label_id):
        await show(update, context, UNAVAILABLE_TEXT,
                   markup([[btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()]]), new=new)
        return
    back = btn("⬅️ Back", f"sh:{label['parent_id'] or 0}:0")
    lines = [note, ""] if note else []
    lines += [f"🛒 <b>{esc(label['name'])}</b>", f"📂 {esc(db.path_text(label_id))}", ""]
    rows: list[list[Any]] = []
    off = disabled_note(context, label_id)  # only the owner gets this far when disabled
    if off:
        lines += [off, ""]
    if label["price"] is None:
        lines.append("⚠️ This product has no price yet, so it can't be bought.")
    else:
        stock = db.count_items(label_id)
        unit = unit_price_for(context, user_id, label["price"])
        if unit != label["price"]:
            lines.append(
                f"💵 Price: <s>{money(label['price'])}</s> <b>{money(unit)}</b> ⭐ seller price"
            )
        else:
            lines.append(f"💵 Price: <b>{money(unit)}</b>")
        lines.append(f"📦 In stock: <b>{stock}</b>")
        balance = db.get_user(user_id)["balance"]  # type: ignore[index]
        lines.append(f"💰 Your balance: <b>{money(balance)}</b>")
        lines.append("")
        if off:
            names = off_names(db.disabled_on_path(label_id))
            lines.append(f"It can't be bought until {names} enabled again.")
        elif stock:
            lines.append("How many do you want to buy?")
            quick = [
                btn(str(n), f"sb:{label_id}:{n}", GREEN) for n in QUICK_QUANTITIES if n <= stock
            ]
            rows.append(quick)
            if stock > 1:
                rows.append([btn("🔢 Other amount", f"sq:{label_id}", BLUE)])
        else:
            lines.append("❌ Sold out — please check back later.")
    rows.append([back, menu_btn()])
    await show(update, context, "\n".join(lines), markup(rows), new=new)


# ------------------------------------------------------------------- quantity
async def cb_quantity(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        label_id, quantity = callback_ints(query.data, 2)
    except ValueError:
        await stale_button(update)
        return
    await answer(query)
    if not shop_open_for(context, query.from_user.id):
        await show(update, context, CLOSED_TEXT, markup([[menu_btn()]]))
        return
    await render_confirm(update, context, label_id, quantity)


async def cb_custom_quantity(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        (label_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    db = db_of(context)
    if not shop_open_for(context, query.from_user.id):
        await answer(query)
        await show(update, context, CLOSED_TEXT, markup([[menu_btn()]]))
        return
    label = db.get_label(label_id)
    if label is None or not buyable(context, label_id):
        await answer(query, "This product isn't available.", show_alert=True)
        return
    stock = db.count_items(label_id)
    if not stock:
        await answer(query, "Sorry, this product is sold out.", show_alert=True)
        return
    await answer(query)
    set_state(context, "buy_quantity", label_id=label_id)
    await show(
        update,
        context,
        f"🔢 How many <b>{esc(label['name'])}</b> do you want?\n"
        f"Send a number from 1 to {min(stock, MAX_QUANTITY)}.",
        markup([[btn("❌ Cancel", f"sh:{label_id}:0", RED)]]),
    )


@state_handler("buy_quantity")
async def on_quantity(update: Update, context: Ctx, state: dict) -> None:
    text = await need_text(update)
    if text is None:
        return
    if not shop_open_for(context, update.effective_user.id):  # type: ignore[union-attr]
        clear_state(context)
        await show(update, context, CLOSED_TEXT, markup([[menu_btn()]]), new=True)
        return
    db = db_of(context)
    label_id = state["label_id"]
    if not buyable(context, label_id):  # e.g. disabled while the customer was typing
        clear_state(context)
        await show(update, context, "⚠️ This product isn't available.",
                   markup([[btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()]]), new=True)
        return
    stock = db.count_items(label_id)
    quantity = parse_int(text)
    limit = min(stock, MAX_QUANTITY)
    if quantity is None or quantity < 1:
        await update.effective_message.reply_text("🔢 Please send a whole number, e.g. 3.")  # type: ignore[union-attr]
        return
    if limit < 1:
        clear_state(context)
        await render_product(update, context, label_id, new=True, note="❌ Sold out.")
        return
    if quantity > limit:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"🔢 You can buy at most {limit} at once. Send a smaller number."
        )
        return
    clear_state(context)
    await render_confirm(update, context, label_id, quantity, new=True)


async def render_confirm(
    update: Update, context: Ctx, label_id: int, quantity: int, *, new: bool = False,
    note: str = "",
) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    if not shop_open_for(context, user_id):
        await show(update, context, CLOSED_TEXT, markup([[menu_btn()]]), new=new)
        return
    label = db.get_label(label_id)
    if label is None or not buyable(context, label_id):
        await show(update, context, "⚠️ This product isn't available.",
                   markup([[btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()]]), new=new)
        return
    stock = db.count_items(label_id)
    if quantity < 1 or quantity > min(stock, MAX_QUANTITY):
        note = "❌ Sold out." if not stock else f"⚠️ Only {stock} left in stock."
        await render_product(update, context, label_id, new=new, note=note)
        return
    unit = unit_price_for(context, user_id, label["price"])
    total = unit * quantity
    balance = db.get_user(user_id)["balance"]  # type: ignore[index]
    path = esc(db.path_text(label_id))
    prefix = f"{note}\n\n" if note else ""
    if balance < total:
        text = prefix + (
            "❌ <b>Not enough balance</b>\n\n"
            f"📦 {path}\n🔢 Quantity: {quantity}\n💰 Total: <b>{money(total)}</b>\n"
            f"💳 Your balance: {money(balance)}\n\n"
            f"You need <b>{money(total - balance)}</b> more."
        )
        rows = [
            [btn("💳 Add Balance", "dp", GREEN)],
            [btn("⬅️ Back", f"sh:{label_id}:0"), menu_btn()],
        ]
        await show(update, context, text, markup(rows), new=new)
        return
    # The Confirm button is valid once, for exactly this product, quantity and price.
    nonce = secrets.token_hex(4)
    context.user_data["buy_nonce"] = {  # type: ignore[index]
        "nonce": nonce, "label_id": label_id, "quantity": quantity, "unit": unit,
    }
    text = prefix + (
        "🧾 <b>Confirm your purchase</b>\n\n"
        f"📦 {path}\n🔢 Quantity: <b>{quantity}</b>\n💵 Price each: {money(unit)}\n"
        f"💰 Total: <b>{money(total)}</b>\n💳 Balance after: {money(balance - total)}"
    )
    rows = [[
        btn("✅ Confirm", f"sc:{label_id}:{quantity}:{nonce}", GREEN),
        btn("❌ Cancel", f"sh:{label_id}:0", RED),
    ]]
    await show(update, context, text, markup(rows), new=new)


# ------------------------------------------------------------------- purchase
async def cb_confirm(update: Update, context: Ctx) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    try:
        label_id, quantity = callback_ints(query.data, 2)
        nonce = parts[3]
    except (ValueError, IndexError):
        await stale_button(update)
        return
    quote = context.user_data.get("buy_nonce")  # type: ignore[union-attr]
    if not (
        isinstance(quote, dict)
        and quote.get("nonce") == nonce
        and quote.get("label_id") == label_id
        and quote.get("quantity") == quantity
    ):
        await answer(query, "This confirmation has expired. Please choose again.", show_alert=True)
        return
    context.user_data.pop("buy_nonce", None)  # type: ignore[union-attr]
    user_id = query.from_user.id
    db, cfg = db_of(context), cfg_of(context)
    if not shop_open_for(context, user_id):
        await answer(query)
        await show(update, context, CLOSED_TEXT, markup([[menu_btn()]]))
        return
    try:
        result = db.purchase(
            user_id, label_id, quantity, cfg.seller_discount_percent, expected_unit=quote["unit"]
        )
    except InsufficientBalance:
        await answer(query)
        await render_confirm(update, context, label_id, quantity)
        return
    except PriceChanged:
        await answer(query, "⚠️ The price changed. Please check the new total.", show_alert=True)
        await render_confirm(
            update, context, label_id, quantity,
            note="⚠️ <b>The price changed.</b> Nothing was charged — please check the new total.",
        )
        return
    except ShopError as exc:
        await answer(query, str(exc), show_alert=True)
        await render_product(update, context, label_id, note=f"⚠️ {esc(exc)}")
        return

    # The purchase is saved. From here on nothing may stop the delivery: every
    # Telegram call is best-effort.
    await answer(query, "✅ Purchase successful!")
    order = result.order
    codes = ", ".join(f"<code>{item['code']}</code>" for item in result.items[:50])
    if len(result.items) > 50:
        codes += f" … (+{len(result.items) - 50})"
    summary = (
        "✅ <b>Purchase successful!</b>\n\n"
        f"🧾 Order: <code>#{order['code']}</code>\n"
        f"📦 {esc(order['product_name'])}\n"
        f"🔢 Quantity: {order['quantity']}\n"
        f"💰 Paid: <b>{money(order['total'])}</b>\n"
        f"💳 New balance: <b>{money(result.new_balance)}</b>\n"
        f"🆔 Product IDs: {codes}\n\n"
        "Your product(s) 👇" + eta_note(len(result.items))
    )
    await try_show(update, context, summary)

    async def deliver() -> None:
        await _finish_purchase(context, user_id, result)

    start_delivery(context, user_id, deliver)


def eta_note(count: int) -> str:
    seconds = max(0, count - DELIVERY_BURST) * DELIVERY_INTERVAL
    if seconds < 60:
        return ""
    return f"\n⏳ Sending {count} products takes about {round(seconds / 60)} min — please wait."


async def _finish_purchase(context: Ctx, chat_id: int, result: PurchaseResult) -> None:
    order = result.order
    failed = await deliver_items(context, chat_id, result.items)
    if failed:
        end = (
            f"⚠️ {failed} product(s) could not be sent. Open 🧾 My Orders to get them again, "
            f"or {contact_owner(context)} with order <code>#{order['code']}</code>."
        )
    else:
        end = (
            f"✅ Delivered {len(result.items)} product(s) from order <code>#{order['code']}</code>. "
            "Thank you for your purchase!"
        )
    try:
        await context.bot.send_message(
            chat_id,
            end,
            parse_mode=ParseMode.HTML,
            reply_markup=markup([
                [btn("🛍 Shop", "sh:0:0", GREEN), btn("🧾 My Orders", "mo:0", BLUE)],
                [menu_btn()],
            ]),
        )
    except TelegramError as exc:
        log.warning("Could not send the delivery summary for order %s: %s", order["code"], exc)
    await notify_sellers(context, order, result.items, result.credits, chat_id)


# ------------------------------------------------------------------- delivery
def delivery_running(context: Ctx, chat_id: int) -> bool:
    return context.bot_data.get("deliveries", {}).get(chat_id, 0) > 0


def start_delivery(context: Ctx, chat_id: int, job: Callable[[], Awaitable[None]]) -> None:
    """Run ``job`` (which sends products to ``chat_id``) in the background.

    Deliveries to the same chat run one after another, so their pacing adds up; the
    bot keeps answering everyone else meanwhile. A delivery still running at shutdown
    is finished first (Application.stop waits for it).
    """
    running: dict[int, int] = context.bot_data.setdefault("deliveries", {})
    locks: dict[int, asyncio.Lock] = context.bot_data.setdefault("delivery_locks", {})
    running[chat_id] = running.get(chat_id, 0) + 1
    lock = locks.setdefault(chat_id, asyncio.Lock())

    async def run() -> None:
        try:
            async with lock:
                await job()
        except Exception:  # never surface as "Something went wrong" after a purchase
            log.exception("Delivery to %s failed", chat_id)
        finally:
            running[chat_id] -= 1
            if running[chat_id] <= 0:
                running.pop(chat_id, None)
                locks.pop(chat_id, None)

    context.application.create_task(run(), name=f"shopbot:delivery:{chat_id}")


async def deliver_items(
    context: Ctx, chat_id: int, items: list[Any], not_sent: Optional[list[Any]] = None,
    unsure: Optional[list[Any]] = None,
) -> int:
    """Send each product message, paced for Telegram. Returns how many weren't sent;
    ``not_sent``, if given, gets those items. With ``unsure``, the ones Telegram didn't
    answer for (they may have arrived) go there instead."""
    failed: list[Any] = [] if not_sent is None else not_sent
    maybe: list[Any] = failed if unsure is None else unsure
    start = len(failed) + (len(maybe) if maybe is not failed else 0)

    def count() -> int:
        return len(failed) + (len(maybe) if maybe is not failed else 0) - start

    for index, item in enumerate(items):
        if not context.application.running:  # stopping: finish soon
            since = context.bot_data.setdefault("stopping_since", time.monotonic())
            if time.monotonic() - since > STOP_GRACE:
                log.warning("Stopping: %s product(s) for %s not sent", len(items) - index, chat_id)
                failed.extend(items[index:])
                return count()
        if index >= DELIVERY_BURST and DELIVERY_INTERVAL > 0:
            await asyncio.sleep(DELIVERY_INTERVAL)
        try:
            await send_product(context, chat_id, json.loads(item["content"]))
        except Forbidden as exc:  # the buyer blocked the bot: the rest can't arrive either
            log.warning("Could not deliver to %s: %s", chat_id, exc)
            failed.extend(items[index:])
            return count()
        except (TelegramError, ValueError, KeyError) as exc:
            if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
                maybe.append(item)  # no answer from Telegram: it may have arrived
            else:
                failed.append(item)
            log.warning("Could not deliver item %s to %s: %s", item["code"], chat_id, exc)
    return count()


async def send_product(context: Ctx, chat_id: int, content: dict) -> None:
    try:
        await send_content(context.bot, chat_id, content)
    except (RetryAfter, FloodWait) as exc:
        seconds = wait_seconds(exc)
        if seconds > MAX_DELIVERY_WAIT:
            raise
        log.info("Flood wait of %.0fs while delivering to %s; retrying once", seconds, chat_id)
        await asyncio.sleep(seconds + 0.5)
        await send_content(context.bot, chat_id, content)


async def notify_sellers(
    context: Ctx, order: Any, items: list[Any], credits: dict[int, int], buyer_id: int
) -> None:
    for seller_id, amount in credits.items():
        if seller_id == buyer_id:
            continue
        count = sum(1 for item in items if item["owner_id"] == seller_id)
        await notify(
            context,
            seller_id,
            "💰 <b>New sale!</b>\n"
            f"📦 {esc(order['product_name'])}\n"
            f"🔢 {count} product(s) · <b>+{money(amount)}</b> earnings\n"
            f"🧾 Order <code>#{order['code']}</code>",
        )


# ------------------------------------------------------------------ my orders
async def cb_orders(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        (page,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    await answer(query)
    db = db_of(context)
    user_id = query.from_user.id
    total = db.count_user_orders(user_id)
    pages = page_count(total, ORDERS_PAGE_SIZE)
    page = min(page, pages - 1)
    orders = db.user_orders(user_id, page * ORDERS_PAGE_SIZE, ORDERS_PAGE_SIZE)
    lines = ["🧾 <b>My Orders</b>", ""]
    lines.append("Tap an order to get its products again." if total else "You haven't bought anything yet.")
    rows = []
    for order in orders:
        name = order["product_name"].split(" › ")[-1]
        rows.append([btn(
            f"#{order['code']} · {short(name, 18)} ×{order['quantity']} · {money(order['total'])}",
            f"mov:{order['id']}",
        )])
    rows.append(pager("mo:", page, pages))
    rows.append([btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()])
    await show(update, context, "\n".join(lines), markup(rows))


async def cb_order_view(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        (order_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    db = db_of(context)
    user_id = query.from_user.id
    order = db.get_order(order_id)
    if order is None or order["buyer_id"] != user_id:
        await answer(query, "Order not found.", show_alert=True)
        return
    if delivery_running(context, user_id):
        await answer(
            query, "⏳ Your products are still being sent. Please wait until that's done.",
            show_alert=True,
        )
        return
    await answer(query)
    items = db.order_items(order_id)
    header = (
        f"🧾 <b>Order <code>#{order['code']}</code></b>\n"
        f"📦 {esc(order['product_name'])}\n"
        f"🔢 Quantity: {order['quantity']} · 💰 {money(order['total'])}\n"
        f"🕒 {order['created_at']} UTC\n\nYour product(s) 👇" + eta_note(len(items))
    )

    async def resend() -> None:
        try:
            await context.bot.send_message(user_id, header, parse_mode=ParseMode.HTML)
        except TelegramError as exc:
            log.info("Could not send the order header to %s: %s", user_id, exc)
        failed = await deliver_items(context, user_id, items)
        end = "✅ Done." if not failed else f"⚠️ {failed} product(s) could not be sent."
        try:
            await context.bot.send_message(
                user_id,
                end,
                reply_markup=markup([[btn("🧾 My Orders", "mo:0", BLUE), menu_btn()]]),
            )
        except TelegramError as exc:
            log.info("Could not send the re-download summary to %s: %s", user_id, exc)

    start_delivery(context, user_id, resend)
