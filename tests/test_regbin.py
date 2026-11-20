"""🧾 /regbin and /addbal with an Order ID: a Binance Pay payment the owner added by hand
is recorded as added, under both its IDs, so it's never added again. With automatic
deposits on, Binance is asked first and the owner confirms what it has. Binance is the
stand-in from test_autopay."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

from harness import OWNER, OWNER_ID, ShopTest, run
from shopbot.binance import EPOCH_2020, BinanceKeyError, BinanceStopping, BinanceUnavailable, Payment
from shopbot.handlers import autopay as autopay_module
from shopbot.handlers.money import deposit_caption
from test_autopay import (ORDER, OTHER, TX, FakeBinance, balance, choose, owner_texts, pay, request_text,
                          texts, with_binance)
from test_shop import ALICE, BOB, register


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(autopay_module, "CHECK_GAP", 0)


CARL = {"id": 1003, "is_bot": False, "first_name": "Carl", "username": "carl"}


def deposits_of(t: ShopTest, user: dict) -> list:
    return t.db.conn.execute("SELECT * FROM deposits WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()


async def to_the_owner(t: ShopTest, user: dict, amount: int, order_id: str = ORDER) -> dict:
    """Two failed checks: the deposit becomes a request waiting for the owner."""
    await choose(t, user, amount)
    await t.send(user, order_id)
    await t.send(user, order_id)
    (request,) = t.db.pending_deposits()
    return request


async def confirm(t: ShopTest, calls: list, *, anyway: bool = False) -> list:
    """Press ✅ on "Binance has this payment" (shown once Binance was asked), or ⚠️ ... anyway
    when it says the payment may be added already."""
    assert any("Binance has this payment" in x for x in owner_texts(calls)), owner_texts(calls)
    shown = [x for x in owner_texts(calls) if "Binance has this payment" in x][-1]
    assert ("May be added already" in shown) == anyway, shown
    return calls + await t.press(OWNER, t.button(calls, "anyway" if anyway else "✅"))


async def by_hand(t: ShopTest, text: str) -> list:
    """An /addbal or /regbin with an Order ID, confirmed."""
    return await confirm(t, await t.send(OWNER, text))


# ===================================================================== /regbin
def test_a_registered_order_id_is_never_added_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10")))
        register(t, ALICE)
        register(t, BOB)
        calls = await t.send(OWNER, f"/regbin {ORDER} {ALICE['id']}")
        shown = owner_texts(calls)
        assert f"Checking Order ID <code>{ORDER}</code> with Binance" in shown[0]
        assert "Binance has this payment" in shown[1] and "<b>10 USDT</b>, paid " in shown[1]
        assert "from Ali" in shown[1] and f"<code>{ORDER}</code> · <code>{TX}</code>" in shown[1]
        assert "Register it as added by hand for <b>Alice</b>" in shown[1]
        assert t.db.binance_payment(ORDER) is None  # nothing before the ✅
        calls = await confirm(t, calls)
        shown = owner_texts(calls)[-1]
        assert "✅ Done." in shown and "Paid by <b>Alice</b>" in shown
        assert f"Order ID <code>{ORDER}</code> is registered as added" in shown
        row = t.db.binance_payment(TX)  # known by both its IDs
        assert row is not None and row["order_id"] == ORDER and row["status"] == "credited"
        assert (row["user_id"], row["amount"], row["raw_amount"], row["currency"]) == (ALICE["id"], 0, "10", "USDT")
        assert row["paid_at"] and row["deposit_id"] is None
        assert balance(t, ALICE) == 0 and deposits_of(t, ALICE) == []  # nothing added by /regbin
        # Alice tries to have it added automatically: it was hers, added by hand already.
        await choose(t, ALICE, 10)
        calls = await t.send(ALICE, ORDER)
        assert "already added to your balance" in texts(calls, ALICE)[-1]
        assert not [x for x in owner_texts(calls) if "failed" in x]  # not a failed try
        assert balance(t, ALICE) == 0
        # Anyone else, by either ID: already used, and the owner hears it's one he registered.
        await choose(t, BOB, 10)
        calls = await t.send(BOB, TX)
        assert "This Order ID was already used" in texts(calls, BOB)[-1]
        assert "registered as added by hand for <b>Alice</b>" in owner_texts(calls)[-1]
        assert balance(t, BOB) == 0 and t.db.pending_deposits() == []

    run(tmp_path, scenario)


def test_a_confirmation_works_once_and_can_be_cancelled(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10"), pay(order_id=OTHER, tx="P_OTHER", amount="7")))
        register(t, ALICE)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        add = t.button(calls, "✅ Add 10.00$")
        calls = await t.press(ALICE, add)
        assert "Only the owner can do that." in t.alert(calls)
        await t.press(OWNER, add)
        calls = await t.press(OWNER, add)  # a second press
        assert "This has expired" in t.alert(calls) and balance(t, ALICE) == 1000
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 7 {OTHER}")
        calls = await t.press(OWNER, t.button(calls, "Cancel"))
        assert "Cancelled: nothing was added." in owner_texts(calls)[-1]
        assert balance(t, ALICE) == 1000 and t.db.binance_payment(OTHER) is None

    run(tmp_path, scenario)


def test_without_a_user_the_payment_is_added_already_whoever_sends_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10")))
        register(t, ALICE)
        calls = await by_hand(t, f"/regbin #{ORDER}")  # as some apps copy it
        assert "Paid by" not in owner_texts(calls)[-1]
        assert t.db.binance_payment(ORDER)["user_id"] == OWNER_ID
        for _ in range(3):  # however often: never a failed try, never a request
            await choose(t, ALICE, 10)
            calls = await t.send(ALICE, ORDER)
            assert "already used: this payment was already added by the owner" in texts(calls, ALICE)[-1]
            shown = owner_texts(calls)
            assert len(shown) == 1 and "<b>Alice</b>" in shown[0] and "which you registered" in shown[0]
        assert t.db.pending_deposits() == [] and balance(t, ALICE) == 0
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        assert "Nothing was registered" in owner_texts(calls)[-1] and "was used already" in owner_texts(calls)[-1]
        assert "registered as added by hand (" in owner_texts(calls)[-1]

    run(tmp_path, scenario)


def test_a_request_naming_only_payments_added_already_cant_be_approved(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        register(t, CARL)
        bobs = await to_the_owner(t, BOB, 20)  # Bob claims Alice's payment: kept for his request
        await t.press(OWNER, f"dno:{bobs['id']}")
        await t.send(OWNER, f"/addbal {ALICE['id']} 25")  # Alice's 25$, by hand
        await by_hand(t, f"/regbin {ORDER} {CARL['id']}")  # registered for Carl (a mistake)
        await choose(t, ALICE, 25)  # Alice sends it now: used already, her deposit is cancelled
        assert "This deposit was cancelled." in texts(await t.send(ALICE, ORDER), ALICE)[-1]
        assert t.db.pending_deposits() == []
        # A request naming only such payments (an older version's, or sent a moment before)
        alices = t.db.create_review_request(ALICE["id"], 2500, ORDER, f"{ORDER}: already used", [], [ORDER])
        assert alices["order_id"] is None and alices["unconfirmed"] == ORDER
        calls = await t.press(OWNER, f"dok:{alices['id']}")
        assert "can't be approved" in t.alert(calls) and balance(t, ALICE) == 2500
        shown = t.texts(calls, OWNER_ID)[-1]
        assert "⛔ <b>Can't be approved:</b> This request names no payment that can still be added" in shown
        assert f"⚠️ Order ID <code>{ORDER}</code> was added already: you registered it as added by hand for Carl (" in shown
        assert t.button(calls, "Reject") == f"dno:{alices['id']}" and t.button(calls, "✅ Approve") == f"dok:{alices['id']}"
        calls = await t.press(OWNER, f"dokx:{alices['id']}")  # an old button: still no
        assert "can't be approved" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_registering_a_payment_for_its_customer_closes_their_request_naming_only_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        alices = await to_the_owner(t, ALICE, 20)  # paid 25, chose 20: her request holds it
        await t.send(OWNER, f"/addbal {ALICE['id']} 25")  # the owner pays her out by hand
        assert alices["order_id"] == ORDER and t.db.binance_payment(ORDER)["deposit_id"] == alices["id"]
        calls = await by_hand(t, f"/regbin {ORDER} {ALICE['id']}")
        assert f"Request #{alices['id']} was waiting with it: closed." in owner_texts(calls)[-1]
        assert any(f"Your deposit request #{alices['id']} (20.00$) is closed" in x for x in texts(calls, ALICE))
        assert t.db.get_deposit(alices["id"])["status"] == "closed" and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_payment_shown_in_an_approved_request_is_never_added_again_by_hand(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20"), pay(order_id=OTHER, tx="P_OTHER", amount="5")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # 20 USDT, not 25
        await t.send(ALICE, OTHER)  # 5 USDT, not 25: both kept for one request
        (request,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{request['id']}")  # 25$ for both payments
        assert balance(t, ALICE) == 2500 and t.db.binance_payment(ORDER)["status"] == "credited"
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 20 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert "Nothing was added" in shown and "was used already" in shown
        assert f"added with request #{request['id']}: <b>25.00$</b> to <b>Alice</b>" in shown
        calls = await t.send(OWNER, f"/regbin {TX} {ALICE['id']}")  # by its other ID too
        assert "Nothing was registered" in owner_texts(calls)[-1] and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_payment_known_by_its_other_id_is_never_added_again_by_hand(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        # Bob's request named the Order ID while Binance didn't list it; the owner approved it.
        earlier = t.db.create_review_request(BOB["id"], 2500, ORDER, "", [], [ORDER])
        t.db.decide_deposit(earlier["id"], True, 10**9)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {TX}")  # the same payment's other ID
        shown = owner_texts(calls)[-1]
        assert "Nothing was added" in shown
        assert f"added with request #{earlier['id']}: <b>25.00$</b> to <b>Bob</b>" in shown
        assert balance(t, ALICE) == 0 and t.db.binance_payment(ORDER)["user_id"] == BOB["id"]
        assert t.db.conn.execute("SELECT COUNT(*) FROM binance_payments").fetchone()[0] == 1
        # An older version's record kept under the transaction ID alone: found the same way.
        t.db.conn.execute("UPDATE binance_payments SET order_id = ? WHERE order_id = ?", (TX, ORDER))
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}")
        assert "Nothing was added" in owner_texts(calls)[-1] and balance(t, ALICE) == 0

    run(tmp_path, scenario)


def test_balance_added_by_hand_still_holds_up_earlier_payments(tmp_path):
    # Balance added by hand may have been any payment made before it: the customer's other
    # payments, and anyone else's, go to the owner.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10", minutes_ago=30),
                                    pay(order_id=OTHER, tx="P_OTHER", amount="25", minutes_ago=10)))
        register(t, ALICE)
        register(t, BOB)
        await by_hand(t, f"/addbal {ALICE['id']} 10 {ORDER}")
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, OTHER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 1000
        assert "you added 10.00$ by hand (" in request_text(calls)

    run(tmp_path, scenario)


def test_an_order_id_already_added_is_refused_and_says_how_it_was_added(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # added automatically
        assert balance(t, ALICE) == 2500
        calls = await t.send(OWNER, f"/regbin {TX} {BOB['id']}")
        shown = owner_texts(calls)[-1]
        assert "Nothing was registered" in shown and "added automatically: <b>25.00$</b> to <b>Alice</b>" in shown
        calls = await t.send(OWNER, f"/addbal {BOB['id']} 25 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert "Nothing was added" in shown and "was used already" in shown and "Alice" in shown
        assert balance(t, BOB) == 0 and deposits_of(t, BOB) == []
        assert t.db.binance_payment(ORDER)["user_id"] == ALICE["id"]

    run(tmp_path, scenario)


def test_a_payment_waiting_with_a_request_is_settled_by_hand_only_for_that_customer(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20")))
        register(t, ALICE)
        register(t, BOB)
        request = await to_the_owner(t, ALICE, 25)  # paid 20, chose 25: the owner decides
        assert t.db.binance_payment(ORDER)["deposit_id"] == request["id"]
        # Without saying whose it is, or for someone else: decide the request first.
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        shown = owner_texts(calls)[-1]
        assert "Nothing was registered" in shown and "decide that first" in shown
        assert f"waiting with request #{request['id']} from <b>Alice</b>" in shown
        assert f"/regbin {ORDER} {ALICE['id']}: that closes the request" in shown
        calls = await t.send(OWNER, f"/addbal {BOB['id']} 20 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert "Nothing was added" in shown and "decide that first" in shown and "closes" not in shown
        assert balance(t, BOB) == 0 and t.db.binance_payment(ORDER)["status"] == "owner"
        # Alice's own: adding it by hand settles her request.
        calls = await by_hand(t, f"/addbal {ALICE['id']} 20 {ORDER}")
        assert f"Request #{request['id']} was waiting with it: closed." in owner_texts(calls)[-1]
        assert any(f"Your deposit request #{request['id']} (25.00$) is closed" in x for x in texts(calls, ALICE))
        assert balance(t, ALICE) == 2000
        row = t.db.binance_payment(ORDER)
        manual = deposits_of(t, ALICE)[-1]
        assert (row["status"], row["user_id"], row["amount"]) == ("credited", ALICE["id"], 2000)
        assert (row["deposit_id"], row["held_before"]) == (manual["id"], request["id"])
        assert (manual["method"], manual["order_id"]) == ("manual", ORDER)
        assert t.db.get_deposit(request["id"])["status"] == "closed"
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "already closed" in t.alert(calls) and balance(t, ALICE) == 2000
        # It can't come back to the owner, or be added again.
        await choose(t, ALICE, 20)
        calls = await t.send(ALICE, ORDER)
        assert "already added to your balance" in texts(calls, ALICE)[-1]
        assert t.db.pending_deposits() == [] and balance(t, ALICE) == 2000

    run(tmp_path, scenario)


def test_a_request_holding_other_payments_too_is_decided_first(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20"), pay(order_id=OTHER, tx="P_OTHER", amount="5")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, OTHER)  # one request holds both
        (request,) = t.db.pending_deposits()
        calls = await t.send(OWNER, f"/regbin {ORDER} {ALICE['id']}")
        shown = owner_texts(calls)[-1]
        assert "Nothing was registered" in shown and "decide that first" in shown
        assert t.db.get_deposit(request["id"])["status"] == "pending"
        assert t.db.binance_payment(ORDER)["status"] == "owner"

    run(tmp_path, scenario)


def test_registering_closes_the_customers_requests_naming_that_order_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceKeyError("refused")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # Binance refuses the key: straight to the owner
        (request,) = t.db.pending_deposits()
        assert request["order_id"] == ORDER and t.db.binance_payment(ORDER) is None
        fake.error = None
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}")
        assert "That's not the" not in owner_texts(calls)[-1]  # Binance has 25 USDT
        calls = await confirm(t, calls)
        assert f"Request #{request['id']} was waiting with it: closed." in owner_texts(calls)[-1]
        assert t.db.get_deposit(request["id"])["status"] == "closed"
        assert "closed: added by hand" in t.db.get_deposit(request["id"])["note"]
        assert balance(t, ALICE) == 2500
        # Approving the closed request is no longer possible.
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "already closed" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_what_binance_has_is_shown_before_anything_is_added(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10", payer="Bob B."),
                                    pay(order_id=OTHER, tx="P_SENT", amount="-5")))
        register(t, ALICE)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 12 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert "<b>10 USDT</b>" in shown and "from Bob B." in shown  # a typo would show here
        assert "⚠️ That's not the <b>12.00$</b> you're adding." in shown
        assert "Add <b>12.00$</b> to <b>Alice</b>" in shown and balance(t, ALICE) == 0
        calls = await t.send(OWNER, f"/regbin {OTHER}")
        assert "Nothing was registered: this ID isn't a payment to the shop." in owner_texts(calls)[-1]
        assert t.db.binance_payment(OTHER) is None

    run(tmp_path, scenario)


def test_an_order_id_binance_cant_confirm_isnt_recorded(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        assert "Nothing was registered: Binance doesn't list a payment with this ID in the last 89 days" \
            in owner_texts(calls)[-1]
        fake.error = BinanceUnavailable("busy")
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        assert "Nothing was added: Binance can't be asked right now (busy): try again in a minute." \
            in owner_texts(calls)[-1]
        fake.error = BinanceStopping("stopping")
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        assert "the bot restarted while asking Binance" in owner_texts(calls)[-1]
        assert t.db.binance_payment(ORDER) is None and balance(t, ALICE) == 0

    run(tmp_path, scenario)


def test_without_a_binance_key_only_order_ids_are_registered(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        (shown,) = owner_texts(calls)
        assert "registered as added" in shown and "Checking" not in shown
        calls = await t.send(OWNER, f"/regbin {TX}")  # can't be told apart from its Order ID
        assert "That's Binance's transaction ID" in owner_texts(calls)[0]
        assert t.db.binance_payment(TX) is None
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 5")
        assert "/regbin ORDER_ID" not in owner_texts(calls)[0]  # no tip without automatic deposits

    run(tmp_path, scenario)


def test_regbin_input_is_checked(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/regbin")
        assert "Register a Binance payment" in owner_texts(calls)[0]
        assert "/addbal USER_ID AMOUNT ORDER_ID" in owner_texts(calls)[0]
        calls = await t.send(OWNER, "/regbin 12345")
        assert "isn't a Binance Pay Order ID" in owner_texts(calls)[0]
        calls = await t.send(OWNER, f"/regbin {ORDER} bob")
        assert "isn't a valid user ID" in owner_texts(calls)[0]
        calls = await t.send(OWNER, f"/regbin {ORDER} 1 2")
        assert "Register a Binance payment" in owner_texts(calls)[0]
        calls = await t.send(ALICE, f"/regbin {ORDER}")
        assert "Only the owner can use this command" in texts(calls, ALICE)[0]
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 12345")
        assert "isn't a Binance Pay Order ID" in owner_texts(calls)[0]
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER} more")
        assert "isn't a Binance Pay Order ID" in owner_texts(calls)[0]
        assert t.db.binance_payment(ORDER) is None and balance(t, ALICE) == 0

    run(tmp_path, scenario)


# ============================================================ the Add balance prompt
def test_add_balance_asks_for_the_order_id_after_the_amount_and_it_is_optional(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10"), pay(order_id=OTHER, tx="P_OTHER", amount="7")))
        register(t, ALICE)
        await t.send(OWNER, "/addbal")
        calls = await t.send(OWNER, str(ALICE["id"]))
        assert "Next you can give the Binance Pay Order ID of the payment, or skip it." in owner_texts(calls)[0]
        calls = await t.send(OWNER, "10")
        shown = owner_texts(calls)[0]
        assert "Order ID (optional)" in shown and "Amount: <b>10.00$</b> (not added yet)" in shown
        assert "<b>Alice</b>" in shown and "If it isn't one, press ⏭ Skip." in shown
        assert t.button(calls, "⏭ Skip: add 10.00$ to Alice").startswith("abk:")
        assert t.button(calls, "Cancel") == "cancel"
        assert balance(t, ALICE) == 0  # nothing yet
        calls = await t.send(OWNER, "not-an-id")
        shown = owner_texts(calls)[0]
        assert "isn't a Binance Pay Order ID" in shown and "press ⏭ Skip to add 10.00$ without one" in shown
        assert t.has_button(calls, "⏭ Skip: add 10.00$") and balance(t, ALICE) == 0
        calls = await confirm(t, await t.send(OWNER, ORDER))
        shown = owner_texts(calls)[-1]
        assert "Added <b>10.00$</b>" in shown and "registered as added" in shown
        assert balance(t, ALICE) == 1000 and t.db.binance_payment(ORDER)["user_id"] == ALICE["id"]
        # Skipped: added without one, and no tip about /regbin (it was asked).
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "5")
        calls = await t.press(OWNER, t.button(await t.send(OWNER, "nope"), "⏭ Skip: add 5.00$"))
        shown = owner_texts(calls)
        assert "Added <b>5.00$</b>" in shown[-1] and "/regbin" not in shown[-1]
        assert balance(t, ALICE) == 1500 and t.db.payment_records("nope") == []
        assert "5.00$</b> was added to your balance" in texts(calls, ALICE)[-1]
        # Both in one message: no question, the same check. The balance added since OTHER
        # was paid may have been it: shown, and only "anyway" adds it.
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        calls = await t.send(OWNER, f"7 {OTHER}")
        shown = owner_texts(calls)[-1]
        assert "you added 10.00$ by hand (" in shown and "you added 5.00$ by hand (" in shown
        assert t.button(calls, "⚠️ Add 7.00$ anyway").startswith("pyc:") and not t.has_button(calls, "✅")
        calls = await confirm(t, calls, anyway=True)
        assert "Added <b>7.00$</b>" in owner_texts(calls)[-1] and "registered as added" in owner_texts(calls)[-1]
        assert balance(t, ALICE) == 2200

    run(tmp_path, scenario)


def test_a_refused_order_id_in_the_prompt_asks_for_another_one_without_skip(tmp_path):
    # Right after the bot said a payment was added already (or waits with a request), Skip
    # could add that same money again without its Order ID: only another Order ID or Cancel.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10"), pay(order_id=OTHER, tx="P_OTHER", amount="10")))
        register(t, ALICE)
        register(t, BOB)
        await by_hand(t, f"/addbal {ALICE['id']} 10 {ORDER}")
        await t.send(OWNER, "/addbal")
        await t.send(OWNER, str(BOB["id"]))
        await t.send(OWNER, "10")
        calls = await t.send(OWNER, ORDER)  # Alice's, a typo
        shown = owner_texts(calls)[-1]
        assert "Nothing was added" in shown and "Send another Order ID, or press Cancel." in shown
        assert not t.has_button(calls, "Skip") and t.button(calls, "Cancel") == "cancel"
        assert balance(t, BOB) == 0
        calls = await t.send(OWNER, "nope")
        assert "Send only the Order ID" in owner_texts(calls)[0] and "Skip" not in owner_texts(calls)[0]
        calls = await confirm(t, await t.send(OWNER, OTHER), anyway=True)  # Alice's 10$ came after it
        assert "Added <b>10.00$</b>" in owner_texts(calls)[-1] and balance(t, BOB) == 1000

    run(tmp_path, scenario)


def test_the_skip_button_works_once_and_only_for_its_own_question(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        alices = t.button(await t.send(OWNER, "10"), "⏭ Skip: add 10.00$")
        calls = await t.send(OWNER, f"/addbal {BOB['id']}")  # a new question replaces it
        assert "⚠️ Add balance stopped: 10.00$ for Alice was not added." in owner_texts(calls)
        bobs = t.button(await t.send(OWNER, "20"), "⏭ Skip: add 20.00$")
        calls = await t.press(OWNER, alices)
        assert "This has expired" in t.alert(calls) and balance(t, ALICE) == 0
        calls = await t.press(ALICE, bobs)
        assert "Only the owner can do that." in t.alert(calls)
        calls = await t.press(OWNER, bobs)
        assert "Added <b>20.00$</b>" in owner_texts(calls)[-1] and balance(t, BOB) == 2000
        calls = await t.press(OWNER, bobs)  # a second press
        assert "This has expired" in t.alert(calls) and "/modifybal" in t.alert(calls)
        assert balance(t, BOB) == 2000
        # Cancelled: nothing to skip to.
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        skip = t.button(await t.send(OWNER, "4"), "⏭ Skip: add 4.00$")
        calls = await t.press(OWNER, "cancel")
        assert not [x for x in owner_texts(calls) if "stopped" in x]  # cancelled on purpose: no warning
        calls = await t.press(OWNER, skip)
        assert "This has expired" in t.alert(calls) and balance(t, ALICE) == 0

    run(tmp_path, scenario)


def test_amounts_written_with_a_dollar_sign_still_work(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance())
        register(t, ALICE)
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        calls = await t.send(OWNER, "10 $")
        calls = await t.press(OWNER, t.button(calls, "⏭ Skip: add 10.00$"))
        assert "Added <b>10.00$</b>" in owner_texts(calls)[-1]
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 5 $")  # typed in full: no question
        assert "Added <b>5.00$</b>" in owner_texts(calls)[0]
        assert balance(t, ALICE) == 1500 and t.db.payment_records("$") == []

    run(tmp_path, scenario)


def test_without_automatic_deposits_add_balance_doesnt_ask_for_an_order_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']}")
        assert "Order ID" not in owner_texts(calls)[0]
        calls = await t.send(OWNER, "10")
        assert "Added <b>10.00$</b>" in owner_texts(calls)[0] and balance(t, ALICE) == 1000

    run(tmp_path, scenario)


def test_an_amount_or_a_word_next_to_the_order_id_never_becomes_part_of_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10"), pay(order_id=OTHER, tx="P_OTHER", amount="10"),
                                    pay(order_id="466000199987322889", tx="P_THIRD", amount="5")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 10)
        await t.send(ALICE, ORDER)  # added automatically
        assert balance(t, ALICE) == 1000
        # The amount and the Order ID together at the Order ID question: read as both, so a used
        # payment is still refused (not glued into "10466...").
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "10")
        calls = await t.send(OWNER, f"10 {ORDER}")
        assert "Nothing was added" in owner_texts(calls)[-1] and "was used already" in owner_texts(calls)[-1]
        calls = await t.send(OWNER, f"{ORDER}\n10")  # either way round
        assert "Nothing was added" in owner_texts(calls)[-1] and balance(t, ALICE) == 1000
        # Another amount than the one asked about: refused, nothing added.
        calls = await t.send(OWNER, f"15 {OTHER}")
        assert "The amount here is <b>10.00$</b>, not 15.00$" in owner_texts(calls)[0]
        assert t.db.payment_records(OTHER) == [] and balance(t, ALICE) == 1000
        # Words after a transaction ID, or several IDs: send only the Order ID.
        for text in ("P_OTHER ok", f"{OTHER} {ORDER} 10"):
            calls = await t.send(OWNER, text)
            assert "Send only the Order ID" in owner_texts(calls)[0], text
        assert t.db.conn.execute("SELECT COUNT(*) FROM binance_payments").fetchone()[0] == 1
        # The matching amount with a good Order ID is fine.
        calls = await confirm(t, await t.send(OWNER, f"10 {OTHER}"))
        assert "Added <b>10.00$</b>" in owner_texts(calls)[-1]
        assert t.db.binance_payment(OTHER)["user_id"] == ALICE["id"] and balance(t, ALICE) == 2000
        # At the amount step too: "AMOUNT ORDER_ID extra" isn't one ID.
        await t.send(OWNER, f"/addbal {BOB['id']}")
        calls = await t.send(OWNER, f"5 {ORDER} 5")
        assert "isn't a Binance Pay Order ID" in owner_texts(calls)[0] and balance(t, BOB) == 0
        # "Order ID: ..." and "#..." still work.
        await t.send(OWNER, "5")
        calls = await confirm(t, await t.send(OWNER, "Order ID: 466000199987322889"), anyway=True)
        assert "Added <b>5.00$</b>" in owner_texts(calls)[-1]
        assert t.db.binance_payment("466000199987322889")["user_id"] == BOB["id"]

    run(tmp_path, scenario)


def test_leaving_the_order_id_question_says_nothing_was_added(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance())
        register(t, ALICE)
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "10")
        calls = await t.press(OWNER, "menu")
        assert "⚠️ Add balance stopped: 10.00$ for Alice was not added." in owner_texts(calls)
        assert balance(t, ALICE) == 0
        # After a restart the question is gone: an Order ID sent then adds nothing, and says so.
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "10")
        t.app.user_data[OWNER_ID].pop("state")  # what a restart forgets
        calls = await t.send(OWNER, ORDER)
        assert "Nothing is waiting for an Order ID (the bot may have restarted)" in owner_texts(calls)[0]
        assert balance(t, ALICE) == 0 and t.db.payment_records(ORDER) == []
        calls = await t.send(ALICE, ORDER)  # a customer still gets the usual answer
        assert "Send /start to open the shop menu." in texts(calls, ALICE)[0]

    run(tmp_path, scenario)


def test_the_customer_is_told_even_if_the_confirmation_to_the_owner_fails(tmp_path):
    async def scenario(t: ShopTest) -> None:
        from telegram.error import TimedOut

        from test_shop import inject_failure

        with_binance(t, FakeBinance())
        register(t, ALICE)
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        skip = t.button(await t.send(OWNER, "10"), "⏭ Skip: add 10.00$")
        inject_failure(t, lambda method, params: method == "sendMessage" and params.get("chat_id") == OWNER_ID
                       and "Added <b>10.00$</b>" in params.get("text", ""), error=TimedOut)
        calls = await t.press(OWNER, skip)
        assert balance(t, ALICE) == 1000
        assert "10.00$</b> was added to your balance" in texts(calls, ALICE)[-1]
        assert "Something went wrong" not in "\n".join(owner_texts(calls))
        calls = await t.press(OWNER, skip)
        assert "check the balance with /modifybal" in t.alert(calls) and balance(t, ALICE) == 1000

    run(tmp_path, scenario)


# ===================================================== what the audit found: never twice
def test_a_balance_raised_with_modifybal_holds_up_payments_made_before_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceUnavailable("busy")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        await t.send(OWNER, f"/modifybal {ALICE['id']} 25")  # the owner pays it out this way
        fake.error = None
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2500
        assert "you raised a balance by 25.00$ with /modifybal (" in request_text(calls)

    run(tmp_path, scenario)


def test_skipped_order_id_holds_up_that_payment_for_every_account(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.press(OWNER, t.button(await t.send(OWNER, "25"), "⏭ Skip"))  # Alice's payment, no ID
        await choose(t, BOB, 25)  # her second account sends its Order ID
        calls = await t.send(BOB, ORDER)
        assert "Sent to the owner" in texts(calls, BOB)[-1] and balance(t, BOB) == 0
        assert "you added 25.00$ by hand for Alice (" in request_text(calls)
        (request,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_a_request_naming_a_transaction_id_binance_didnt_confirm_cant_be_approved(tmp_path):
    # Approving it would record the payment under its transaction ID alone: its Order ID could
    # then be approved or added again. The bot asks for the Order ID instead.
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceKeyError("refused")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # can't be checked: a request naming the Order ID
        (first,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{first['id']}")
        assert balance(t, ALICE) == 2500
        await choose(t, ALICE, 25)
        await t.send(ALICE, TX)  # the same payment's other ID, still can't be checked
        second = t.db.pending_deposits()[-1]
        calls = await t.press(OWNER, f"dok:{second['id']}")
        assert "can't be approved" in t.alert(calls) and balance(t, ALICE) == 2500
        assert "names Binance's transaction ID" in t.texts(calls, OWNER_ID)[-1]
        fake.error = None  # and when Binance answers again, both IDs lead to the one record
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, TX)
        assert "already added to your balance" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_balance_added_by_hand_before_a_request_is_shown_with_it(tmp_path):
    # Binance can't be asked; the owner adds the customer's payment by hand; her check later
    # comes to him: the request shows the balance added by hand, and takes ⚠️ Approve anyway.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(error=BinanceKeyError("refused")))
        register(t, ALICE)
        await t.send(OWNER, f"/addbal {ALICE['id']} 25")
        t.db.conn.execute("UPDATE deposits SET created_at = datetime(created_at, '-20 minutes'),"
                          " decided_at = datetime(decided_at, '-20 minutes')")
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "25.00$ by hand (" in request_text(calls)
        (request,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_receipt_approved_before_automatic_checking_is_shown_with_a_request_for_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        receipt = t.db.create_deposit(ALICE["id"], 2500, "file", "photo")
        t.db.decide_deposit(receipt["id"], True, 10**9)  # approved from its receipt, a day ago
        t.db.conn.execute("UPDATE deposits SET created_at = datetime(created_at, '-1 day'),"
                          " decided_at = datetime(decided_at, '-1 day')")
        with_binance(t, FakeBinance())  # then the key: the payment is too old to be found
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        calls = await t.send(ALICE, ORDER)
        assert f"with a receipt, request #{receipt['id']}" in request_text(calls)
        (request,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_binance_transaction_ids_have_one_spelling(tmp_path):
    from shopbot import binance as binance_module
    from shopbot.db import Database

    payment = binance_module._payment({"orderId": "0466000199987322880", "transactionId": "p_a25pqjvad2171114",
                                       "orderType": "C2C", "amount": "25", "currency": "usdt",
                                       "transactionTime": 1, "payerInfo": {"name": "Ali"}})
    assert (payment.order_id, payment.transaction_id, payment.currency) == (ORDER, TX, "USDT")
    path = str(tmp_path / "shop.db")
    db = Database(path, OWNER_ID)
    db.conn.execute("INSERT INTO binance_payments (order_id, transaction_id, user_id, amount, raw_amount,"
                    " currency, status, created_at) VALUES (?, ?, 1, 0, '', '', 'credited', 'x')",
                    (ORDER, "p_a25pqjvad2171114"))  # as an older version stored it
    db.close()
    db = Database(path, OWNER_ID)  # the next start writes it in capitals
    try:
        assert db.binance_payment(TX)["order_id"] == ORDER
    finally:
        db.close()


def test_the_database_refuses_an_approval_the_screen_would_refuse(tmp_path):
    from shopbot.db import Database, ShopError

    db = Database(str(tmp_path / "shop.db"), OWNER_ID)
    try:
        db.register_payment(ORDER, BOB["id"])  # Bob's, added by hand: the request can't name it
        nothing = db.create_review_request(ALICE["id"], 2500, ORDER, "x", [], [ORDER])
        assert nothing["order_id"] is None
        with pytest.raises(ShopError, match="names no payment"):
            db.decide_deposit(nothing["id"], True, 10**9)
        unconfirmed = db.create_review_request(ALICE["id"], 2500, TX, "x", [], [TX])
        with pytest.raises(ShopError, match="transaction ID"):
            db.decide_deposit(unconfirmed["id"], True, 10**9)
        assert db.get_user(ALICE["id"])["balance"] == 0
        db.decide_deposit(nothing["id"], False, 10**9)  # rejecting is always possible
    finally:
        db.close()


def test_an_older_versions_payment_kept_for_an_approved_request_counts_as_added(tmp_path):
    # Older versions left a request's other payments "kept" after approving it.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20")))
        register(t, ALICE)
        register(t, BOB)
        approved = t.db.create_review_request(ALICE["id"], 2500, None, "x", [pay(amount="20")], [ORDER])
        t.db.conn.execute("UPDATE deposits SET status = 'approved', decided_at = created_at WHERE id = ?",
                          (approved["id"],))
        t.db.conn.execute("UPDATE binance_payments SET approved_in = ? WHERE order_id = ?", (approved["id"], ORDER))
        await choose(t, ALICE, 20)
        calls = await t.send(ALICE, ORDER)
        assert f"was in your request #{approved['id']}, which the owner approved" in texts(calls, ALICE)[-1]
        assert t.db.pending_deposits() == [] and balance(t, ALICE) == 0
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 20 {ORDER}")
        assert "Nothing was added" in owner_texts(calls)[-1] and balance(t, ALICE) == 0
        await choose(t, BOB, 20)  # someone else: used already, never takes it over
        assert "This Order ID was already used" in texts(await t.send(BOB, ORDER), BOB)[-1]
        assert t.db.pending_deposits() == [] and t.db.binance_payment(ORDER)["deposit_id"] == approved["id"]

    run(tmp_path, scenario)


def test_a_request_whose_payment_was_added_since_for_someone_else_cant_be_approved(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceKeyError("refused")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, BOB, 25)
        await t.send(BOB, ORDER)  # can't be checked: Bob's request names it
        bobs = t.db.pending_deposits()[-1]
        fake.error = None
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)  # Bob's waiting request was sent with it first
        assert "This deposit was cancelled." in texts(calls, ALICE)[-1] and len(t.db.pending_deposits()) == 1
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}")  # the owner: it's hers
        assert f"request #{bobs['id']} for Bob is waiting" in owner_texts(calls)[-1]
        await confirm(t, calls, anyway=True)
        assert balance(t, ALICE) == 2500 and t.db.get_deposit(bobs["id"])["status"] == "pending"
        calls = await t.press(OWNER, f"dok:{bobs['id']}")
        assert "can't be approved" in t.alert(calls) and balance(t, BOB) == 0
        assert f"Order ID {ORDER} was added already" in t.texts(calls, OWNER_ID)[-1]

    run(tmp_path, scenario)


def test_the_database_checks_both_ids_of_a_payment_added_by_hand(tmp_path):
    from shopbot.db import Database, PaymentUsed

    db = Database(str(tmp_path / "shop.db"), OWNER_ID)
    try:
        earlier = db.create_review_request(BOB["id"], 2500, ORDER, "x", [], [ORDER])
        db.decide_deposit(earlier["id"], True, 10**9)  # known by its Order ID only
        with pytest.raises(PaymentUsed):  # its transaction ID, with what Binance has
            db.manual_deposit(ALICE["id"], 2500, 10**9, TX, pay(amount="25"))
        with pytest.raises(PaymentUsed):
            db.register_payment(TX, ALICE["id"], pay(amount="25"))
        assert db.get_user(ALICE["id"]) is None or db.get_user(ALICE["id"])["balance"] == 0
    finally:
        db.close()


def test_an_unconfirmed_request_lists_balance_added_by_hand_to_anyone_in_the_week_before(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(error=BinanceKeyError("refused")))
        register(t, ALICE)
        register(t, CARL)
        await t.send(OWNER, f"/addbal {CARL['id']} 30")  # someone else, another amount
        await t.send(OWNER, f"/modifybal {ALICE['id']} 7")  # and a raise of hers
        t.db.conn.execute("UPDATE deposits SET created_at = datetime(created_at, '-3 days'),"
                          " decided_at = datetime(decided_at, '-3 days')")
        t.db.conn.execute("UPDATE ledger SET created_at = datetime(created_at, '-3 days')")
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, "999999999999999999")  # its digits tell no time
        shown = request_text(calls)
        assert "30.00$ by hand for Carl (" in shown and "7.00$ raised with /modifybal (" in shown
        (request,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 700

    run(tmp_path, scenario)


# ================================================ what the owner sees before adding by hand
def test_balance_that_may_be_this_payment_is_shown_before_it_is_added_by_hand(tmp_path):
    # Balance added by hand since the payment was made may have been it: the confirmation
    # says so, and only "anyway" adds it. Registering adds nothing: no warning there.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10", minutes_ago=30),
                                    pay(order_id=OTHER, tx="P_OTHER", amount="10", minutes_ago=30)))
        register(t, ALICE)
        register(t, BOB)
        await t.send(OWNER, f"/addbal {BOB['id']} 10")  # without an Order ID, after both were paid
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert "⚠️ May be added already: after it was paid, you added 10.00$ by hand for " in shown
        assert t.button(calls, "⚠️ Add 10.00$ anyway").startswith("pyc:") and not t.has_button(calls, "✅")
        assert balance(t, ALICE) == 0 and t.db.binance_payment(ORDER) is None
        calls = await confirm(t, calls, anyway=True)
        assert "Added <b>10.00$</b>" in owner_texts(calls)[-1] and balance(t, ALICE) == 1000
        calls = await t.send(OWNER, f"/regbin {OTHER} {BOB['id']}")
        assert "May be added already" not in owner_texts(calls)[-1]
        assert t.button(calls, "✅ Register").startswith("pyc:")

    run(tmp_path, scenario)


def test_anything_new_about_the_payment_after_it_was_shown_needs_the_order_id_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10", minutes_ago=30)))
        register(t, ALICE)
        register(t, BOB)
        add = t.button(await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}"), "✅ Add 10.00$")
        await t.send(OWNER, f"/addbal {BOB['id']} 10")  # meanwhile: may have been this payment
        calls = await t.press(OWNER, add)
        shown = owner_texts(calls)
        assert "Nothing was added: something new may be about this payment, so here it is again." in shown[-2]
        assert "you added 10.00$ by hand for Bob (" in shown[-1]  # the screen again, with it
        assert balance(t, ALICE) == 0 and t.db.binance_payment(ORDER) is None
        assert "This has expired" in t.alert(await t.press(OWNER, add))  # its buttons are gone
        anyway = t.button(calls, "anyway")
        await t.send(OWNER, "/modifybal")  # a raise meanwhile: new too, also after a warning
        await t.send(OWNER, str(BOB["id"]))
        await t.send(OWNER, "15")
        calls = await t.press(OWNER, anyway)
        assert "something new may be about this payment" in owner_texts(calls)[-2] and balance(t, ALICE) == 0
        assert "you raised a balance by 5.00$ with /modifybal" in owner_texts(calls)[-1]
        await confirm(t, calls, anyway=True)  # nothing new since: added, once
        assert balance(t, ALICE) == 1000 and "This has expired" in t.alert(await t.press(OWNER, anyway))

    run(tmp_path, scenario)


def test_a_payment_made_before_automatic_checking_started_is_shown_as_maybe_added(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10", minutes_ago=180)))  # it started an hour ago
        register(t, ALICE)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        assert ("⚠️ May be added already: it was paid before automatic checking started, when deposits "
                "came with receipts.") in owner_texts(calls)[-1]
        await confirm(t, calls, anyway=True)
        assert balance(t, ALICE) == 1000

    run(tmp_path, scenario)


def test_a_payment_kept_for_a_rejected_request_says_so_when_added_by_hand(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10", minutes_ago=180)))
        register(t, ALICE)
        await choose(t, ALICE, 10)
        await t.send(ALICE, ORDER)  # paid before automatic checking: kept for the owner
        (request,) = t.db.pending_deposits()
        assert t.db.binance_payment(ORDER)["deposit_id"] == request["id"]
        await t.press(OWNER, f"dno:{request['id']}")
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        assert f"it was in request #{request['id']}, which you rejected" in owner_texts(calls)[-1]
        await confirm(t, calls, anyway=True)
        assert balance(t, ALICE) == 1000 and t.db.binance_payment(ORDER)["status"] == "credited"

    run(tmp_path, scenario)


def test_someone_who_never_used_the_bot_is_shown_by_their_id_with_a_warning(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10"), pay(order_id=OTHER, tx="P_OTHER", amount="7")))
        register(t, ALICE)
        calls = await t.send(OWNER, f"/addbal 999111 10 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert "⚠️ User <code>999111</code> has never used the bot: is the ID right?" in shown
        assert "Add <b>10.00$</b> to User <code>999111</code> for this payment?" in shown
        assert t.db.get_user(999111) is None  # nothing recorded before the ✅
        calls = await t.send(OWNER, f"/regbin {OTHER} 999112")
        shown = owner_texts(calls)[-1]
        assert "⚠️ User <code>999112</code> has never used the bot" in shown
        assert "Register it as added by hand for User <code>999112</code>?" in shown
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 7 {OTHER}")
        assert "never used the bot" not in owner_texts(calls)[-1]

    run(tmp_path, scenario)


def test_the_add_balance_prompt_waits_for_binance_and_can_be_left_meanwhile(tmp_path):
    class SlowBinance(FakeBinance):
        go = asyncio.Event()

        async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
            await self.go.wait()
            return await super().find(wanted, since_ms)

    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, SlowBinance(pay(amount="10")))
        SlowBinance.go = asyncio.Event()
        register(t, ALICE)
        settle = t.settle

        async def quick() -> None:  # don't wait for the check that's still running
            await asyncio.sleep(0.01)

        async def asking() -> list:  # the Order ID, sent at the prompt
            await t.send(OWNER, f"/addbal {ALICE['id']}")
            await t.send(OWNER, "10")
            t.settle = quick  # type: ignore[method-assign]
            return await t.send(OWNER, ORDER)

        async def answered() -> list:
            mark = len(t.tg.calls)
            fake.go.set()
            t.settle = settle  # type: ignore[method-assign]
            await t.settle()
            SlowBinance.go = asyncio.Event()
            return t.tg.calls[mark:]

        calls = await asking()
        assert "Checking Order ID" in owner_texts(calls)[0] and t.button(calls, "Cancel") == "cancel"
        calls = await t.send(OWNER, ORDER)  # sent again meanwhile
        assert "Still checking that Order ID with Binance" in owner_texts(calls)[0]
        calls = await answered()
        assert "Binance has this payment" in owner_texts(calls)[-1] and len(fake.asked) == 1
        # Cancelled meanwhile: nothing comes afterwards.
        await asking()
        await t.press(OWNER, "cancel")
        assert owner_texts(await answered()) == []
        # Another command meanwhile: it says nothing was added, and nothing comes afterwards.
        await asking()
        calls = await t.send(OWNER, "/status")
        assert "⚠️ Add balance stopped: 10.00$ for Alice was not added." in owner_texts(calls)
        assert owner_texts(await answered()) == []
        assert balance(t, ALICE) == 0 and t.db.binance_payment(ORDER) is None

    run(tmp_path, scenario)


def test_with_a_key_binance_refuses_nothing_is_added_with_an_order_id_and_skip_adds_without(tmp_path):
    # Binance refusing the key won't change in a minute: nothing is added for an Order ID it
    # can't confirm, and the owner is told so, and how to add the balance meanwhile.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10"), error=BinanceKeyError("refused")))
        register(t, ALICE)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert "Nothing was added: Binance refuses to answer (refused), and will until that's fixed." in shown
        assert "Meanwhile the balance can be added without the Order ID" in shown
        assert "try again in a minute" not in shown and balance(t, ALICE) == 0
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        assert "Send it again once Binance answers." in owner_texts(calls)[-1]
        assert t.db.binance_payment(ORDER) is None
        # At the prompt: ⏭ Skip adds it without the Order ID ...
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "10")
        calls = await t.send(OWNER, ORDER)
        assert "press ⏭ Skip to add 10.00$" in owner_texts(calls)[-1]
        calls = await t.press(OWNER, t.button(calls, "⏭ Skip: add 10.00$"))
        assert "Added <b>10.00$</b>" in owner_texts(calls)[-1] and balance(t, ALICE) == 1000
        assert t.db.binance_payment(ORDER) is None
        # ... unless the bot knows that Order ID was added already.
        t.db.register_payment(OTHER, ALICE["id"])
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "5")
        calls = await t.send(OWNER, OTHER)
        assert "was used already" in owner_texts(calls)[-1] and not t.has_button(calls, "Skip")
        # Once the key works, the payment made before the balance added by hand goes to the owner.
        t.app.bot_data["binance"].error = None
        await choose(t, ALICE, 10)
        calls = await t.send(ALICE, ORDER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 1000

    run(tmp_path, scenario)


def test_a_check_that_fails_unexpectedly_asks_for_the_order_id_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="10"), error=RuntimeError("boom")))
        register(t, ALICE)
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "10")
        calls = await t.send(OWNER, ORDER)
        shown = owner_texts(calls)[-1]
        assert "Nothing was added: something went wrong while asking Binance: send it again." in shown
        assert "Send another Order ID, or press Cancel." in shown and balance(t, ALICE) == 0
        fake.error = None
        calls = await confirm(t, await t.send(OWNER, ORDER))  # not "Still checking"
        assert "Added <b>10.00$</b>" in owner_texts(calls)[-1] and balance(t, ALICE) == 1000
        fake.error = RuntimeError("boom")
        calls = await t.send(OWNER, f"/regbin {OTHER}")  # without the prompt: said, that's all
        assert "Nothing was registered: something went wrong while asking Binance" in owner_texts(calls)[-1]

    run(tmp_path, scenario)


# ======================================================= requests: what's shown and settled
def order_id_at(seconds_ago: float, low: int = 123456) -> str:
    """A well-formed Order ID whose own time (order_id_time) says it was paid ``seconds_ago``."""
    return str(((int(time.time() - seconds_ago) - EPOCH_2020) << 31) | low)


def backdate_manual(t: ShopTest, seconds_ago: float) -> None:
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - seconds_ago))
    t.db.conn.execute("UPDATE deposits SET created_at = ?, decided_at = ? WHERE method = 'manual'", (when, when))


def test_an_unconfirmed_request_lists_balance_added_since_the_earliest_order_id_it_was_sent_with(tmp_path):
    # Any Order ID a request was sent with may be its payment, not only the one it names: a
    # hand-add after the earliest of them was paid is listed, whichever was sent last.
    old_id, new_id = order_id_at(10 * 86_400), order_id_at(3600, low=777)

    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(order_id=old_id, amount="25", tx="P_OLD", minutes_ago=10 * 1440)))
        register(t, ALICE)
        await t.send(OWNER, f"/addbal {ALICE['id']} 25")  # without its Order ID ...
        backdate_manual(t, 10 * 86_400 - 3600)  # ... ten days ago, just after it was paid
        await choose(t, ALICE, 25)
        await t.send(ALICE, old_id)  # too old for the customer's search
        calls = await t.send(ALICE, new_id)  # the request names this one
        (request,) = t.db.pending_deposits()
        assert request["order_id"] == new_id and request["unconfirmed"] == f"{old_id} {new_id}"
        assert " UTC: 25.00$ by hand" in request_text(calls) and "⚠️ Added since " in request_text(calls)
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_request_names_the_latest_order_id_nobody_used_even_if_one_used_was_sent_last(tmp_path):
    unknown = order_id_at(3 * 86_400)  # 3 days old: the customer's search misses it

    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(order_id=OTHER, amount="25", tx="P_OTHER")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, BOB, 25)
        await t.send(BOB, OTHER)  # Bob's, added automatically
        await choose(t, ALICE, 25)
        await t.send(ALICE, unknown)
        calls = await t.send(ALICE, OTHER)  # used by someone else: her deposit is cancelled
        assert "This deposit was cancelled." in texts(calls, ALICE)[-1] and t.db.pending_deposits() == []
        # A request sent with both (an older version's): it names the one nobody used.
        request = t.db.create_review_request(ALICE["id"], 2500, OTHER, "x", [], [unknown, OTHER])
        assert request["order_id"] == unknown and request["unconfirmed"] == f"{unknown} {OTHER}"
        assert t.db.approval_block(request["id"]) is None
        calls = await t.press(OWNER, f"dok:{request['id']}")  # OTHER was used: a warning first
        assert "may have been added already" in t.alert(calls)
        await t.press(OWNER, f"dokx:{request['id']}")
        assert balance(t, ALICE) == 2500 and t.db.binance_payment(unknown)["user_id"] == ALICE["id"]

    run(tmp_path, scenario)


def test_every_payment_an_approval_adds_closes_the_requests_waiting_with_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20"), pay(order_id=OTHER, tx="P_OTHER", amount="5")))
        register(t, ALICE)
        first = t.db.create_review_request(ALICE["id"], 2000, ORDER,
                                           f"{ORDER}: not checked (Binance couldn't be reached: busy)", [], [ORDER])
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # 20, not 25
        await t.send(ALICE, OTHER)  # 5, not 25: both kept, and the request names OTHER
        second = t.db.pending_deposits()[-1]
        assert second["order_id"] == OTHER
        await t.press(OWNER, f"dok:{second['id']}")
        calls = await t.press(OWNER, f"dokx:{second['id']}")  # adds ORDER and OTHER
        assert f"Request #{first['id']} was waiting with this payment: closed." in t.texts(calls, OWNER_ID)[-1]
        assert t.db.get_deposit(first["id"])["status"] == "closed"
        assert f"closed: added with request #{second['id']}" in t.db.get_deposit(first["id"])["note"]
        calls = await t.press(OWNER, f"dno:{first['id']}")
        assert "already closed" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_request_the_bot_closed_itself_holds_nothing_back_and_one_closed_by_hand_says_so(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25", minutes_ago=30), error=BinanceUnavailable("busy")))
        for user in (ALICE, BOB, CARL):
            register(t, user)
        sent, carls = "466000199987322888", "466000199987322777"
        note = ": not checked (Binance couldn't be reached: busy)"
        first = t.db.create_review_request(ALICE["id"], 2500, sent, sent + note, [], [sent])
        second = t.db.create_review_request(ALICE["id"], 2500, sent, sent + note, [], [sent])
        await t.press(OWNER, f"dok:{first['id']}")  # closes the second by itself: it added nothing
        assert t.db.get_deposit(second["id"])["status"] == "closed"
        third = t.db.create_review_request(CARL["id"], 1000, carls, carls + note, [], [carls])
        t.db.register_payment(carls, CARL["id"])  # settled by hand: the owner closed it
        assert t.db.get_deposit(third["id"])["status"] == "closed"
        fake.error = None
        await choose(t, BOB, 25)
        shown = request_text(await t.send(BOB, ORDER))
        assert f"you approved request #{first['id']} for Alice, whose Order ID Binance didn't confirm" in shown
        assert f"request #{second['id']}" not in shown and "you closed" not in shown
        assert f"request #{third['id']} for Carl was closed, with an Order ID Binance didn't confirm" in shown

    run(tmp_path, scenario)


# ===================================================== what older versions recorded
def test_an_order_id_an_older_version_kept_with_leading_zeros_is_found_without_them(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        # As older versions stored it: the customer typed "0" + ORDER, the owner approved it.
        cur = t.db.conn.execute(
            "INSERT INTO deposits (user_id, amount, method, status, created_at, decided_at, order_id, note)"
            " VALUES (?, 2500, 'binance', 'approved', datetime('now'), datetime('now'), ?, ?)",
            (ALICE["id"], "0" + ORDER, f"0{ORDER}: not found in your Binance Pay history"))
        t.db.conn.execute(
            "INSERT INTO binance_payments (order_id, user_id, amount, raw_amount, currency, status, deposit_id,"
            " created_at) VALUES (?, ?, 2500, '', '', 'credited', ?, datetime('now'))",
            ("0" + ORDER, ALICE["id"], cur.lastrowid))
        t.db.conn.execute("UPDATE deposits SET unconfirmed = ? WHERE id = ?", (f"00{OTHER}", cur.lastrowid))
        t.db._migrate()  # the new version starts
        row = t.db.binance_payment(ORDER)
        assert row is not None and row["order_id"] == ORDER and t.db.binance_payment("0" + ORDER) is None
        deposit = t.db.get_deposit(cur.lastrowid)
        assert deposit["order_id"] == ORDER and deposit["unconfirmed"] == OTHER
        calls = await t.send(OWNER, f"/addbal {BOB['id']} 25 {ORDER}")
        assert "Nothing was added" in owner_texts(calls)[-1] and "was used already" in owner_texts(calls)[-1]
        await choose(t, BOB, 25)
        await t.send(BOB, ORDER)
        assert balance(t, BOB) == 0

    run(tmp_path, scenario)


def old_transaction_id_record(t: ShopTest, user_id: int, minutes_ago: int = 30) -> None:
    """As an older version's /regbin TX (or approval of a request naming it) recorded a
    payment Binance didn't list: by its transaction ID alone."""
    t.db.conn.execute(
        "INSERT INTO binance_payments (order_id, user_id, amount, raw_amount, currency, status, deposit_id,"
        " created_at) VALUES (?, ?, 0, '', '', 'credited', NULL, datetime('now', ?))",
        (TX, user_id, f"-{minutes_ago} minutes"))


