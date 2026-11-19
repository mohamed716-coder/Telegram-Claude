"""📣 Messages to every user: /announce (owner) and "🆕 New in the shop" after stock is added.

Both go to everyone who has started the bot, except the owner and banned users (checked
again right before each message). A broadcast is saved in the database while it goes
out, with how far it got, so a restart (e.g. a deploy) continues it instead of losing
it. Messages go out in the background, one broadcast at a time, at about 15 a second
(Telegram allows about 30); a "too many requests" answer or a network outage pauses it
instead of skipping people.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Awaitable, Callable, Optional

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
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
    edit_hook,
    esc,
    grid,
    is_owner,
    markup,
    menu_btn,
    money,
    set_state,
    short,
    show,
    state_handler,
    try_show,
    utf16_len,
)
from .owner import owner_command
from .stock import content_problem

log = logging.getLogger("shopbot.broadcast")

SEND_INTERVAL = 1 / 15  # seconds per message (Telegram allows about 30 a second)
PAGE_SIZE = 200  # recipients read from the database at a time
SAVE_EVERY = 10  # messages between progress saves: after a crash at most this many repeat
PROGRESS_EVERY = 30.0  # seconds between progress updates to the owner
RECHECK_EVERY = 100  # stock message: re-check the shop and the products every N users
NETWORK_BACKOFF = 2.0  # first wait (s) when Telegram can't be reached; doubles up to 60
NETWORK_GIVE_UP = 30 * 60  # seconds of retrying one user through an outage before moving on
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
EXPIRED = ("This announcement has expired: it was sent, cancelled or replaced, or the bot "
           "restarted. Nothing more was sent; send /announce again if needed.")


# ------------------------------------------------------------------ the jobs
def _lock(app: Application) -> asyncio.Lock:
    return app.bot_data.setdefault("broadcast_lock", asyncio.Lock())


def running_job(app: Application) -> Optional[dict]:
    """The broadcast going out now (or about to), if any."""
    return app.bot_data.get("broadcast_running")


def _new_job(kind: str, total: int, **data: Any) -> dict:
    return {"id": secrets.token_hex(4), "kind": kind, "after": 0, "total": total, "sent": 0,
            "unreachable": 0, "failed": 0, "skipped": 0, **data}


def done_count(job: dict) -> int:
    return job["sent"] + job["unreachable"] + job["failed"] + job["skipped"]


def _start(app: Application, coroutine: Any, name: str) -> asyncio.Task:
    if app.running:  # Application.stop() then waits for it
        task = app.create_task(coroutine, name=name)
    else:  # e.g. while starting up; _post_stop waits for it
        task = asyncio.create_task(coroutine, name=name)
    app.bot_data.setdefault("broadcast_tasks", set()).add(task)
    task.add_done_callback(app.bot_data["broadcast_tasks"].discard)
    return task


async def _until_running(app: Application, timeout: float = 120.0) -> bool:
    """At start-up: wait until the bot runs (post_init comes before that)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not app.running:
        if loop.time() > end:
            return False
        await asyncio.sleep(0.2)
    return True


