"""💳 Automatic deposits: the customer pays with Binance Pay and sends the Order ID; the
bot finds the payment in the owner's Pay history and adds it, once. Binance is replaced by
a stand-in here (the real client is tested at the bottom with a fake HTTP server)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest

from harness import OWNER, ShopTest, run
from shopbot import binance as binance_module
from shopbot.binance import (BinanceError, BinanceKeyError, BinancePay, BinanceUnavailable, Payment,
                             clean_order_id, order_id_time)
from shopbot.db import Database, PaymentInDoubt, PaymentUsed
from shopbot.handlers import autopay as autopay_module
from test_shop import ALICE, BOB, register

ORDER = "466000199987322880"  # an Order ID as the Binance app shows it (Nov 2026)
TX = "P_A25PQJVAD2171114"  # Binance's other id for the same transfer


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(autopay_module, "CHECK_GAP", 0)


def pay(order_id: str = ORDER, amount: str = "25", currency: str = "USDT", kind: str = "C2C",
        minutes_ago: float = 5, tx: str = TX, payer: str = "Ali") -> Payment:
    return Payment(order_id, tx, kind, Decimal(amount), currency,
                   int((time.time() - minutes_ago * 60) * 1000), payer, "")


class FakeBinance:
    """Stands in for BinancePay: knows some payments, or fails the way Binance can."""

    def __init__(self, *payments: Payment, error: Optional[Exception] = None) -> None:
        self.payments = list(payments)
        self.error = error
        self.asked: list[tuple[str, int]] = []

    async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
        self.asked.append((wanted, since_ms))
        if self.error is not None:
            raise self.error
        for payment in self.payments:
            if wanted in (payment.order_id, payment.transaction_id.upper()) and payment.time_ms >= since_ms:
                return payment
        return None

    async def close(self) -> None:
        pass


def with_binance(t: ShopTest, fake: FakeBinance) -> FakeBinance:
    t.app.bot_data["binance"] = fake
    t.db.binance_auto_since()  # automatic checking starts now
    t.db.set_setting("binance_auto_since", str(int((time.time() - 3600) * 1000)))
    return fake


def texts(calls: list, user: dict) -> list[str]:
    return [c.text or "" for c in ShopTest.messages(calls, user["id"])]


def balance(t: ShopTest, user: dict) -> int:
    return t.db.get_user(user["id"])["balance"]


# ================================================================ the flow
OTHER = "466000199987322881"  # another Order ID


async def choose(t: ShopTest, user: dict, amount: int) -> list:
    """💳 Add Balance → an amount: the screen that asks for the payment and its Order ID."""
    await t.press(user, "dp")
    return await t.press(user, f"da:{amount}")


def owner_texts(calls: list) -> list[str]:
    return texts(calls, OWNER)


def test_customer_chooses_an_amount_pays_exactly_that_and_the_order_id_adds_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25.00000000")))
        register(t, ALICE)
        calls = await t.press(ALICE, "dp")
        assert "Choose how much you want to add" in t.last_text(calls)
        calls = await t.press(ALICE, "da:25")
        screen = t.last_text(calls)
        assert "exactly <b>25 USDT</b>" in screen and "Binance ID <code>850940840</code>" in screen
        assert "Order ID" in screen
        calls = await t.send(ALICE, f"  {ORDER} ")
        shown = texts(calls, ALICE)
        assert any("Checking Order ID" in x for x in shown)
        assert "Payment received!" in shown[-1] and "+25.00$" in shown[-1] and "25 USDT" in shown[-1]
        assert balance(t, ALICE) == 2500 and t.db.get_user(ALICE["id"])["total_deposits"] == 2500
        owner = owner_texts(calls)[-1]
        assert "Deposit added automatically" in owner and "+25.00$" in owner and ORDER in owner
        deposit = t.db.conn.execute("SELECT * FROM deposits WHERE user_id = ?", (ALICE["id"],)).fetchone()
        assert (deposit["method"], deposit["status"], deposit["amount"]) == ("binance_auto", "approved", 2500)
        assert t.db.binance_payment(ORDER)["status"] == "credited"

    run(tmp_path, scenario)


def test_a_wrong_amount_tells_the_owner_asks_again_and_then_the_owner_decides(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        mine = texts(calls, ALICE)[-1]
        assert "This payment is <b>20 USDT</b>, but you chose to add <b>25$</b>" in mine
        assert "send the Order ID again" in mine and not t.has_button(calls, "Check again")
        owner = owner_texts(calls)[-1]
        assert "Deposit check failed" in owner and "try 1 of 2" in owner and "25$" in owner
        assert ORDER in owner and "is 20 USDT, not 25 USDT" in owner
        assert balance(t, ALICE) == 0 and t.db.pending_deposits() == []
        # The second try fails too: now it's a request for the owner.
        calls = await t.send(ALICE, ORDER)
        mine = texts(calls, ALICE)[-1]
        assert "Sent to the owner" in mine and "checks it by hand" in mine
        (deposit,) = t.db.pending_deposits()
        assert (deposit["amount"], deposit["order_id"]) == (2500, ORDER)
        request = [x for x in owner_texts(calls) if "Deposit request" in x][-1]
        assert "Binance has: <b>20 USDT</b>" in request and "Not added automatically" in request
        assert request.count("is 20 USDT, not 25 USDT") == 2
        assert t.button(calls, "Approve") == f"dok:{deposit['id']}"
        assert not [x for x in owner_texts(calls) if "Deposit check failed" in x]
        # While the owner decides, nobody can claim that payment.
        await choose(t, ALICE, 20)
        assert "already with the owner" in texts(await t.send(ALICE, ORDER), ALICE)[-1]
        await choose(t, BOB, 20)
        assert "already used" in texts(await t.send(BOB, ORDER), BOB)[-1]
        assert balance(t, BOB) == 0
        calls = await t.press(OWNER, f"dok:{deposit['id']}")
        assert balance(t, ALICE) == 2500 and t.db.binance_payment(ORDER)["status"] == "credited"
        assert "Deposit approved!" in texts(calls, ALICE)[-1]

    run(tmp_path, scenario)


def test_the_right_order_id_on_the_second_try_is_added(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25"), pay(order_id=OTHER, amount="30", tx="P_OTHER")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, OTHER)
        assert "This payment is <b>30 USDT</b>" in texts(calls, ALICE)[-1]
        calls = await t.send(ALICE, ORDER)
        assert "Payment received!" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2500
        # The other payment wasn't kept from its owner (it never went to the shop's owner).
        await choose(t, BOB, 30)
        assert "Payment received!" in texts(await t.send(BOB, OTHER), BOB)[-1]
        assert balance(t, BOB) == 3000

    run(tmp_path, scenario)


def test_not_found_is_a_failed_try_that_can_be_checked_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "can't find a payment" in texts(calls, ALICE)[-1] and t.has_button(calls, "Check again")
        assert "not found in your Binance Pay history" in owner_texts(calls)[-1]
        fake.payments.append(pay(amount="25"))  # it shows up at Binance
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        assert "Payment received!" in t.last_text(calls, ALICE["id"]) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_two_tries_without_a_payment_go_to_the_owner_who_can_reject(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance())
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        calls = await t.send(ALICE, OTHER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1]
        (deposit,) = t.db.pending_deposits()
        assert deposit["order_id"] == OTHER and deposit["note"].count("not found") == 2
        request = [x for x in owner_texts(calls) if "Deposit request" in x][-1]
        assert ORDER in request and OTHER in request and "Binance has" not in request
        again = [x for x in owner_texts(await t.send(OWNER, "/pending")) if "Deposit request" in x]
        assert again == [request]  # /pending shows the same details
        calls = await t.press(OWNER, f"dno:{deposit['id']}")
        assert "was rejected" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 0

    run(tmp_path, scenario)


def test_a_request_for_a_payment_added_meanwhile_cannot_be_approved(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, ORDER)
        (deposit,) = t.db.pending_deposits()
        fake.payments.append(pay(amount="25"))  # Binance shows it now: added automatically
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Payment received!" in texts(calls, ALICE)[-1]
        assert f"Request #{deposit['id']} was waiting with this payment: closed." in owner_texts(calls)[-1]
        assert t.db.get_deposit(deposit["id"])["status"] == "closed" and t.db.pending_deposits() == []
        calls = await t.press(OWNER, f"dok:{deposit['id']}")  # an old message's button
        assert "already closed" in t.alert(calls)
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_something_that_is_not_an_order_id_is_explained_and_not_counted(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, "I paid 25$")
        assert "That isn't an Order ID" in t.last_text(calls)
        calls = await t.send_photo(ALICE)
        assert "That isn't an Order ID" in t.last_text(calls)
        assert fake.asked == [] and owner_texts(calls) == []
        calls = await t.send(ALICE, ORDER)  # the question is still open
        assert "Payment received!" in t.last_text(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_other_coins_and_payments_sent_by_the_shop_are_failed_tries(tmp_path):
    async def scenario(t: ShopTest) -> None:
        out = "466000199987322882"
        with_binance(t, FakeBinance(pay(amount="0.001", currency="BTC"),
                                    pay(order_id=out, amount="-25", tx="P_OUT")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "This payment is <b>0.001 BTC</b>, but you chose to add <b>25$</b>" in texts(calls, ALICE)[-1]
        calls = await t.send(ALICE, out)
        assert "Sent to the owner" in texts(calls, ALICE)[-1]
        request = [x for x in owner_texts(calls) if "Deposit request" in x][-1]
        assert "Binance has: <b>0.001 BTC</b>" in request and "not a payment to the shop" in request
        (deposit,) = t.db.pending_deposits()
        assert deposit["order_id"] == ORDER  # the payment Binance has, kept for this request
        assert t.db.binance_payment(ORDER)["status"] == "owner"
        assert t.db.binance_payment(out) is None and balance(t, ALICE) == 0
        # Binance didn't confirm the second one as a payment to the shop: approving the
        # request may be for a payment the bot can't name.
        assert deposit["unverified"] == 1 and deposit["unconfirmed"] == out
        assert f"❗ Binance didn't confirm Order ID <code>{out}</code> as a new payment to you" in request

    run(tmp_path, scenario)


def test_payments_from_before_automatic_checking_are_not_found(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25", minutes_ago=60 * 24 * 5)))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        assert "can't find a payment" in texts(await t.send(ALICE, ORDER), ALICE)[-1]
        assert balance(t, ALICE) == 0

    run(tmp_path, scenario)


def test_an_order_id_adds_money_only_once(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "already added to your balance" in texts(calls, ALICE)[-1]
        assert not [x for x in owner_texts(calls) if "failed" in x]  # not a failed try
        await choose(t, BOB, 25)
        calls = await t.send(BOB, ORDER)
        assert "This Order ID was already used" in texts(calls, BOB)[-1]
        assert "already used by someone else" in owner_texts(calls)[-1]
        calls = await t.send(BOB, TX)  # Binance's other ID for the same payment
        assert "Sent to the owner" in texts(calls, BOB)[-1]
        (deposit,) = t.db.pending_deposits()
        assert deposit["order_id"] is None  # approving it can't touch Alice's payment
        assert balance(t, ALICE) == 2500 and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_two_claims_of_one_payment_at_the_same_moment_add_it_once(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        register(t, BOB)
        payment = pay()
        t.db.credit_binance_payment(ALICE["id"], payment, payment.cents(), 100_000)
        with pytest.raises(PaymentUsed):
            t.db.credit_binance_payment(BOB["id"], payment, payment.cents(), 100_000)
        assert balance(t, ALICE) == 2500 and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_a_key_problem_sends_deposits_to_the_owner_and_tells_the_owner_once(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(error=BinanceKeyError("Binance refused the API key")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 10)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1]
        owner = owner_texts(calls)
        assert any("Automatic deposits don't work" in x for x in owner)
        request = [x for x in owner if "Deposit request" in x][-1]
        assert f"Binance Order ID: <code>{ORDER}</code>" in request and "API key" in request
        (deposit,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{deposit['id']}")
        assert balance(t, ALICE) == 1000
        # Once the key works again, the same payment can't be added a second time.
        fake.error = None
        fake.payments.append(pay(amount="10"))
        await choose(t, ALICE, 10)
        assert "already added to your balance" in texts(await t.send(ALICE, ORDER), ALICE)[-1]
        assert balance(t, ALICE) == 1000
        # The owner hears about the broken key only once.
        fake.error = BinanceKeyError("still broken")
        await choose(t, BOB, 10)
        calls = await t.send(BOB, "466000199987322999")
        assert not [x for x in owner_texts(calls) if "don't work" in x]
        assert "Sent to the owner" in texts(calls, BOB)[-1]

    run(tmp_path, scenario)


def test_binance_unreachable_is_not_a_failed_try(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceUnavailable("busy")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "can't be reached right now" in t.last_text(calls)
        assert owner_texts(calls) == []
        fake.error = None
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        assert "Payment received!" in t.last_text(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_the_bot_keeps_answering_while_binance_is_slow(tmp_path):
    class SlowBinance(FakeBinance):
        def __init__(self, *payments: Payment) -> None:
            super().__init__(*payments)
            self.go = asyncio.Event()

        async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
            await self.go.wait()
            return await super().find(wanted, since_ms)

    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, SlowBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        settle = t.settle

        async def quick() -> None:  # don't wait for the check that's still running
            await asyncio.sleep(0.01)

        t.settle = quick  # type: ignore[method-assign]
        calls = await t.send(ALICE, ORDER)
        assert "Checking Order ID" in t.last_text(calls, ALICE["id"])
        calls = await t.press(BOB, "dp")  # someone else, meanwhile
        assert "Choose how much you want to add" in t.last_text(calls, BOB["id"])
        calls = await t.send(ALICE, ORDER)
        assert "Still checking" in t.last_text(calls, ALICE["id"])
        mark = len(t.tg.calls)
        fake.go.set()
        t.settle = settle  # type: ignore[method-assign]
        await t.settle()
        assert "Payment received!" in t.last_text(t.tg.calls[mark:], ALICE["id"])
        assert balance(t, ALICE) == 2500 and len(fake.asked) == 1

    run(tmp_path, scenario)


def test_cancelling_while_binance_is_asked_tells_nobody_and_sends_nothing(tmp_path):
    class SlowBinance(FakeBinance):
        go = asyncio.Event()

        async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
            await self.go.wait()
            return await super().find(wanted, since_ms)

    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, SlowBinance())
        SlowBinance.go = asyncio.Event()
        register(t, ALICE)
        await choose(t, ALICE, 25)
        state = t.app.user_data[ALICE["id"]]["state"]
        state["tries"] = 1  # the second try: a failure would go to the owner
        settle = t.settle

        async def quick() -> None:
            await asyncio.sleep(0.01)

        t.settle = quick  # type: ignore[method-assign]
        await t.send(ALICE, ORDER)
        await t.press(ALICE, "cancel")
        mark = len(t.tg.calls)
        fake.go.set()
        t.settle = settle  # type: ignore[method-assign]
        await t.settle()
        calls = t.tg.calls[mark:]
        assert "can't find a payment" in t.last_text(calls, ALICE["id"])
        assert owner_texts(calls) == [] and t.db.pending_deposits() == []

    run(tmp_path, scenario)


def test_the_first_deposit_counts_from_when_the_bot_started_with_the_key(tmp_path):
    # Automatic checking starts when the bot starts with a key, not at the first check: the
    # very first customer's payment, made before their check, must be found.
    async def scenario(t: ShopTest) -> None:
        started = int(t.db.get_setting("binance_auto_since"))
        assert started <= int(time.time() * 1000)
        started -= 60_000  # as if the bot had been running for a minute
        t.db.set_setting("binance_auto_since", str(started))
        payment = pay(amount="25", minutes_ago=0.5)  # after the start, before the check
        assert payment.time_ms >= started
        t.app.bot_data["binance"] = FakeBinance(payment)
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Payment received!" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2500
        assert int(t.db.get_setting("binance_auto_since")) == started

    run(tmp_path, scenario, binance_api_key="k" * 64, binance_api_secret="s" * 64)


def test_turning_automatic_checking_off_and_on_again_starts_the_count_afresh(tmp_path):
    db = Database(str(tmp_path / "shop.db"), OWNER["id"])
    db.start_binance_auto(True)
    first = db.get_setting("binance_auto_since")
    db.start_binance_auto(True)  # a restart: same start
    assert db.get_setting("binance_auto_since") == first
    db.start_binance_auto(False)  # started without a key: receipts again
    assert db.get_setting("binance_auto_since") is None
    time.sleep(0.01)
    db.start_binance_auto(True)
    assert int(db.get_setting("binance_auto_since")) > int(first)
    db.close()


def test_without_a_key_deposits_use_a_receipt_as_before(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.press(ALICE, "dp")
        assert "Choose how much you want to add" in t.last_text(calls)
        calls = await t.press(ALICE, "da:10")
        assert "photo of the payment receipt" in t.last_text(calls)

    run(tmp_path, scenario)


# ============================================ what the owner decided by hand
def request_text(calls: list) -> str:
    return [x for x in owner_texts(calls) if "Deposit request" in x][-1]


def test_an_approved_order_id_binance_did_not_know_cannot_let_the_real_one_be_added_again(tmp_path):
    # Order IDs Binance doesn't know (typos, by mistake or on purpose) end with the owner,
    # who finds the payment in the Binance app and approves. The bot can't tell which
    # payment that was, so the real Order ID sent afterwards must not be added again.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        calls = await t.send(ALICE, "466000199987322889")
        (request,) = t.db.pending_deposits()
        assert request["unverified"] == 1
        assert ("❗ Binance didn't confirm Order ID <code>466000199987322888</code>, "
                "<code>466000199987322889</code> as a new payment to you") in request_text(calls)
        await t.press(OWNER, f"dok:{request['id']}")
        assert balance(t, ALICE) == 2500
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2500
        shown = request_text(calls)
        assert (f"⚠️ May be added already: after <code>{ORDER}</code> was paid, you approved "
                f"request #{request['id']}, whose Order ID Binance didn't confirm.") in shown
        assert "Binance has: <b>25 USDT</b>" in shown and "❗" not in shown
        (again,) = t.db.pending_deposits()
        assert again["order_id"] == ORDER and t.db.binance_payment(ORDER)["status"] == "owner"
        calls = await t.press(OWNER, f"dok:{again['id']}")  # approving it asks once more
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500
        await t.press(OWNER, f"dno:{again['id']}")
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_the_real_order_id_sent_while_such_a_request_waits_goes_to_the_owner_too(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (first,) = t.db.pending_deposits()
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 0
        assert (f"request #{first['id']} is waiting, with an Order ID Binance didn't confirm"
                in request_text(calls))
        second = t.db.pending_deposits()[-1]
        calls = await t.press(OWNER, f"dok:{second['id']}")  # the one Binance confirmed ...
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 0
        await t.press(OWNER, f"dokx:{second['id']}")  # ... the owner is sure: Approve anyway
        assert balance(t, ALICE) == 2500
        calls = await t.press(OWNER, f"dok:{first['id']}")  # it may be that same payment
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500
        assert f"25.00$ with request #{second['id']}" in t.last_text(calls)  # shown on the request
        await t.press(OWNER, f"dno:{first['id']}")
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_payment_settled_by_hand_after_a_rejected_request_is_not_added_again(tmp_path):
    # While Binance refuses the key, the deposit (25$ chosen, 20 USDT paid) goes to the
    # owner, who rejects it and adds the 20$ by hand. Once the key works again, the same
    # payment must not be added a second time.
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="20"), error=BinanceKeyError("refused")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        (request,) = t.db.pending_deposits()
        assert request["unverified"] == 1 and request["order_id"] == ORDER
        await t.press(OWNER, f"dno:{request['id']}")
        await t.send(OWNER, f"/addbal {ALICE['id']} 20")
        assert balance(t, ALICE) == 2000
        fake.error = None
        await choose(t, ALICE, 20)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2000
        shown = request_text(calls)
        assert f"you rejected request #{request['id']}, whose Order ID Binance didn't confirm" in shown
        assert "you added 20.00$ by hand (" in shown

    run(tmp_path, scenario)


def test_balance_added_by_hand_stops_an_earlier_payment_being_added_again(tmp_path):
    # Binance can't be reached, so the owner adds the customer's 25$ by hand. When Binance
    # is back, "Check again" must not add the same payment a second time.
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceUnavailable("busy")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        await t.send(OWNER, f"/addbal {ALICE['id']} 25")
        fake.error = None
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        assert "Sent to the owner" in t.last_text(calls, ALICE["id"]) and balance(t, ALICE) == 2500
        assert "you added 25.00$ by hand (" in request_text(calls)
        (request,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500
        assert t.button(calls, "Approve anyway") == f"dokx:{request['id']}"
        await t.press(OWNER, f"dokx:{request['id']}")  # the owner is sure
        assert balance(t, ALICE) == 5000
        # Balance added by hand to Alice doesn't hold up Bob's own payment.
        fake.payments.append(pay(order_id=OTHER, amount="10", tx="P_OTHER"))
        await choose(t, BOB, 10)
        assert "Payment received!" in texts(await t.send(BOB, OTHER), BOB)[-1]

    run(tmp_path, scenario)


def test_which_deposits_put_a_payment_in_doubt(tmp_path):
    db = Database(str(tmp_path / "shop.db"), OWNER["id"])
    payment = pay(amount="25")  # paid 5 minutes ago

    def doubts(user: dict = ALICE, paid: Payment = payment) -> list[int]:
        ids = (paid.order_id, paid.transaction_id)
        return [r["id"] for r in db.payment_doubts(ids, db._paid_at(paid), user["id"])]

    db.create_deposit(BOB["id"], 1000, "file", "photo")  # a receipt: no Order ID at all
    db.create_review_request(BOB["id"], 1000, OTHER, "x", [pay(order_id=OTHER, tx="P_OTHER")])  # confirmed
    db.manual_deposit(BOB["id"], 1000, 100_000)  # someone else's balance added by hand
    bobs = db.conn.execute("SELECT MAX(id) FROM deposits").fetchone()[0]
    assert doubts() == [] and doubts(BOB) == [bobs]
    named = db.create_review_request(ALICE["id"], 2500, ORDER, "x", [])  # Binance didn't confirm it
    assert named["unverified"] == 1 and named["unconfirmed"] == ORDER
    assert doubts() == []  # it waits, and names exactly this payment
    db.decide_deposit(named["id"], False, 100_000)
    assert doubts() == [named["id"]]  # rejected after it was paid: refunded, or added by hand?
    other = db.create_review_request(BOB["id"], 2500, "466000199987322888", "x", [])
    db.manual_deposit(ALICE["id"], 2500, 100_000)
    added = db.conn.execute("SELECT MAX(id) FROM deposits").fetchone()[0]
    assert doubts() == [named["id"], other["id"], added]
    assert doubts(BOB) == [bobs, named["id"], other["id"]]  # Alice's balance by hand isn't Bob's
    with pytest.raises(PaymentInDoubt):
        db.credit_binance_payment(ALICE["id"], payment, 2500, 100_000)
    assert db.get_user(ALICE["id"])["balance"] == 2500 and db.binance_payment(ORDER) is None
    # A payment made after all of these: the decided ones and Bob's can't be about it, but
    # Alice's own waiting request can (she may have sent its Order IDs before paying).
    later = pay(order_id="466000199987322999", amount="25", minutes_ago=-30, tx="P_LATER")
    assert doubts(paid=later) == []
    waiting = db.create_review_request(ALICE["id"], 2500, "466000199987322777", "x", [])
    db.conn.execute("UPDATE deposits SET created_at = '2000-01-01 00:00:00' WHERE id = ?", (waiting["id"],))
    assert doubts(paid=later) == [waiting["id"]]
    assert doubts(BOB, later) == [other["id"]]  # Bob's own waiting request, not Alice's
    # Decided after the payment counts, however long before it the request was made.
    db.decide_deposit(waiting["id"], True, 100_000)
    db.conn.execute("UPDATE deposits SET decided_at = ? WHERE id = ?",
                    (db._paid_at(later), waiting["id"]))
    assert doubts(BOB, later) == [other["id"], waiting["id"]]
    db.close()


def test_an_older_database_marks_its_requests_binance_did_not_confirm(tmp_path):
    path = str(tmp_path / "shop.db")
    db = Database(path, OWNER["id"])

    def as_before(note: str) -> int:  # a request written by an older version
        db.ensure_user(ALICE["id"])
        cur = db.conn.execute(
            "INSERT INTO deposits (user_id, amount, method, status, created_at, note)"
            " VALUES (?, 2500, 'binance', 'pending', '2026-11-19 10:00:00', ?)", (ALICE["id"], note))
        return int(cur.lastrowid)

    marked = [as_before(f"{ORDER}: not found in your Binance Pay history"),
              as_before(f"{ORDER}: not checked (Binance refused the API key)"),
              as_before(f"{ORDER}: not a payment to the shop"),
              as_before(f"{ORDER}: already used by someone else")]
    left = [as_before(f"{ORDER}: is 20 USDT, not 25 USDT"),
            db.create_deposit(ALICE["id"], 1000, "file", "photo")["id"]]
    # Written by this version: Binance confirmed its Order ID in the end (the first check
    # came before Binance listed it), so it stays unmarked although its notes say "not found".
    mine = db.create_review_request(ALICE["id"], 2000, ORDER, f"{ORDER}: not found in your Binance Pay history",
                                    [pay(amount="20")], [ORDER])
    assert mine["unverified"] == 0 and mine["unconfirmed"] == ""
    db.close()
    db = Database(path, OWNER["id"])  # the next start marks the older version's requests
    assert [db.get_deposit(i)["unverified"] for i in marked] == [1, 1, 1, 1]
    assert [db.get_deposit(i)["unverified"] for i in left + [mine["id"]]] == [0, 0, 0]
    again = as_before(f"{OTHER}: not found in your Binance Pay history")  # e.g. after going back
    db.close()
    db = Database(path, OWNER["id"])
    assert db.get_deposit(again)["unverified"] == 1  # marked at every start
    db.close()


def test_a_request_made_before_the_payment_and_approved_after_it_still_holds_it(tmp_path):
    # Order IDs Binance doesn't know sent BEFORE paying (an old one, or made up): the owner
    # then finds the payment in the app and approves. What counts is when he decided.
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (request,) = t.db.pending_deposits()
        earlier = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 3600))
        t.db.conn.execute("UPDATE deposits SET created_at = ? WHERE id = ?", (earlier, request["id"]))
        fake.payments.append(pay(amount="25"))  # paid 5 minutes ago, long after the request
        await choose(t, ALICE, 25)  # while it waits, her own payments aren't added either
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 0
        assert f"request #{request['id']} is waiting" in request_text(calls)
        held = t.db.pending_deposits()[-1]
        await t.press(OWNER, f"dno:{held['id']}")  # the owner deals with the first one
        await t.press(OWNER, f"dok:{request['id']}")  # he found her 25 USDT in the app
        assert balance(t, ALICE) == 2500
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2500
        assert f"you approved request #{request['id']}" in request_text(calls)
        # Bob's payment made before that decision is in doubt too: it may be the one.
        fake.payments.append(pay(order_id=OTHER, amount="10", tx="P_OTHER"))
        await choose(t, BOB, 10)
        calls = await t.send(BOB, OTHER)
        assert "Sent to the owner" in texts(calls, BOB)[-1] and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_a_payment_that_reaches_the_owner_another_way_still_says_why_it_may_be_added(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (first,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{first['id']}")  # the owner found her 25 USDT in the app
        await choose(t, ALICE, 24)  # another amount: her payment doesn't match it
        await t.send(ALICE, ORDER)
        calls = await t.send(ALICE, ORDER)
        shown = request_text(calls)
        assert "is 25 USDT, not 24 USDT" in shown and f"you approved request #{first['id']}" in shown
        (second,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{second['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_payment_kept_for_a_rejected_request_and_settled_by_hand_says_so(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, ORDER)
        (first,) = t.db.pending_deposits()
        await t.press(OWNER, f"dno:{first['id']}")
        await t.send(OWNER, f"/addbal {ALICE['id']} 20")
        await choose(t, ALICE, 20)
        calls = await t.send(ALICE, ORDER)
        shown = request_text(calls)
        assert f"was in request #{first['id']}, which you rejected" in shown
        assert "you added 20.00$ by hand (" in shown
        (second,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{second['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2000

    run(tmp_path, scenario)


def test_the_request_names_the_payment_being_claimed(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25"), pay(order_id=OTHER, amount="20", tx="P_OTHER")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, BOB, 10)  # Bob's request with Order IDs Binance doesn't know
        await t.send(BOB, "466000199987322888")
        await t.send(BOB, "466000199987322889")
        await choose(t, ALICE, 25)
        await t.send(ALICE, OTHER)  # her 20 USDT payment: not the amount chosen
        calls = await t.send(ALICE, ORDER)  # her 25 USDT one, which Bob's request may be about
        assert "Sent to the owner" in texts(calls, ALICE)[-1]
        request = t.db.pending_deposits()[-1]
        assert request["order_id"] == ORDER and request["unverified"] == 0
        calls = await t.press(OWNER, f"dok:{request['id']}")  # Bob's request may be about it
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 0
        await t.press(OWNER, f"dokx:{request['id']}")
        assert balance(t, ALICE) == 2500
        assert t.db.binance_payment(ORDER)["status"] == "credited"
        assert t.db.binance_payment(OTHER)["status"] == "owner"  # not added: the owner decides it

    run(tmp_path, scenario)


def test_an_order_id_binance_lists_late_is_confirmed_and_holds_up_nobody(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(order_id=OTHER, amount="10", tx="P_OTHER", minutes_ago=3)))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)  # sent before Binance lists it: not found
        fake.payments.append(pay(amount="20"))  # listed now: 20 USDT, not the 25 chosen
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        (request,) = t.db.pending_deposits()
        assert request["unverified"] == 0 and request["unconfirmed"] == ""
        assert "❗" not in request_text(calls)
        await choose(t, BOB, 10)  # paid before Alice's request: not held up by it
        assert "Payment received!" in texts(await t.send(BOB, OTHER), BOB)[-1]

    run(tmp_path, scenario)


def test_the_owner_sees_how_things_stand_when_deciding(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (first,) = t.db.pending_deposits()
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        second = t.db.pending_deposits()[-1]
        await t.press(OWNER, f"dok:{first['id']}")  # meanwhile: approved for her 25 USDT
        calls = await t.send(OWNER, "/pending")
        (shown,) = [x for x in owner_texts(calls) if f"Deposit request #{second['id']}" in x]
        assert f"you approved request #{first['id']}, whose Order ID Binance didn't confirm" in shown
        calls = await t.press(OWNER, f"dok:{second['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500
        assert f"you approved request #{first['id']}" in t.last_text(calls)  # the request now says so

    run(tmp_path, scenario)


def test_a_server_clock_behind_binances_still_counts_a_decision_after_the_payment(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25", minutes_ago=-1)))  # Binance's clock is ahead
        register(t, ALICE)
        register(t, BOB)
        await choose(t, BOB, 25)
        await t.send(BOB, "466000199987322888")
        await t.send(BOB, "466000199987322889")
        (request,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{request['id']}")  # "before" the payment by this clock
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 0

    run(tmp_path, scenario)


def test_a_clock_difference_binance_measured_is_allowed_for(tmp_path):
    # This server's clock is 30 minutes behind Binance's: the client measures that (-1021)
    # and it's kept, so a payment's time can be compared with the owner's decisions.
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25", minutes_ago=-25)))  # by Binance's clock
        fake.offset_ms = 30 * 60_000
        register(t, ALICE)
        register(t, BOB)
        await choose(t, BOB, 25)
        await t.send(BOB, "466000199987322888")
        await t.send(BOB, "466000199987322889")
        (request,) = t.db.pending_deposits()
        assert t.db.binance_clock_offset() == 30 * 60_000  # noted at Bob's checks
        await t.press(OWNER, f"dok:{request['id']}")  # 5 minutes after Alice paid, really
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 0
        fake.offset_ms = 0  # the clock was set right: the next check notes that too
        await t.send(ALICE, "/start")
        await choose(t, BOB, 25)
        await t.send(BOB, "466000199987322777")
        assert t.db.binance_clock_offset() == 0

    run(tmp_path, scenario)


def test_any_warning_takes_the_second_press(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (first,) = t.db.pending_deposits()
        await t.press(OWNER, f"dno:{first['id']}")  # rejected: refunded on Binance, maybe
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert f"you rejected request #{first['id']}" in request_text(calls)
        (second,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{second['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 0
        calls = await t.press(OWNER, f"dok:{second['id']}")  # a double tap doesn't confirm it
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 0
        await t.press(OWNER, f"dokx:{second['id']}")  # ⚠️ Approve anyway does
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_the_most_telling_warnings_come_first(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        others = [{"id": 2001 + n, "is_bot": False, "first_name": f"P{n}", "username": f"p{n}"} for n in range(4)]
        for user in [*others, BOB]:
            register(t, user)
        for n, user in enumerate(others):  # waiting requests of other customers
            await choose(t, user, 10)
            await t.send(user, f"4660001999873330{n}1")
            await t.send(user, f"4660001999873330{n}2")
        await choose(t, BOB, 30)
        await t.send(BOB, "466000199987322777")
        await t.send(BOB, "466000199987322778")
        bobs = t.db.pending_deposits()[-1]
        await t.press(OWNER, f"dok:{bobs['id']}")  # approved: it may have been Alice's payment
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        line = [x for x in request_text(calls).splitlines() if x.startswith("⚠️ May be added already")][0]
        assert line.index(f"you approved request #{bobs['id']} for Bob") < line.index("is waiting")
        assert "and 2 more" in line
        # For a request Binance didn't confirm: the customer's own deposits come first.
        for n in range(4):
            fake.payments.append(pay(order_id=f"46600019998734440{n}", amount="10", tx=f"P_X{n}"))
            await choose(t, others[n], 10)
            await t.send(others[n], f"46600019998734440{n}")
        await t.send(OWNER, f"/addbal {BOB['id']} 5")
        line = [x for x in deposit_caption_text(t, bobs) if x.startswith("⚠️ Added since")]
        assert line == []  # decided requests show no warnings
        await choose(t, BOB, 30)
        await t.send(BOB, "466000199987322779")
        calls = await t.send(BOB, "466000199987322770")
        line = [x for x in request_text(calls).splitlines() if x.startswith("⚠️ Added since")][0]
        assert line.startswith("⚠️ Added since this request was made: 5.00$ by hand (")

    run(tmp_path, scenario)


def deposit_caption_text(t: ShopTest, deposit: Any) -> list[str]:
    from shopbot.handlers.money import deposit_caption
    return deposit_caption(t.app, t.db.get_deposit(deposit["id"])).splitlines()


def test_a_second_account_claiming_the_payment_with_another_amount_is_listed(tmp_path):
    # Alice's request has Order IDs Binance doesn't know. Bob claims the same 25 USDT
    # payment, choosing 24$, and the owner approves his request; approving Alice's then
    # shows that this payment was just added for Bob.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (alices,) = t.db.pending_deposits()
        earlier = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 3600))
        t.db.conn.execute("UPDATE deposits SET created_at = ? WHERE id = ?", (earlier, alices["id"]))
        await choose(t, BOB, 24)
        await t.send(BOB, ORDER)
        await t.send(BOB, ORDER)
        bobs = t.db.pending_deposits()[-1]
        await t.press(OWNER, f"dok:{bobs['id']}")
        assert balance(t, BOB) == 2400
        calls = await t.press(OWNER, f"dok:{alices['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 0
        shown = t.last_text(calls)
        assert f"24.00$ for Bob with request #{bobs['id']}, Order ID <code>{ORDER}</code> of 25 USDT" in shown

    run(tmp_path, scenario)


def test_a_clock_set_right_after_a_decision_still_holds_the_payment(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25", minutes_ago=-25)))  # by Binance's clock
        fake.offset_ms = 30 * 60_000  # this server's clock is 30 minutes behind
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (request,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{request['id']}")  # the owner found her payment in the app
        fake.offset_ms = 0  # then the clock is set right: times written before are 30 min off
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_the_warnings_have_indexes(tmp_path):
    db = Database(str(tmp_path / "shop.db"), OWNER["id"])
    names = {row["name"] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_deposits_doubt", "idx_deposits_method", "idx_deposits_decided",
            "idx_binance_payments_deposit"} <= names
    plan = " ".join(str(tuple(r)) for r in db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM deposits WHERE status = 'approved' AND decided_at >= '2026'"))
    assert "idx_deposits_decided" in plan
    db.close()


def test_approve_anyway_approves_only_what_was_shown(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (request,) = t.db.pending_deposits()
        await t.send(OWNER, f"/addbal {ALICE['id']} 5")  # something else, by hand
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and "5.00$ by hand" in t.last_text(calls)
        # Meanwhile Bob's payment of the same amount is added automatically: it may be the
        # payment the owner found for Alice, so "Approve anyway" first shows it.
        fake.payments.append(pay(order_id=OTHER, amount="25", tx="P_OTHER", minutes_ago=-5))  # after hers
        await choose(t, BOB, 25)
        assert "Payment received!" in texts(await t.send(BOB, OTHER), BOB)[-1]
        calls = await t.press(OWNER, f"dokx:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 500
        assert f"Order ID <code>{OTHER}</code>" in t.last_text(calls)
        await t.press(OWNER, f"dokx:{request['id']}")  # now he has seen it
        assert balance(t, ALICE) == 3000

    run(tmp_path, scenario)


def test_a_request_settled_by_an_automatic_deposit_stops_counting(tmp_path):
    # Binance listed Alice's payment late, so her request named its Order ID unconfirmed.
    # Once it's added automatically, the request is closed and holds up nobody.
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, ORDER)
        (request,) = t.db.pending_deposits()
        fake.payments.append(pay(amount="25"))
        await choose(t, ALICE, 25)
        assert "Payment received!" in texts(await t.send(ALICE, ORDER), ALICE)[-1]
        assert t.db.get_deposit(request["id"])["status"] == "closed"
        fake.payments += [pay(order_id=OTHER, amount="10", tx="P_OTHER", minutes_ago=1),
                          pay(order_id="466000199987322999", amount="5", tx="P_THIRD", minutes_ago=1)]
        await choose(t, ALICE, 10)  # her next payment isn't held up by the closed request
        assert "Payment received!" in texts(await t.send(ALICE, OTHER), ALICE)[-1]
        await choose(t, BOB, 5)  # nor is anyone else's
        assert "Payment received!" in texts(await t.send(BOB, "466000199987322999"), BOB)[-1]
        # Settled without being named: its other unconfirmed Order ID was added for her.
        # While it waits, it holds up nobody ...
        t.db.conn.execute("UPDATE deposits SET status = 'pending', order_id = NULL,"
                          " unconfirmed = ? WHERE id = ?", (OTHER, request["id"]))
        assert t.db.still_unconfirmed(t.db.get_deposit(request["id"])) == []
        assert t.db.payment_doubts(("466000199987322777",), t.db._paid_at(pay()), BOB["id"]) == []
        # ... but once decided it always counts: it may have been decided for any payment.
        t.db.conn.execute("UPDATE deposits SET status = 'approved', decided_at = ? WHERE id = ?",
                          (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), request["id"]))
        doubts = t.db.payment_doubts(("466000199987322777",), t.db._paid_at(pay()), BOB["id"])
        assert [r["id"] for r in doubts] == [request["id"]]

    run(tmp_path, scenario)


def test_an_older_versions_requests_are_marked_from_their_notes_both_ways(tmp_path):
    path = str(tmp_path / "shop.db")
    db = Database(path, OWNER["id"])
    db.ensure_user(ALICE["id"])
    cur = db.conn.execute(  # as e64c71f wrote it: marked although the payment was found late
        "INSERT INTO deposits (user_id, amount, method, status, created_at, note, unverified)"
        " VALUES (?, 2500, 'binance', 'pending', '2026-11-19 10:00:00', ?, 1)",
        (ALICE["id"], f"{ORDER}: not found in your Binance Pay history\n{ORDER}: is 20 USDT, not 25 USDT"))
    late = int(cur.lastrowid)
    db._record_payment(ALICE["id"], pay(amount="20"), 0, "owner", late)
    db.conn.execute("UPDATE deposits SET unconfirmed = NULL WHERE id = ?", (late,))
    cur = db.conn.execute(  # and one whose typo Binance never found
        "INSERT INTO deposits (user_id, amount, method, status, created_at, note)"
        " VALUES (?, 2500, 'binance', 'pending', '2026-11-19 10:00:00', ?)",
        (ALICE["id"], f"{OTHER}: not found in your Binance Pay history"))
    typo = int(cur.lastrowid)
    cur = db.conn.execute(  # marked, for a reason its notes don't give: left marked
        "INSERT INTO deposits (user_id, amount, method, status, created_at, note, unverified)"
        " VALUES (?, 2500, 'binance', 'pending', '2026-11-19 10:00:00', 'checked by hand', 1)",
        (ALICE["id"],))
    unknown = int(cur.lastrowid)
    db.close()
    db = Database(path, OWNER["id"])
    assert (db.get_deposit(late)["unverified"], db.get_deposit(late)["unconfirmed"]) == (0, "")
    assert (db.get_deposit(typo)["unverified"], db.get_deposit(typo)["unconfirmed"]) == (1, OTHER)
    assert (db.get_deposit(unknown)["unverified"], db.get_deposit(unknown)["unconfirmed"]) == (1, None)
    assert db.still_unconfirmed(db.get_deposit(unknown)) is None  # still counts, as not known
    db.close()


def test_binance_answering_in_between_resets_the_minute(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(error=BinanceUnavailable("busy")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)  # a blip
        fake.error = None
        calls = await t.press(ALICE, t.button(calls, "Check again"))  # answered: not listed yet
        assert "can't find a payment" in t.last_text(calls, ALICE["id"])
        assert "unreachable" not in t.app.user_data[ALICE["id"]]["state"]
        fake.error = BinanceUnavailable("busy")  # another blip, a minute later
        calls = await t.send(ALICE, ORDER)
        assert "can't be reached right now" in t.last_text(calls, ALICE["id"])
        assert t.db.pending_deposits() == []

    run(tmp_path, scenario)


def test_an_approval_is_remembered_however_often_the_payment_comes_back(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20"), pay(order_id=OTHER, amount="5", tx="P_OTHER")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, OTHER)
        (first,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{first['id']}")  # 20 + 5 USDT for the 25$
        await choose(t, ALICE, 20)
        await t.send(ALICE, ORDER)  # back to the owner, who rejects it ...
        (second,) = t.db.pending_deposits()
        await t.press(OWNER, f"dno:{second['id']}")
        await choose(t, ALICE, 20)
        calls = await t.send(ALICE, ORDER)  # ... and again: the approval is still shown
        shown = request_text(calls)
        assert f"it was in request #{first['id']}, which you approved" in shown
        assert f"it was in request #{second['id']}, which you rejected" in shown
        (third,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{third['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_payment_made_before_automatic_checking_started_goes_to_the_owner(tmp_path):
    # This server's clock is 30 minutes behind Binance's. A payment made 10 minutes before
    # the key was added (it may have come with a receipt the owner approved) is found by
    # Binance's clock, but compared on the same clock it was made before the start.
    async def scenario(t: ShopTest) -> None:
        started = int(time.time() * 1000)
        t.db.set_setting("binance_auto_since", str(started))
        t.db.set_setting("binance_clock_offset_ms", str(30 * 60_000))  # measured before
        fake = FakeBinance(pay(amount="25", minutes_ago=-20))  # 10 min before the start, really
        fake.offset_ms = 30 * 60_000
        t.app.bot_data["binance"] = fake
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 0
        shown = request_text(calls)
        assert f"<code>{ORDER}</code> was paid before automatic checking started" in shown
        (request,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 0

    run(tmp_path, scenario)


def test_a_decided_request_keeps_holding_payments_after_its_order_id_is_added(tmp_path):
    # Request #1 (❗: X not listed yet) is approved for the payment Y the owner found in the
    # app. Later X is added for Alice through another request: #1 may still have been for Y.
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(order_id=OTHER, amount="5", tx="P_OTHER")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # X: not listed yet
        await t.send(ALICE, OTHER)  # 5 USDT: wrong amount
        (first,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{first['id']}")  # approved for Y, which he found in the app
        y = "466000199987322777"
        fake.payments += [pay(amount="25"), pay(order_id=y, amount="25", tx="P_Y")]
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # X, now listed: in doubt because of #1
        (second,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{second['id']}")
        await t.press(OWNER, f"dokx:{second['id']}")  # X is another payment: approved too
        assert balance(t, ALICE) == 5000
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, y)  # Y, the one #1 was approved for
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 5000

    run(tmp_path, scenario)


def test_approving_a_payment_closes_the_requests_waiting_with_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, ORDER)  # not listed yet: request #1 names exactly this payment
        (first,) = t.db.pending_deposits()
        fake.payments.append(pay(amount="25"))
        await choose(t, ALICE, 24)  # listed now, but sent with another amount: #2 holds it
        await t.send(ALICE, ORDER)
        await t.send(ALICE, ORDER)
        second = t.db.pending_deposits()[-1]
        calls = await t.press(OWNER, f"dok:{second['id']}")  # her own #1 names it: no warning
        assert balance(t, ALICE) == 2400
        assert f"Request #{first['id']} was waiting with this payment: closed." in t.last_text(calls, OWNER["id"])
        assert t.db.get_deposit(first["id"])["status"] == "closed" and t.db.pending_deposits() == []

    run(tmp_path, scenario)


def test_two_customers_waiting_with_one_order_id_is_for_the_owner_to_settle(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, ORDER)  # not listed yet: request #1 names exactly this payment
        (alices,) = t.db.pending_deposits()
        fake.payments.append(pay(amount="25"))
        await choose(t, BOB, 25)
        calls = await t.send(BOB, ORDER)  # Bob sends the same Order ID
        assert "Sent to the owner" in texts(calls, BOB)[-1] and balance(t, BOB) == 0
        assert f"request #{alices['id']} for Alice is waiting" in request_text(calls)
        assert t.db.get_deposit(alices["id"])["status"] == "pending"

    run(tmp_path, scenario)


def test_unrelated_deposits_between_the_presses_dont_hold_up_approve_anyway(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (request,) = t.db.pending_deposits()
        await t.send(OWNER, f"/addbal {ALICE['id']} 5")
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls)
        for n in range(3):  # other customers keep paying other amounts meanwhile
            fake.payments.append(pay(order_id=f"46600019998735550{n}", amount="3", tx=f"P_C{n}", minutes_ago=-5))
            await choose(t, BOB, 3)
            assert "Payment received!" in texts(await t.send(BOB, f"46600019998735550{n}"), BOB)[-1]
        assert "Bob" not in t.last_text(calls)
        await t.press(OWNER, f"dokx:{request['id']}")
        assert balance(t, ALICE) == 3000

    run(tmp_path, scenario)


def test_a_check_cut_off_by_the_stop_says_the_bot_restarted(tmp_path):
    class Stopping(FakeBinance):
        async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
            raise binance_module.BinanceStopping("the bot is stopping")

    async def scenario(t: ShopTest) -> None:
        with_binance(t, Stopping())
        register(t, ALICE)
        await choose(t, ALICE, 25)
        t.app.user_data[ALICE["id"]]["state"]["unreachable"] = [time.monotonic() - 120]  # an outage before
        calls = await t.send(ALICE, ORDER)
        assert "restarted while checking" in t.last_text(calls, ALICE["id"])
        assert t.db.pending_deposits() == [] and owner_texts(calls) == []

    run(tmp_path, scenario)


def test_a_request_the_stop_cut_off_mid_flight_says_the_bot_restarted(tmp_path):
    class CutOff(FakeBinance):
        closed = False

        async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
            self.closed = True  # close() ended the request: it fails like the network
            raise BinanceUnavailable("network: ReadError")

    async def scenario(t: ShopTest) -> None:
        with_binance(t, CutOff())
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "restarted while checking" in t.last_text(calls, ALICE["id"])
        assert "unreachable" not in t.app.user_data[ALICE["id"]]["state"]

    run(tmp_path, scenario)


def test_approve_anyway_compares_what_the_request_shows(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, "466000199987322888")
        await t.send(ALICE, "466000199987322889")
        (request,) = t.db.pending_deposits()
        for n in range(3):  # her own deposits since: listed first
            await t.send(OWNER, f"/addbal {ALICE['id']} {n + 1}")
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls)
        # Bob's 25$ is of the same amount, but ranked after her own three: not shown
        fake.payments.append(pay(order_id=OTHER, amount="25", tx="P_OTHER", minutes_ago=-5))
        await choose(t, BOB, 25)
        assert "Payment received!" in texts(await t.send(BOB, OTHER), BOB)[-1]
        await t.press(OWNER, f"dokx:{request['id']}")  # what's shown didn't change
        assert balance(t, ALICE) == 600 + 2500

    run(tmp_path, scenario)


def test_long_names_dont_make_a_request_too_long_to_send(tmp_path):
    from shopbot.handlers.common import utf16_len
    from shopbot.handlers.money import deposit_caption

    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        others = [{"id": 3001 + n, "is_bot": False, "first_name": "🦄" * 64, "last_name": "𝓩" * 64,
                   "username": f"u{n}"} for n in range(8)]
        for user in others:
            register(t, user)
            t.db.create_review_request(user["id"], 1000, f"4660001999876{user['id']:05d}", "x", [])
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        request = t.db.pending_deposits()[-1]
        caption = deposit_caption(t.app, t.db.get_deposit(request["id"]))
        assert utf16_len(caption) <= 4096 and "is waiting" in caption
        assert "🦄" * 25 not in caption  # names are shortened in warnings

    run(tmp_path, scenario)


def test_a_request_stays_short_enough_to_send_whatever_it_lists(tmp_path):
    from shopbot.handlers.common import utf16_len
    from shopbot.handlers.money import deposit_caption

    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        for n in range(70):  # e.g. a day while Binance refused the key
            t.db.create_review_request(BOB["id"], 1000, f"4660001999873{n:05d}", "x", [])
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "and 67 more." in request_text(calls)
        request = t.db.pending_deposits()[-1]
        notes = "\n".join(f"{n:05d}: " + "why " * 60 for n in range(20))
        t.db.conn.execute("UPDATE deposits SET note = ?, unconfirmed = ?, unverified = 1 WHERE id = ?",
                          (notes, " ".join(f"4660001999872{n:05d}" for n in range(40)), request["id"]))
        caption = deposit_caption(t.app, t.db.get_deposit(request["id"]))
        assert "... and 15 more" in caption and "and 37 more as a new payment" in caption
        assert utf16_len(caption) <= 4096

    run(tmp_path, scenario)


def test_the_other_id_of_a_payment_the_owner_added_is_recognised(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceKeyError("refused")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        (request,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{request['id']}")
        assert t.db.binance_payment(TX) is None  # known by its Order ID only
        fake.error = None
        await choose(t, ALICE, 10)  # another amount, with Binance's other ID for it
        calls = await t.send(ALICE, TX)
        assert "already added to your balance" in texts(calls, ALICE)[-1]
        assert owner_texts(calls) == [] and t.db.pending_deposits() == []
        assert t.db.binance_payment(TX)["order_id"] == ORDER  # known by both IDs now
        await choose(t, BOB, 25)
        assert "already used" in texts(await t.send(BOB, TX), BOB)[-1]
        assert balance(t, ALICE) == 2500 and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_a_payment_kept_for_a_rejected_request_goes_back_to_the_owner(tmp_path):
    # 25$ chosen but 20 USDT paid, twice: the request keeps the payment. The owner rejects
    # it for its amount, and the customer then asks for the 20$ actually paid. The bot
    # can't add it by itself (the owner may have settled it by hand): the owner decides.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, ORDER)
        (first,) = t.db.pending_deposits()
        await t.press(OWNER, f"dno:{first['id']}")
        await choose(t, ALICE, 20)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1]
        shown = request_text(calls)
        assert f"was in request #{first['id']}, which you rejected" in shown
        assert "Binance has: <b>20 USDT</b>" in shown
        (second,) = t.db.pending_deposits()
        assert second["order_id"] == ORDER and t.db.binance_payment(ORDER)["deposit_id"] == second["id"]
        await choose(t, BOB, 20)
        assert "already used" in texts(await t.send(BOB, ORDER), BOB)[-1]
        calls = await t.press(OWNER, f"dok:{second['id']}")  # did he refund it? He's asked once more
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 0
        await t.press(OWNER, f"dokx:{second['id']}")
        assert balance(t, ALICE) == 2000 and t.db.binance_payment(ORDER)["status"] == "credited"

    run(tmp_path, scenario)


def test_a_payment_left_over_from_an_approved_request_goes_back_to_the_owner(tmp_path):
    # 25$ chosen; the request shows 20 and 30 USDT and the owner approves it. Whether that
    # covered both payments only he knows: the other one comes back to him, with a warning.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20"), pay(order_id=OTHER, amount="30", tx="P_OTHER")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, OTHER)
        (first,) = t.db.pending_deposits()
        assert first["order_id"] == OTHER  # the one sent last
        assert t.db.binance_payment(ORDER)["status"] == "owner"
        await t.press(OWNER, f"dok:{first['id']}")  # 25$: the owner's call
        assert t.db.binance_payment(OTHER)["status"] == "credited"
        assert t.db.binance_payment(ORDER)["status"] == "owner"
        await choose(t, ALICE, 20)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2500
        shown = request_text(calls)
        assert f"was in request #{first['id']}, which you approved" in shown
        assert f"⚠️ May be added already: after <code>{ORDER}</code> was paid, it was in request #{first['id']}, which you approved." in shown
        (second,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{second['id']}")  # one press isn't enough
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500
        await t.press(OWNER, f"dno:{second['id']}")  # it was part of the 25$
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_an_order_id_with_leading_zeros_is_the_same_payment(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await choose(t, BOB, 25)
        calls = await t.send(BOB, "00" + ORDER)
        assert "This Order ID was already used" in texts(calls, BOB)[-1] and balance(t, BOB) == 0

    run(tmp_path, scenario)


# ==================================================== Binance can't be asked
def test_when_binance_refuses_this_server_deposits_go_to_the_owner(tmp_path):
    async def scenario(t: ShopTest) -> None:
        where = "Binance doesn't serve this server's location (HTTP 451)"
        with_binance(t, FakeBinance(pay(amount="25"), error=BinanceError(where)))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1]
        assert any("Automatic deposits don't work" in x and "HTTP 451" in x for x in owner_texts(calls))
        (request,) = t.db.pending_deposits()
        assert f"{ORDER}: not checked ({where})" in request["note"] and request["unverified"] == 1

    run(tmp_path, scenario)


def test_when_binance_stays_unreachable_the_deposit_goes_to_the_owner(tmp_path, monkeypatch):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceUnavailable("Binance is busy (HTTP 503)")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        mark = len(t.tg.calls)
        calls = await t.send(ALICE, ORDER)
        for _ in range(3):  # checked again and again within a minute: it may pass by itself
            assert "can't be reached right now" in t.last_text(calls, ALICE["id"])
            calls = await t.press(ALICE, t.button(calls, "Check again"))
        assert "can't be reached right now" in t.last_text(calls, ALICE["id"])
        assert owner_texts(t.tg.calls[mark:]) == [] and t.db.pending_deposits() == []
        monkeypatch.setattr(autopay_module, "UNREACHABLE_FOR", 0)  # ... but not for long
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        assert "Sent to the owner" in texts(calls, ALICE)[-1]
        (request,) = t.db.pending_deposits()
        assert "not checked (Binance couldn't be reached: Binance is busy (HTTP 503))" in request["note"]
        assert request["unverified"] == 1 and request["order_id"] == ORDER
        # The approval marks the payment as added: once Binance is back, not again.
        await t.press(OWNER, f"dok:{request['id']}")
        fake.error = None
        await choose(t, ALICE, 25)
        assert "already added to your balance" in texts(await t.send(ALICE, ORDER), ALICE)[-1]
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_binance_unreachable_twice_a_minute_apart_sends_the_deposit_to_the_owner(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25"), error=BinanceUnavailable("busy")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "if it still can't be reached then, the owner checks your payment by hand" in t.last_text(calls)
        t.app.user_data[ALICE["id"]]["state"]["unreachable"][0] -= 61  # as if a minute ago
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        assert "Sent to the owner" in t.last_text(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_cancelling_while_binance_cannot_be_reached_offers_nothing(tmp_path):
    class SlowDown(FakeBinance):
        go = asyncio.Event()

        async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
            await self.go.wait()
            raise BinanceUnavailable("busy")

    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, SlowDown())
        SlowDown.go = asyncio.Event()
        register(t, ALICE)
        await choose(t, ALICE, 25)
        settle = t.settle

        async def quick() -> None:
            await asyncio.sleep(0.01)

        t.settle = quick  # type: ignore[method-assign]
        await t.send(ALICE, ORDER)
        await t.press(ALICE, "cancel")
        mark = len(t.tg.calls)
        fake.go.set()
        t.settle = settle  # type: ignore[method-assign]
        await t.settle()
        calls = t.tg.calls[mark:]
        assert "Cancelled" in t.last_text(calls, ALICE["id"]) and not t.has_button(calls, "Check again")
        assert owner_texts(calls) == [] and t.db.pending_deposits() == []

    run(tmp_path, scenario)


def test_stopping_lets_a_payment_check_finish_before_binance_is_closed(tmp_path):
    from shopbot import app as app_module

    async def scenario(t: ShopTest) -> None:
        events: list[str] = []

        class Client:
            async def close(self) -> None:
                events.append("closed")

        async def check() -> None:  # started while the bot was stopping: nobody awaits it
            await asyncio.sleep(0.05)
            events.append("checked")

        t.app.bot_data["binance"] = Client()
        task = asyncio.get_running_loop().create_task(check(), name=f"shopbot:binance:{ALICE['id']}")
        await app_module._post_stop(t.app)
        assert events == ["checked", "closed"] and task.done()

    run(tmp_path, scenario)


def test_a_check_that_hangs_past_the_stop_is_ended_and_says_so(tmp_path, monkeypatch):
    from shopbot import app as app_module

    monkeypatch.setattr(app_module, "CHECKS_TIMEOUT", 0.01)

    async def scenario(t: ShopTest) -> None:
        with_binance(t, Hanging())  # still connecting when the bot stops
        register(t, ALICE)
        await choose(t, ALICE, 25)
        settle = t.settle

        async def quick() -> None:
            await asyncio.sleep(0.01)

        t.settle = quick  # type: ignore[method-assign]
        await t.send(ALICE, ORDER)
        mark = len(t.tg.calls)
        started = asyncio.get_running_loop().create_task(asyncio.sleep(3600), name="binance-check")
        t.app.bot_data["binance_check"] = started
        await app_module._post_stop(t.app)
        t.settle = settle  # type: ignore[method-assign]
        assert "restarted while checking" in t.last_text(t.tg.calls[mark:], ALICE["id"])
        assert started.cancelled()
        assert not [x for x in asyncio.all_tasks() if x.get_name().startswith("shopbot:binance:")]

    run(tmp_path, scenario)


class Hanging(FakeBinance):
    """Binance that never answers (every host hangs)."""

    async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
        await asyncio.sleep(3600)
        return None


def test_a_stop_doesnt_wait_without_limit_for_a_check_started_before_it(tmp_path, monkeypatch):
    from shopbot import app as app_module

    monkeypatch.setattr(app_module, "CHECKS_TIMEOUT", 0.01)

    async def scenario(t: ShopTest) -> None:
        with_binance(t, Hanging())
        register(t, ALICE)
        await choose(t, ALICE, 25)
        settle = t.settle

        async def quick() -> None:
            await asyncio.sleep(0.01)

        t.settle = quick  # type: ignore[method-assign]
        await t.send(ALICE, ORDER)  # the check starts while the bot runs
        mark = len(t.tg.calls)
        await asyncio.wait_for(t.app.stop(), timeout=5)  # doesn't wait for it ...
        await app_module._post_stop(t.app)  # ... this does, for a bounded time, then ends it
        t.settle = settle  # type: ignore[method-assign]
        assert "restarted while checking" in t.last_text(t.tg.calls[mark:], ALICE["id"])
        await t.app.start()  # for the harness, which stops it at the end

    run(tmp_path, scenario)


def test_a_check_binance_never_answers_ends_as_an_outage(tmp_path, monkeypatch):
    monkeypatch.setattr(autopay_module, "CHECK_DEADLINE", 0.05)

    async def scenario(t: ShopTest) -> None:
        with_binance(t, Hanging())
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "can't be reached right now" in t.last_text(calls, ALICE["id"])
        assert t.has_button(calls, "Check again") and t.db.pending_deposits() == []

    run(tmp_path, scenario)


def test_a_check_cut_off_by_the_stop_still_answers_before_the_bot_is_gone(tmp_path, monkeypatch):
    from shopbot import app as app_module

    monkeypatch.setattr(app_module, "CHECKS_TIMEOUT", 0.01)  # Binance hangs longer than allowed

    async def scenario(t: ShopTest) -> None:
        events: list[str] = []

        class Client:
            async def close(self) -> None:
                events.append("closed")

        async def check() -> None:
            await asyncio.sleep(0.1)  # its request fails once the client is closed ...
            events.append("answered")  # ... and the customer is told

        t.app.bot_data["binance"] = Client()
        task = asyncio.get_running_loop().create_task(check(), name=f"shopbot:binance:{ALICE['id']}")
        await app_module._post_stop(t.app)
        assert events == ["closed", "answered"] and task.done()

    run(tmp_path, scenario)


# ============================================================ the client
def test_order_ids_are_cleaned_and_read():
    assert clean_order_id(" 466000199987322880 ") == ORDER
    assert clean_order_id("4660 0019 9987 3228 80") == ORDER
    assert clean_order_id("Order ID: 466000199987322880") == ORDER
    assert clean_order_id("p_a25pqjvad2171114") == TX
    assert clean_order_id("I paid") is None and clean_order_id("12345") is None
    assert clean_order_id("0" + ORDER) == ORDER and clean_order_id("00 " + ORDER) == ORDER  # one spelling
    assert clean_order_id("0" * 18) is None and clean_order_id("000000012345") is None
    assert binance_module._payment({"orderId": "0" + ORDER, "amount": "1"}).order_id == ORDER
    paid = order_id_time(ORDER)
    assert paid is not None and time.gmtime(paid / 1000)[:2] == (2026, 11)
    assert order_id_time("999999999999999999999") is None  # in the future: not trusted
    assert Payment(ORDER, TX, "C2C", Decimal("0.019"), "USDT", 0, "", "").cents() == 1


def fake_api(handler) -> BinancePay:
    client = BinancePay("k" * 64, "s" * 64, base_urls=("https://api.test",),
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return client


def record(order_id: str, amount: str = "10", minutes_ago: float = 1, kind: str = "C2C") -> dict:
    return {"orderType": kind, "transactionId": f"P_{order_id[-12:]}", "orderId": order_id,
            "transactionTime": int((time.time() - minutes_ago * 60) * 1000), "amount": amount,
            "currency": "USDT", "payerInfo": {"name": "Ali"}, "note": ""}


def test_the_client_signs_its_requests_and_reads_the_history(monkeypatch):
    monkeypatch.setattr(binance_module, "MIN_GAP", 0)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.url.query.decode()))
        seen.append({"path": request.url.path, "key": request.headers.get("X-MBX-APIKEY"), **params})
        signature = params.pop("signature")
        expected = hmac.new(b"s" * 64, urlencode(params).encode(), hashlib.sha256).hexdigest()
        assert signature == expected
        return httpx.Response(200, json={"code": "000000", "message": "success", "success": True,
                                         "data": [record(ORDER, "25")]})

    async def go() -> Any:
        client = fake_api(handler)
        try:
            return await client.find(ORDER, int((time.time() - 3600) * 1000))
        finally:
            await client.close()

    found = asyncio.run(go())
    assert found is not None and found.amount == Decimal("25") and found.received
    assert seen[0]["path"] == "/sapi/v1/pay/transactions" and seen[0]["key"] == "k" * 64
    assert seen[0]["limit"] == "100" and "timestamp" in seen[0] and "recvWindow" in seen[0]


def history_server(records: list[dict], newest_first: bool, requests: list[tuple[int, int]]):
    """Binance's Pay history: up to 100 of the payments in [startTime, endTime], newest or
    oldest first (its documentation and its real answers disagree on which)."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.url.query.decode()))
        start, end = int(params["startTime"]), int(params["endTime"])
        requests.append((start, end))
        inside = sorted((r for r in records if start <= r["transactionTime"] <= end),
                        key=lambda r: r["transactionTime"], reverse=newest_first)
        return httpx.Response(200, json={"code": "000000", "success": True,
                                         "data": inside[:int(params["limit"])]})

    return handler


