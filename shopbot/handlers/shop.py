"""🛍 Shop: browse labels, buy products, get them delivered, 🧾 My Orders."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Optional

from telegram import Update
from telegram.error import TelegramError

from ..content import send_content
from ..db import InsufficientBalance, ShopError, seller_price
from .common import (
    BLUE,
    GREEN,
    RED,
    Ctx,
    btn,
    callback_ints,
    cfg_of,
    db_of,
    esc,
    grid,
    is_owner,
    markup,
    menu_btn,
    money,
    need_text,
    notify,
    page_count,
    pager,
    parse_int,
    set_state,
    clear_state,
    show,
    short,
    stale_button,
    state_handler,
)

log = logging.getLogger("shopbot.shop")

PAGE_SIZE = 20
QUICK_QUANTITIES = (1, 2, 3, 5, 10)
MAX_QUANTITY = 200
ORDERS_PAGE_SIZE = 8
CLOSED_TEXT = "🔒 <b>The shop is closed right now.</b>\nPlease check back later."


def shop_open_for(context: Ctx, user_id: int) -> bool:
    """The owner can always browse; everyone else only while the shop is open."""
    return db_of(context).shop_is_open() or is_owner(context, user_id)


def unit_price_for(context: Ctx, user_id: int, price: int) -> int:
    row = db_of(context).get_user(user_id)
    if row is not None and row["is_seller"]:
        return seller_price(price, cfg_of(context).seller_discount_percent)
    return price


def visible_children(context: Ctx, parent_id: Optional[int], user_id: int) -> list[Any]:
    """Labels shown in the shop. Customers don't see products without a price."""
    db = db_of(context)
    children = db.children(parent_id)
    if is_owner(context, user_id):
        return children
    visible = []
    for child in children:
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
    await query.answer()
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
            await show(
                update,
                context,
                "⚠️ This category no longer exists.",
                markup([[btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()]]),
                new=new,
            )
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
    lines.append("")
    if not children:
        lines.append("Nothing here yet." if label else "The shop is empty right now. Check back soon!")
    else:
        lines.append("Choose a category or product:")

    categories, products = [], []
    for child in chunk:
        if db.has_children(child["id"]):
            categories.append(btn(f"📂 {child['name']}", f"sh:{child['id']}:0"))
        elif child["price"] is None:
            products.append([btn(f"{child['name']} · ⚠️ no price", f"sh:{child['id']}:0")])
        else:
            unit = unit_price_for(context, user_id, child["price"])
            sold_out = "" if db.count_items(child["id"]) else " · sold out"
            products.append(
                [btn(f"🛒 {child['name']} · {money(unit)}{sold_out}", f"sh:{child['id']}:0")]
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
        await show(update, context, "⚠️ This product no longer exists.",
                   markup([[btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()]]), new=new)
        return
    back = btn("⬅️ Back", f"sh:{label['parent_id'] or 0}:0")
    lines = [note, ""] if note else []
    lines += [f"🛒 <b>{esc(label['name'])}</b>", f"📂 {esc(db.path_text(label_id))}", ""]
    rows: list[list[Any]] = []
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
        if stock:
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
    await query.answer()
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
        await query.answer()
        await show(update, context, CLOSED_TEXT, markup([[menu_btn()]]))
        return
    label = db.get_label(label_id)
    if label is None or db.has_children(label_id) or label["price"] is None:
        await query.answer("This product isn't available.", show_alert=True)
        return
    stock = db.count_items(label_id)
    if not stock:
        await query.answer("Sorry, this product is sold out.", show_alert=True)
        return
    await query.answer()
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
    db = db_of(context)
    label_id = state["label_id"]
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
    update: Update, context: Ctx, label_id: int, quantity: int, *, new: bool = False
) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    label = db.get_label(label_id)
    if label is None or db.has_children(label_id) or label["price"] is None:
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
    if balance < total:
        text = (
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
    nonce = secrets.token_hex(4)
    context.user_data["buy_nonce"] = nonce  # type: ignore[index]
    text = (
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
    if context.user_data.get("buy_nonce") != nonce:  # type: ignore[union-attr]
        await query.answer("This confirmation has expired. Please choose again.", show_alert=True)
        return
    context.user_data.pop("buy_nonce", None)  # type: ignore[union-attr]
    user_id = query.from_user.id
    db, cfg = db_of(context), cfg_of(context)
    if not shop_open_for(context, user_id):
        await query.answer()
        await show(update, context, CLOSED_TEXT, markup([[menu_btn()]]))
        return
    try:
        result = db.purchase(user_id, label_id, quantity, cfg.seller_discount_percent)
    except InsufficientBalance:
        await query.answer()
        await render_confirm(update, context, label_id, quantity)
        return
    except ShopError as exc:
        await query.answer(str(exc), show_alert=True)
        await render_product(update, context, label_id, note=f"⚠️ {esc(exc)}")
        return
    await query.answer("✅ Purchase successful!")
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
        "Your product(s) 👇"
    )
    await show(update, context, summary)
    failed = await deliver_items(context, user_id, result.items)
    if failed:
        end = (
            f"⚠️ {failed} product(s) could not be sent. Open 🧾 My Orders to get them again, "
            f"or contact the owner with order <code>#{order['code']}</code>."
        )
    else:
        end = f"✅ Delivered {len(result.items)} product(s). Thank you for your purchase!"
    await context.bot.send_message(
        user_id,
        end,
        parse_mode="HTML",
        reply_markup=markup([
            [btn("🛍 Shop", "sh:0:0", GREEN), btn("🧾 My Orders", "mo:0", BLUE)],
            [menu_btn()],
        ]),
    )
    await notify_sellers(context, result.order, result.items, result.credits, user_id)


async def deliver_items(context: Ctx, chat_id: int, items: list[Any]) -> int:
    """Send each product message. Returns how many could not be sent."""
    failed = 0
    for item in items:
        try:
            await send_content(context.bot, chat_id, json.loads(item["content"]))
        except (TelegramError, ValueError, KeyError) as exc:
            failed += 1
            log.warning("Could not deliver item %s to %s: %s", item["code"], chat_id, exc)
    return failed


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
    await query.answer()
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
    order = db.get_order(order_id)
    if order is None or order["buyer_id"] != query.from_user.id:
        await query.answer("Order not found.", show_alert=True)
        return
    await query.answer()
    items = db.order_items(order_id)
    await context.bot.send_message(
        query.from_user.id,
        f"🧾 <b>Order <code>#{order['code']}</code></b>\n"
        f"📦 {esc(order['product_name'])}\n"
        f"🔢 Quantity: {order['quantity']} · 💰 {money(order['total'])}\n"
        f"🕒 {order['created_at']} UTC\n\nYour product(s) 👇",
        parse_mode="HTML",
    )
    failed = await deliver_items(context, query.from_user.id, items)
    end = "✅ Done." if not failed else f"⚠️ {failed} product(s) could not be sent."
    await context.bot.send_message(
        query.from_user.id,
        end,
        reply_markup=markup([[btn("🧾 My Orders", "mo:0", BLUE), menu_btn()]]),
    )