def test_a_request_that_may_be_a_payment_recorded_by_its_transaction_id_only_says_so(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25", minutes_ago=60), error=BinanceUnavailable("busy")))
        register(t, ALICE)
        old_transaction_id_record(t, OWNER_ID)  # recorded after ORDER was paid: may be it
        request = t.db.create_review_request(
            ALICE["id"], 2500, ORDER, f"{ORDER}: not checked (Binance couldn't be reached: busy)", [], [ORDER])
        recent = t.db.create_review_request(  # paid after it was recorded: can't be it
            ALICE["id"], 1000, order_id_at(60), "x: not checked (Binance couldn't be reached: busy)", [],
            [order_id_at(60)])
        assert t.db.request_unlinked(recent["id"]) == []
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 0
        caption = deposit_caption(t.app, t.db.get_deposit(request["id"]))
        assert f"the bot knows some payments by Binance's transaction ID only (<code>{TX}</code>" in caption
        assert f"If one of them has Order ID <code>{ORDER}</code> in your Binance app" in caption
        await t.press(OWNER, f"dokx:{request['id']}")  # checked: it isn't
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_without_binance_an_order_id_that_may_be_one_recorded_by_its_transaction_id_isnt_taken(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        register(t, BOB)
        old_transaction_id_record(t, BOB["id"])
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}")  # no Binance key now
        shown = owner_texts(calls)[-1]
        assert f"Binance can't be asked about Order ID {ORDER}" in shown and f"({TX} on " in shown
        assert "add the balance without the Order ID" in shown
        assert balance(t, ALICE) == 0 and t.db.binance_payment(ORDER) is None
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        assert f"Binance can't be asked about Order ID {ORDER}" in owner_texts(calls)[-1]
        recent = order_id_at(60)  # paid after it was recorded: can't be it
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {recent}")
        assert "Added <b>25.00$</b>" in owner_texts(calls)[-1] and t.db.binance_payment(recent) is not None

    run(tmp_path, scenario)


# ================================================== the customer's own come first in warnings
@pytest.mark.parametrize("how", ["/addbal", "/modifybal"])
def test_a_held_payment_lists_the_customers_own_balance_added_by_hand_first(tmp_path, how):
    # While Binance can't be reached the owner pays Alice out by hand, then adds balance by
    # hand for three others: her own line must be shown, not hidden as "and 1 more".
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceUnavailable("busy")))
        register(t, ALICE)
        others = [{"id": 2301 + n, "is_bot": False, "first_name": f"E{n}", "username": f"e{n}"} for n in range(3)]
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        await t.send(OWNER, f"{how} {ALICE['id']} 25")  # her payment, paid out by hand
        for user in others:
            register(t, user)
            await t.send(OWNER, f"/addbal {user['id']} 7")  # others' cash, afterwards
        fake.error = None
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        line = [x for x in request_text(calls).splitlines() if x.startswith("⚠️ May be added already")][0]
        own = "you added 25.00$ by hand (" if how == "/addbal" else "you raised a balance by 25.00$ with /modifybal ("
        assert line.index(own) < line.index("you added 7.00$ by hand for") and "and 1 more" in line, line

    run(tmp_path, scenario)


