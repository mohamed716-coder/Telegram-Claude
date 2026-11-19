"""📣 Messages to every user: /announce (owner) and "🆕 New in the shop" after stock is added.

Both go to everyone who has started the bot, except the owner and banned users. They
are sent in the background, about 15 a second: far below Telegram's limit of 30 a
second, so the bot keeps answering everyone else meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application

from ..content import extract_content, send_content
from ..db import seller_price
from ..ratelimit import FloodWait, wait_seconds
from .common import (
    GREEN,
    NO_PREVIEW,
    RED,
    Ctx,
    answer,
    btn,
    clear_state,
    db_of,
    esc,
    grid,
    is_owner,
    markup,
    menu_btn,
    money,
    notify,
    set_state,
    short,
    show,
    state_handler,
    utf16_len,
)
from .owner import owner_command
from .stock import content_problem

log = logging.getLogger("shopbot.broadcast")

SEND_INTERVAL = 1 / 15  # seconds between two messages (Telegram allows about 30 a second)
MAX_WAIT = 60  # a longer "too many requests" wait counts that user as failed
PROGRESS_EVERY = 30.0  # seconds between progress updates while an announcement goes out
NEW_STOCK_DELAY = 60.0  # seconds: stock added meanwhile goes out in the same message
NEW_STOCK_LISTED = 10  # products named in one "New in the shop" message
NEW_STOCK_BUTTONS = 6  # product buttons under it

ANNOUNCE_PROMPT = (
    "📣 <b>Announcement</b>\n\n"
    "Send the message for all users: text, a photo, video, file, voice message… "
    "It is sent exactly as you send it, to everyone who has started the bot "
    "(not to you or banned users).\n\n"
    "Tip: <code>/announce your text</code> works too."
)


# -------------------------------------------------------------------- sending
@dataclass
class Outcome:
    total: int
    sent: int = 0
    unreachable: int = 0  # blocked the bot or deleted their account
    failed: int = 0
    left: int = 0  # not sent because the bot was stopping

    @property
    def done(self) -> int:
        return self.sent + self.unreachable + self.failed


async def send_to_all(
    app: Application,
    users: list[Any],
    send_one: Callable[[Any], Awaitable[Any]],
    out: Outcome,
    progress: Optional[Callable[[Outcome], Awaitable[None]]] = None,
) -> Outcome:
    """Call ``send_one(user)`` for every user, paced, filling in ``out``. Never raises.

    Broadcasts take turns (an announcement and a stock message at the same time would
    together reach Telegram's limit, and then the whole bot has to wait)."""
    lock: asyncio.Lock = app.bot_data.setdefault("broadcast_lock", asyncio.Lock())
    async with lock:
        await _send_each(app, users, send_one, out, progress)
    return out


async def _send_each(
    app: Application,
    users: list[Any],
    send_one: Callable[[Any], Awaitable[Any]],
    out: Outcome,
    progress: Optional[Callable[[Outcome], Awaitable[None]]],
) -> None:
    for index, user in enumerate(users):
        if not app.running:  # stopping: don't hold up the restart
            out.left = len(users) - index
            log.warning("Stopping: %s message(s) of a broadcast were not sent", out.left)
            break
        if index and SEND_INTERVAL > 0:
            await asyncio.sleep(SEND_INTERVAL)
        try:
            try:
                await send_one(user)
            except (RetryAfter, FloodWait) as exc:
                seconds = wait_seconds(exc)
                if seconds > MAX_WAIT:
                    raise
                await asyncio.sleep(seconds + 0.5)
                await send_one(user)
            out.sent += 1
        except Forbidden:
            out.unreachable += 1
        except (TelegramError, ValueError, KeyError) as exc:
            out.failed += 1
            log.info("Broadcast message to %s failed: %s", user["id"], exc)
        if progress is not None:
            await progress(out)


def _start(app: Application, coroutine: Any, name: str) -> asyncio.Task:
    if app.running:  # Application.stop() then waits for it
        return app.create_task(coroutine, name=name)
    return asyncio.create_task(coroutine, name=name)  # e.g. while starting up


# ------------------------------------------------------------------- announce
def _text_after_command(message: Message) -> Optional[dict]:
    """``/announce some text``: the text after the command, with its formatting."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    start = text.index(parts[1], len(parts[0]))
    shift = utf16_len(text[:start])  # entity offsets count UTF-16 units
    entities = []
    for entity in message.entities or ():
        if entity.type == "bot_command" or entity.offset < shift:
            continue
        item = entity.to_dict()
        item["offset"] = entity.offset - shift
        entities.append(item)
    return {"kind": "text", "text": text[start:], "entities": entities}


@owner_command
async def cmd_announce(update: Update, context: Ctx) -> None:
    message = update.effective_message
    content = _text_after_command(message)  # type: ignore[arg-type]
    if content is None:
        set_state(context, "announce")
        await message.reply_text(  # type: ignore[union-attr]
            ANNOUNCE_PROMPT, parse_mode=ParseMode.HTML,
            reply_markup=markup([[btn("❌ Cancel", "anc", RED)]]),
        )
        return
    await _preview(update, context, content)


@state_handler("announce")
async def on_announce_message(update: Update, context: Ctx, state: dict) -> None:
    message = update.effective_message
    if not is_owner(context, update.effective_user.id):  # type: ignore[union-attr]
        clear_state(context)
        return
    content = extract_content(message)  # type: ignore[arg-type]
    problem = content_problem(content)
    if problem:
        await message.reply_text(  # type: ignore[union-attr]
            problem.replace("can't be sold", "can't be announced")
            + "\nSend another message, or press Cancel.",
            reply_markup=markup([[btn("❌ Cancel", "anc", RED)]]),
        )
        return
    clear_state(context)
    await _preview(update, context, content)  # type: ignore[arg-type]


async def _preview(update: Update, context: Ctx, content: dict) -> None:
    """Show the owner the announcement exactly as users will get it, then ask."""
    users = db_of(context).broadcast_recipients()
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not users:
        await context.bot.send_message(
            chat_id, "📣 Nobody would get it yet: no one except you has started the bot.",
            reply_markup=markup([[menu_btn()]]),
        )
        return
    try:
        await send_content(context.bot, chat_id, content)
    except TelegramError as exc:
        await context.bot.send_message(
            chat_id, f"⚠️ Telegram doesn't accept this message ({exc}), so it can't be "
                     "announced. Send /announce and try another one.",
        )
        return
    nonce = secrets.token_hex(4)
    context.user_data["announce"] = {"nonce": nonce, "content": content}  # type: ignore[index]
    count = len(users)
    await context.bot.send_message(
        chat_id,
        f"📣 <b>Send the message above to {count} user(s)?</b>\n"
        "That's everyone who has started the bot, except you and banned users.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup([[
            btn(f"✅ Send to {count}", f"an:{nonce}", GREEN),
            btn("❌ Cancel", "anc", RED),
        ]]),
    )


async def cb_announce_send(update: Update, context: Ctx) -> None:
    query = update.callback_query
    if not is_owner(context, query.from_user.id):
        await answer(query, "Only the owner can send announcements.", show_alert=True)
        return
    nonce = (query.data or "").partition(":")[2]
    pending = context.user_data.get("announce")  # type: ignore[union-attr]
    if not (isinstance(pending, dict) and nonce and pending.get("nonce") == nonce):
        await answer(query, "This announcement was already sent or cancelled.", show_alert=True)
        return
    running: Optional[Outcome] = context.bot_data.get("announcing")
    if running is not None:
        await answer(
            query, f"📣 The last announcement is still being sent ({running.done} of "
                   f"{running.total}). Press Send again when it's finished.", show_alert=True,
        )
        return
    context.user_data.pop("announce", None)  # type: ignore[union-attr]
    users = db_of(context).broadcast_recipients()
    content = pending["content"]
    out = Outcome(total=len(users))
    context.bot_data["announcing"] = out
    await answer(query, "📣 Sending…")
    await show(update, context, f"📣 Sending the announcement to {out.total} user(s)…")
    owner_id = query.from_user.id
    status = query.message.message_id if query.message is not None else None
    app = context.application

    async def progress(current: Outcome) -> None:
        nonlocal last
        if status is None or time.monotonic() - last < PROGRESS_EVERY:
            return
        last = time.monotonic()
        try:
            await context.bot.edit_message_text(
                f"📣 Sending the announcement… {current.done} of {current.total}",
                chat_id=owner_id, message_id=status,
            )
        except TelegramError as exc:
            log.info("Could not update the announcement progress: %s", exc)

    async def run() -> None:
        try:
            await send_to_all(
                app, users, lambda user: send_content(context.bot, user["id"], content),
                out, progress,
            )
        finally:
            context.bot_data.pop("announcing", None)
        await notify(context, owner_id, announce_summary(out))

    last = time.monotonic()
    _start(app, run(), "shopbot:announce")


def announce_summary(out: Outcome) -> str:
    if out.sent == out.total:
        lines = [f"📣 <b>Announcement sent</b> to all {out.total} user(s)."]
    else:
        lines = [f"📣 <b>Announcement sent</b> to {out.sent} of {out.total} user(s)."]
    if out.unreachable:
        lines.append(f"🚫 {out.unreachable} blocked the bot or deleted their account.")
    if out.failed:
        lines.append(f"⚠️ {out.failed} couldn't get it because of an error (see the bot's log).")
    if out.left:
        lines.append(f"⏸ The bot restarted while sending: {out.left} user(s) didn't get it.")
    return "\n".join(lines)


async def cb_announce_cancel(update: Update, context: Ctx) -> None:
    context.user_data.pop("announce", None)  # type: ignore[union-attr]
    clear_state(context)
    await answer(update.callback_query, "Cancelled")
    await show(update, context, "❌ Announcement cancelled. Nothing was sent.",
               markup([[menu_btn()]]))


# ----------------------------------------------------------------- new stock
def _soon() -> str:
    minutes = round(NEW_STOCK_DELAY / 60)
    return "in a moment" if minutes < 1 else f"in about {minutes} minute{'s' if minutes > 1 else ''}"


def stock_added(context: Ctx, label_id: int, count: int, adder_id: int) -> str:
    """Note stock that was just added for the "🆕 New in the shop" message. Returns a
    line for whoever added it, saying whether customers will be told."""
    db = db_of(context)
    label = db.get_label(label_id)
    if label is None or label["price"] is None:
        return ""
    if db.disabled_on_path(label_id):
        return "📣 Customers aren't told: this product is disabled."
    if not db.shop_is_open():
        return "📣 Customers aren't told: the shop is closed."
    db.add_pending_stock(label_id, count, adder_id)
    schedule_new_stock(context.application)
    return f"📣 Customers get a message about the new stock {_soon()}."


def schedule_new_stock(app: Application) -> None:
    task = app.bot_data.get("new_stock_task")
    if task is not None and not task.done():
        return  # the coming message takes this stock along
    app.bot_data["new_stock_task"] = _start(app, _new_stock_later(app), "shopbot:new-stock")


def resume_new_stock(app: Application) -> None:
    """At start-up: stock noted before a restart is still announced."""
    if app.bot_data["db"].has_pending_stock():
        schedule_new_stock(app)


async def _wait_while_running(app: Application, seconds: float) -> bool:
    """Sleep ``seconds``; False if the bot stopped meanwhile."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    seen_running = app.running
    while (left := end - loop.time()) > 0:
        await asyncio.sleep(min(1.0, left))
        if app.running:
            seen_running = True
        elif seen_running:
            return False
    return app.running


async def _new_stock_later(app: Application) -> None:
    if await _wait_while_running(app, NEW_STOCK_DELAY):
        await send_new_stock(app)
    # else: stopping. The noted stock stays in the database and goes out after the restart.


async def send_new_stock(app: Application) -> Optional[Outcome]:
    db = app.bot_data["db"]
    discount = app.bot_data["settings"].seller_discount_percent
    batch = db.take_pending_stock()
    if not batch:
        return None
    if not db.shop_is_open():
        log.info("New stock not announced: the shop is closed")
        return None
    products = []
    for label_id, count in batch["labels"].items():
        label = db.get_label(label_id)
        if (label is None or label["price"] is None or db.has_children(label_id)
                or db.disabled_on_path(label_id) or not db.count_items(label_id)):
            continue  # gone, disabled or sold out again in the meantime
        products.append((label, count))
    users = db.broadcast_recipients(exclude=batch["adders"])
    if not products or not users:
        return None
    texts = {
        seller: _new_stock_text(db, products, seller, discount) for seller in (False, True)
    }
    keyboard = _new_stock_keyboard(products)

    async def send_one(user: Any) -> None:
        await app.bot.send_message(
            user["id"], texts[bool(user["is_seller"])], parse_mode=ParseMode.HTML,
            reply_markup=keyboard, link_preview_options=NO_PREVIEW,
        )

    out = await send_to_all(app, users, send_one, Outcome(total=len(users)))
    log.info("New-stock message: %s sent, %s unreachable, %s failed, %s not sent",
             out.sent, out.unreachable, out.failed, out.left)
    return out


def _new_stock_text(db: Any, products: list, seller: bool, discount: int) -> str:
    lines = ["🆕 <b>New in the shop!</b>", ""]
    for label, count in products[:NEW_STOCK_LISTED]:
        price = seller_price(label["price"], discount) if seller else label["price"]
        star = " ⭐" if seller and price != label["price"] else ""
        path = short(db.path_text(label["id"]), 90)  # deep label trees: stay within one message
        lines.append(f"🛒 {esc(path)} · <b>{money(price)}</b>{star} · {count} new")
    if len(products) > NEW_STOCK_LISTED:
        lines.append(f"… and {len(products) - NEW_STOCK_LISTED} more")
    lines += ["", "Tap a product to buy it, or open 🛍 Shop."]
    return "\n".join(lines)


def _new_stock_keyboard(products: list) -> Any:
    buttons = [btn(f"🛒 {short(label['name'], 24)}", f"sh:{label['id']}:0", GREEN)
               for label, _ in products[:NEW_STOCK_BUTTONS]]
    return markup(grid(buttons, 2) + [[btn("🛍 Shop", "sh:0:0"), menu_btn()]])
