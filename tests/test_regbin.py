"""🧾 /regbin and /addbal with an Order ID: a Binance Pay payment the owner added by hand
is recorded as added, so it can't be added again, automatically or with a request.
Binance is the stand-in from test_autopay."""

from __future__ import annotations

import pytest

from harness import OWNER, OWNER_ID, ShopTest, run
from shopbot.binance import BinanceKeyError, BinanceStopping, BinanceUnavailable
from shopbot.handlers import autopay as autopay_module
from test_autopay import (ORDER, OTHER, TX, FakeBinance, balance, choose, owner_texts, pay, texts,
                          with_binance)
from test_shop import ALICE, BOB, register


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(autopay_module, "CHECK_GAP", 0)


ADMIN_LIKE = {"id": 1003, "is_bot": False, "first_name": "Carl", "username": "carl"}


def deposits_of(t: ShopTest, user: dict) -> list:
    return t.db.conn.execute("SELECT * FROM deposits WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()


async def to_the_owner(t: ShopTest, user: dict, amount: int, order_id: str = ORDER) -> dict:
    """Two failed checks: the deposit becomes a request waiting for the owner."""
    await choose(t, user, amount)
    await t.send(user, order_id)
    await t.send(user, order_id)
    (request,) = t.db.pending_deposits()
    return request


# ===================================================================== /regbin
def test_a_registered_order_id_is_never_added_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10")))
        register(t, ALICE)
        register(t, BOB)
        calls = await t.send(OWNER, f"/regbin {ORDER} {ALICE['id']}")
        shown = owner_texts(calls)
        assert "✅ Done." in shown[0] and "Paid by <b>Alice</b>" in shown[0]
        assert f"Order ID <code>{ORDER}</code> is registered as added" in shown[0]
        assert "Asking Binance for its details" in shown[0]
        # What Binance has comes next; the payment is now known by both its IDs.
        assert "Binance has <b>10 USDT</b>, paid " in shown[1] and "from Ali" in shown[1]
        assert "⚠️" not in shown[1]
        row = t.db.binance_payment(TX)
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
        assert "already added by hand (you registered it)" in owner_texts(calls)[-1]
        assert balance(t, BOB) == 0

    run(tmp_path, scenario)


def test_without_a_user_the_payment_is_added_already_whoever_sends_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10")))
        register(t, ALICE)
        calls = await t.send(OWNER, f"/regbin #{ORDER}")  # as some apps copy it
        assert "Paid by" not in owner_texts(calls)[0]
        assert t.db.binance_payment(ORDER)["user_id"] == OWNER_ID
        for _ in range(3):  # however often: never a failed try, never a request
            await choose(t, ALICE, 10)
            calls = await t.send(ALICE, ORDER)
            assert "This payment was already added by the owner." in texts(calls, ALICE)[-1]
            shown = owner_texts(calls)
            assert len(shown) == 1 and "<b>Alice</b>" in shown[0] and "which you registered" in shown[0]
        assert t.db.pending_deposits() == [] and balance(t, ALICE) == 0
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        assert "can't be added again already, nothing changed" in owner_texts(calls)[0]
        assert "registered as added by hand (" in owner_texts(calls)[0]

    run(tmp_path, scenario)


def test_a_request_naming_a_registered_payment_needs_approve_anyway(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        bobs = await to_the_owner(t, BOB, 20)  # Bob claims Alice's payment: kept for his request
        await t.press(OWNER, f"dno:{bobs['id']}")
        await t.send(OWNER, f"/addbal {ALICE['id']} 25")  # Alice's 25$, by hand, a while ago
        t.db.conn.execute("UPDATE deposits SET created_at = datetime(created_at, '-40 minutes'),"
                          " decided_at = datetime(decided_at, '-40 minutes') WHERE method = 'manual'")
        alices = await to_the_owner(t, ALICE, 25)  # "already used": a request naming no payment
        assert alices["order_id"] is None and alices["unconfirmed"] == ORDER
        # Registered for someone else: Alice's request stays, and needs Approve anyway.
        register(t, ADMIN_LIKE)
        calls = await t.send(OWNER, f"/regbin {ORDER} {ADMIN_LIKE['id']}")
        assert "registered as added" in owner_texts(calls)[0]
        assert t.db.get_deposit(alices["id"])["status"] == "pending"
        calls = await t.press(OWNER, f"dok:{alices['id']}")
        assert "may have been added already" in t.alert(calls)
        assert t.button(calls, "Approve anyway") == f"dokx:{alices['id']}"
        shown = t.texts(calls, OWNER_ID)[-1]
        assert (f"⚠️ Order ID <code>{ORDER}</code> was added already: you registered it as added by "
                "hand for Carl (") in shown
        assert balance(t, ALICE) == 2500
        await t.press(OWNER, f"dokx:{alices['id']}")  # the owner is sure
        assert balance(t, ALICE) == 5000

    run(tmp_path, scenario)


def test_registering_a_payment_for_its_customer_closes_their_request_naming_only_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        bobs = await to_the_owner(t, BOB, 20)
        await t.press(OWNER, f"dno:{bobs['id']}")
        await t.send(OWNER, f"/addbal {ALICE['id']} 25")
        alices = await to_the_owner(t, ALICE, 25)
        assert alices["order_id"] is None and alices["unconfirmed"] == ORDER
        calls = await t.send(OWNER, f"/regbin {ORDER} {ALICE['id']}")
        assert f"Request #{alices['id']} was waiting with it: closed." in owner_texts(calls)[0]
        assert any(f"Your deposit request #{alices['id']} (25.00$) is closed" in x for x in texts(calls, ALICE))
        assert t.db.get_deposit(alices["id"])["status"] == "closed" and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_a_payment_in_a_request_the_owner_approved_is_not_added_again_by_hand(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="20"), pay(order_id=OTHER, tx="P_OTHER", amount="5")))
        register(t, ALICE)
        await choose(t, ALICE, 25)
        await t.send(ALICE, ORDER)  # 20 USDT, not 25
        await t.send(ALICE, OTHER)  # 5 USDT, not 25: both kept for one request
        (request,) = t.db.pending_deposits()
        await t.press(OWNER, f"dok:{request['id']}")  # 25$ for both payments
        assert balance(t, ALICE) == 2500 and t.db.binance_payment(ORDER)["approved_in"] == request["id"]
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 20 {ORDER}")
        shown = owner_texts(calls)[0]
        assert "Nothing was added" in shown and f"was in request #{request['id']} (25.00$ for" in shown
        assert "which you approved" in shown and balance(t, ALICE) == 2500
        calls = await t.send(OWNER, f"/regbin {ORDER} {ALICE['id']}")  # no money: fine
        assert f"It was in request #{request['id']}, which you approved." in owner_texts(calls)[0]
        assert t.db.binance_payment(ORDER)["status"] == "credited" and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_the_lookup_names_the_other_record_when_registered_by_the_other_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        # Bob's request named the Order ID while Binance didn't list it; the owner approved it.
        earlier = t.db.create_review_request(BOB["id"], 2500, ORDER, "", [], [ORDER])
        t.db.decide_deposit(earlier["id"], True, 10**9)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {TX}")
        shown = owner_texts(calls)[-1]
        assert f"added with request #{earlier['id']}: <b>25.00$</b> to <b>Bob</b>" in shown
        assert "to <b>Alice</b>" not in shown
        mine, bobs = t.db.registered_payment(TX), t.db.registered_payment(ORDER)
        assert (mine["currency"], mine["raw_amount"], mine["user_id"]) == ("USDT", "25", ALICE["id"])
        assert bobs["currency"] == "" and bobs["transaction_id"] is None

    run(tmp_path, scenario)