def test_an_unconfirmed_request_lists_the_customers_own_raise_first_and_says_since_when(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(error=BinanceKeyError("refused")))
        register(t, ALICE)
        others = [{"id": 2101 + n, "is_bot": False, "first_name": f"C{n}", "username": f"c{n}"} for n in range(3)]
        for user in others:
            register(t, user)
            await t.send(OWNER, f"/addbal {user['id']} 12")  # others' cash, earlier this week
        t.db.conn.execute("UPDATE deposits SET created_at = datetime(created_at, '-2 days'),"
                          " decided_at = datetime(decided_at, '-2 days')")
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # not checked: a request
        (request,) = t.db.pending_deposits()
        await t.send(OWNER, f"/modifybal {ALICE['id']} 25")  # the owner pays her out instead
        line = [x for x in deposit_caption(t.app, t.db.get_deposit(request["id"])).splitlines()
                if x.startswith("⚠️ Added since")][0]
        assert line.startswith(f"⚠️ Added since {t.db.unverified_start(request)} UTC: 25.00$ raised with /modifybal")
        assert "and 1 more" in line
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_request_sent_with_an_order_id_and_then_its_transaction_id_names_the_order_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance())  # Binance doesn't list it yet
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await t.send(ALICE, TX)  # the other code on the same payment screen
        (request,) = t.db.pending_deposits()
        assert request["order_id"] == ORDER and request["unconfirmed"] == f"{ORDER} {TX}"
        assert t.db.approval_block(request["id"]) is None
        await t.press(OWNER, f"dok:{request['id']}")
        assert balance(t, ALICE) == 2500 and t.db.binance_payment(ORDER)["user_id"] == ALICE["id"]

    run(tmp_path, scenario)