@pytest.mark.parametrize("newest_first", [True, False])
def test_the_client_finds_a_payment_in_a_busy_history_in_either_order(monkeypatch, newest_first):
    monkeypatch.setattr(binance_module, "MIN_GAP", 0)
    wanted = "466000199987300001"
    records = [record(f"4660001999874{i:05d}", minutes_ago=60 * i / 250) for i in range(250)]
    records.append(record(wanted, minutes_ago=37))
    requests: list[tuple[int, int]] = []

    async def go(order_id: str) -> Any:
        client = fake_api(history_server(records, newest_first, requests))
        try:
            now = int(time.time() * 1000)
            return await client.search(order_id, now - 3_600_000, now)
        finally:
            await client.close()

    found = asyncio.run(go(wanted))
    assert found is not None and found.order_id == wanted
    assert len(requests) <= binance_module.MAX_PAGES
    requests.clear()
    assert asyncio.run(go("466000199987399999")) is None  # not there: gives up in time
    assert len(requests) <= binance_module.MAX_PAGES


def test_the_client_tells_key_problems_from_outages_and_fixes_its_clock(monkeypatch):
    monkeypatch.setattr(binance_module, "MIN_GAP", 0)
    answers = {"mode": "clock"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000) + 5000})
        if answers["mode"] == "clock":
            answers["mode"] = "ok"
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"})
        if answers["mode"] == "ok":
            return httpx.Response(200, json={"code": "000000", "success": True, "data": []})
        if answers["mode"] == "key":
            return httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key, IP, or permissions"})
        return httpx.Response(429, json={"code": -1003, "msg": "Too many requests"})

    async def go() -> None:
        client = fake_api(handler)
        try:
            assert await client.page(0, int(time.time() * 1000)) == []
            assert 4000 < client._offset_ms < 6000  # now in step with Binance's clock
            answers["mode"] = "key"
            client._cache.clear()
            with pytest.raises(BinanceKeyError):
                await client.page(1, 2)
            answers["mode"] = "busy"
            with pytest.raises(BinanceUnavailable):
                await client.page(3, 4)
        finally:
            await client.close()

    asyncio.run(go())