async def _sleep_while_running(app: Application, seconds: float) -> bool:
    """Sleep ``seconds``; False as soon as the bot is stopping."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while (left := end - loop.time()) > 0:
        if not app.running:
            return False
        await asyncio.sleep(min(1.0, left))
    return app.running


async def _deliver(app: Application, send: Callable[[], Awaitable[Any]]) -> str:
    """Send one message: 'sent', 'unreachable', 'failed' or 'stopping'."""
    loop = asyncio.get_running_loop()
    give_up = loop.time() + NETWORK_GIVE_UP
    backoff = NETWORK_BACKOFF
    while True:
        try:
            await send()
            return "sent"
        except Forbidden:  # blocked the bot, or deleted their account
            return "unreachable"
        except (RetryAfter, FloodWait) as exc:  # pause the whole broadcast, then go on
            seconds = wait_seconds(exc)
            log.warning("Telegram asked to wait %.0f s: the broadcast pauses", seconds)
            if not await _sleep_while_running(app, seconds + 1):
                return "stopping"
        except TimedOut:  # it may well have arrived: never send it twice
            return "sent"
        except BadRequest as exc:  # e.g. "chat not found": this user only
            log.info("A broadcast message failed: %s", exc)
            return "failed"
        except NetworkError as exc:  # Telegram can't be reached: wait for it
            if loop.time() + backoff > give_up:
                return "failed"
            log.warning("Can't reach Telegram (%s): the broadcast tries again in %.0f s", exc, backoff)
            if not await _sleep_while_running(app, backoff):
                return "stopping"
            backoff = min(backoff * 2, 60.0)
        except (TelegramError, ValueError, KeyError) as exc:
            log.info("A broadcast message failed: %s", exc)
            return "failed"


async def _run(app: Application, job: dict) -> str:
    """Send ``job`` to every recipient after job["after"] (the lock is held). Returns
    "done", "stopped" (the owner pressed Stop), "ended" (a stock message with nothing
    left to tell) or "stopping" (the bot is stopping: the job stays saved)."""
    db = app.bot_data["db"]
    loop = asyncio.get_running_loop()
    next_at = loop.time()
    last_progress = loop.time()
    labels: dict[int, Any] = {}
    unsaved = handled = 0
    outcome = "done"
    try:
        while outcome == "done":
            page = db.broadcast_page(job["after"], PAGE_SIZE)
            if not page:
                break
            for user in page:
                if not app.running:
                    outcome = "stopping"
                    break
                if app.bot_data.get("broadcast_stop") == job["id"]:
                    outcome = "stopped"
                    break
                if job["kind"] == "stock" and handled % RECHECK_EVERY == 0:
                    labels = _recheck_stock(db, job)
                    if not labels:
                        outcome = "ended"
                        break
                handled += 1
                send = _message_for(app, job, user, labels)
                if send is None or db.is_banned(user["id"]):
                    result = "skipped"
                else:
                    now = loop.time()
                    if next_at > now:  # an even pace, whatever each request takes
                        await asyncio.sleep(next_at - now)
                    elif now - next_at > 1.0:  # after a pause: don't rush to catch up
                        next_at = now
                    next_at += SEND_INTERVAL
                    result = await _deliver(app, send)
                    if result == "stopping":
                        outcome = "stopping"
                        break
                job[result] += 1
                job["after"] = user["id"]
                unsaved += 1
                if unsaved >= SAVE_EVERY:
                    db.save_broadcast_job(job)
                    unsaved = 0
                if job["kind"] == "announce" and loop.time() - last_progress >= PROGRESS_EVERY:
                    last_progress = loop.time()
                    await _show_progress(app, job)
    except Exception:  # a bug must not leave a job behind that crashes every restart
        log.exception("A broadcast stopped because of an error")
        outcome = "error"
    if outcome == "stopping":
        db.save_broadcast_job(job)  # continues after the restart
        log.info("Stopping: the broadcast continues after the restart (%s of %s done)",
                 done_count(job), job["total"])
    else:
        db.clear_broadcast_job()
    return outcome


async def _run_job(app: Application, job: dict) -> str:
    """Take the lock (one broadcast at a time) and send ``job``."""
    db = app.bot_data["db"]
    app.bot_data["broadcast_running"] = job
    try:
        async with _lock(app):
            app.bot_data["broadcast_running"] = job
            db.save_broadcast_job(job)
            return await _run(app, job)
    finally:
        if app.bot_data.get("broadcast_running") is job:
            app.bot_data.pop("broadcast_running", None)
        if app.bot_data.get("broadcast_stop") == job["id"]:
            app.bot_data.pop("broadcast_stop", None)


def resume_broadcasts(app: Application) -> None:
    """At start-up: continue a broadcast a restart interrupted, and announce stock that
    was noted before it."""
    db = app.bot_data["db"]
    job = db.get_broadcast_job()
    if job is not None:
        _start(app, _resume(app, job), "shopbot:broadcast")
    elif db.has_pending_stock():
        schedule_new_stock(app)


async def _resume(app: Application, job: dict) -> None:
    if not await _until_running(app):
        return
    log.info("Continuing a broadcast that a restart interrupted (%s of %s done)",
             done_count(job), job["total"])
    if job["kind"] == "announce":
        await _tell_owner(app, job, f"📣 The bot restarted while sending your announcement. It "
                                    f"goes on now ({done_count(job)} of {job['total']} done).")
        await _announce(app, job)
    else:
        await _run_job(app, job)
    if app.bot_data["db"].has_pending_stock():
        schedule_new_stock(app)


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
        end = entity.offset + entity.length
        if entity.type == "bot_command" or end <= shift:
            continue
        begin = max(entity.offset, shift)  # formatting that starts on the command: clip it
        item = entity.to_dict()
        item["offset"], item["length"] = begin - shift, end - begin
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
    await _preview(update, context, content, message.message_id)  # type: ignore[union-attr]


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
    await _preview(update, context, content, message.message_id)  # type: ignore[arg-type, union-attr]


async def _preview(update: Update, context: Ctx, content: dict, source: int) -> None:
    """Show the owner the announcement exactly as users will get it, then ask."""
    count = db_of(context).count_broadcast_recipients()
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not count:
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
    context.user_data["announce"] = {  # type: ignore[index]
        "nonce": nonce, "content": content, "source": source,
    }
    await context.bot.send_message(
        chat_id,
        f"📣 <b>Send the message above to {count} user(s)?</b>\n"
        "That's everyone who has started the bot, except you and banned users.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup([[
            btn(f"✅ Send to {count}", f"an:{nonce}", GREEN),
            btn("❌ Cancel", f"anc:{nonce}", RED),
        ]]),
    )


@edit_hook
async def on_announce_edit(update: Update, context: Ctx) -> bool:
    """An edit of the message being announced: say that the preview is what goes out."""
    message = update.edited_message
    pending = (context.user_data or {}).get("announce")
    if message is None or not isinstance(pending, dict) or pending.get("source") != message.message_id:
        return False
    await message.reply_text(
        "✏️ That edit isn't used: the preview above is what will be sent. To change it, press "
        "❌ Cancel and send /announce again."
    )
    return True


def _busy_text(job: dict) -> str:
    what = "The last announcement" if job["kind"] == "announce" else "A “New in the shop” message"
    return (f"📣 {what} is still going out ({done_count(job)} of {job['total']}). Press Send "
            "again when it's finished.")


def _stop_kb(job: dict) -> Any:
    return markup([[btn("⏹ Stop sending", f"ans:{job['id']}", RED)]])


async def cb_announce_send(update: Update, context: Ctx) -> None:
    query = update.callback_query
    if not is_owner(context, query.from_user.id):
        await answer(query, "Only the owner can send announcements.", show_alert=True)
        return
    nonce = (query.data or "").partition(":")[2]
    pending = context.user_data.get("announce")  # type: ignore[union-attr]
    if not (isinstance(pending, dict) and nonce and pending.get("nonce") == nonce):
        await answer(query, EXPIRED, show_alert=True)
        return
    app = context.application
    busy = running_job(app)
    if busy is not None:
        await answer(query, _busy_text(busy), show_alert=True)
        return
    context.user_data.pop("announce", None)  # type: ignore[union-attr]
    job = _new_job(
        "announce", db_of(context).count_broadcast_recipients(), content=pending["content"],
        owner_chat=query.from_user.id,
        status_msg=query.message.message_id if query.message is not None else None,
    )
    app.bot_data["broadcast_running"] = job  # from now on, a second Send is refused
    await answer(query, "📣 Sending…")
    _start(app, _announce(app, job), "shopbot:announce")  # before anything that can fail
    await try_show(update, context, f"📣 Sending the announcement to {job['total']} user(s)…",
                   _stop_kb(job))


async def _announce(app: Application, job: dict) -> None:
    outcome = await _run_job(app, job)
    if outcome != "stopping":
        await _finish_announce(app, job, outcome)


async def _show_progress(app: Application, job: dict) -> None:
    if not job.get("status_msg"):
        return
    try:
        await app.bot.edit_message_text(
            f"📣 Sending the announcement… {done_count(job)} of {max(job['total'], done_count(job))}",
            chat_id=job["owner_chat"], message_id=job["status_msg"], reply_markup=_stop_kb(job),
        )
    except TelegramError as exc:
        log.info("Could not update the announcement progress: %s", exc)


async def _tell_owner(app: Application, job: dict, text: str) -> None:
    try:
        await app.bot.send_message(job["owner_chat"], text, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        log.info("Could not tell the owner about the announcement: %s", exc)


async def _finish_announce(app: Application, job: dict, outcome: str) -> None:
    if job.get("status_msg"):
        try:
            await app.bot.edit_message_text(
                "📣 Announcement " + ("stopped." if outcome == "stopped" else "finished."),
                chat_id=job["owner_chat"], message_id=job["status_msg"],
            )
        except TelegramError as exc:
            log.info("Could not update the announcement status: %s", exc)
    await _tell_owner(app, job, announce_summary(job, outcome))


def announce_summary(job: dict, outcome: str = "done") -> str:
    total = max(job["total"], done_count(job))
    if outcome == "stopped":
        lines = [f"⏹ <b>Announcement stopped</b>: {job['sent']} of {total} user(s) got it."]
    elif outcome == "error":
        lines = [f"⚠️ <b>The announcement stopped because of an error</b> (see the bot's log): "
                 f"{job['sent']} of {total} user(s) got it."]
    elif job["sent"] == total:
        lines = [f"📣 <b>Announcement sent</b> to all {total} user(s)."]
    else:
        lines = [f"📣 <b>Announcement sent</b> to {job['sent']} of {total} user(s)."]
    if job["unreachable"]:
        lines.append(f"🚫 {job['unreachable']} blocked the bot or deleted their account.")
    if job["failed"]:
        lines.append(f"⚠️ {job['failed']} couldn't get it because of an error (see the bot's log).")
    if job["skipped"]:
        lines.append(f"⛔ {job['skipped']} were banned in the meantime and didn't get it.")
    return "\n".join(lines)


async def cb_announce_cancel(update: Update, context: Ctx) -> None:
    """anc:<nonce> cancels that preview; a plain anc is the prompt's Cancel."""
    query = update.callback_query
    nonce = (query.data or "").partition(":")[2]
    if nonce:
        pending = context.user_data.get("announce")  # type: ignore[union-attr]
        if not (isinstance(pending, dict) and pending.get("nonce") == nonce):
            await answer(query, EXPIRED, show_alert=True)
            return
        context.user_data.pop("announce", None)  # type: ignore[union-attr]
        text = "❌ Announcement cancelled. Nothing was sent."
    else:
        clear_state(context)
        text = "❌ Cancelled."
    busy = running_job(context.application)
    if busy is not None and busy["kind"] == "announce":
        text += (f"\n📣 The announcement already going out goes on ({done_count(busy)} of "
                 f"{busy['total']}): press ⏹ Stop sending on it to stop it.")
    await answer(query, "Cancelled")
    await show(update, context, text, markup([[menu_btn()]]))