def test_a_blocked_request_can_be_approved_from_its_message_once_nothing_blocks_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        alices = await to_the_owner(t, ALICE, 25)  # names ORDER, not listed yet
        fake.payments.append(pay(amount="20"))
        # A request of Bob's holding that payment (sent before Alice's, or by an older version)
        bobs = t.db.create_review_request(BOB["id"], 2500, ORDER, f"{ORDER}: is 20 USDT, not 25 USDT",
                                          [pay(amount="20")], [ORDER])
        assert t.db.binance_payment(ORDER)["deposit_id"] == bobs["id"]
        calls = await t.press(OWNER, f"dok:{alices['id']}")
        assert "can't be approved" in t.alert(calls)
        approve = t.button(calls, "✅ Approve")
        await t.press(OWNER, f"dno:{bobs['id']}")  # not Bob's
        calls = await t.press(OWNER, approve)  # it checks again: Alice's payment, in a request Bob had
        assert "may have been added already" in t.alert(calls)
        caption = deposit_caption(t.app, t.db.get_deposit(alices["id"]))
        assert f"it was in request #{bobs['id']} for Bob, which you rejected" in caption
        await t.press(OWNER, f"dokx:{alices['id']}")
        assert balance(t, ALICE) == 2500 and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_a_rejected_receipt_is_described_as_rejected(tmp_path):
    from shopbot.handlers.money import doubt_text

    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        register(t, BOB)
        receipt = t.db.create_deposit(BOB["id"], 2500, "photo-id", "photo")
        t.db.decide_deposit(receipt["id"], False, 10**9)
        text = doubt_text(t.app, t.db.get_deposit(receipt["id"]), ALICE["id"])
        assert "you rejected it" in text and "waiting" not in text

    run(tmp_path, scenario)