def test_balance_added_by_hand_still_holds_up_the_customers_earlier_payments(tmp_path):
    # The Order ID typed with /addbal is the owner's word, not Binance's: a typo could name
    # someone else's payment, so the customer's other payments still go to the owner.
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10", minutes_ago=30),
                                    pay(order_id=OTHER, tx="P_OTHER", amount="25", minutes_ago=10)))
        register(t, ALICE)
        await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        await choose(t, ALICE, 25)
        calls = await t.send(ALICE, OTHER)
        assert "Sent to the owner" in texts(calls, ALICE)[-1] and balance(t, ALICE) == 1000
        assert "you added 10.00$ by hand (" in [x for x in owner_texts(calls) if "Deposit request" in x][-1]

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
        shown = owner_texts(calls)[0]
        assert "nothing changed" in shown and "added automatically: <b>25.00$</b> to <b>Alice</b>" in shown
        calls = await t.send(OWNER, f"/addbal {BOB['id']} 25 {ORDER}")
        shown = owner_texts(calls)[0]
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
        shown = owner_texts(calls)[0]
        assert "Nothing was registered" in shown and "decide that first" in shown
        assert f"waiting with request #{request['id']} from <b>Alice</b>" in shown
        assert f"/regbin {ORDER} {ALICE['id']}: that closes the request" in shown
        calls = await t.send(OWNER, f"/addbal {BOB['id']} 20 {ORDER}")
        shown = owner_texts(calls)[0]
        assert "Nothing was added" in shown and "decide that first" in shown and "closes" not in shown
        assert balance(t, BOB) == 0 and t.db.binance_payment(ORDER)["status"] == "owner"
        # Alice's own: adding it by hand settles her request.
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 20 {ORDER}")
        assert f"Request #{request['id']} was waiting with it: closed." in owner_texts(calls)[0]
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
        shown = owner_texts(calls)[0]
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
        shown = owner_texts(calls)
        assert f"Request #{request['id']} was waiting with it: closed." in shown[0]
        assert t.db.get_deposit(request["id"])["status"] == "closed"
        assert "closed: added by hand" in t.db.get_deposit(request["id"])["note"]
        assert "Binance has <b>25 USDT</b>" in shown[1] and "You added" not in shown[1]
        assert balance(t, ALICE) == 2500
        # Approving the closed request is no longer possible.
        calls = await t.press(OWNER, f"dok:{request['id']}")
        assert "already closed" in t.alert(calls) and balance(t, ALICE) == 2500

    run(tmp_path, scenario)


