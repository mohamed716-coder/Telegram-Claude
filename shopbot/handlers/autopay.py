"""💳 Add Balance, checked automatically with Binance Pay.

The customer chooses an amount, sends exactly that with Binance Pay to the shop's Binance
ID, and sends the payment's Order ID here. The bot finds the payment in the owner's Pay
history (shopbot/binance.py) and adds it when the coin and the amount match exactly.

A check that fails is reported to the owner and the customer is asked for the Order ID
again. When that fails too, the deposit becomes a request the owner approves or rejects.
So does a deposit Binance can't be asked about (the key is refused, or Binance can't be
reached for a while), and a payment the owner may have decided on already (a request with
an Order ID Binance didn't confirm came after it, or it was in a request that was decided).
Without a key, deposits go to the owner with a receipt (money.py).

Binance is asked in the background, so the bot keeps answering everyone meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Optional

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application

from ..binance import (BinanceError, BinanceKeyError, BinancePay, BinanceStopping, BinanceUnavailable,
                       Payment, check, clean_order_id)
from ..db import PaymentInDoubt, PaymentUsed, ShopError
from .common import (
    BLUE,
    GREEN,
    NO_PREVIEW,
    RED,
    Ctx,
    answer,
    btn,
    cfg_of,
    clear_state,
    db_of,
    esc,
    get_state,
    markup,
    menu_btn,
    money,
    notify,
    set_state,
    show,
    state_handler,
    try_show,
    user_html,
)

log = logging.getLogger("shopbot.autopay")

CHECK_GAP = 5.0  # seconds between two checks of one customer
TRIES = 2  # failed checks of one deposit before the owner decides
UNREACHABLE_TRIES = 2  # checks of one deposit while Binance can't be reached, before the owner
UNREACHABLE_FOR = 60.0  # decides; they must span this many seconds (short outages pass)
OWNER_ALERT_GAP = 6 * 3600  # seconds between two "automatic deposits don't work" alerts
CHECK_DEADLINE = 90.0  # seconds one check may ask Binance (a long search takes ~40)
RESTARTED = ("⚠️ The bot restarted while checking your payment. Please start again from "
             "💳 Add Balance in a minute.")


def client_of(context: Ctx) -> Optional[BinancePay]:
    return context.bot_data.get("binance")


def _checking(context: Ctx) -> set[int]:
    """The customers whose payment is being checked right now."""
    return context.bot_data.setdefault("binance_checking", set())


def _to_pay(context: Ctx, amount: int) -> str:
    """'25 USDT', or '25 USDT (or USDC)' when more coins are taken (1 coin = 1$)."""
    coins = cfg_of(context).binance_coins
    text = f"{amount} {coins[0]}"
    if len(coins) > 1:
        text += " (or " + ", ".join(coins[1:]) + ")"
    return text


def _paid(payment: Payment) -> str:
    """What was paid, e.g. '20 USDT' (no trailing zeros)."""
    return f"{format(payment.amount.normalize(), 'f')} {payment.currency}"


def _cancel_kb(again: bool = False) -> Any:
    rows = [[btn("🔄 Check again", "dck", BLUE)]] if again else []
    rows.append([btn("❌ Cancel", "cancel", RED)])
    return markup(rows)


# ------------------------------------------------------------------ the steps
async def ask_order_id(update: Update, context: Ctx, amount: int, *, new: bool = False) -> None:
    """After the customer chose an amount: how to pay it, then wait for the Order ID."""
    cfg = cfg_of(context)
    set_state(context, "deposit_order", amount=amount, order_id=None, tries=0, notes=[], found=[])
    pay = esc(_to_pay(context, amount))
    await show(
        update,
        context,
        f"💳 <b>Add {amount}$ to your balance</b>\n\n"
        f"1️⃣ In the Binance app: <b>Pay</b> → <b>Send</b> exactly <b>{pay}</b> to Binance ID "
        f"<code>{esc(cfg.binance_pay_id)}</code>.\n"
        "2️⃣ Open that payment in your Pay history and copy its <b>Order ID</b>.\n"
        "3️⃣ Send the Order ID here: your balance is added right away.\n\n"
        f"⚠️ The payment must be exactly {pay}, or it isn't added automatically.",
        _cancel_kb(),
        new=new,
    )


@state_handler("deposit_order")
async def on_order_id(update: Update, context: Ctx, state: dict) -> None:
    message = update.effective_message
    order_id = clean_order_id(message.text or "")  # type: ignore[union-attr]
    if order_id is None:
        await message.reply_text(  # type: ignore[union-attr]
            "🔎 That isn't an Order ID. Open the payment in your Binance Pay history, copy its "
            "Order ID (a long number like 466000199987322880) and send it here as text.",
            reply_markup=_cancel_kb(),
        )
        return
    if update.effective_user.id in _checking(context):  # type: ignore[union-attr]
        await message.reply_text("⏳ Still checking your last Order ID: the answer comes in a moment.")  # type: ignore[union-attr]
        return
    state["order_id"] = order_id
    await verify(update, context, state)


async def cb_check_again(update: Update, context: Ctx) -> None:
    query = update.callback_query
    state = get_state(context)
    if state is None or state.get("name") != "deposit_order" or not state.get("order_id"):
        await answer(query, "This check has expired. Start again from 💳 Add Balance.", show_alert=True)
        return
    if query.from_user.id in _checking(context):  # type: ignore[union-attr]
        await answer(query, "⏳ Still checking: the answer comes in a moment.")
        return
    await answer(query)
    await verify(update, context, state)


async def verify(update: Update, context: Ctx, state: dict) -> None:
    """Check the Order ID in ``state`` with Binance, in the background."""
    user_id = update.effective_user.id  # type: ignore[union-attr]
    new = update.callback_query is None  # a typed Order ID gets a new message
    last = context.user_data.get("binance_checked_at")  # type: ignore[union-attr]
    waited = CHECK_GAP if last is None else time.monotonic() - last
    if waited < CHECK_GAP:
        await show(update, context, f"⏳ Please wait {int(CHECK_GAP - waited) + 1} seconds, then press "
                   "🔄 Check again.", _cancel_kb(again=True), new=new)
        return
    context.user_data["binance_checked_at"] = time.monotonic()  # type: ignore[index]
    checking = _checking(context)
    checking.add(user_id)
    try:
        reply = await _Reply.start(update, context, state["order_id"], new)
        # Not Application.create_task: a stop waits for those without a limit, while
        # _post_stop gives checks a bounded time and then ends them (app.py).
        task = asyncio.get_running_loop().create_task(_check(update, context, state, reply),
                                                      name=f"shopbot:binance:{user_id}")
        tasks = context.bot_data.setdefault("binance_tasks", set())  # keep a reference
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    except BaseException:
        checking.discard(user_id)
        raise


class _Reply:
    """Where a check's answer goes: the "Checking ..." message, edited."""

    def __init__(self, update: Update, context: Ctx, message: Optional[Message]) -> None:
        self.update, self.context, self.message = update, context, message

    @classmethod
    async def start(cls, update: Update, context: Ctx, order_id: str, new: bool) -> "_Reply":
        text = f"🔎 Checking Order ID <code>{esc(order_id)}</code> with Binance ..."
        if not new:  # a button was pressed: its message says it
            await try_show(update, context, text)
            return cls(update, context, None)
        try:
            message = await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)  # type: ignore[union-attr]
        except TelegramError as exc:
            log.info("Could not say that a payment is being checked: %s", exc)
            message = None
        return cls(update, context, message)

    async def __call__(self, text: str, keyboard: Any = None) -> None:
        if self.message is not None:
            try:
                await self.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                             link_preview_options=NO_PREVIEW)
                return
            except TelegramError as exc:
                log.info("Could not edit the checking message, sending a new one: %s", exc)
        await try_show(self.update, self.context, text, keyboard,
                       new=self.update.callback_query is None)


