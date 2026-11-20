"""➕ Add Product and 📦 My Products (owner and admins)."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.constants import MessageLimit, ParseMode
from telegram.error import TelegramError

from ..content import extract_content, send_content
from ..db import ShopError
from . import shop
from .common import (
    BLUE,
    GREEN,
    RED,
    Ctx,
    answer,
    btn,
    callback_ints,
    cancel_kb,
    cfg_of,
    clear_state,
    csv_safe,
    db_of,
    edit_hook,
    esc,
    get_state,
    grid,
    hidden_note,
    is_owner,
    is_staff,
    markup,
    menu_btn,
    money,
    need_text,
    notify,
    page_count,
    pager,
    parse_int,
    set_state,
    short,
    show,
    stale_button,
    state_handler,
    try_show,
    user_html,
    user_name,
    utf16_len,
)

log = logging.getLogger("shopbot.stock")

BROWSE_PAGE_SIZE = 30
ITEMS_PAGE_SIZE = 30
MAX_BATCH = 1000
# Seconds of quiet after the last product message before the "Received N" status.
STATUS_DELAY = 1.5
# Product messages remembered per seller after Done, to warn when one is edited later.
REMEMBERED_SAVED = 2000

ADD_PROMPT = (
    "❗ <b>SEND THE PRODUCTS — ONE MESSAGE PER PRODUCT</b>\n"
    "Each message is delivered to the buyer exactly as you send it.\n\n"
    "📦 Product: <b>{path}</b>\n💵 Price: <b>{price}</b>\n\n"
    "Press <b>Done</b> when finished."
)


def done_cancel_kb() -> Any:
    return markup([[btn("✅ Done", "apd", GREEN), btn("❌ Cancel", "apc", RED)]])


async def _staff_only(update: Update, context: Ctx) -> bool:
    if is_staff(context, update.effective_user.id):  # type: ignore[union-attr]
        return True
    await answer(
        update.callback_query, "This section is for the owner and admins.", show_alert=True
    )
    return False


def _owner_filter(context: Ctx, user_id: int) -> Any:
    """The owner sees every product; an admin only their own."""
    return None if is_owner(context, user_id) else user_id


# =============================================================== Add Product
async def cb_add_browse(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        label_id, page = callback_ints(query.data, 2)
    except ValueError:
        await stale_button(update)
        return
    if not await _staff_only(update, context):
        return
    db = db_of(context)
    label = db.get_label(label_id) if label_id else None
    if label_id and label is None:
        await answer(query, "This label no longer exists.", show_alert=True)
        return
    if label is not None and not db.has_children(label_id) and label["price"] is None \
            and not is_owner(context, query.from_user.id):
        await answer(
            query, "This product has no price yet. Ask the owner to set one first.", show_alert=True
        )
        return
    await answer(query)
    await render_add(update, context, label_id, page)


async def render_add(update: Update, context: Ctx, label_id: int, page: int = 0) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    label = db.get_label(label_id) if label_id else None
    if label is not None and not db.has_children(label_id):
        await begin_add(update, context, label)
        return
    children = db.children(label_id or None)
    lines = ["➕ <b>Add Product</b>"]
    if label is not None:
        lines.append(f"📂 {esc(db.path_text(label_id))}")
    lines.append("")
    if not children:
        if is_owner(context, user_id):
            lines.append("There are no labels yet. Create them first with 🏷 Add Label.")
        else:
            lines.append("There are no labels yet. Ask the owner to create them.")
    else:
        lines.append(
            "Choose the product to add stock to — open the labels until you reach the last one."
        )
    pages = page_count(len(children), BROWSE_PAGE_SIZE)
    page = min(page, pages - 1)
    categories, products = [], []
    for child in children[page * BROWSE_PAGE_SIZE : (page + 1) * BROWSE_PAGE_SIZE]:
        off = "🚫 " if child["disabled"] else ""  # disabled: customers don't see it
        if db.has_children(child["id"]):
            categories.append(btn(f"{off}📂 {child['name']}", f"ap:{child['id']}:0"))
        else:
            info = f"{db.count_items(child['id'])} in stock" if child["price"] is not None else "no price"
            products.append([btn(f"{off}🛒 {child['name']} · {info}", f"ap:{child['id']}:0")])
    rows: list[list[Any]] = grid(categories, 2) + products
    rows.append(pager(f"ap:{label_id}:", page, pages))
    back = f"ap:{label['parent_id'] or 0}:0" if label is not None else "menu"
    rows.append([btn("⬅️ Back", back), menu_btn()])
    await show(update, context, "\n".join(lines), markup(rows))


async def begin_add(
    update: Update, context: Ctx, label: Any, *, new: bool = False, note: str = ""
) -> None:
    """Start collecting product messages for a product label."""
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    if label["price"] is None:
        if is_owner(context, user_id):
            from .labels import ask_price  # local import: labels imports this module

            await ask_price(update, context, label, then="add", new=new)
        else:
            await show(
                update,
                context,
                f"⚠️ <b>{esc(label['name'])}</b> has no price yet. "
                "Ask the owner to set one before adding products.",
                markup([[menu_btn()]]),
                new=new,
            )
        return
    set_state(
        context,
        "add_items",
        label_id=label["id"],
        label_name=label["name"],
        items=[],
        msg_ids=[],  # message id of each product message, to match later edits
        status_msg_id=None,
    )
    text = ADD_PROMPT.format(path=esc(db.path_text(label["id"])), price=money(label["price"]))
    hidden = hidden_note(db, label["id"])
    if hidden:
        text = (f"{hidden}\n<i>You can still add stock: it goes on sale once that is "
                "enabled again.</i>\n\n" + text)
    if note:
        text = f"{note}\n\n{text}"
    await show(update, context, text, done_cancel_kb(), new=new)


UNSUPPORTED = (
    "⚠️ This kind of message can't be sold. Send text, a photo, video, GIF, file, "
    "audio, voice message or sticker."
)


def content_problem(content: Any) -> str:
    """Why a product message can't be stored, or '' if it can."""
    if content is None:
        return UNSUPPORTED
    caption = content.get("caption")
    if caption and utf16_len(caption) > MessageLimit.CAPTION_LENGTH:
        # Telegram Premium lets people send longer captions, but bots can only send 1024
        # characters with a photo or file, so this product could never be delivered.
        return (
            f"⚠️ This caption is too long to deliver: bots can send at most "
            f"{MessageLimit.CAPTION_LENGTH} characters with a photo or file, and this one has "
            f"{utf16_len(caption)}. Shorten the caption, or send the text as its own message."
        )
    return ""