def test_a_waiting_receipt_lists_what_was_added_for_its_payment_since(tmp_path):
    # Without a key: Alice's receipt waits. The key is added, she also sends the payment's
    # Order ID, and the owner approves that. Her receipt then shows it, and ✅ doesn't add
    # the payment a second time without ⚠️ Approve anyway.
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        await t.press(ALICE, "dp")
        await t.press(ALICE, "da:25")
        await t.send_photo(ALICE)
        await t.press(ALICE, "dsend")
        (receipt,) = t.db.pending_deposits()
        t.app.bot_data["binance"] = FakeBinance(pay(amount="25", minutes_ago=10))
        t.db.set_setting("binance_auto_since", str(int(time.time() * 1000)))  # starts now
        await choose(t, ALICE, 25)
        assert "Sent to the owner" in texts(await t.send(ALICE, ORDER), ALICE)[-1]  # paid before
        request = t.db.pending_deposits()[-1]
        await t.press(OWNER, f"dok:{request['id']}")
        await t.press(OWNER, f"dokx:{request['id']}")
        assert balance(t, ALICE) == 2500
        calls = await t.send(OWNER, "/pending")
        (photo,) = [c for c in t.messages(calls, OWNER_ID) if c.method == "sendPhoto"]
        assert f"⚠️ Added since {t.db.unverified_start(receipt)} UTC: 25.00$ with request #{request['id']}" in photo.text
        assert f"Order ID <code>{ORDER}</code>" in photo.text
        calls = await t.press(OWNER, f"dok:{receipt['id']}", photo=True)
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 2500
        await t.press(OWNER, f"dno:{receipt['id']}", photo=True)
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_receipts_warnings_fit_in_a_photo_caption(tmp_path):
    from shopbot.handlers.common import utf16_len

    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        for n in range(30):  # many deposits of hers this week
            t.db.manual_deposit(ALICE["id"], 100 + n, 10**9)
        receipt = t.db.create_deposit(ALICE["id"], 2500, "photo-id", "photo")
        caption = deposit_caption(t.app, t.db.get_deposit(receipt["id"]))
        assert utf16_len(caption) <= 1024 and "⚠️ Added since" in caption

    run(tmp_path, scenario)


