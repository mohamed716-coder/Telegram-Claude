"""🏷 Add Label (owner only): build the label tree that the shop shows.

Labels nest without limit, e.g. Esim › Oman › Omantel › 5GB. The last label of a
branch is the product people buy; it has a price and holds the stock.
"""

from __future__ import annotations

from typing import Any

from telegram import Update

from ..db import ShopError
from .common import (
    BLUE,
    GREEN,
    RED,
    Ctx,
    btn,
    callback_ints,
    clear_state,
    db_of,
    esc,
    grid,
    is_owner,
    markup,
    menu_btn,
    money,
    need_text,
    page_count,
    pager,
    parse_amount,
    set_state,
    short,
    show,
    stale_button,
    state_handler,
)

PAGE_SIZE = 30


async def _owner_only(update: Update, context: Ctx) -> bool:
    if is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
        return True
    await update.callback_query.answer("Only the owner can manage labels.", show_alert=True)  # type: ignore[union-attr]
    return False


async def cb_labels(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        label_id, page = callback_ints(query.data, 2)
    except ValueError:
        await stale_button(update)
        return
    if not await _owner_only(update, context):
        return
    await query.answer()
    await render_labels(update, context, label_id, page)


async def render_labels(
    update: Update, context: Ctx, label_id: int, page: int = 0, *, new: bool = False, note: str = ""
) -> None:
    db = db_of(context)
    label = db.get_label(label_id) if label_id else None
    if label_id and label is None:
        await show(update, context, "⚠️ This label no longer exists.",
                   markup([[btn("🏷 Labels", "lb:0:0", GREEN), menu_btn()]]), new=new)
        return
    children = db.children(label_id or None)
    is_product = label is not None and not children
    stock = db.count_items(label_id) if label is not None else 0

    lines = [note, ""] if note else []
    lines.append("🏷 <b>Add Label</b>")
    lines.append(f"📂 {esc(db.path_text(label_id))}" if label else "📂 Top level (shown in 🛍 Shop)")
    if is_product:
        price = money(label["price"]) if label["price"] is not None else "⚠️ not set"
        lines.append(f"💵 Price: <b>{price}</b>")
        lines.append(f"📦 In stock: <b>{stock}</b>")
    lines.append("")
    if label is None:
        lines.append(
            "Open a label to add labels inside it. The last label of a branch is the "
            "product people buy (it needs a price)."
        )
    elif is_product and stock:
        lines.append("This is a product with stock, so it can't contain other labels.")
    elif is_product:
        lines.append(
            "This label is a product (it's the last one). Set its price, or add labels "
            "inside it to turn it into a category."
        )
    else:
        lines.append("Open a label, or add a new one here.")

    pages = page_count(len(children), PAGE_SIZE)
    page = min(page, pages - 1)
    chunk = children[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    categories, products = [], []
    for child in chunk:
        if db.has_children(child["id"]):
            categories.append(btn(f"📂 {child['name']}", f"lb:{child['id']}:0"))
        else:
            price = money(child["price"]) if child["price"] is not None else "no price"
            products.append(btn(f"{child['name']} · {price}", f"lb:{child['id']}:0"))
    rows: list[list[Any]] = grid(categories, 2) + grid(products, 2)
    rows.append(pager(f"lb:{label_id}:", page, pages))
    if not (is_product and stock):
        where = f" inside {short(label['name'], 20)}" if label else ""
        rows.append([btn(f"➕ New label{where}", f"ln:{label_id}", GREEN)])
    if label is not None:
        actions = []
        if is_product:
            actions.append(btn("💲 Set price", f"lp:{label_id}", BLUE))
        actions.append(btn("✏️ Rename", f"lr:{label_id}", BLUE))
        actions.append(btn("🗑 Delete", f"ld:{label_id}", RED))
        rows.append(actions)
    back = f"lb:{label['parent_id'] or 0}:0" if label is not None else "menu"
    rows.append([btn("⬅️ Back", back), menu_btn()])
    await show(update, context, "\n".join(lines), markup(rows), new=new)


# ------------------------------------------------------------------- new label
async def cb_new_label(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        (parent_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    if not await _owner_only(update, context):
        return
    db = db_of(context)
    parent = db.get_label(parent_id) if parent_id else None
    if parent_id and parent is None:
        await query.answer("This label no longer exists.", show_alert=True)
        return
    if parent is not None and db.count_items(parent_id):
        await query.answer(
            "This product has stock, so it can't contain labels. Remove or pull its products first.",
            show_alert=True,
        )
        return
    await query.answer()
    set_state(context, "label_name", parent_id=parent_id or None)
    where = f"inside <b>{esc(db.path_text(parent_id))}</b>" if parent else "at the top level"
    await show(
        update,
        context,
        f"🏷 <b>New label</b> {where}\n\n"
        "Send the label name. To add several labels at once, send one name per line, e.g.\n"
        "<code>5GB\n10GB\n20GB</code>",
        markup([[btn("❌ Cancel", f"lb:{parent_id}:0", RED)]]),
    )


@state_handler("label_name")
async def on_label_name(update: Update, context: Ctx, state: dict) -> None:
    if not is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
        clear_state(context)
        return
    text = await need_text(update)
    if text is None:
        return
    db = db_of(context)
    parent_id = state.get("parent_id")
    try:
        created, skipped = db.add_labels(parent_id, text.splitlines())
    except ShopError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}\nSend the name again, or press Cancel.")  # type: ignore[union-attr]
        return
    clear_state(context)
    parts = []
    if created:
        parts.append("✅ Added: " + ", ".join(f"<b>{esc(row['name'])}</b>" for row in created))
    if skipped:
        parts.append("ℹ️ Already existed: " + ", ".join(esc(name) for name in skipped))
    await render_labels(update, context, parent_id or 0, new=True, note="\n".join(parts))


# ----------------------------------------------------------------------- price
async def cb_set_price(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        (label_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    if not await _owner_only(update, context):
        return
    db = db_of(context)
    label = db.get_label(label_id)
    if label is None:
        await query.answer("This label no longer exists.", show_alert=True)
        return
    if db.has_children(label_id):
        await query.answer("Only the last label (the product) has a price.", show_alert=True)
        return
    await query.answer()
    await ask_price(update, context, label, then="labels")


async def ask_price(update: Update, context: Ctx, label: Any, *, then: str, new: bool = False) -> None:
    db = db_of(context)
    set_state(context, "label_price", label_id=label["id"], then=then)
    current = (
        f"Current price: <b>{money(label['price'])}</b>\n" if label["price"] is not None
        else "⚠️ This product has no price yet.\n"
    )
    await show(
        update,
        context,
        f"💲 <b>Price for {esc(label['name'])}</b>\n📂 {esc(db.path_text(label['id']))}\n\n"
        f"{current}Send the price in $, e.g. <code>9.99</code>",
        markup([[btn("❌ Cancel", f"lb:{label['id']}:0" if then == "labels" else "cancel", RED)]]),
        new=new,
    )


@state_handler("label_price")
async def on_label_price(update: Update, context: Ctx, state: dict) -> None:
    if not is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
        clear_state(context)
        return
    text = await need_text(update)
    if text is None:
        return
    price = parse_amount(text)
    if price is None or price <= 0:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "💲 Please send a price like 9.99 (more than 0)."
        )
        return
    db = db_of(context)
    try:
        label = db.set_label_price(state["label_id"], price)
    except ShopError as exc:
        clear_state(context)
        await update.effective_message.reply_text(f"⚠️ {exc}")  # type: ignore[union-attr]
        return
    clear_state(context)
    note = f"✅ Price of <b>{esc(label['name'])}</b> set to <b>{money(price)}</b>."
    if state.get("then") == "add":
        from .stock import begin_add  # local import: stock imports this module

        await begin_add(update, context, label, new=True, note=note)
        return
    await render_labels(update, context, label["id"], new=True, note=note)


# ---------------------------------------------------------------------- rename
async def cb_rename(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        (label_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    if not await _owner_only(update, context):
        return
    db = db_of(context)
    label = db.get_label(label_id)
    if label is None:
        await query.answer("This label no longer exists.", show_alert=True)
        return
    await query.answer()
    set_state(context, "label_rename", label_id=label_id)
    await show(
        update,
        context,
        f"✏️ Send the new name for <b>{esc(label['name'])}</b>.",
        markup([[btn("❌ Cancel", f"lb:{label_id}:0", RED)]]),
    )


@state_handler("label_rename")
async def on_label_rename(update: Update, context: Ctx, state: dict) -> None:
    if not is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
        clear_state(context)
        return
    text = await need_text(update)
    if text is None:
        return
    db = db_of(context)
    try:
        label = db.rename_label(state["label_id"], text)
    except ShopError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}\nSend another name, or press Cancel.")  # type: ignore[union-attr]
        return
    clear_state(context)
    await render_labels(update, context, label["id"], new=True,
                        note=f"✅ Renamed to <b>{esc(label['name'])}</b>.")


# ---------------------------------------------------------------------- delete
async def cb_delete(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        (label_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    if not await _owner_only(update, context):
        return
    db = db_of(context)
    label = db.get_label(label_id)
    if label is None:
        await query.answer("This label no longer exists.", show_alert=True)
        return
    stock = db.count_subtree_items(label_id)
    if stock:
        await query.answer(
            f"There are {stock} product(s) in stock inside this label. "
            "Remove or pull them from 📦 My Products first.",
            show_alert=True,
        )
        return
    await query.answer()
    inside = len(db.subtree_ids(label_id)) - 1
    extra = f" and the {inside} label(s) inside it" if inside else ""
    await show(
        update,
        context,
        f"🗑 Delete <b>{esc(db.path_text(label_id))}</b>{extra}?\n\n"
        "It disappears from the shop. Past sales stay in the sales analysis.",
        markup([[
            btn("🗑 Yes, delete", f"ldy:{label_id}", RED),
            btn("⬅️ Back", f"lb:{label_id}:0"),
        ]]),
    )


async def cb_delete_yes(update: Update, context: Ctx) -> None:
    query = update.callback_query
    try:
        (label_id,) = callback_ints(query.data, 1)
    except ValueError:
        await stale_button(update)
        return
    if not await _owner_only(update, context):
        return
    db = db_of(context)
    label = db.get_label(label_id)
    if label is None:
        await query.answer("This label was already deleted.", show_alert=True)
        return
    try:
        db.delete_label(label_id)
    except ShopError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer("🗑 Deleted")
    await render_labels(update, context, label["parent_id"] or 0,
                        note=f"🗑 Deleted <b>{esc(label['name'])}</b>.")