async def _check(update: Update, context: Ctx, state: dict, reply: _Reply) -> None:
    user_id = update.effective_user.id  # type: ignore[union-attr]
    try:
        await _check_payment(update, context, state, reply)
    except asyncio.CancelledError:  # the bot is stopping and couldn't wait any longer
        try:
            await reply(RESTARTED, markup([[menu_btn()]]))
        finally:
            raise
    except Exception:  # never leave the customer at "Checking ..."
        log.exception("Checking a Binance payment failed")
        await reply("⚠️ Something went wrong while checking. Please press 🔄 Check again.",
                    _cancel_kb(again=True))
    finally:
        _checking(context).discard(user_id)


async def _check_payment(update: Update, context: Ctx, state: dict, reply: _Reply) -> None:
    db, cfg = db_of(context), cfg_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    order_id, amount = state["order_id"], state["amount"]
    tried = state.setdefault("tried", [])
    if order_id not in tried:  # for the owner's request: which Order IDs Binance confirmed
        tried.append(order_id)
    used = db.binance_payment(order_id)
    if used is not None:
        await _already_used(update, context, state, reply, used)
        return
    client = client_of(context)
    if client is None:  # the key was taken out meanwhile: the owner checks it
        await _to_owner(update, context, state, reply, f"{order_id}: not checked (no Binance key)")
        return
    since = max(db.binance_auto_since(), int(time.time() * 1000) - cfg.binance_max_age_hours * 3_600_000)
    try:
        try:
            async with asyncio.timeout(CHECK_DEADLINE):
                payment = await client.find(order_id, since)
        except TimeoutError:  # e.g. every host hangs: like an outage
            raise BinanceUnavailable("Binance took too long to answer") from None
    except BinanceUnavailable as exc:  # for now: the network, maintenance, too many requests
        if isinstance(exc, BinanceStopping) or getattr(client, "closed", False):
            # Not an outage: the bot is restarting (a request it cut off fails like the
            # network would), and it forgets this deposit.
            await reply(RESTARTED, markup([[menu_btn()]]))
            return
        log.warning("Couldn't check a payment with Binance: %s", exc)
        await _unreachable(update, context, state, reply, exc)
        return
    except BinanceError as exc:  # until it's fixed: the key, the server's location, Binance's answers
        log.error("Automatic deposits don't work: %s", exc)
        await _alert_owner(context, f"⚠️ <b>Automatic deposits don't work</b>: {esc(exc)}\n"
                                    "Deposits come to you to approve until it's fixed.")
        why = "Binance refused the API key" if isinstance(exc, BinanceKeyError) else str(exc)
        await _to_owner(update, context, state, reply, f"{order_id}: not checked ({why})")
        return
    db.note_binance_clock(getattr(client, "offset_ms", 0))  # to compare Binance's times with ours
    state.pop("unreachable", None)  # Binance answered: an outage is over
    if payment is None:
        await _failed(
            update, context, state, reply,
            f"I can't find a payment with Order ID <code>{esc(order_id)}</code> to Binance ID "
            f"<code>{esc(cfg.binance_pay_id)}</code> in the last {cfg.binance_max_age_hours} hours.",
            "not found in your Binance Pay history", again=True,
        )
        return
    used = db.binance_payment(payment.order_id, payment.transaction_id)  # sent by its other ID?
    if used is not None:
        await _already_used(update, context, state, reply, db.link_payment(used, payment))
        return
    if not payment.received:
        await _failed(update, context, state, reply, "This Order ID isn't a payment to this shop.",
                      "not a payment to the shop")
        return
    if payment.currency not in cfg.binance_coins or payment.amount != Decimal(amount):
        state["found"].append(payment)  # real money: kept for the owner if this ends there
        await _failed(
            update, context, state, reply,
            f"This payment is <b>{esc(_paid(payment))}</b>, but you chose to add <b>{amount}$</b> "
            f"({esc(_to_pay(context, amount))}).",
            f"is {_paid(payment)}, not {amount} {cfg.binance_coins[0]}",
        )
        return
    try:
        decision = db.credit_binance_payment(user_id, payment, amount * 100,
                                             cfg.seller_auto_threshold * 100)
    except PaymentUsed as exc:  # claimed a moment earlier
        await _already_used(update, context, state, reply, exc.row)
        return
    except PaymentInDoubt as exc:  # the owner may have approved or settled it by hand already
        log.info("Payment %s may be decided already (deposits %s): the owner decides", order_id,
                 ", ".join(str(r["id"]) for r in exc.requests))
        state["found"].append(payment)  # kept for the owner's decision (the request says why)
        await _to_owner(update, context, state, reply,
                        f"{order_id}: paid before automatic checking started" if exc.early and not exc.requests
                        else f"{order_id}: may have been added already")
        return
    except ShopError as exc:
        _done(context, state)
        await reply(f"⚠️ {esc(exc)}", markup([[menu_btn()]]))
        return
    _done(context, state)
    deposit, user = decision.deposit, decision.user
    text = (f"✅ <b>Payment received!</b>\n\n💵 +{money(deposit['amount'])} ({esc(_paid(payment))})\n"
            f"💰 New balance: <b>{money(user['balance'])}</b>")
    if decision.became_seller:
        text += (f"\n\n⭐ Congratulations! Your total deposits reached {cfg.seller_auto_threshold}$, "
                 f"so you're now a <b>Seller</b> with <b>{cfg.seller_discount_percent}% off</b> "
                 "all products.")
    await reply(text, markup([[btn("🛍 Shop", "sh:0:0", GREEN), menu_btn()]]))
    closed = "".join(f"\n🧾 Request #{request_id} was waiting with this payment: closed."
                     for request_id in decision.closed)
    await notify(context, cfg.owner_id,
                 f"💳 <b>Deposit added automatically</b>\n👤 {user_html(user)}\n"
                 f"💵 +{money(deposit['amount'])} ({esc(_paid(payment))}"
                 f"{', from ' + esc(payment.payer) if payment.payer else ''})\n"
                 f"🧾 Order ID <code>{esc(payment.order_id or payment.transaction_id)}</code>" + closed)