def test_a_payment_from_someone_elses_rejected_request_is_refused_and_the_owner_can_add_it(tmp_path):
    # Bob claims Alice's 25 USDT payment with another amount; the owner rejects it, so nobody
    # got it. Sent again by anyone else it's refused as used (the owner hears who sent it and
    # how to add it for them); only Bob, whose request it was, goes to the owner with it again.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        bobs = await to_the_owner(t, BOB, 20)  # 25 USDT, not 20: kept for Bob's request
        await t.press(OWNER, f"dno:{bobs['id']}")
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "This Order ID was already used" in texts(calls, ALICE)[-1]
        assert "If you made this payment yourself, please contact" in texts(calls, ALICE)[-1]
        notice = owner_texts(calls)[-1]
        assert "which someone else sent first" in notice and f"kept for request #{bobs['id']} from <b>Bob</b>" in notice
        assert f"<code>/addbal {ALICE['id']} 25 {ORDER}</code>" in notice
        assert t.db.pending_deposits() == [] and t.db.binance_payment(ORDER)["user_id"] == BOB["id"]
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}")  # it's hers: the owner adds it
        assert f"it was in request #{bobs['id']} for Bob, which you rejected" in owner_texts(calls)[-1]
        await confirm(t, calls, anyway=True)
        assert balance(t, ALICE) == 2500 and balance(t, BOB) == 0
        await choose(t, BOB, 25)  # Bob trying it again now: added already, for someone else
        assert "already used" in texts(await t.send(BOB, ORDER), BOB)[-1] and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_a_payment_from_the_customers_own_rejected_request_goes_to_the_owner_again_saying_the_amount(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        rejected = await to_the_owner(t, ALICE, 20)  # 25 USDT, not 20
        await t.press(OWNER, f"dno:{rejected['id']}")
        for chosen, note in ((20, "; is 25 USDT, not 20 USDT"), (25, "")):
            await choose(t, ALICE, chosen)
            calls = await t.send(ALICE, ORDER)  # her own: the owner decides on it again
            assert "Sent to the owner" in texts(calls, ALICE)[-1]
            request = t.db.pending_deposits()[-1]
            assert (request["note"] or "").endswith(f"{ORDER}: was in request #{rejected['id']}, which you rejected{note}")
            if note:  # still the wrong amount: rejected again
                await t.press(OWNER, f"dno:{request['id']}")
                rejected = request
        await t.press(OWNER, f"dok:{request['id']}")
        await t.press(OWNER, f"dokx:{request['id']}")
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_the_check_runs_even_if_saying_it_started_fails(tmp_path):
    class Recovering(FakeBinance):  # Telegram works again by the time Binance answers
        async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
            self.t.tg.fail.pop("sendMessage", None)
            return await super().find(wanted, since_ms)

    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, Recovering(pay(amount="10")))
        fake.t = t  # type: ignore[attr-defined]
        register(t, ALICE)
        t.tg.fail["sendMessage"] = "Bad Gateway"
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        assert "Binance has this payment" in owner_texts(calls)[-1]
        await t.press(OWNER, t.button(calls, "✅ Add 10.00$"))
        assert balance(t, ALICE) == 1000

    run(tmp_path, scenario)


def test_an_approval_the_last_version_counted_as_confirmed_holds_back_payments_again(tmp_path):
    # The version before this one marked an approved request as confirmed when its Order ID
    # was only recorded by the approval itself: it's an approval Binance never confirmed.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25", minutes_ago=60)))
        register(t, ALICE)
        register(t, BOB)
        sent = "466000199987322888"
        cur = t.db.conn.execute(
            "INSERT INTO deposits (user_id, amount, method, status, created_at, decided_at, order_id, note,"
            " unverified, unconfirmed) VALUES (?, 2500, 'binance', 'approved', datetime('now', '-30 minutes'),"
            " datetime('now', '-30 minutes'), ?, ?, 0, '')",
            (ALICE["id"], sent, f"{sent}: not found in your Binance Pay history"))
        t.db.conn.execute(
            "INSERT INTO binance_payments (order_id, user_id, amount, raw_amount, currency, status, deposit_id,"
            " created_at) VALUES (?, ?, 2500, '', '', 'credited', ?, datetime('now', '-30 minutes'))",
            (sent, ALICE["id"], cur.lastrowid))
        t.db._migrate()  # the new version starts
        repaired = t.db.get_deposit(cur.lastrowid)
        assert repaired["unverified"] == 1 and repaired["unconfirmed"] == sent
        await choose(t, BOB, 25)  # paid before that approval: it may have been this payment
        calls = await t.send(BOB, ORDER)
        assert "Sent to the owner" in texts(calls, BOB)[-1] and balance(t, BOB) == 0
        assert f"you approved request #{cur.lastrowid} for Alice" in request_text(calls)
        t.db._migrate()  # once only: nothing more changes
        assert t.db.get_deposit(cur.lastrowid)["unconfirmed"] == sent

    run(tmp_path, scenario)


