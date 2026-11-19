"""Regression tests for the review findings: edits, failed Telegram calls after a change
was saved, locked prices, background deliveries, pending requests, and more."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

import pytest
from telegram.error import RetryAfter, TelegramError

from harness import OWNER, OWNER_ID, Call, ShopTest, run, tg_user
from shopbot import ratelimit
from shopbot.db import Database
from shopbot.handlers import shop as shop_module
from shopbot.handlers.common import EDIT_HINT, setup_bot_profile

ALICE = tg_user(1001, "Alice", "alice")
BOB = tg_user(1002, "Bob", "bob")
ADMIN = tg_user(2001, "Adam", "adam")

EXPIRED = "This confirmation has expired"
TOO_OLD = "Bad Request: query is too old and response timeout expired or query ID is invalid"


# ===================================================================== helpers
def register(t: ShopTest, user: dict) -> None:
    t.db.touch_user(user["id"], user["first_name"], user.get("last_name"), user.get("username"))


def make_admin(t: ShopTest, user: dict) -> None:
    register(t, user)
    t.db.set_admin(user["id"], True)


def make_label(t: ShopTest, path: str, price: Optional[int] = None) -> int:
    parent: Optional[int] = None
    for name in path.split("/"):
        found = [row for row in t.db.children(parent) if row["name"] == name]
        parent = found[0]["id"] if found else t.db.add_labels(parent, [name])[0][0]["id"]
    if price is not None:
        t.db.set_label_price(parent, price)
    assert parent is not None
    return parent


def add_stock(t: ShopTest, label_id: int, *texts: str, owner_id: int = OWNER_ID) -> list[str]:
    return t.db.add_items(label_id, owner_id, [{"kind": "text", "text": x, "entities": []} for x in texts])


def give_earnings(t: ShopTest, seller: dict, cents: int) -> None:
    """``seller`` sells one product worth ``cents`` to a fixture buyer."""
    buyer = tg_user(3999, "Fixture buyer")
    register(t, buyer)
    label = make_label(t, f"Fixture/Sale {seller['id']}-{cents}", cents)
    add_stock(t, label, f"sold-{cents}", owner_id=seller["id"])
    t.db.set_balance(buyer["id"], t.db.get_user(buyer["id"])["balance"] + cents)
    t.db.purchase(buyer["id"], label, 1, 10)


def all_text(calls: list[Call], chat_id: Optional[int] = None) -> str:
    return "\n".join(ShopTest.texts(calls, chat_id))


def datas(calls: list[Call]) -> list[str]:
    return [b.get("callback_data", "") for b in ShopTest.all_buttons(calls)]


def balance(t: ShopTest, user: dict) -> int:
    return t.db.get_user(user["id"])["balance"]


def orders(t: ShopTest, user: dict) -> list:
    return t.db.user_orders(user["id"], 0, 100)


def contents(t: ShopTest, label_id: int) -> list[str]:
    rows = t.db.conn.execute(
        "SELECT content FROM items WHERE label_id = ? AND status = 'available' ORDER BY id",
        (label_id,),
    ).fetchall()
    return [json.loads(r["content"]).get("text") or json.loads(r["content"]).get("caption") for r in rows]


def fail_when(t: ShopTest, predicate: Callable[[str, dict], bool], description: str,
              code: int = 400, times: int = 1_000_000) -> None:
    """Make matching Bot API calls fail with HTTP ``code`` (403 = Forbidden)."""
    original = type(t.tg).do_request.__get__(t.tg)
    left = {"n": times}

    async def do_request(url, method, request_data=None, read_timeout=None, write_timeout=None,
                         connect_timeout=None, pool_timeout=None):
        api_method = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        if left["n"] > 0 and predicate(api_method, params):
            left["n"] -= 1
            t.tg.calls.append(Call(method=api_method, params=params, files={}, failed=True))
            body = {"ok": False, "error_code": code, "description": description}
            return code, json.dumps(body).encode()
        return await original(url, method, request_data, read_timeout, write_timeout,
                              connect_timeout, pool_timeout)

    t.tg.do_request = do_request  # type: ignore[method-assign]


def to(chat_id: int) -> Callable[[str, dict], bool]:
    return lambda method, params: int(params.get("chat_id") or 0) == chat_id


async def edit(t: ShopTest, user: dict, message_id: int, text: Optional[str] = None, **extra) -> list[Call]:
    """The update Telegram sends when ``user`` edits their message ``message_id``."""
    now = int(time.time())
    message = {
        "message_id": message_id, "date": now - 30, "edit_date": now,
        "chat": {"id": user["id"], "type": "private", "first_name": user["first_name"]},
        "from": user, **extra,
    }
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return await t._process({"edited_message": message})


def photo(file_id: str) -> list[dict]:
    return [{"file_id": file_id, "file_unique_id": f"u-{file_id}", "width": 800, "height": 800}]


async def withdraw(t: ShopTest, who: dict, amount: str, binance: str = "123456789") -> list[Call]:
    await t.send(who, "/withdraw")
    await t.send(who, amount)
    return await t.send(who, binance)


async def deposit(t: ShopTest, who: dict, amount: int = 20) -> list[Call]:
    await t.press(who, "dp")
    await t.press(who, f"da:{amount}")
    await t.send_photo(who, f"{who['first_name']}-receipt")
    return await t.press(who, "dsend")


# ============================================================ pending requests
def test_requests_the_owner_never_got_are_listed_by_pending(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        make_admin(t, ADMIN)
        register(t, ALICE)
        give_earnings(t, ADMIN, 3000)
        fail_when(t, to(OWNER_ID), "Forbidden: bot was blocked by the user", code=403, times=3)

        calls = await withdraw(t, ADMIN, "10")
        assert "Withdrawal request sent" in all_text(calls, ADMIN["id"])
        assert "couldn't be notified right now" in all_text(calls, ADMIN["id"])
        calls = await deposit(t, ALICE, 20)
        assert "Request sent!" in all_text(calls, ALICE["id"])
        assert "couldn't be notified right now" in all_text(calls, ALICE["id"])
        (wd,) = t.db.pending_withdrawals()
        (dep,) = t.db.pending_deposits()

        calls = await t.send(OWNER, "/start")
        assert "Requests waiting for you: <b>2</b>" in t.last_text(calls)
        assert t.button(calls, "Pending requests (2)") == "pq"

        calls = await t.send(OWNER, "/pending")
        assert "Pending requests: 2" in all_text(calls, OWNER_ID)
        receipt = [c for c in calls if c.method == "sendPhoto" and c.chat_id == OWNER_ID]
        assert receipt and receipt[0].params["photo"] == "Alice-receipt"
        assert f"Deposit request #{dep['id']}" in receipt[0].text
        assert {f"dok:{dep['id']}", f"dno:{dep['id']}", f"wok:{wd['id']}", f"wno:{wd['id']}"} <= set(datas(calls))

        calls = await t.press(OWNER, f"wok:{wd['id']}")
        assert "Withdrawal approved" in all_text(calls, ADMIN["id"])
        calls = await t.press(OWNER, "pq")
        assert "Pending requests: 1" in all_text(calls, OWNER_ID)
        assert f"wok:{wd['id']}" not in datas(calls)
        await t.press(OWNER, f"dok:{dep['id']}", photo=True)
        assert balance(t, ALICE) == 2000
        calls = await t.send(OWNER, "/pending")
        assert "No deposits or withdrawals are waiting" in t.last_text(calls)
        calls = await t.send(OWNER, "/start")
        assert not t.has_button(calls, "Pending requests")

    run(tmp_path, scenario)


def test_pending_is_owner_only_and_shown_on_the_earnings_screen(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        give_earnings(t, ADMIN, 3000)
        await withdraw(t, ADMIN, "10")
        calls = await t.send(ADMIN, "/pending")
        assert "Only the owner" in t.last_text(calls)
        calls = await t.press(ADMIN, "pq")
        assert "Only the owner" in t.alert(calls)
        calls = await t.press(OWNER, "ea")
        assert t.button(calls, "Pending requests (1)") == "pq"
        calls = await t.send(OWNER, "/help")
        assert "/pending" in t.last_text(calls)

    run(tmp_path, scenario)


# ======================================================================= edits
def test_editing_an_unsaved_product_replaces_it_and_a_saved_one_is_flagged(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, "CODE-TYPO", message_id=50)
        await t.send(OWNER, "CODE-2", message_id=51)
        calls = await edit(t, OWNER, 50, "CODE-FIXED")
        assert "Product 1 updated" in t.last_text(calls)
        calls = await t.press(OWNER, "apd")
        assert "Saved 2 product(s)" in t.last_text(calls)
        assert contents(t, five) == ["CODE-FIXED", "CODE-2"]

        code = t.db.list_items(five)[1]["code"]
        calls = await edit(t, OWNER, 51, "CODE-CHANGED")
        assert f"Product <code>{code}</code> was already saved" in t.last_text(calls)
        assert contents(t, five) == ["CODE-FIXED", "CODE-2"]

        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 5000)
        t.db.purchase(ALICE["id"], five, 2, 10)
        calls = await edit(t, OWNER, 50, "CODE-AGAIN")
        assert "is no longer in stock" in t.last_text(calls)

    run(tmp_path, scenario)


def test_an_edit_that_makes_the_caption_too_long_keeps_the_first_version(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, message_id=60, photo=photo("qr"), caption="PIN 1234")
        calls = await edit(t, OWNER, 60, photo=photo("qr"), caption="x" * 1025)
        assert "too long" in t.last_text(calls) and "first is kept" in t.last_text(calls)
        await t.press(OWNER, "apd")
        assert contents(t, five) == ["PIN 1234"]

    run(tmp_path, scenario)


def test_edits_never_act_as_input_and_get_a_hint_only_when_it_matters(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        await t.send(OWNER, "/addbal", message_id=70)
        await t.send(OWNER, str(ALICE["id"]), message_id=71)
        calls = await edit(t, OWNER, 71, "1002")
        assert t.last_text(calls) == EDIT_HINT
        assert t.app.user_data[OWNER_ID]["state"]["name"] == "owner_amount"
        calls = await t.send(OWNER, "5")
        assert balance(t, ALICE) == 500 and "Added <b>5.00$</b>" in t.last_text(calls, OWNER_ID)

        calls = await edit(t, OWNER, 70, "/addbal 1001 50")  # an old command, no flow running
        assert t.last_text(calls) == EDIT_HINT and balance(t, ALICE) == 500
        calls = await edit(t, ALICE, 80, "hello there")  # plain chat edit: nothing to say
        assert t.messages(calls) == []

        t.db.set_banned(ALICE["id"], True)
        calls = await edit(t, ALICE, 81, "/start")
        assert t.messages(calls) == [] and t.answers(calls) == []

    run(tmp_path, scenario)


def test_long_captions_are_refused_when_adding_products(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        await t.press(OWNER, f"ap:{five}:0")
        calls = await t.send(OWNER, photo=photo("a"), caption="😀" * 513)  # 1026 UTF-16 units
        assert "caption is too long" in t.last_text(calls) and "1026" in t.last_text(calls)
        calls = await t.press(OWNER, "apd")
        assert "Send at least one product message first." in t.alert(calls)
        await t.send(OWNER, photo=photo("b"), caption="y" * 1024)
        calls = await t.press(OWNER, "apd")
        assert "Saved 1 product(s)" in t.last_text(calls)

    run(tmp_path, scenario)


def test_an_old_add_product_cancel_button_really_cancels_the_current_flow(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        give_earnings(t, ADMIN, 3000)
        await t.send(ADMIN, "/withdraw")
        await t.send(ADMIN, "10")
        calls = await t.press(ADMIN, "apc")
        assert "Cancelled" in t.last_text(calls)
        calls = await t.send(ADMIN, "thanks")
        assert "Send /start" in t.last_text(calls)
        assert t.db.pending_withdrawals() == []

    run(tmp_path, scenario)


# ================================================================ price lock
def test_a_price_change_asks_to_confirm_again_and_never_charges_another_amount(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B", "C", "D")
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 10000)
        confirm = t.button(await t.press(ALICE, f"sb:{five}:2"), "Confirm")

        t.db.set_label_price(five, 1000)
        calls = await t.press(ALICE, confirm)
        assert "price changed" in t.alert(calls)
        assert "The price changed" in t.last_text(calls) and "Total: <b>20.00$</b>" in t.last_text(calls)
        assert balance(t, ALICE) == 10000 and orders(t, ALICE) == []
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        assert balance(t, ALICE) == 8000

        confirm = t.button(await t.press(ALICE, f"sb:{five}:1"), "Confirm")
        t.db.set_label_price(five, 500)  # cheaper: still asks, then charges what is shown
        calls = await t.press(ALICE, confirm)
        assert "Total: <b>5.00$</b>" in t.last_text(calls) and balance(t, ALICE) == 8000
        await t.press(ALICE, t.button(calls, "Confirm"))
        assert balance(t, ALICE) == 7500

    run(tmp_path, scenario)


def test_a_confirmation_stays_valid_while_the_price_is_the_one_shown(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A")
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 5000)
        confirm = t.button(await t.press(ALICE, f"sb:{five}:1"), "Confirm")
        await t.send(ALICE, "/start")
        await t.press(ALICE, "dp")
        t.db.set_label_price(five, 2000)
        t.db.set_label_price(five, 950)
        calls = await t.press(ALICE, confirm)
        assert "Purchase successful" in t.alert(calls)
        assert balance(t, ALICE) == 5000 - 950

    run(tmp_path, scenario)


# ==================================================================== delivery
def test_a_big_delivery_does_not_hold_up_other_users(tmp_path):
    async def scenario(t: ShopTest) -> None:
        shop_module.DELIVERY_INTERVAL = 0.01
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, *(f"CODE-{i:02d}" for i in range(30)))
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 10000)
        confirm = t.button(await t.press(ALICE, f"sb:{five}:30"), "Confirm")

        real_settle = t.settle

        async def quick_settle() -> None:
            await asyncio.sleep(0.005)

        t.settle = quick_settle  # type: ignore[method-assign]
        mark = len(t.tg.calls)
        await t.press(ALICE, confirm)
        bob = await t.send(BOB, "/start")
        assert "Welcome Bob" in t.last_text(bob, BOB["id"])
        (order,) = orders(t, ALICE)
        calls = await t.press(ALICE, f"mov:{order['id']}")
        assert "still being sent" in t.alert(calls)
        t.settle = real_settle  # type: ignore[method-assign]
        await t.settle()

        calls = t.tg.calls[mark:]
        sent = [c.text for c in calls if c.method == "sendMessage" and c.chat_id == ALICE["id"]]
        assert [x for x in sent if x.startswith("CODE-")] == [f"CODE-{i:02d}" for i in range(30)]
        assert "Delivered 30 product(s)" in sent[-1]
        bob_index = next(i for i, c in enumerate(calls) if c.chat_id == BOB["id"])
        last_code = max(i for i, c in enumerate(calls) if (c.text or "").startswith("CODE-"))
        assert bob_index < last_code, "Bob had to wait for Alice's whole delivery"

        calls = await t.press(ALICE, f"mov:{order['id']}")  # done now: re-download works
        assert t.last_text(calls, ALICE["id"]) == "✅ Done."

    run(tmp_path, scenario)


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")  # RetryAfter(int)
def test_a_short_flood_wait_during_a_delivery_is_waited_out(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "CODE-1", "CODE-2")
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 5000)
        confirm = t.button(await t.press(ALICE, f"sb:{five}:2"), "Confirm")
        original = type(t.tg).do_request.__get__(t.tg)
        state = {"limited": False}

        async def do_request(url, method, request_data=None, *args, **kwargs):
            params = dict(request_data.parameters) if request_data is not None else {}
            if params.get("text") == "CODE-2" and not state["limited"]:
                state["limited"] = True
                body = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1",
                        "parameters": {"retry_after": 1}}
                return 429, json.dumps(body).encode()
            return await original(url, method, request_data, *args, **kwargs)

        t.tg.do_request = do_request  # type: ignore[method-assign]
        calls = await t.press(ALICE, confirm)
        sent = [c.text for c in calls if c.method == "sendMessage" and c.chat_id == ALICE["id"]]
        assert state["limited"] and "CODE-1" in sent and "CODE-2" in sent
        assert "Delivered 2 product(s)" in sent[-1]

    run(tmp_path, scenario)


def test_a_buyer_who_blocked_the_bot_is_not_sent_every_product(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, *(f"CODE-{i}" for i in range(10)))
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 5000)
        confirm = t.button(await t.press(ALICE, f"sb:{five}:10"), "Confirm")
        fail_when(t, lambda m, p: m == "sendMessage" and to(ALICE["id"])(m, p),
                  "Forbidden: bot was blocked by the user", code=403)
        calls = await t.press(ALICE, confirm)
        attempts = [c for c in calls if c.method == "sendMessage" and (c.text or "").startswith("CODE-")]
        assert len(attempts) == 1
        assert len(orders(t, ALICE)) == 1 and balance(t, ALICE) == 4000

    run(tmp_path, scenario)


# ================================================================ /modifybal
def test_modifybal_asks_again_if_the_balance_changed_while_typing(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 1000)
        calls = await t.send(OWNER, f"/modifybal {ALICE['id']}")
        assert "Balance: <b>10.00$</b>" in t.last_text(calls)
        t.db.manual_deposit(ALICE["id"], 10000, 10**9)  # e.g. a deposit approved meanwhile
        calls = await t.send(OWNER, "15")
        assert "changed to <b>110.00$</b>" in t.last_text(calls) and "nothing was changed" in t.last_text(calls)
        assert balance(t, ALICE) == 11000
        calls = await t.send(OWNER, "15")
        assert "from 110.00$ to <b>15.00$</b>" in t.last_text(calls)
        assert balance(t, ALICE) == 1500

    run(tmp_path, scenario)


def test_one_step_addbal_and_modifybal_warn_about_ids_that_never_used_the_bot(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/addbal 555000 5")
        assert "hasn't started the bot yet" in t.last_text(calls, OWNER_ID)
        calls = await t.send(OWNER, "/modifybal 555000 1")
        assert "hasn't started the bot yet" in t.last_text(calls, OWNER_ID)
        register(t, ALICE)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 5")
        assert "hasn't started" not in t.last_text(calls, OWNER_ID)

    run(tmp_path, scenario)


# ============================================================== removed admins
def test_a_removed_admin_can_still_find_and_withdraw_their_earnings(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        give_earnings(t, ADMIN, 950)
        calls = await t.send(OWNER, f"/removeadmin {ADMIN['id']}")
        notice = all_text(calls, ADMIN["id"])
        assert "You still have <b>9.50$</b>" in notice and "/withdraw" in notice
        calls = await t.send(ADMIN, "/start")
        assert "Earnings: <b>9.50$</b>" in t.last_text(calls)
        assert t.button(calls, "Earnings") == "ea"
        calls = await t.send(ADMIN, "/help")
        assert "/withdraw" in t.last_text(calls)
        calls = await withdraw(t, ADMIN, "9.50")
        assert "Withdrawal request sent" in all_text(calls, ADMIN["id"])

    run(tmp_path, scenario)


# ============================================================== support contact
def test_customers_are_told_who_to_contact(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")  # the owner's @username is known from now on
        register(t, ALICE)
        await deposit(t, ALICE)
        (dep,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dno:{dep['id']}", photo=True)
        assert "please contact @the_owner" in all_text(calls, ALICE["id"])
        calls = await t.send(ALICE, "/help")
        assert "Contact @the_owner" in t.last_text(calls)

    run(tmp_path, scenario)


@pytest.mark.parametrize("setting, expected", [("@help_desk", "contact @help_desk"),
                                               ("none", "contact the owner")])
def test_the_support_contact_can_be_set_or_hidden(tmp_path, setting, expected):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        register(t, ALICE)
        await deposit(t, ALICE)
        (dep,) = t.db.pending_deposits()
        calls = await t.press(OWNER, f"dno:{dep['id']}", photo=True)
        assert expected in all_text(calls, ALICE["id"])
        assert "the_owner" not in all_text(calls, ALICE["id"])
        calls = await t.send(ALICE, "/help")
        assert ("Contact" in t.last_text(calls)) == (setting != "none")

    run(tmp_path, scenario, support_contact=setting)


# ======================================================================= bans
def test_a_banned_user_is_blocked_even_if_the_ban_notice_fails(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        five = make_label(t, "Esim/5GB", 950)
        (code,) = add_stock(t, five, "ADMIN-CODE", owner_id=ADMIN["id"])
        item = t.db.get_item_by_code(code)
        t.db.set_banned(ADMIN["id"], True)
        t.tg.fail["answerCallbackQuery"] = TOO_OLD
        calls = await t.press(ADMIN, f"mu:{item['id']}")
        assert t.db.get_item(item["id"])["status"] == "available"
        assert "ADMIN-CODE" not in all_text(calls)
        del t.tg.fail["answerCallbackQuery"]
        fail_when(t, to(ADMIN["id"]), "Bad Request: injected failure")
        calls = await t.send(ADMIN, "/withdraw")
        assert not [c for c in calls if not c.get("failed") and c.method == "sendMessage"]

    run(tmp_path, scenario)


def test_a_database_error_during_the_ban_check_stops_the_update(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        t.db.touch_user = broken  # type: ignore[method-assign]
        calls = await t.press(ALICE, "sh:0:0")
        assert "Something went wrong" in t.alert(calls)
        assert t.messages(calls) == []
        calls = await t.send(ALICE, "/start")
        assert [c.text for c in t.messages(calls)] == [
            "⚠️ Something went wrong. Please try again in a moment."
        ]

    run(tmp_path, scenario)


# ================================================================ bot profile
def test_bot_profile_is_only_sent_again_when_it_changed(tmp_path):
    def profile_calls(calls: list[Call]) -> list[str]:
        return [c.method for c in calls if c.method.startswith(("setMy", "deleteMy"))]

    async def scenario(t: ShopTest) -> None:
        mark = len(t.tg.calls)
        await setup_bot_profile(t.app)
        assert "setMyDescription" in profile_calls(t.tg.calls[mark:])
        mark = len(t.tg.calls)
        await setup_bot_profile(t.app)
        assert profile_calls(t.tg.calls[mark:]) == []

        make_admin(t, ADMIN)  # the staff list changed: the menus are sent again
        mark = len(t.tg.calls)
        await setup_bot_profile(t.app)
        admin_menus = [c for c in t.tg.calls[mark:] if c.method == "setMyCommands"
                       and (c.params.get("scope") or {}).get("chat_id") == ADMIN["id"]]
        assert admin_menus

    run(tmp_path, scenario)


def test_bot_profile_is_retried_after_a_failure(tmp_path):
    async def scenario(t: ShopTest) -> None:
        t.tg.fail["setMyDescription"] = "Too Many Requests: retry after 999"
        await setup_bot_profile(t.app)
        del t.tg.fail["setMyDescription"]
        mark = len(t.tg.calls)
        await setup_bot_profile(t.app)
        assert any(c.method == "setMyDescription" for c in t.tg.calls[mark:])

    run(tmp_path, scenario)


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")  # RetryAfter(int)
def test_the_rate_limiter_does_not_sit_out_long_flood_waits():
    async def main() -> None:
        limiter = ratelimit.ShopRateLimiter(max_retries=5)
        await limiter.initialize()
        attempts: list[str] = []

        def failing(wait: int, succeed_after: Optional[int] = None):
            async def callback(*args, **kwargs):
                attempts.append("x")
                if succeed_after is not None and len(attempts) > succeed_after:
                    return True
                raise RetryAfter(wait)
            return callback

        started = time.monotonic()
        with pytest.raises(ratelimit.FloodWait):
            await limiter.process_request(failing(3600), (), {}, "sendMessage", {"chat_id": 1}, None)
        assert len(attempts) == 1
        attempts.clear()
        with pytest.raises(TelegramError):  # bot settings never wait, even for 1 second
            await limiter.process_request(failing(1), (), {}, "setMyCommands", {}, None)
        assert len(attempts) == 1
        assert time.monotonic() - started < 1
        attempts.clear()
        result = await limiter.process_request(failing(0, succeed_after=1), (), {}, "sendMessage",
                                               {"chat_id": 1}, None)
        assert result is True and len(attempts) == 2  # short waits are still retried
        await limiter.shutdown()

    asyncio.run(main())


# ================================================================= database
def test_a_failed_commit_is_rolled_back_and_later_writes_are_kept(tmp_path):
    path = str(Path(tmp_path) / "shop.db")
    db = Database(path, OWNER_ID)
    db.conn.execute("PRAGMA journal_mode = DELETE")  # e.g. a network drive without WAL
    db.conn.execute("PRAGMA busy_timeout = 50")
    reader = sqlite3.connect(path, timeout=0)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM users").fetchall()  # holds a read lock: COMMIT can't finish
    with pytest.raises(sqlite3.OperationalError):
        db.manual_deposit(1001, 777, 10**9)
    assert not db.conn.in_transaction
    reader.rollback()
    reader.close()
    db.manual_deposit(1001, 500, 10**9)
    db.close()

    again = Database(path, OWNER_ID)
    assert again.get_user(1001)["balance"] == 500
    assert again.conn.execute("SELECT COUNT(*) FROM deposits").fetchone()[0] == 1
    again.close()


def test_nested_transactions_roll_back_together(tmp_path):
    db = Database(str(Path(tmp_path) / "shop.db"), OWNER_ID)
    with pytest.raises(RuntimeError):
        with db.tx():
            db.ensure_user(1001)
            with db.tx():
                db.conn.execute("UPDATE users SET balance = 99 WHERE id = 1001")
            raise RuntimeError("boom")
    assert db.get_user(1001) is None
    with db.tx():
        db.ensure_user(1002)
    assert db.get_user(1002) is not None and not db.conn.in_transaction
    db.close()


# ===================================================================== labels
def test_a_long_list_of_new_labels_is_summarised(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.press(OWNER, "lb:0:0")
        await t.press(OWNER, "ln:0")
        calls = await t.send(OWNER, "\n".join(f"Country {i:02d}" for i in range(60)))
        text = t.last_text(calls)
        assert "Added (60):" in text and "… and 35 more" in text
        assert len(t.db.children(None)) == 60

    run(tmp_path, scenario)


def test_stopping_cuts_a_long_delivery_short_and_leaves_the_rest_for_my_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(shop_module, "STOP_GRACE", 0.3)

    async def main() -> None:
        t = ShopTest(tmp_path)
        await t.__aenter__()
        shop_module.DELIVERY_INTERVAL = 0.05
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, *(f"CODE-{i:02d}" for i in range(40)))
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 10000)
        confirm = t.button(await t.press(ALICE, f"sb:{five}:40"), "Confirm")

        async def quick_settle() -> None:
            await asyncio.sleep(0.005)

        t.settle = quick_settle  # type: ignore[method-assign]
        mark = len(t.tg.calls)
        await t.press(ALICE, confirm)
        await asyncio.sleep(0.2)
        started = time.monotonic()
        await t.app.stop()  # what a restart does while products are still going out
        assert time.monotonic() - started < 3
        sent = [c.text for c in t.tg.calls[mark:] if c.method == "sendMessage" and c.chat_id == ALICE["id"]]
        codes = [x for x in sent if x.startswith("CODE-")]
        assert 0 < len(codes) < 40
        assert f"{40 - len(codes)} product(s) could not be sent" in sent[-1] and "My Orders" in sent[-1]
        assert len(orders(t, ALICE)) == 1  # paid, and every product is in My Orders
        await t.app.shutdown()
        t.db.close()

    asyncio.run(main())


def test_pressing_done_twice_says_the_products_are_already_saved(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, "CODE-1")
        await t.press(OWNER, "apd")
        calls = await t.press(OWNER, "apd")
        assert "already saved" in t.alert(calls) and "not saved" not in t.alert(calls)
        assert t.db.count_items(five) == 1

    run(tmp_path, scenario)