@state_handler("add_items")
async def on_add_item(update: Update, context: Ctx, state: dict) -> None:
    message = update.effective_message
    if not is_staff(context, update.effective_user.id):  # type: ignore[union-attr]
        clear_state(context)
        await message.reply_text(  # type: ignore[union-attr]
            "⛔ You're no longer an admin, so products can't be added. Nothing was saved."
        )
        return
    content = extract_content(message)  # type: ignore[arg-type]
    problem = content_problem(content)
    if problem:
        await message.reply_text(problem)  # type: ignore[union-attr]
        return
    if len(state["items"]) >= MAX_BATCH:
        await message.reply_text(  # type: ignore[union-attr]
            f"⚠️ You can add up to {MAX_BATCH} products at a time. Press Done to save these first."
        )
        return
    state["items"].append(content)
    state.setdefault("msg_ids", []).append(message.message_id)  # type: ignore[union-attr]
    _schedule_status(update, context, state)


@edit_hook
async def on_product_edit(update: Update, context: Ctx) -> bool:
    """An edited product message: replace it if it isn't saved yet, warn if it is."""
    message = update.edited_message
    if message is None or context.user_data is None:
        return False
    state = get_state(context)
    if state is not None and state.get("name") == "add_items" and is_staff(context, update.effective_user.id):  # type: ignore[union-attr]
        msg_ids = state.get("msg_ids") or []
        if message.message_id in msg_ids:
            index = msg_ids.index(message.message_id)
            content = extract_content(message)
            problem = content_problem(content)
            if problem:
                await message.reply_text(problem + "\nThe version you sent first is kept.")
                return True
            state["items"][index] = content
            await message.reply_text(
                f"✏️ Product {index + 1} updated — the edited version will be saved."
            )
            return True
    code = context.user_data.get("saved_products", {}).get(message.message_id)
    if code is None:
        return False
    item = db_of(context).get_item_by_code(code)
    if item is not None and item["status"] == "available":
        text = (
            f"⚠️ Product <code>{code}</code> was already saved, so this edit doesn't change it.\n"
            "To fix it, remove it in 📦 My Products and add the corrected one."
        )
    else:
        text = f"⚠️ Product <code>{code}</code> is no longer in stock, so this edit changes nothing."
    await message.reply_text(text, parse_mode=ParseMode.HTML)
    return True


def _remember_saved(context: Ctx, msg_ids: list[int], codes: list[str]) -> None:
    saved: dict[int, str] = context.user_data.setdefault("saved_products", {})  # type: ignore[union-attr]
    for msg_id, code in zip(msg_ids, codes):
        saved[msg_id] = code
    while len(saved) > REMEMBERED_SAVED:
        saved.pop(next(iter(saved)))


def _schedule_status(update: Update, context: Ctx, state: dict) -> None:
    """After a short pause, post one 'Received N products' message with Done/Cancel."""
    user_data = context.user_data
    old = user_data.pop("status_task", None)  # type: ignore[union-attr]
    if old is not None and not old.done():
        old.cancel()
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    bot = context.bot

    async def post_status() -> None:
        await asyncio.sleep(STATUS_DELAY)
        if user_data.get("state") is not state:  # type: ignore[union-attr]
            return
        previous = state.get("status_msg_id")
        sent = await bot.send_message(
            chat_id,
            f"📥 Received <b>{len(state['items'])}</b> product(s) for "
            f"<b>{esc(state['label_name'])}</b>.\nSend more, or press <b>Done</b> to save them.",
            parse_mode=ParseMode.HTML,
            reply_markup=done_cancel_kb(),
        )
        state["status_msg_id"] = sent.message_id
        if previous:
            try:
                await bot.delete_message(chat_id, previous)
            except TelegramError:
                pass

    user_data["status_task"] = context.application.create_task(  # type: ignore[index]
        post_status(), update=update, name=f"shopbot:status:{chat_id}"
    )