async def _already_used(update: Update, context: Ctx, state: dict, reply: _Reply, row: Any) -> None:
    """The payment is in the bot's records already (``row``, from binance_payments)."""
    if row["user_id"] != update.effective_user.id:  # type: ignore[union-attr]
        await _failed(update, context, state, reply, "This Order ID was already used.",
                      "already used by someone else")
        return
    request = db_of(context).get_deposit(row["deposit_id"]) if row["status"] == "owner" else None
    if request is not None and request["status"] != "pending":
        # Kept for a request decided without adding it (rejected for its amount, approved
        # for another payment it held, closed): never added automatically; the owner
        # decides again, told which request had it.
        verb = {"approved": "you approved", "rejected": "you rejected"}.get(request["status"], "was closed")
        await _to_owner(update, context, state, reply,
                        f"{state['order_id']}: was in request #{request['id']}, which {verb}")
        return
    _done(context, state)
    await reply(_used_text(row), markup([[menu_btn()]]))


async def _unreachable(update: Update, context: Ctx, state: dict, reply: _Reply, exc: Exception) -> None:
    """Binance can't be reached right now. That usually passes by itself, so it's not a
    failed try; when it lasts, the owner checks the payment by hand."""
    if get_state(context) is not state:  # cancelled while Binance was asked
        await reply("❌ Cancelled: Binance couldn't be reached, and this deposit wasn't sent to "
                    "the owner.", markup([[menu_btn()]]))
        return
    times = state.setdefault("unreachable", [])
    times.append(time.monotonic())
    if len(times) >= UNREACHABLE_TRIES and times[-1] - times[0] >= UNREACHABLE_FOR:
        await _to_owner(update, context, state, reply,
                        f"{state['order_id']}: not checked (Binance couldn't be reached: {exc})")
        return
    await reply("⏳ Binance can't be reached right now. Please press 🔄 Check again in a minute: "
                "if it still can't be reached then, the owner checks your payment by hand.",
                _cancel_kb(again=True))