def test_what_binance_has_is_compared_with_what_was_added(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10"), pay(order_id=OTHER, tx="P_SENT", amount="-5")))
        register(t, ALICE)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 12 {ORDER}")
        assert "⚠️ You added <b>12.00$</b> for it." in owner_texts(calls)[-1]
        calls = await t.send(OWNER, f"/regbin {OTHER}")
        assert "⚠️ It isn't a payment to the shop." in owner_texts(calls)[-1]

    run(tmp_path, scenario)


def test_when_binance_does_not_list_it_or_cannot_be_asked_it_stays_registered(tmp_path):
    async def scenario(t: ShopTest) -> None:
        fake = with_binance(t, FakeBinance())
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        assert "Binance doesn't list a payment with this ID in the last 48 hours" in owner_texts(calls)[-1]
        assert t.db.binance_payment(ORDER)["status"] == "credited"
        fake.error = BinanceUnavailable("busy")
        calls = await t.send(OWNER, f"/regbin {OTHER}")
        assert "Binance couldn't be asked about it (busy)" in owner_texts(calls)[-1]
        assert t.db.binance_payment(OTHER)["status"] == "credited"
        fake.error = BinanceStopping("stopping")  # the bot is restarting: nothing to tell
        calls = await t.send(OWNER, "/regbin 466000199987322882")
        assert len(owner_texts(calls)) == 1 and t.db.binance_payment("466000199987322882") is not None

    run(tmp_path, scenario)


def test_without_a_binance_key_it_is_only_registered(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, f"/regbin {ORDER}")
        (shown,) = owner_texts(calls)
        assert "registered as added" in shown and "Asking Binance" not in shown
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 5")
        assert "/regbin ORDER_ID" not in owner_texts(calls)[0]  # no tip without automatic deposits

    run(tmp_path, scenario)


def test_the_lookup_warns_when_the_bot_knows_the_payment_by_its_other_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="25")))
        register(t, ALICE)
        register(t, BOB)
        # An earlier request named only Binance's other ID, and the owner approved it.
        earlier = t.db.create_review_request(BOB["id"], 2500, TX, "", [], [TX])
        t.db.decide_deposit(earlier["id"], True, 10**9)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 25 {ORDER}")
        shown = owner_texts(calls)[-1]
        assert "⚠️ The bot has it under its other ID too" in shown
        assert f"added with request #{earlier['id']}: <b>25.00$</b> to <b>Bob</b>" in shown

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
        assert "<b>Alice</b>" in shown
        assert t.button(calls, "⏭ Skip: add 10.00$ to Alice").startswith("abk:")
        assert t.button(calls, "Cancel") == "cancel"
        assert balance(t, ALICE) == 0  # nothing yet
        calls = await t.send(OWNER, "not-an-id")
        shown = owner_texts(calls)[0]
        assert "isn't a Binance Pay Order ID" in shown and "press ⏭ Skip to add 10.00$ without one" in shown
        assert t.has_button(calls, "⏭ Skip: add 10.00$") and balance(t, ALICE) == 0
        calls = await t.send(OWNER, ORDER)
        shown = owner_texts(calls)[0]
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
        # Both in one message: no question.
        await t.send(OWNER, f"/addbal {ALICE['id']}")
        calls = await t.send(OWNER, f"7 {OTHER}")
        assert "Added <b>7.00$</b>" in owner_texts(calls)[0] and "registered as added" in owner_texts(calls)[0]
        assert balance(t, ALICE) == 2200

    run(tmp_path, scenario)