def test_every_one_of_the_customers_own_rows_is_shown_before_anyone_elses(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceUnavailable("busy")))
        register(t, ALICE)
        others = [{"id": 2401 + n, "is_bot": False, "first_name": f"F{n}", "username": f"f{n}"} for n in range(4)]
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        for n in range(5):
            await t.send(OWNER, f"/addbal {ALICE['id']} {n + 1}")  # five of hers ...
        for user in others:  # ... then four of others'
            register(t, user)
            await t.send(OWNER, f"/addbal {user['id']} 9")
        fake.error = None
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        line = [x for x in request_text(calls).splitlines() if x.startswith("⚠️ May be added already")][0]
        assert all(f"you added {n + 1}.00$ by hand (" in line for n in range(5)), line
        assert line.count("you added 9.00$ by hand for") == 0 and "and 4 more" in line

    run(tmp_path, scenario)


# ======================================================= what the mutation checks asked for
def test_by_hand_a_new_warning_anywhere_in_the_list_needs_the_order_id_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25", minutes_ago=60)))
        register(t, ALICE)
        register(t, BOB)
        for n in range(4):  # four of hers: listed first, newest first
            await t.send(OWNER, f"/addbal {ALICE['id']} {n + 1}")
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}")
        assert all(f"you added {n + 1}.00$ by hand (" in owner_texts(calls)[-1] for n in range(4))  # all hers
        anyway = t.button(calls, "anyway")
        await t.send(OWNER, f"/addbal {BOB['id']} 9")  # someone else's, after hers in the list
        calls = await t.press(OWNER, anyway)
        assert "something new may be about this payment" in owner_texts(calls)[-2]
        assert "; and 1 more." in owner_texts(calls)[-1]  # counted after hers: new, so shown again
        assert balance(t, ALICE) == 1000 and t.db.binance_payment(ORDER) is None

    run(tmp_path, scenario)