def test_a_location_binance_refuses_is_reported_at_once_not_retried(monkeypatch):
    monkeypatch.setattr(binance_module, "MIN_GAP", 0)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        return httpx.Response(451, json={"code": 0, "msg": "Service unavailable from a restricted location"})

    async def go() -> None:
        client = BinancePay("k" * 64, "s" * 64, base_urls=("https://a.test", "https://b.test"),
                            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            with pytest.raises(BinanceError) as caught:
                await client.page(0, 1)
            assert not isinstance(caught.value, BinanceUnavailable) and "HTTP 451" in str(caught.value)
        finally:
            await client.close()

    asyncio.run(go())
    assert asked == ["a.test"]  # the same on every host: lasting, not an outage


def test_a_search_after_a_clock_correction_reaches_binances_now(monkeypatch):
    # The server's clock is 30 s behind Binance's. The first signed call is refused (-1021)
    # and the clock corrected, but that search ended at the server's slow "now", before a
    # payment made 20 s ago (by Binance's clock): it's searched for once more.
    monkeypatch.setattr(binance_module, "MIN_GAP", 0)
    ahead = 30_000
    paid = int(time.time() * 1000) + ahead - 20_000
    wanted = "P_CLOCKTEST123"
    windows: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        binance_now = int(time.time() * 1000) + ahead
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": binance_now})
        params = dict(parse_qsl(request.url.query.decode()))
        if binance_now - int(params["timestamp"]) > int(params["recvWindow"]):
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside the recvWindow"})
        start, end = int(params["startTime"]), int(params["endTime"])
        windows.append((start, end))
        payment = {"orderType": "C2C", "transactionId": wanted, "orderId": "", "transactionTime": paid,
                   "amount": "25", "currency": "USDT"}
        return httpx.Response(200, json={"code": "000000", "success": True,
                                         "data": [payment] if start <= paid <= end else []})

    async def go() -> Any:
        client = fake_api(handler)
        try:
            return await client.find(wanted, paid - 3_600_000)
        finally:
            await client.close()

    found = asyncio.run(go())
    assert found is not None and found.transaction_id == wanted
    assert windows[0][1] < paid <= windows[-1][1]  # the first window ended too early


def test_a_closed_client_stays_closed(monkeypatch):
    monkeypatch.setattr(binance_module, "MIN_GAP", 0)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json={"code": "000000", "success": True, "data": []})

    async def go() -> None:
        client = fake_api(handler)
        assert await client.page(0, 1) == []
        await client.close()  # the bot stops; a check still running mustn't open a new one
        with pytest.raises(BinanceUnavailable):
            await client.page(5_000, 9_000)  # another window: not answered from the cache
        assert client._http is None

    asyncio.run(go())
    assert asked == ["/sapi/v1/pay/transactions"]