def test_a_refused_order_id_in_the_prompt_asks_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        with_binance(t, FakeBinance(pay(amount="10")))
        register(t, ALICE)
        register(t, BOB)
        await t.send(OWNER, f"/addbal {ALICE['id']} 10 {ORDER}")
        await t.send(OWNER, "/addbal")
        await t.send(OWNER, str(BOB["id"]))
        await t.send(OWNER, "10")
        calls = await t.send(OWNER, ORDER)  # Alice's, a typo
        shown = owner_texts(calls)[0]
        assert "Nothing was added" in shown and "Send another Order ID, or press ⏭ Skip to add 10.00$ to <b>Bob</b>" in shown
        assert t.has_button(calls, "⏭ Skip: add 10.00$") and balance(t, BOB) == 0
        calls = await t.send(OWNER, OTHER)
        assert "Added <b>10.00$</b>" in owner_texts(calls)[0] and balance(t, BOB) == 1000
        # A refusal, then Skip: added without an Order ID.
        await t.send(OWNER, f"/addbal {BOB['id']}")
        await t.send(OWNER, "3")
        calls = await t.send(OWNER, ORDER)
        calls = await t.press(OWNER, t.button(calls, "⏭ Skip: add 3.00$"))
        assert "Added <b>3.00$</b>" in owner_texts(calls)[-1] and balance(t, BOB) == 1300

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
        with_binance(t, FakeBinance(pay(amount="10"), pay(order_id=OTHER, tx="P_OTHER", amount="15")))
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
        assert "Nothing was added" in owner_texts(calls)[0] and "was used already" in owner_texts(calls)[0]
        calls = await t.send(OWNER, f"{ORDER}\n10")  # either way round
        assert "Nothing was added" in owner_texts(calls)[0] and balance(t, ALICE) == 1000
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
        calls = await t.send(OWNER, f"10 {OTHER}")
        assert "Added <b>10.00$</b>" in owner_texts(calls)[0]
        assert t.db.binance_payment(OTHER)["user_id"] == ALICE["id"] and balance(t, ALICE) == 2000
        # At the amount step too: "AMOUNT ORDER_ID extra" isn't one ID.
        await t.send(OWNER, f"/addbal {BOB['id']}")
        calls = await t.send(OWNER, f"5 {ORDER} 5")
        assert "isn't a Binance Pay Order ID" in owner_texts(calls)[0] and balance(t, BOB) == 0
        # "Order ID: ..." and "#..." still work.
        calls = await t.send(OWNER, "5")
        calls = await t.send(OWNER, "Order ID: 466000199987322889")
        assert "Added <b>5.00$</b>" in owner_texts(calls)[0]
        assert t.db.registered_payment("466000199987322889")["user_id"] == BOB["id"]

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