async def _failed(update: Update, context: Ctx, state: dict, reply: _Reply, reason: str, why: str,
                  *, again: bool = False) -> None:
    """A check that didn't work out. The owner hears of it and the customer sends the Order
    ID again; after the last try the deposit goes to the owner to approve or reject.
    ``reason`` is for the customer (HTML), ``why`` for the owner (text)."""
    db, cfg = db_of(context), cfg_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    if get_state(context) is not state:  # cancelled while Binance was asked: just say why
        await reply(f"❌ {reason}", markup([[menu_btn()]]))
        return
    state["tries"] = state.get("tries", 0) + 1
    state.setdefault("notes", []).append(f"{state['order_id']}: {why}")
    if state["tries"] >= TRIES:
        await _to_owner(update, context, state, reply)
        return
    await notify(
        context, cfg.owner_id,
        f"⚠️ <b>Deposit check failed</b> (try {state['tries']} of {TRIES})\n"
        f"👤 {user_html(db.get_user(user_id))}\n"
        f"💵 Wants to add: <b>{state['amount']}$</b>\n"
        f"🧾 Order ID: <code>{esc(state['order_id'])}</code>\n"
        f"❌ {esc(why)}\n\n"
        "They were asked for the Order ID again. If that fails too, the deposit comes to you "
        "to approve or reject.",
    )
    await reply(
        f"❌ {reason}\n\nPlease check it and send the Order ID again. If it doesn't work this "
        "time either, the owner checks your payment by hand.",
        _cancel_kb(again=again),
    )