async def _drop_status_message(update: Update, context: Ctx, state: dict) -> None:
    status_id = state.get("status_msg_id")
    pressed = update.callback_query.message  # type: ignore[union-attr]
    if status_id and (pressed is None or pressed.message_id != status_id):
        try:
            await context.bot.delete_message(update.effective_chat.id, status_id)  # type: ignore[union-attr]
        except TelegramError:
            pass


async def cb_add_done(update: Update, context: Ctx) -> None:
    query = update.callback_query
    state = get_state(context)
    if state is None or state.get("name") != "add_items":
        saved_at = (context.user_data or {}).get("saved_at", 0.0)
        if time.monotonic() - saved_at < 600:  # e.g. Done pressed twice
            text = "There is nothing to save. Your products are already saved."
        else:
            text = (
                "There is nothing to save. If you had sent products, they were not saved (Add "
                "Product was stopped, or the bot restarted): open ➕ Add Product and send them again."
            )
        await answer(query, text, show_alert=True)
        return
    user_id = query.from_user.id
    if not is_staff(context, user_id):
        clear_state(context)
        await answer(query, "This section is for the owner and admins.", show_alert=True)
        return
    if not state["items"]:
        await answer(query, "Send at least one product message first.", show_alert=True)
        return
    db = db_of(context)
    label_id = state["label_id"]
    try:
        codes = db.add_items(label_id, user_id, state["items"])
    except ShopError as exc:
        clear_state(context)
        await answer(query)
        await show(update, context, f"⚠️ {esc(exc)}", markup([[menu_btn()]]))
        return
    # Saved. Telegram errors from here on must not look like "nothing was saved".
    clear_state(context)
    _remember_saved(context, state.get("msg_ids") or [], codes)
    context.user_data["saved_at"] = time.monotonic()  # type: ignore[index]
    await answer(query, f"✅ Saved {len(codes)}")
    await _drop_status_message(update, context, state)
    label = db.get_label(label_id)
    shown = " ".join(f"<code>{code}</code>" for code in codes[:60])
    if len(codes) > 60:
        shown += f" … (+{len(codes) - 60} more)"
    from .broadcast import stock_added  # local import: broadcast imports this module

    try:
        alert = stock_added(context, label_id, len(codes), user_id)
    except Exception:  # the products are saved; a failed alert must not hide that
        log.exception("Could not note the new stock for the customers' message")
        alert = ""
    text = (
        f"✅ <b>Saved {len(codes)} product(s)</b>\n"
        f"📂 {esc(db.path_text(label_id))}\n"
        f"💵 Price: {money(label['price']) if label and label['price'] is not None else '—'}\n"
        f"📦 Now in stock: <b>{db.count_items(label_id)}</b>\n\n"
        f"🆔 Product IDs:\n{shown}"
        + (f"\n\n{alert}" if alert else "")
    )
    rows = [
        [btn("➕ Add more", f"ap:{label_id}:0", GREEN), btn("📦 My Products", f"mi:{label_id}:0", BLUE)],
        [menu_btn()],
    ]
    if not await try_show(update, context, text, markup(rows)):
        await try_show(update, context, text, markup(rows), new=True)


async def cb_add_cancel(update: Update, context: Ctx) -> None:
    query = update.callback_query
    state = get_state(context)
    if state is None or state.get("name") != "add_items":
        # An old Cancel button: it still cancels whatever is in progress, as it says.
        clear_state(context)
        await answer(query)
        await show(update, context, "❌ Cancelled.", markup([[menu_btn()]]))
        return
    clear_state(context)
    await answer(query, "Cancelled")
    await _drop_status_message(update, context, state)
    count = len(state["items"])
    await show(
        update,
        context,
        f"❌ Cancelled. {count} product(s) were not saved." if count else "❌ Cancelled.",
        markup([[menu_btn()]]),
    )


