"""📣 /announce (owner) and the "🆕 New in the shop" message after stock is added.

Both go to everyone who has started the bot, except the owner and banned users; the
stock message also skips whoever added the stock.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from harness import OWNER, OWNER_ID, ShopTest, run, tg_user
from shopbot.handlers import broadcast as broadcast_module
from test_shop import (
    ADMIN,
    ALICE,
    BOB,
    add_stock,
    inject_failure,
    make_admin,
    make_label,
    names,
    register,
    ui_add_products,
)

CAROL = tg_user(1003, "Carol", "carol")  # banned
DAVE = tg_user(1004, "Dave", "dave")  # never started the bot


def jparam(call: Any, key: str) -> Any:
    value = call.params.get(key)
    return json.loads(value) if isinstance(value, str) else value


def to(calls: list, user: dict, method: str = "sendMessage") -> list:
    return [c for c in calls if c.method == method and c.chat_id == user["id"] and not c.get("failed")]


def people(t: ShopTest) -> None:
    """Alice, Bob and an admin use the bot; Carol is banned; Dave never started it."""
    for user in (ALICE, BOB, CAROL):
        register(t, user)
    make_admin(t, ADMIN)
    t.db.set_banned(CAROL["id"], True)
    t.db.ensure_user(DAVE["id"])


# ==================================================================== announce
def test_announce_with_text_goes_to_everyone_except_owner_banned_and_unstarted(tmp_path):
    async def scenario(t: ShopTest) -> None:
        people(t)
        text = "/announce 🎉 Big sale today!"
        bold = {"type": "bold", "offset": 13, "length": 8}  # "Big sale" (🎉 is 2 UTF-16 units)
        calls = await t.send(OWNER, text, entities=[
            {"type": "bot_command", "offset": 0, "length": 9}, bold])
        preview = to(calls, OWNER)
        assert preview[0].params["text"] == "🎉 Big sale today!"
        assert jparam(preview[0], "entities") == [{"type": "bold", "offset": 3, "length": 8}]
        assert "Send the message above to 3 user(s)?" in preview[-1].params["text"]
        assert not [c for c in calls if c.chat_id not in (None, OWNER_ID) and c.method.startswith("send")]

        calls = await t.press(OWNER, t.button(calls, "Send to 3"))
        for user in (ALICE, BOB, ADMIN):
            got = to(calls, user)
            assert [c.params["text"] for c in got] == ["🎉 Big sale today!"], user
            assert jparam(got[0], "entities") == [{"type": "bold", "offset": 3, "length": 8}]
        for user in (CAROL, DAVE):
            assert to(calls, user) == []
        assert "Announcement sent</b> to all 3 user(s)" in to(calls, OWNER)[-1].params["text"]

    run(tmp_path, scenario)


def test_announce_asks_for_any_message_and_sends_it_exactly(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/announce")
        assert "Send the message for all users" in t.last_text(calls)
        photo = [{"file_id": "promo-small", "file_unique_id": "ps", "width": 90, "height": 90},
                 {"file_id": "promo", "file_unique_id": "pl", "width": 1280, "height": 720}]
        calls = await t.send(OWNER, photo=photo, caption="New products!",
                             caption_entities=[{"type": "italic", "offset": 0, "length": 3}])
        shown = to(calls, OWNER, "sendPhoto")
        assert shown and shown[0].params["photo"] == "promo"
        calls = await t.press(OWNER, t.button(calls, "Send to 1"))
        sent = to(calls, ALICE, "sendPhoto")
        assert len(sent) == 1 and sent[0].params["photo"] == "promo"
        assert sent[0].params["caption"] == "New products!"
        assert jparam(sent[0], "caption_entities") == [{"type": "italic", "offset": 0, "length": 3}]

    run(tmp_path, scenario)


def test_unsupported_messages_are_refused_and_the_question_stays(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        await t.send(OWNER, "/announce")
        calls = await t.send(OWNER, location={"latitude": 1.0, "longitude": 2.0})
        assert "can't be announced" in t.last_text(calls)
        calls = await t.send(OWNER, "Now a text")
        assert "Send the message above to 1 user(s)?" in t.last_text(calls)

    run(tmp_path, scenario)


def test_cancelled_old_and_repeated_send_buttons_send_nothing_more(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/announce first")
        first = t.button(calls, "Send to 1")
        calls = await t.press(OWNER, t.button(calls, "Cancel"))
        assert "Announcement cancelled" in t.last_text(calls)
        calls = await t.press(OWNER, first)
        assert "already sent or cancelled" in t.alert(calls) and to(calls, ALICE) == []

        calls = await t.send(OWNER, "/announce second")
        older = t.button(calls, "Send to 1")
        calls = await t.send(OWNER, "/announce third")
        newer = t.button(calls, "Send to 1")
        calls = await t.press(OWNER, older)  # replaced by the newer one
        assert "already sent or cancelled" in t.alert(calls) and to(calls, ALICE) == []
        calls = await t.press(OWNER, newer)
        assert [c.params["text"] for c in to(calls, ALICE)] == ["third"]
        calls = await t.press(OWNER, newer)  # pressed again
        assert "already sent or cancelled" in t.alert(calls) and to(calls, ALICE) == []

    run(tmp_path, scenario)


def test_only_the_owner_can_announce(tmp_path):
    async def scenario(t: ShopTest) -> None:
        people(t)
        for user in (ADMIN, ALICE):
            calls = await t.send(user, "/announce hello")
            assert "Only the owner can use this command" in t.last_text(calls)
        calls = await t.send(OWNER, "/announce real")
        send = t.button(calls, "Send to")
        calls = await t.press(ADMIN, send)
        assert "Only the owner can send announcements" in t.alert(calls)
        assert to(calls, ALICE) == []

    run(tmp_path, scenario)


def test_users_who_blocked_the_bot_are_counted_not_retried(tmp_path):
    async def scenario(t: ShopTest) -> None:
        people(t)
        inject_failure(t, lambda method, p: method == "sendMessage" and str(p.get("chat_id")) == str(BOB["id"]),
                       status=403, description="Forbidden: bot was blocked by the user")
        calls = await t.send(OWNER, "/announce hi all")
        calls = await t.press(OWNER, t.button(calls, "Send to 3"))
        summary = to(calls, OWNER)[-1].params["text"]
        assert "sent</b> to 2 of 3 user(s)" in summary
        assert "1 blocked the bot or deleted their account" in summary
        assert len([c for c in calls if c.chat_id == BOB["id"]]) == 1  # tried once

    run(tmp_path, scenario)


def test_announce_with_nobody_to_send_to(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/announce hello?")
        assert "Nobody would get it yet" in t.last_text(calls)
        assert not t.has_button(calls, "Send to")

    run(tmp_path, scenario)


def test_a_broadcast_stops_when_the_bot_stops():
    class App:
        running = False
        bot_data: dict = {}

    out = broadcast_module.Outcome(total=3)
    sent: list = []

    async def send_one(user: Any) -> None:
        sent.append(user["id"])

    asyncio.run(broadcast_module.send_to_all(App(), [{"id": 1}, {"id": 2}, {"id": 3}], send_one, out))  # type: ignore[arg-type]
    assert sent == [] and out.left == 3
    assert "3 user(s) didn't get it" in broadcast_module.announce_summary(out)


# ================================================================= new stock
def test_new_stock_is_announced_to_everyone_except_owner_adder_and_banned(tmp_path):
    async def scenario(t: ShopTest) -> None:
        people(t)
        five = make_label(t, "Esim/Oman/5GB", 950)
        calls = await t.send(ADMIN, "/start")
        calls = await t.press(ADMIN, t.button(calls, "Add Product"))
        for name in ("Esim", "Oman", "5GB"):
            calls = await t.press(ADMIN, t.button(calls, name))
        await t.send(ADMIN, "code-1")
        await t.send(ADMIN, "code-2")
        calls = await t.press(ADMIN, t.button(calls, "Done"))
        assert "Customers get a message about the new stock" in t.last_text(calls, ADMIN["id"])

        for user in (ALICE, BOB):
            got = to(calls, user)
            assert len(got) == 1, user
            assert "New in the shop!" in got[0].params["text"]
            assert "Esim › Oman › 5GB · <b>9.50$</b> · 2 new" in got[0].params["text"]
            assert f"sh:{five}:0" in [b.get("callback_data") for b in got[0].buttons]
        for user in (OWNER, CAROL, DAVE):
            assert [c for c in to(calls, user) if "New in the shop" in (c.params.get("text") or "")] == []
        assert [c for c in to(calls, ADMIN) if "New in the shop" in (c.params.get("text") or "")] == []

        calls = await t.press(ALICE, f"sh:{five}:0")  # the button in the message
        assert "In stock: <b>2</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_sellers_see_their_own_price_in_the_stock_message(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        register(t, BOB)
        t.db.set_seller(BOB["id"], True)
        make_label(t, "Esim/Oman/5GB", 950)
        calls = await t.send(OWNER, "/start")  # the owner adds it
        await ui_add_products(t, OWNER, ["Esim", "Oman", "5GB"], [{"text": "X"}])
        sent = t.tg.calls
        alice = [c for c in to(sent, ALICE) if "New in the shop" in c.params["text"]]
        bob = [c for c in to(sent, BOB) if "New in the shop" in c.params["text"]]
        assert "<b>9.50$</b> · 1 new" in alice[-1].params["text"]
        assert "<b>8.55$</b> ⭐ · 1 new" in bob[-1].params["text"]
        assert calls

    run(tmp_path, scenario)


def test_no_stock_message_for_a_disabled_product_or_a_closed_shop(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        five = make_label(t, "Esim/Oman/5GB", 950)
        t.db.set_label_disabled(make_label(t, "Esim"), True)
        calls = await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, "A")
        calls = await t.press(OWNER, "apd")
        assert "Customers aren't told: this product is disabled" in t.last_text(calls, OWNER_ID)
        assert to(calls, ALICE) == []

        t.db.set_label_disabled(make_label(t, "Esim"), False)
        t.db.set_shop_open(False)
        calls = await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, "B")
        calls = await t.press(OWNER, "apd")
        assert "Customers aren't told: the shop is closed" in t.last_text(calls, OWNER_ID)
        assert to(calls, ALICE) == [] and not t.db.has_pending_stock()

    run(tmp_path, scenario)


def test_stock_noted_together_goes_out_as_one_message_without_sold_out_items(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        five = make_label(t, "Esim/Oman/5GB", 950)
        ten = make_label(t, "Esim/Oman/10GB", 1500)
        gone = make_label(t, "Netflix/1 Month", 1200)
        add_stock(t, five, "a", "b")
        add_stock(t, ten, "c")
        t.db.add_pending_stock(five, 1, OWNER_ID)
        t.db.add_pending_stock(ten, 1, OWNER_ID)
        t.db.add_pending_stock(five, 1, ADMIN["id"])
        t.db.add_pending_stock(gone, 3, OWNER_ID)  # its stock was sold or removed since
        mark = len(t.tg.calls)
        out = await broadcast_module.send_new_stock(t.app)
        got = to(t.tg.calls[mark:], ALICE)
        assert out is not None and out.sent == 1 and len(got) == 1
        text = got[0].params["text"]
        assert "5GB · <b>9.50$</b> · 2 new" in text and "10GB · <b>15.00$</b> · 1 new" in text
        assert "Netflix" not in text
        assert not t.db.has_pending_stock()
        assert await broadcast_module.send_new_stock(t.app) is None  # nothing left to send

    run(tmp_path, scenario)


def test_stock_noted_before_a_restart_is_still_announced(tmp_path):
    async def first(t: ShopTest) -> None:
        register(t, ALICE)
        five = make_label(t, "Esim/Oman/5GB", 950)
        add_stock(t, five, "a")
        t.db.add_pending_stock(five, 1, OWNER_ID)  # the bot stopped before sending it

    async def after_restart(t: ShopTest) -> None:
        assert t.db.has_pending_stock()
        mark = len(t.tg.calls)
        broadcast_module.resume_new_stock(t.app)
        await t.settle()
        got = to(t.tg.calls[mark:], ALICE)
        assert len(got) == 1 and "5GB · <b>9.50$</b> · 1 new" in got[0].params["text"]
        assert not t.db.has_pending_stock()

    run(tmp_path, first)
    run(tmp_path, after_restart)


def test_the_stock_message_buttons_open_the_products(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        ids = [make_label(t, f"P{i}", 100 + i) for i in range(8)]
        for label_id in ids:
            add_stock(t, label_id, "x")
            t.db.add_pending_stock(label_id, 1, OWNER_ID)
        mark = len(t.tg.calls)
        await broadcast_module.send_new_stock(t.app)
        got = to(t.tg.calls[mark:], ALICE)[0]
        datas = [b.get("callback_data") for b in got.buttons]
        assert datas[:6] == [f"sh:{i}:0" for i in ids[:6]]  # 6 product buttons at most
        assert "sh:0:0" in datas and "menu" in datas
        assert all(f"P{i} " in got.params["text"] for i in range(8))
        calls = await t.press(ALICE, datas[0])
        assert "🛒 <b>P0</b>" in t.last_text(calls)
        assert names(calls)

    run(tmp_path, scenario)
