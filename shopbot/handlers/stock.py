"""➕ Add Product and 📦 My Products (owner and admins)."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.constants import MessageLimit, ParseMode
from telegram.error import TelegramError

from ..content import extract_content, send_content
from ..db import ShopError
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
    csv_safe,
    db_of,
    edit_hook,
    esc,
    get_state,
    grid,
    is_owner,
    is_staff,
    markup,
    menu_btn,
    money,
    page_count,
    pager,
    set_state,
    short,
    show,
    stale_button,
    state_handler,
    try_show,
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
        if db.has_children(child["id"]):
            categories.append(btn(f"📂 {child['name']}", f"ap:{child['id']}:0"))
        else:
            info = f"{db.count_items(child['id'])} in stock" if child["price"] is not None else "no price"
            products.append([btn(f"🛒 {child['name']} · {info}", f"ap:{child['id']}:0")])
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
        await answer(query, "There is nothing to save.", show_alert=True)
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
    await answer(query, f"✅ Saved {len(codes)}")
    await _drop_status_message(update, context, state)
    label = db.get_label(label_id)
    shown = " ".join(f"<code>{code}</code>" for code in codes[:60])
    if len(codes) > 60:
        shown += f" … (+{len(codes) - 60} more)"
    text = (
        f"✅ <b>Saved {len(codes)} product(s)</b>\n"
        f"📂 {esc(db.path_text(label_id))}\n"
        f"💵 Price: {money(label['price']) if label and label['price'] is not None else '—'}\n"
        f"📦 Now in stock: <b>{db.count_items(label_id)}</b>\n\n"
        f"🆔 Product IDs:\n{shown}"
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
        if db.has_children(child["id"]):
            categories.append(btn(f"📂 {child['name']} · {count}", f"mp:{child['id']}:0"))
        else:
            products.append(btn(f"{child['name']} · {count}", f"mp:{child['id']}:0"))
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