# =============================================================== My Products
async def cb_mine(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        label_id, page = callback_ints(query.data, 2)
    except ValueError:
        await stale_button(update)
        return
    if not await _staff_only(update, context):
        return
    await answer(query)
    await render_mine(update, context, label_id, page)


async def render_mine(update: Update, context: Ctx, label_id: int, page: int = 0) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    owner_filter = _owner_filter(context, user_id)
    label = db.get_label(label_id) if label_id else None
    if label_id and label is None:
        await show(update, context, "⚠️ This label no longer exists.",
                   markup([[btn("📦 My Products", "mp:0:0", BLUE), menu_btn()]]))
        return
    if label is not None and not db.has_children(label_id):
        await render_items(update, context, label_id, 0)
        return
    children = db.children(label_id or None)
    lines = ["📦 <b>My Products</b>"]
    if label is not None:
        lines.append(f"📂 {esc(db.path_text(label_id))}")
        if hidden := hidden_note(db, label_id):
            lines.append(hidden)
        lines.append(f"In stock here: <b>{db.count_subtree_items(label_id, owner_filter)}</b>")
    elif owner_filter is None:
        total = db.count_all_items()
        mine = db.count_all_items(user_id)
        lines.append(f"In stock: <b>{total}</b> (yours: {mine}, admins: {total - mine})")
    else:
        lines.append(f"Your products in stock: <b>{db.count_all_items(user_id)}</b>")
    lines.append("")
    lines.append(
        "Open the labels to reach a product and see its product IDs." if children
        else "There are no labels yet."
    )
    pages = page_count(len(children), BROWSE_PAGE_SIZE)
    page = min(page, pages - 1)
    categories, products = [], []
    for child in children[page * BROWSE_PAGE_SIZE : (page + 1) * BROWSE_PAGE_SIZE]:
        count = db.count_subtree_items(child["id"], owner_filter)
        off = "🚫 " if child["disabled"] else ""  # disabled: customers don't see it
        if db.has_children(child["id"]):
            categories.append(btn(f"{off}📂 {child['name']} · {count}", f"mp:{child['id']}:0"))
        else:
            products.append(btn(f"{off}{child['name']} · {count}", f"mp:{child['id']}:0"))
    rows: list[list[Any]] = grid(categories, 2) + grid(products, 2)
    rows.append(pager(f"mp:{label_id}:", page, pages))
    if label is None:
        rows.append([btn("📥 Download sales analysis", "ma", BLUE)])
    back = f"mp:{label['parent_id'] or 0}:0" if label is not None else "menu"
    rows.append([btn("⬅️ Back", back), menu_btn()])
    await show(update, context, "\n".join(lines), markup(rows))


async def cb_items(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        label_id, page = callback_ints(query.data, 2)
    except ValueError:
        await stale_button(update)
        return
    if not await _staff_only(update, context):
        return
    await answer(query)
    await render_items(update, context, label_id, page)


async def render_items(
    update: Update, context: Ctx, label_id: int, page: int = 0, *, new: bool = False, note: str = ""
) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    owner_filter = _owner_filter(context, user_id)
    label = db.get_label(label_id)
    if label is None:
        await show(update, context, "⚠️ This product no longer exists.",
                   markup([[btn("📦 My Products", "mp:0:0", BLUE), menu_btn()]]), new=new)
        return
    if db.has_children(label_id):
        await render_mine(update, context, label_id, page)
        return
    total = db.count_items(label_id, owner_filter)
    pages = page_count(total, ITEMS_PAGE_SIZE)
    page = min(page, pages - 1)
    items = db.list_items(label_id, owner_filter, page * ITEMS_PAGE_SIZE, ITEMS_PAGE_SIZE)
    price = money(label["price"]) if label["price"] is not None else "⚠️ not set"
    lines = [note, ""] if note else []
    lines += [
        f"📦 <b>{esc(label['name'])}</b>",
        f"📂 {esc(db.path_text(label_id))}",
        f"💵 Price: <b>{price}</b>",
    ]
    if hidden := hidden_note(db, label_id):
        lines.append(hidden)
    if owner_filter is None:
        mine = db.count_items(label_id, user_id)
        lines.append(f"📦 In stock: <b>{total}</b> (yours: {mine}, admins: {total - mine})")
    else:
        lines.append(f"📦 Your stock: <b>{total}</b>")
    lines.append("")
    lines.append("Tap a product ID to open it." if total else "No products in stock here.")
    buttons = []
    for item in items:
        text = f"🆔 {item['code']}"
        if owner_filter is None and item["owner_id"] != user_id:
            text += f" · {short(user_name(db.get_user(item['owner_id'])), 12)}"
        buttons.append(btn(text, f"mv:{item['id']}"))
    rows: list[list[Any]] = grid(buttons, 2)
    rows.append(pager(f"mi:{label_id}:", page, pages))
    rows.append([btn("➕ Add products", f"ap:{label_id}:0", GREEN)])
    rows.append([btn("⬅️ Back", f"mp:{label['parent_id'] or 0}:0"), menu_btn()])
    await show(update, context, "\n".join(lines), markup(rows), new=new)


def _can_manage(context: Ctx, user_id: int, item: Any) -> bool:
    return is_owner(context, user_id) or (
        is_staff(context, user_id) and item["owner_id"] == user_id
    )


def _item_info(context: Ctx, item: Any) -> str:
    db, cfg = db_of(context), cfg_of(context)
    label = db.get_label(item["label_id"])
    owner = db.get_user(item["owner_id"])
    added_by = "Owner" if item["owner_id"] == cfg.owner_id else esc(user_name(owner))
    price = money(label["price"]) if label is not None and label["price"] is not None else "—"
    return (
        f"🆔 Product ID: <code>{item['code']}</code>\n"
        f"📂 {esc(db.path_text(item['label_id']))}\n"
        f"💵 Price: {price}\n"
        f"👤 Added by: {added_by} (<code>{item['owner_id']}</code>)\n"
        f"🕒 Added: {item['created_at']} UTC"
    )


async def _load_item(update: Update, context: Ctx) -> Any:
    """The item named in the callback data, after permission checks; None if not allowed."""
    query = update.callback_query
    try:
        (item_id,) = callback_ints(query.data, 1)  # type: ignore[union-attr]
    except ValueError:
        await stale_button(update)
        return None
    if not await _staff_only(update, context):
        return None
    item = db_of(context).get_item(item_id)
    if item is None or item["status"] != "available":
        await answer(query, "This product is no longer in stock.", show_alert=True)
        return None
    if not _can_manage(context, query.from_user.id, item):  # type: ignore[union-attr]
        await answer(query, "You can only manage your own products.", show_alert=True)
        return None
    return item


async def cb_item_view(update: Update, context: Ctx) -> None:
    item = await _load_item(update, context)
    if item is None:
        return
    query = update.callback_query
    await answer(query)
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    try:
        await send_content(context.bot, chat_id, json.loads(item["content"]))
    except (TelegramError, ValueError, KeyError) as exc:
        await context.bot.send_message(chat_id, f"⚠️ Couldn't show this product's message: {exc}")
    rows = [
        [btn("🗑 Remove", f"mr:{item['id']}", RED), btn("📤 Pull", f"mu:{item['id']}", BLUE)],
        [btn("⬅️ Back to list", f"mi:{item['label_id']}:0"), menu_btn()],
    ]
    await context.bot.send_message(
        chat_id, _item_info(context, item), parse_mode=ParseMode.HTML, reply_markup=markup(rows)
    )


async def cb_item_remove(update: Update, context: Ctx) -> None:
    item = await _load_item(update, context)
    if item is None:
        return
    await answer(update.callback_query)
    await show(
        update,
        context,
        _item_info(context, item)
        + "\n\n🗑 <b>Remove this product?</b> It will be deleted and can't be sold.",
        markup([[
            btn("🗑 Yes, remove", f"mry:{item['id']}", RED),
            btn("⬅️ Back", f"mi:{item['label_id']}:0"),
        ]]),
    )


async def cb_item_remove_yes(update: Update, context: Ctx) -> None:
    item = await _load_item(update, context)
    if item is None:
        return
    query = update.callback_query
    closed = db_of(context).close_item(item["id"], "removed", query.from_user.id)  # type: ignore[union-attr]
    if closed is None:
        await answer(query, "This product is no longer in stock.", show_alert=True)
        return
    await answer(query, "🗑 Removed")
    try:
        await render_items(update, context, item["label_id"],
                           note=f"🗑 Product <code>{item['code']}</code> was removed.")
    except TelegramError as exc:
        log.info("Could not show the list after removing %s: %s", item["code"], exc)


async def cb_item_pull(update: Update, context: Ctx) -> None:
    item = await _load_item(update, context)
    if item is None:
        return
    query = update.callback_query
    db = db_of(context)
    closed = db.close_item(item["id"], "pulled", query.from_user.id)  # type: ignore[union-attr]
    if closed is None:
        await answer(query, "This product is no longer in stock.", show_alert=True)
        return
    # Pulled. Every Telegram call from here on is best-effort, so the product itself is
    # always sent even if a step before it fails.
    await answer(query, "📤 Pulled")
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    await try_show(
        update,
        context,
        f"📤 Product <code>{item['code']}</code> was pulled from the shop and sent to you below.",
        markup([[btn("⬅️ Back to list", f"mi:{item['label_id']}:0"), menu_btn()]]),
    )
    try:
        await context.bot.send_message(
            chat_id,
            f"📤 <b>Pulled product</b>\n🆔 <code>{item['code']}</code>\n"
            f"📦 {esc(db.path_text(item['label_id']))}\n\nYour product 👇",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        log.info("Could not send the pull header to %s: %s", chat_id, exc)
    try:
        await send_content(context.bot, chat_id, json.loads(item["content"]))
    except (TelegramError, ValueError, KeyError) as exc:
        log.warning("Could not send pulled product %s to %s: %s", item["code"], chat_id, exc)
        try:
            await context.bot.send_message(
                chat_id, f"⚠️ Couldn't send the product's message: {exc}"
            )
        except TelegramError:
            pass


# ===================================================================== /pull
PULL_PAGE_SIZE = 20
PULL_QUICK = (1, 5, 10, 50)
MAX_PULL = shop.MAX_QUANTITY  # products in one pull (they're sent one message each)
RESEND_BUTTONS = 10  # "send again" buttons under a pull's summary


def tail_path(path: str, limit: int = 40) -> str:
    """'A › B › C' cut from the front, so the product's own name always shows."""
    if len(path) <= limit:
        return path
    parts = path.split(" › ")
    text = parts[-1]
    for part in reversed(parts[:-1]):
        if len("… › ") + len(part) + len(" › ") + len(text) > limit:
            break
        text = f"{part} › {text}"
    return short("… › " + text, limit)


def _from_whom(context: Ctx, user_id: int, seller: int) -> str:
    if seller == user_id:
        return "your stock"
    return f"the stock of {user_html(db_of(context).get_user(seller))}"


async def cmd_pull(update: Update, context: Ctx) -> None:
    """/pull: take products out of your stock, picked at random. The owner pulls from
    anyone's stock with /pull ID."""
    message = update.effective_message
    user_id = update.effective_user.id  # type: ignore[union-attr]
    if message is None:
        return
    if not is_staff(context, user_id):
        await message.reply_text("⛔ Only the owner and admins can use this command.")
        return
    args = context.args or []
    seller = user_id
    if args:
        target = parse_int(args[0]) if len(args) == 1 else None
        if target is None or target <= 0:
            await message.reply_text(
                f"⚠️ {' '.join(args)} isn't a user ID. Send /pull for your products"
                + (", or /pull ID for another seller's." if is_owner(context, user_id) else "."))
            return
        if target != user_id and not is_owner(context, user_id):
            await message.reply_text("⛔ Only the owner can pull another seller's products. "
                                     "Send /pull for yours.")
            return
        seller = target
    await render_pull_list(update, context, seller, 0, new=True)


async def _pull_allowed(update: Update, context: Ctx, seller: int) -> bool:
    """A button of /pull: still staff, and the owner or pulling from their own stock."""
    if not await _staff_only(update, context):
        return False
    user_id = update.effective_user.id  # type: ignore[union-attr]
    if seller != user_id and not is_owner(context, user_id):
        await answer(update.callback_query, "You can only pull your own products.", show_alert=True)
        return False
    return True


async def render_pull_list(
    update: Update, context: Ctx, seller: int, page: int = 0, *, new: bool = False
) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    products = db.seller_products(seller)
    lines = ["📤 <b>Pull products</b>", f"From {_from_whom(context, user_id, seller)}.", ""]
    if not products:
        lines.append("There are no products in stock." if seller == user_id
                     else "They have no products in stock.")
        await show(update, context, "\n".join(lines), markup([[menu_btn()]]), new=new)
        return
    lines.append("Choose a product, then how many: they're picked at random, taken out of the "
                 "shop and sent to you here.")
    if seller == user_id and is_owner(context, user_id):
        lines.append("To pull an admin's products: /pull <code>ID</code>")
    pages = page_count(len(products), PULL_PAGE_SIZE)
    page = min(page, pages - 1)
    buttons = [btn(f"{tail_path(db.path_text(row['label_id']))} · {row['n']}", f"pl:{seller}:{row['label_id']}")
               for row in products[page * PULL_PAGE_SIZE : (page + 1) * PULL_PAGE_SIZE]]
    rows: list[list[Any]] = [[b] for b in buttons]
    rows.append(pager(f"plp:{seller}:", page, pages))
    rows.append([menu_btn()])
    await show(update, context, "\n".join(lines), markup(rows), new=new)


async def cb_pull_page(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        seller, page = callback_ints(query.data, 2)
    except ValueError:
        await stale_button(update)
        return
    if not await _pull_allowed(update, context, seller):
        return
    await answer(query)
    await render_pull_list(update, context, seller, page)


async def render_pull_product(
    update: Update, context: Ctx, seller: int, label_id: int, *, new: bool = False, note: str = ""
) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    label = db.get_label(label_id)
    stock = db.count_items(label_id, seller) if label is not None else 0
    lines = [note, ""] if note else []
    lines += ["📤 <b>Pull products</b>"]
    if label is None or not stock:
        lines.append("There are no products left here." if label is not None
                     else "This product no longer exists.")
        await show(update, context, "\n".join(lines),
                   markup([[btn("⬅️ Back", f"plp:{seller}:0"), menu_btn()]]), new=new)
        return
    most = min(stock, MAX_PULL)
    lines += [
        f"📦 {esc(db.path_text(label_id))}",
        f"From {_from_whom(context, user_id, seller)}: <b>{stock}</b> in stock",
        "",
        "How many? They're picked at random.",
    ]
    if stock > MAX_PULL:
        lines.append(f"Up to {MAX_PULL} at a time.")
    buttons = [btn(str(n), f"plq:{seller}:{label_id}:{n}") for n in PULL_QUICK if n < most]
    buttons.append(btn(f"All · {most}" if most == stock else str(most), f"plq:{seller}:{label_id}:{most}"))
    rows: list[list[Any]] = grid(buttons, 3)
    rows.append([btn("✏️ Other amount", f"plo:{seller}:{label_id}", BLUE)])
    rows.append([btn("⬅️ Back", f"plp:{seller}:0"), menu_btn()])
    await show(update, context, "\n".join(lines), markup(rows), new=new)


async def cb_pull_product(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        seller, label_id = callback_ints(query.data, 2)
    except ValueError:
        await stale_button(update)
        return
    if not await _pull_allowed(update, context, seller):
        return
    await answer(query)
    await render_pull_product(update, context, seller, label_id)


async def render_pull_confirm(
    update: Update, context: Ctx, seller: int, label_id: int, count: int, *, new: bool = False
) -> None:
    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    stock = db.count_items(label_id, seller)
    if count < 1 or count > min(stock, MAX_PULL) or db.get_label(label_id) is None:
        note = "⚠️ There are no products left here." if not stock else f"⚠️ There are {stock} in stock."
        await render_pull_product(update, context, seller, label_id, new=new, note=note)
        return
    # Valid once, for exactly this product, seller and number: a second press can't pull again.
    nonce = secrets.token_hex(4)
    context.user_data["pull_nonce"] = {  # type: ignore[index]
        "nonce": nonce, "seller": seller, "label_id": label_id, "count": count,
    }
    text = (
        f"📤 <b>Pull {count} product(s)?</b>\n\n"
        f"📦 {esc(db.path_text(label_id))}\n"
        f"From {_from_whom(context, user_id, seller)}: {stock} in stock\n\n"
        "They're picked at random, taken out of the shop and sent to you here."
    )
    rows = [[btn(f"📤 Pull {count}", f"ply:{seller}:{label_id}:{count}:{nonce}", GREEN),
             btn("❌ Cancel", f"pl:{seller}:{label_id}", RED)]]
    await show(update, context, text, markup(rows), new=new)


async def cb_pull_quantity(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        seller, label_id, count = callback_ints(query.data, 3)
    except ValueError:
        await stale_button(update)
        return
    if not await _pull_allowed(update, context, seller):
        return
    await answer(query)
    await render_pull_confirm(update, context, seller, label_id, count)


async def cb_pull_other(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        seller, label_id = callback_ints(query.data, 2)
    except ValueError:
        await stale_button(update)
        return
    if not await _pull_allowed(update, context, seller):
        return
    await answer(query)
    stock = db_of(context).count_items(label_id, seller)
    if not stock:
        await render_pull_product(update, context, seller, label_id)
        return
    set_state(context, "pull_count", seller=seller, label_id=label_id)
    await show(update, context, f"✏️ How many to pull? Send a number from 1 to {min(stock, MAX_PULL)}.",
               cancel_kb())


@state_handler("pull_count")
async def on_pull_count(update: Update, context: Ctx, state: dict) -> None:
    user_id = update.effective_user.id  # type: ignore[union-attr]
    seller, label_id = state["seller"], state["label_id"]
    if not is_staff(context, user_id) or (seller != user_id and not is_owner(context, user_id)):
        clear_state(context)
        return
    text = await need_text(update)
    if text is None:
        return
    count = parse_int(text)
    most = min(db_of(context).count_items(label_id, seller), MAX_PULL)
    if most and (count is None or not 1 <= count <= most):
        await update.effective_message.reply_text(f"⚠️ Please send a number from 1 to {most}.")  # type: ignore[union-attr]
        return
    clear_state(context)
    await render_pull_confirm(update, context, seller, label_id, count or 0, new=True)


async def cb_pull_yes(update: Update, context: Ctx) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    try:
        seller, label_id, count = callback_ints(query.data, 3)
        nonce = parts[4]
    except (ValueError, IndexError):
        await stale_button(update)
        return
    if not await _pull_allowed(update, context, seller):
        return
    quote = context.user_data.get("pull_nonce")  # type: ignore[union-attr]
    if not (isinstance(quote, dict) and quote.get("nonce") == nonce and quote.get("seller") == seller
            and quote.get("label_id") == label_id and quote.get("count") == count):
        await answer(query, "This confirmation has expired. Please choose again.", show_alert=True)
        return
    context.user_data.pop("pull_nonce", None)  # type: ignore[union-attr]
    db = db_of(context)
    user_id = query.from_user.id
    try:
        items = db.pull_random(label_id, seller, count, user_id)
    except ShopError as exc:
        await answer(query, str(exc), show_alert=True)
        return
    if not items:
        await answer(query, "There are no products left here.", show_alert=True)
        await render_pull_product(update, context, seller, label_id)
        return
    # Pulled. Every Telegram call from here on is best-effort: the products are sent
    # whatever happens before.
    await answer(query, "📤 Pulled")
    path = db.path_text(label_id)
    codes = ", ".join(f"<code>{item['code']}</code>" for item in items[:50])
    if len(items) > 50:
        codes += f" … (+{len(items) - 50})"
    fewer = f" (only {len(items)} were left)" if len(items) < count else ""
    await try_show(
        update, context,
        f"📤 <b>Pulled {len(items)} product(s)</b>{fewer}\n\n📦 {esc(path)}\n"
        f"From {_from_whom(context, user_id, seller)}\n🆔 Product IDs: {codes}\n\n"
        "Your product(s) 👇" + shop.eta_note(len(items)),
    )
    chat_id = update.effective_chat.id  # type: ignore[union-attr]

    async def deliver() -> None:
        failed: list[Any] = []  # not sent
        unsure: list[Any] = []  # Telegram didn't answer: they may have arrived
        await shop.deliver_items(context, chat_id, items, failed, unsure)
        if unsure:  # once more: getting one twice does no harm, selling it after all would
            again, unsure[:] = list(unsure), []
            await shop.deliver_items(context, chat_id, again, failed, unsure)
        back = set(db.restore_pulled([item["id"] for item in failed]))  # nothing is lost
        kept = [item for item in failed if item["id"] not in back] + unsure  # stay pulled
        lines = []
        if back:
            lines.append(f"⚠️ {len(back)} product(s) couldn't be sent, so they're back in stock: "
                         + _codes([item for item in failed if item["id"] in back]))
        if kept:
            lines.append(f"⚠️ {len(kept)} product(s) may not have arrived (or their product was "
                         f"deleted meanwhile): {_codes(kept)}\nThey stay pulled: press one below to get "
                         "it again.")
        end = "\n\n".join(lines) or f"✅ Sent {len(items)} pulled product(s)."
        rows = [[btn(f"📤 {item['code']}", f"plr:{item['id']}", BLUE)] for item in kept[:RESEND_BUTTONS]]
        try:
            await context.bot.send_message(chat_id, end, parse_mode=ParseMode.HTML,
                                           reply_markup=markup(rows + [[menu_btn()]]))
        except TelegramError as exc:
            log.warning("Could not send the pull summary to %s: %s", chat_id, exc)
        taken = len(items) - len(back)
        if seller != user_id and taken:
            await notify(context, seller, f"📤 The owner pulled <b>{taken}</b> of your products out of "
                                          f"the shop:\n📦 {esc(path)}")

    shop.start_delivery(context, chat_id, deliver)


def _codes(items: list[Any], limit: int = 50) -> str:
    text = ", ".join(f"<code>{item['code']}</code>" for item in items[:limit])
    return text + (f" … (+{len(items) - limit})" if len(items) > limit else "")


async def cb_pull_resend(update: Update, context: Ctx) -> None:
    """A pulled product that may not have arrived: send it again to whoever pulled it."""
    query = update.callback_query
    try:
        (item_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    item = db_of(context).get_item(item_id)
    if item is None or item["status"] != "pulled" or item["buyer_id"] != query.from_user.id:
        await answer(query, "This product isn't one you pulled.", show_alert=True)
        return
    try:
        await shop.send_product(context, query.message.chat.id, json.loads(item["content"]))  # type: ignore[union-attr]
    except (TelegramError, ValueError, KeyError) as exc:
        log.warning("Could not send pulled product %s again: %s", item["code"], exc)
        await answer(query, "Couldn't send it right now. Please try again in a minute.", show_alert=True)
        return
    await answer(query, f"📤 Sent {item['code']} again")


# ============================================================ Sales analysis
async def cb_analysis(update: Update, context: Ctx) -> None:
    query = update.callback_query
    if not await _staff_only(update, context):
        return
    db, cfg = db_of(context), cfg_of(context)
    user_id = query.from_user.id
    owner_filter = _owner_filter(context, user_id)
    rows = db.sales_analysis(owner_filter)
    if not rows:
        await answer(query, "There are no products yet.", show_alert=True)
        return
    await answer(query, "📥 Preparing the file…")

    def seller_label(owner_id: int, owner: Any) -> str:
        return "Owner" if owner_id == cfg.owner_id else user_name(owner)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Product", "Seller", "Seller ID", "Price ($)", "Sold", "Left", "Revenue ($)", "Removed", "Pulled"]
    )
    per_seller: dict[int, dict[str, Any]] = {}
    for row in rows:
        writer.writerow([
            csv_safe(row["product"]),
            csv_safe(seller_label(row["owner_id"], row["owner"])),
            row["owner_id"],
            f"{row['price'] / 100:.2f}" if row["price"] is not None else "",
            row["sold"],
            row["left"],
            f"{row['revenue'] / 100:.2f}",
            row["removed"],
            row["pulled"],
        ])
        totals = per_seller.setdefault(
            row["owner_id"],
            {"name": seller_label(row["owner_id"], row["owner"]), "sold": 0, "left": 0,
             "revenue": 0, "removed": 0, "pulled": 0},
        )
        for key in ("sold", "left", "revenue", "removed", "pulled"):
            totals[key] += row[key]
    writer.writerow([])
    writer.writerow(["TOTALS PER SELLER"])
    for seller_id, totals in per_seller.items():
        writer.writerow([
            "All products", csv_safe(totals["name"]), seller_id, "", totals["sold"], totals["left"],
            f"{totals['revenue'] / 100:.2f}", totals["removed"], totals["pulled"],
        ])
    sold = sum(t["sold"] for t in per_seller.values())
    left = sum(t["left"] for t in per_seller.values())
    revenue = sum(t["revenue"] for t in per_seller.values())
    if owner_filter is None:
        writer.writerow([
            "All products", "EVERYONE", "", "", sold, left, f"{revenue / 100:.2f}",
            sum(t["removed"] for t in per_seller.values()),
            sum(t["pulled"] for t in per_seller.values()),
        ])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    data = buffer.getvalue().encode("utf-8-sig")  # BOM so Excel shows emoji/Arabic correctly
    scope = "all sellers" if owner_filter is None else "your products"
    await context.bot.send_document(
        update.effective_chat.id,  # type: ignore[union-attr]
        document=data,
        filename=f"sales_analysis_{stamp}.csv",
        caption=(
            f"📥 <b>Sales analysis</b> ({scope})\n"
            f"✅ Sold: <b>{sold}</b> · 📦 Left: <b>{left}</b> · 💰 Revenue: <b>{money(revenue)}</b>"
        ),
        parse_mode=ParseMode.HTML,
    )