async def _to_owner(update: Update, context: Ctx, state: dict, reply: _Reply, note: str = "") -> None:
    """The bot can't add this deposit: it becomes a request the owner approves or rejects.
    Payments Binance did find are kept for it, so nobody can claim them meanwhile."""
    from .money import send_deposit_to_owner  # local import: money imports this module

    db = db_of(context)
    user_id = update.effective_user.id  # type: ignore[union-attr]
    if get_state(context) is not state:  # cancelled while Binance was asked
        await reply("❌ Cancelled: this deposit wasn't sent to the owner.", markup([[menu_btn()]]))
        return
    notes = list(state.get("notes", [])) + ([note] if note else [])
    found = [p for p in state.get("found", []) if p.received]
    deposit = db.create_review_request(user_id, state["amount"] * 100, state["order_id"],
                                       "\n".join(notes), found, state.get("tried", []))
    _done(context, state)
    reached = await send_deposit_to_owner(context, deposit)
    text = (f"🧾 <b>Sent to the owner</b>\n\nYour deposit of <b>{state['amount']}$</b> couldn't be "
            f"checked automatically, so the owner checks it by hand (request #{deposit['id']}).\n"
            "You'll get a message here as soon as it's decided.")
    if not reached:
        text += "\n\nℹ️ The owner couldn't be notified right now, but your request is saved and waiting."
    await reply(text, markup([[menu_btn()]]))


def _done(context: Ctx, state: dict) -> None:
    """This deposit is settled: stop waiting for an Order ID, unless the customer moved on."""
    if get_state(context) is state:
        clear_state(context)


def _used_text(row: Any) -> str:
    if row["status"] == "credited":
        return "✅ This payment was already added to your balance."
    return "🧾 This payment is already with the owner, who decides on it."


async def _alert_owner(context: Ctx, text: str) -> None:
    last = context.bot_data.get("binance_alert_at")  # None: never alerted yet
    if last is not None and time.monotonic() - last < OWNER_ALERT_GAP:
        return
    context.bot_data["binance_alert_at"] = time.monotonic()
    await notify(context, cfg_of(context).owner_id, text)


async def check_at_start(app: Application) -> None:
    """At start-up: tell the owner if the Binance key doesn't work or can do too much."""
    settings = app.bot_data["settings"]
    report = await check(settings.binance_api_key, settings.binance_api_secret,
                         binance_id=settings.binance_pay_id)
    log.info(report.replace("\n", " "))
    if not report.startswith("Binance: the key works") or "WARNING" in report:
        try:
            await app.bot.send_message(settings.owner_id, "💳 " + esc(report), parse_mode=ParseMode.HTML)
        except Exception as exc:  # best effort: the log has it too
            log.info("Could not tell the owner about the Binance key: %s", exc)