async def cb_announce_stop(update: Update, context: Ctx) -> None:
    query = update.callback_query
    if not is_owner(context, query.from_user.id):
        await answer(query, "Only the owner can stop announcements.", show_alert=True)
        return
    job_id = (query.data or "").partition(":")[2]
    busy = running_job(context.application)
    if busy is None or busy["id"] != job_id:
        await answer(query, "This announcement has already finished.", show_alert=True)
        return
    context.bot_data["broadcast_stop"] = job_id
    await answer(query, "⏹ Stopping…")


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
        return  # it keeps going until nothing is noted any more
    app.bot_data["new_stock_task"] = _start(app, _stock_loop(app), "shopbot:new-stock")


async def _stock_loop(app: Application) -> None:
    db = app.bot_data["db"]
    if not await _until_running(app):
        return
    while db.has_pending_stock():  # stock noted while a message was going out goes next
        if not await _sleep_while_running(app, NEW_STOCK_DELAY):
            return  # stopping: the noted stock stays saved and goes out after the restart
        await send_new_stock(app)


async def send_new_stock(app: Application) -> Optional[dict]:
    """Send one "New in the shop" message about everything noted so far. Waits for a
    broadcast that is going out, then decides with what is true at that moment."""
    db = app.bot_data["db"]
    async with _lock(app):
        batch = db.take_pending_stock()
        if not batch:
            return None
        if not db.shop_is_open():
            log.info("New stock not announced: the shop is closed")
            return None
        products = [{"label_id": label_id, "count": entry["count"], "adders": entry["adders"]}
                    for label_id, entry in batch.items()]
        job = _new_job("stock", db.count_broadcast_recipients(), products=products)
        if not _recheck_stock(db, job):
            return None  # everything is gone, disabled or sold out again
        app.bot_data["broadcast_running"] = job
        try:
            db.save_broadcast_job(job)
            outcome = await _run(app, job)
        finally:
            if app.bot_data.get("broadcast_running") is job:
                app.bot_data.pop("broadcast_running", None)
    log.info("New-stock message (%s): %s sent, %s unreachable, %s failed, %s skipped",
             outcome, job["sent"], job["unreachable"], job["failed"], job["skipped"])
    return job