def test_the_key_check_warns_about_a_key_that_can_do_too_much(monkeypatch):
    monkeypatch.setattr(binance_module, "MIN_GAP", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("apiRestrictions"):
            return httpx.Response(200, json={"enableReading": True, "enableWithdrawals": True,
                                             "ipRestrict": False})
        return httpx.Response(200, json={"code": "000000", "success": True, "data": [record(ORDER)]})

    original = binance_module.BinancePay

    def patched(key: str, secret: str, **kwargs: Any) -> BinancePay:
        return original(key, secret, base_urls=("https://api.test",),
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(binance_module, "BinancePay", patched)
    report = asyncio.run(binance_module.check("k" * 64, "s" * 64))
    assert "the key works" in report and "1 Pay payment(s)" in report
    assert "enableWithdrawals" in report and "restrict the key" in report
    assert json.dumps(report)


def test_the_key_check_warns_when_the_key_is_not_of_the_binance_id_customers_pay(monkeypatch):
    monkeypatch.setattr(binance_module, "MIN_GAP", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("apiRestrictions"):
            return httpx.Response(200, json={"enableReading": True, "ipRestrict": True})
        if request.url.path == "/api/v3/account":
            return httpx.Response(200, json={"uid": 123456789, "balances": []})
        return httpx.Response(200, json={"code": "000000", "success": True, "data": []})

    original = binance_module.BinancePay

    def patched(key: str, secret: str, **kwargs: Any) -> BinancePay:
        return original(key, secret, base_urls=("https://api.test",),
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(binance_module, "BinancePay", patched)
    report = asyncio.run(binance_module.check("k" * 64, "s" * 64, binance_id="850940840"))
    assert "WARNING: this key belongs to Binance ID 123456789" in report and "850940840" in report
    assert "restrict the key" not in report
    report = asyncio.run(binance_module.check("k" * 64, "s" * 64, binance_id="123456789"))
    assert "WARNING" not in report
