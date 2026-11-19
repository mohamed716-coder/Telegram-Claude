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
from shopbot.binance import BinanceKeyError, BinancePay, BinanceUnavailable, Payment, clean_order_id, order_id_time
from shopbot.db import PaymentUsed
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
        assert "Payment received!" in texts(await t.send(ALICE, ORDER), ALICE)[-1]
        calls = await t.press(OWNER, f"dok:{deposit['id']}")
        assert "already added to a balance" in t.alert(calls)
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


def test_without_a_key_deposits_use_a_receipt_as_before(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.press(ALICE, "dp")
        assert "Choose how much you want to add" in t.last_text(calls)
        calls = await t.press(ALICE, "da:10")
        assert "photo of the payment receipt" in t.last_text(calls)

    run(tmp_path, scenario)


# ============================================================ the client
def test_order_ids_are_cleaned_and_read():
    assert clean_order_id(" 466000199987322880 ") == ORDER
    assert clean_order_id("4660 0019 9987 3228 80") == ORDER
    assert clean_order_id("Order ID: 466000199987322880") == ORDER
    assert clean_order_id("p_a25pqjvad2171114") == TX
    assert clean_order_id("I paid") is None and clean_order_id("12345") is None
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