def _recheck_stock(db: Any, job: dict) -> dict[int, Any]:
    """Keep only the products customers can still buy; {label id: row}, empty if none
    (or the shop closed meanwhile)."""
    if not db.shop_is_open():
        job["products"] = []
        return {}
    keep, rows = [], {}
    for product in job["products"]:
        label = db.get_label(product["label_id"])
        if (label is None or label["price"] is None or db.has_children(label["id"])
                or db.disabled_on_path(label["id"]) or not db.count_items(label["id"])):
            continue
        keep.append(product)
        rows[label["id"]] = label
    job["products"] = keep
    return rows


def _message_for(app: Application, job: dict, user: Any,
                 labels: dict[int, Any]) -> Optional[Callable[[], Awaitable[Any]]]:
    """What ``user`` gets from this broadcast, or None if nothing."""
    bot = app.bot
    if job["kind"] == "announce":
        content = job["content"]
        return lambda: send_content(bot, user["id"], content)
    # A stock message leaves out the products this user added themselves.
    products = [(labels[p["label_id"]], p["count"]) for p in job["products"]
                if p["label_id"] in labels and user["id"] not in p["adders"]]
    if not products:
        return None
    discount = app.bot_data["settings"].seller_discount_percent
    text = _new_stock_text(app.bot_data["db"], products, bool(user["is_seller"]), discount)
    keyboard = _new_stock_keyboard(products)
    return lambda: bot.send_message(
        user["id"], text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        link_preview_options=NO_PREVIEW,
    )


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