def test_approve_anyway_asks_again_for_a_new_doubt_anywhere_in_the_list(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25"), error=BinanceUnavailable("busy")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        for n in range(4):  # four of hers after it was paid: the payment is held back
            await t.send(OWNER, f"/addbal {ALICE['id']} {n + 1}")
        fake.error = None
        await t.press(ALICE, t.button(calls, "Check again"))
        (request,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{request['id']}")
        await t.send(OWNER, f"/addbal {BOB['id']} 9")  # someone else's: ranked after hers
        calls = await t.press(OWNER, f"dokx:{request['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 1000
        await t.press(OWNER, f"dokx:{request['id']}")  # now it was shown
        assert balance(t, ALICE) == 1000 + 2500

    run(tmp_path, scenario)


def test_with_a_refused_key_skip_isnt_offered_when_an_old_record_may_be_that_payment(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(error=BinanceKeyError("refused")))
        register(t, ALICE)
        old_transaction_id_record(t, OWNER_ID)  # recorded after ORDER was paid: may be it
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "10")
        calls = await t.send(OWNER, ORDER)
        assert "Nothing was added" in owner_texts(calls)[-1] and not t.has_button(calls, "Skip")
        calls = await t.send(OWNER, order_id_at(60))  # paid after it was recorded: can't be it
        assert t.has_button(calls, "⏭ Skip: add 10.00$")

    run(tmp_path, scenario)


def test_a_receipt_shows_fewer_of_the_customers_own_rows_when_all_wont_fit_its_caption(tmp_path):
    from shopbot.handlers.common import utf16_len

    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        for n in range(10):  # ten of hers this week, added automatically
            t.db.credit_binance_payment(ALICE["id"], pay(order_id=f"46600019998732{n:04d}", tx=f"P_OWN{n:04d}X"),
                                        2500, 10**9)
        receipt = t.db.create_deposit(ALICE["id"], 2500, "photo-id", "photo")
        caption = deposit_caption(t.app, t.db.get_deposit(receipt["id"]))
        assert utf16_len(caption) <= 1024 and "⚠️ Added since" in caption and "and 7 more" in caption
        text = deposit_caption(t.app, t.db.get_deposit(t.db.create_review_request(
            ALICE["id"], 2500, "466000199987329999", "x: not checked (Binance couldn't be reached: busy)",
            [], ["466000199987329999"])["id"]))
        assert "and 7 more" not in text  # a text message has room for all ten

    run(tmp_path, scenario)


def test_a_payment_kept_for_a_rejected_unconfirmed_request_names_that_request(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        alices = await to_the_owner(t, ALICE, 25)  # names ORDER, not listed yet
        fake.payments.append(pay(amount="20"))
        await choose(t, BOB, 25)
        await t.send(BOB, "466000199987322999")
        calls = await t.send(BOB, ORDER)  # Alice's request was sent with it first: cancelled
        assert "This deposit was cancelled." in texts(calls, BOB)[-1]
        # Bob's request sent with an unknown ID and holding that payment (an older version's)
        bobs = t.db.create_review_request(
            BOB["id"], 2500, ORDER, "466000199987322999: not found in your Binance Pay history",
            [pay(amount="20")], ["466000199987322999", ORDER])
        assert bobs["unverified"] == 1 and t.db.binance_payment(ORDER)["deposit_id"] == bobs["id"]
        await t.press(OWNER, f"dno:{bobs['id']}")
        caption = deposit_caption(t.app, t.db.get_deposit(alices["id"]))
        assert f"it was in request #{bobs['id']} for Bob, which you rejected" in caption

    run(tmp_path, scenario)


# ============================================== an Order ID sent again: used, deposit cancelled
def test_an_order_id_used_already_cancels_the_deposit_at_once_whoever_sends_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # added automatically
        assert balance(t, ALICE) == 2500
        # Bob sends it: used, so that deposit ends at once (no second try, no request), and the
        # owner hears it once.
        await choose(t, BOB, 25)
        calls = await t.send(BOB, ORDER)
        shown = texts(calls, BOB)[-1]  # after "Checking Order ID ..."
        assert shown.startswith("❌ This Order ID was already used.") and "This deposit was cancelled." in shown
        assert "If you made this payment yourself, please contact the owner" in shown
        (notice,) = owner_texts(calls)
        assert "<b>Bob</b>" in notice and "which was used already" in notice and "25$ was cancelled" in notice
        assert "added automatically: <b>25.00$</b> to <b>Alice</b>" in notice
        assert t.app.user_data[BOB["id"]].get("state") is None and t.db.pending_deposits() == []
        calls = await t.send(BOB, ORDER)  # nothing waits for an Order ID any more
        assert "Send /start" in texts(calls, BOB)[-1] and owner_texts(calls) == []
        # Alice sends her own again: used, cancelled too, and nobody is bothered.
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "already used: this payment was already added to your balance" in texts(calls, ALICE)[-1]
        assert "This deposit was cancelled." in texts(calls, ALICE)[-1] and owner_texts(calls) == []
        assert balance(t, ALICE) == 2500 and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_a_used_order_id_after_a_failed_try_cancels_instead_of_going_to_the_owner(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await choose(t, BOB, 25)
        calls = await t.send(BOB, "466000199987322999")  # a typo: not found, send it again
        assert "send the Order ID again" in texts(calls, BOB)[-1]
        calls = await t.send(BOB, ORDER)  # then Alice's: used, cancelled, not a request
        assert "This deposit was cancelled." in texts(calls, BOB)[-1]
        assert t.db.pending_deposits() == [] and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_a_payment_waiting_with_someone_elses_request_cancels_the_deposit_and_says_where_it_waits(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20")))
        register(t, ALICE)
        register(t, BOB)
        alices = await to_the_owner(t, ALICE, 25)  # paid 20, chose 25: waits for the owner
        await choose(t, BOB, 20)
        calls = await t.send(BOB, ORDER)
        assert "This Order ID was already used" in texts(calls, BOB)[-1]
        assert "This deposit was cancelled." in texts(calls, BOB)[-1]
        assert f"It's waiting with request #{alices['id']} from <b>Alice</b>" in owner_texts(calls)[-1]
        assert "which someone else sent first" in owner_texts(calls)[-1]  # not "used": it waits
        assert [d["id"] for d in t.db.pending_deposits()] == [alices["id"]]  # hers, untouched
        assert t.db.binance_payment(ORDER)["deposit_id"] == alices["id"] and balance(t, BOB) == 0
        # Her own, sent again while it waits: with the owner (not "used"), nothing new.
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert f"This payment is already with the owner in your request #{alices['id']}" in texts(calls, ALICE)[-1]
        assert "This deposit was cancelled." in texts(calls, ALICE)[-1] and len(t.db.pending_deposits()) == 1

    run(tmp_path, scenario)


def test_an_order_id_someone_elses_waiting_request_was_sent_with_cancels_the_deposit(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())  # Binance doesn't list it (yet)
        for user in (ALICE, BOB, CARL):
            register(t, user)
        alices = await to_the_owner(t, ALICE, 25)  # names ORDER, unconfirmed: no record of it
        assert t.db.binance_payment(ORDER) is None
        asked = len(fake.asked)
        await choose(t, BOB, 25)
        calls = await t.send(BOB, ORDER)  # by the same ID: refused before Binance is asked
        assert "This Order ID was already used" in texts(calls, BOB)[-1]
        assert "This deposit was cancelled." in texts(calls, BOB)[-1] and len(fake.asked) == asked
        notice = owner_texts(calls)[-1]
        assert "which someone else sent first" in notice and f"request #{alices['id']} from <b>Alice</b>" in notice
        assert not [x for x in owner_texts(calls) if "failed" in x]
        # By its other ID, once Binance lists it: the same.
        fake.payments.append(pay(amount="25"))
        await choose(t, BOB, 25)
        calls = await t.send(BOB, TX)
        assert "This deposit was cancelled." in texts(calls, BOB)[-1]
        # An ID only in a waiting request's list (not the one it names): the same.
        carls = t.db.create_review_request(CARL["id"], 1000, "466000199987322999",
                                           "x: not found in your Binance Pay history", [],
                                           [OTHER, "466000199987322999"])
        assert carls["order_id"] == "466000199987322999" and OTHER in carls["unconfirmed"]
        await choose(t, BOB, 10)
        calls = await t.send(BOB, OTHER)
        assert "This deposit was cancelled." in texts(calls, BOB)[-1]
        assert f"request #{carls['id']} from <b>Carl</b>" in owner_texts(calls)[-1]
        assert [d["id"] for d in t.db.pending_deposits()] == [alices["id"], carls["id"]]
        assert balance(t, BOB) == 0 and t.db.binance_payment(ORDER) is None

    run(tmp_path, scenario)


def test_an_order_id_waiting_with_the_customers_own_request_ends_the_deposit_unless_binance_lists_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        first = await to_the_owner(t, ALICE, 25)  # names ORDER: Binance didn't list it
        for error in (None, BinanceKeyError("refused"), BinanceUnavailable("busy")):
            fake.error = error
            await choose(t, ALICE, 25)
            calls = await t.send(ALICE, ORDER)  # sent again, still not listed
            shown = texts(calls, ALICE)[-1]
            assert f"already with the owner in your request #{first['id']}" in shown, (error, shown)
            assert "This deposit was cancelled." in shown and t.app.user_data[ALICE["id"]].get("state") is None
            assert not [x for x in owner_texts(calls) if "check failed" in x or "Deposit request" in x]
            assert [d["id"] for d in t.db.pending_deposits()] == [first["id"]]  # no second request
        fake.error = None
        fake.payments.append(pay(amount="25"))  # listed now, exactly what she chose: added
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, ORDER)
        assert "Payment received!" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 2500
        assert t.db.get_deposit(first["id"])["status"] == "closed"

    run(tmp_path, scenario)


def test_the_transaction_id_of_an_older_payment_known_by_its_order_id_only_is_refused_as_used(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance(pay(amount="25", minutes_ago=3 * 1440), error=BinanceKeyError("refused")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # the key is refused: to the owner, who approves it
        (request,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{request['id']}")
        assert balance(t, ALICE) == 2500 and t.db.binance_payment(ORDER)["transaction_id"] is None
        fake.error = None
        for user in (BOB, ALICE):  # three days after it was paid, by its other ID
            await choose(t, user, 25)
            calls = await t.send(user, TX)
            assert "This Order ID was already used" in texts(calls, user)[-1], user
            assert "This deposit was cancelled." in texts(calls, user)[-1]
        assert t.db.pending_deposits() == [] and balance(t, BOB) == 0 and balance(t, ALICE) == 2500
        assert t.db.binance_payment(ORDER)["transaction_id"] == TX  # known by both IDs now

    run(tmp_path, scenario)


def test_the_owner_trying_a_deposit_with_a_used_order_id_isnt_told_to_contact_themselves(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25"), pay(order_id=OTHER, tx="P_OTHER", amount="10")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        await choose(t, OWNER, 25)
        calls = await t.send(OWNER, ORDER)
        shown = owner_texts(calls)[-1]
        assert "This Order ID was already used" in shown and "contact" not in shown
        assert "added automatically: <b>25.00$</b> to <b>Alice</b>" in shown and "This deposit was cancelled." in shown
        assert not [x for x in owner_texts(calls) if "sent Order ID" in x]  # no notice about the owner
        await by_hand(t, f"/regbin {OTHER}")  # registered without a user
        await choose(t, OWNER, 10)
        shown = owner_texts(await t.send(OWNER, OTHER))[-1]
        assert "registered as added by hand" in shown and "your balance" not in shown
        # One a customer's waiting request was sent with: where it waits, again no notice.
        waiting = t.db.create_review_request(ALICE["id"], 1000, "466000199987322999",
                                             "x: not found in your Binance Pay history", [], ["466000199987322999"])
        await choose(t, OWNER, 10)
        calls = await t.send(OWNER, "466000199987322999")
        shown = owner_texts(calls)[-1]
        assert f"It waits with request #{waiting['id']} from <b>Alice</b>" in shown and "contact" not in shown
        assert "This deposit was cancelled." in shown and not [x for x in owner_texts(calls) if "sent Order ID" in x]

    run(tmp_path, scenario)


def test_an_order_id_of_a_request_the_owner_decided_isnt_refused_as_waiting(tmp_path):
    # Only a request still waiting for the owner makes an Order ID "sent before": one that
    # was rejected leaves it to Binance and the doubts, as for anyone.
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        alices = await to_the_owner(t, ALICE, 25)  # names ORDER, Binance didn't list it
        await t.press(OWNER, f"dno:{alices['id']}")
        fake.payments.append(pay(amount="25"))
        await choose(t, BOB, 25)
        calls = await t.send(BOB, ORDER)
        assert "already used" not in texts(calls, BOB)[-1]
        assert "Sent to the owner" in texts(calls, BOB)[-1]  # the rejected request may be about it
        assert f"you rejected request #{alices['id']} for Alice" in request_text(calls)

    run(tmp_path, scenario)


# ===================================================== the last review's findings
def test_approve_anyway_asks_again_when_a_waiting_request_it_listed_was_approved_meanwhile(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        bobs = await to_the_owner(t, BOB, 25, "466000199987322999")  # a wrong Order ID: waits
        fake.payments.append(pay(amount="25"))
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # Bob's waiting request may be about it: to the owner
        alices = t.db.pending_deposits()[-1]
        await t.press(OWNER, f"dok:{alices['id']}")  # shows "request #… for Bob is waiting"
        await t.press(OWNER, f"dok:{bobs['id']}")  # then Bob's is approved, with one tap
        calls = await t.press(OWNER, f"dokx:{alices['id']}")
        assert "may have been added already" in t.alert(calls) and balance(t, ALICE) == 0
        assert f"you approved request #{bobs['id']} for Bob" in deposit_caption(t.app, t.db.get_deposit(alices["id"]))

    run(tmp_path, scenario)


def test_add_anyway_asks_again_when_a_waiting_request_it_listed_was_approved_meanwhile(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        register(t, BOB)
        bobs = await to_the_owner(t, BOB, 25, "466000199987322999")
        fake.payments.append(pay(amount="25"))
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}")
        assert f"request #{bobs['id']} for Bob is waiting" in owner_texts(calls)[-1]
        anyway = t.button(calls, "anyway")
        await t.press(OWNER, f"dok:{bobs['id']}")
        calls = await t.press(OWNER, anyway)
        assert "something new may be about this payment" in owner_texts(calls)[-2] and balance(t, ALICE) == 0
        assert f"you approved request #{bobs['id']} for Bob" in owner_texts(calls)[-1]

    run(tmp_path, scenario)


def test_with_a_refused_key_a_transaction_id_gets_no_skip_and_skip_is_checked_again_when_pressed(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25"), error=BinanceKeyError("refused")))
        register(t, ALICE)
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "25")
        calls = await t.send(OWNER, TX)
        shown = owner_texts(calls)[-1]
        assert "That's Binance's transaction ID: send the payment's Order ID" in shown
        assert not t.has_button(calls, "Skip") and "can be added without the Order ID" not in shown
        # Skip offered for an Order ID nothing is known about; then Alice's request with it is
        # approved; then Skip: nothing is added.
        calls = await t.send(OWNER, ORDER)
        skip = t.button(calls, "⏭ Skip: add 25.00$")
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # the key is refused: to the owner
        (request,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{request['id']}")
        assert balance(t, ALICE) == 2500
        calls = await t.press(OWNER, skip)
        assert "Nothing was added: that Order ID is known to the bot now" in t.alert(calls)
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_with_a_refused_key_the_customers_own_waiting_request_is_pointed_to(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(error=BinanceKeyError("refused")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)
        (request,) = t.db.pending_deposits()
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        await t.send(OWNER, "25")
        calls = await t.send(OWNER, ORDER)
        shown = owner_texts(calls)[-1]
        assert f"Their request #{request['id']} (25.00$) was sent with this Order ID" in shown
        assert "approve the request instead" in shown and not t.has_button(calls, "Skip")

    run(tmp_path, scenario)


def test_with_a_refused_key_old_records_are_named_instead_of_offering_skip(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(error=BinanceKeyError("refused")))
        register(t, ALICE)
        old_transaction_id_record(t, OWNER_ID)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert f"by Binance's transaction ID only (<code>{TX}</code> on " in shown
        assert f"<code>/addbal {ALICE['id']} 10</code>" in shown and "can be added without the Order ID" not in shown

    run(tmp_path, scenario)


def test_a_payment_binance_listed_without_an_order_id_is_one_that_may_be_any_order_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(order_id="", amount="25")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, ALICE, 25)
        await t.send(ALICE, TX)  # added automatically, known by its transaction ID only
        assert balance(t, ALICE) == 2500 and t.db.binance_payment(TX)["order_id"] == TX
        t.app.bot_data["binance"] = None  # the key is taken out
        calls = await t.send(OWNER, f"/addbal {BOB['id']} 25 {ORDER}")
        assert "by Binance's transaction ID only" in owner_texts(calls)[-1] and balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_a_request_sent_with_a_payment_waiting_elsewhere_names_it_and_waits_for_that_one(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        bobs = await to_the_owner(t, BOB, 20)  # holds the payment, waits
        alices = t.db.create_review_request(ALICE["id"], 2500, ORDER, "x", [], [ORDER])  # an older version's
        assert alices["order_id"] == ORDER
        assert f"is kept for request #{bobs['id']}, which is waiting: decide that one first" in \
            t.db.approval_block(alices["id"])

    run(tmp_path, scenario)


def test_a_request_names_the_order_id_sent_last_also_when_it_was_sent_before(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        register(t, ALICE)
        first, second = "466000199987322777", "466000199987322778"
        await choose(t, ALICE, 25)
        await t.send(ALICE, first)  # not found: a failed try
        fake.error = BinanceUnavailable("busy")
        await t.send(ALICE, second)  # can't be asked: not a try
        calls = await t.send(ALICE, first)  # the first one again, and the key is refused now
        fake.error = BinanceKeyError("refused")
        calls = await t.press(ALICE, t.button(calls, "Check again"))
        (request,) = t.db.pending_deposits()
        assert request["order_id"] == first and request["unconfirmed"].split()[-1] == first

    run(tmp_path, scenario)


def test_a_request_isnt_warned_about_the_payment_it_holds_itself(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(order_id="", amount="20")))  # listed without an Order ID
        register(t, ALICE)
        request = t.db.create_review_request(ALICE["id"], 2500, "466000199987322999",
                                             "x: not found in your Binance Pay history",
                                             [pay(order_id="", amount="20")], [TX, "466000199987322999"])
        assert t.db.binance_payment(TX)["deposit_id"] == request["id"]  # holds it, by its TX only
        assert t.db.request_unlinked(request["id"]) == []  # not "it may be this one" about itself

    run(tmp_path, scenario)


def test_a_payment_from_someone_elses_rejected_request_stays_there_until_a_request_naming_it_is_approved(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        bobs = await to_the_owner(t, BOB, 20)
        await t.press(OWNER, f"dno:{bobs['id']}")
        alices = t.db.create_review_request(ALICE["id"], 2500, ORDER, "x", [], [ORDER])  # an older version's
        assert alices["order_id"] == ORDER and t.db.binance_payment(ORDER)["deposit_id"] == bobs["id"]
        await t.press(OWNER, f"dok:{alices['id']}")  # it was in Bob's rejected request: a warning first
        assert f"it was in request #{bobs['id']} for Bob, which you rejected" in \
            deposit_caption(t.app, t.db.get_deposit(alices["id"]))
        await t.press(OWNER, f"dokx:{alices['id']}")
        assert balance(t, ALICE) == 2500 and t.db.binance_payment(ORDER)["user_id"] == ALICE["id"]

    run(tmp_path, scenario)


def test_something_new_and_the_payment_added_meanwhile_says_it_was_used_instead_of_asking_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        add = t.button(await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}"), "✅ Add 25.00$")
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # meanwhile she sends it: added automatically
        await t.send(OWNER, f"/addbal {BOB['id']} 5")  # and something new about it
        calls = await t.press(OWNER, add)
        shown = owner_texts(calls)[-1]
        assert "Nothing was added" in shown and "was used already" in shown and "Binance has" not in shown
        assert balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_with_a_refused_key_another_customers_waiting_request_is_decided_first(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(error=BinanceKeyError("refused")))
        register(t, ALICE)
        register(t, BOB)
        await choose(t, BOB, 25)
        await t.send(BOB, ORDER)  # the key is refused: Bob's request names it
        (bobs,) = t.db.pending_deposits()
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert f"Request #{bobs['id']} from <b>Bob</b>" in shown and "decide that one first" in shown
        assert balance(t, ALICE) == 0

    run(tmp_path, scenario)
