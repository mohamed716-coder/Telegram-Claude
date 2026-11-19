"""📣 /announce (owner) and the "🆕 New in the shop" message after stock is added.

Both go to everyone who has started the bot, except the owner and banned users; the
stock message also leaves out the products each user added themselves. A broadcast is
saved while it goes out and continues after a restart.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from telegram.error import NetworkError, TimedOut

from harness import OWNER, OWNER_ID, Call, ShopTest, run, tg_user
from shopbot.handlers import broadcast as broadcast_module
from test_shop import (
    ADMIN,
    ADMIN2,
    ALICE,
    BOB,
    add_stock,
    inject_failure,
    make_admin,
    make_label,
    register,
    ui_add_products,
)

CAROL = tg_user(1003, "Carol", "carol")  # banned
DAVE = tg_user(1004, "Dave", "dave")  # never started the bot
NEW = "New in the shop"


def jparam(call: Any, key: str) -> Any:
    value = call.params.get(key)
    return json.loads(value) if isinstance(value, str) else value


def to(calls: list, user: dict, method: str = "sendMessage") -> list:
    return [c for c in calls if c.method == method and c.chat_id == user["id"] and not c.get("failed")]


def texts_to(calls: list, user: dict) -> list[str]:
    return [c.params.get("text") or "" for c in to(calls, user)]


def people(t: ShopTest) -> None:
    """Alice, Bob and an admin use the bot; Carol is banned; Dave never started it."""
    for user in (ALICE, BOB, CAROL):
        register(t, user)
    make_admin(t, ADMIN)
    t.db.set_banned(CAROL["id"], True)
    t.db.ensure_user(DAVE["id"])


def hook_sends(t: ShopTest, hook: Callable[[str, dict], Optional[tuple[int, dict]]]) -> None:
    """Call ``hook(method, params)`` before each Bot API call; it may answer instead."""
    original = type(t.tg).do_request.__get__(t.tg)

    async def do_request(url, method, request_data=None, *args, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        answer = hook(api_method, params)
        if answer is not None:
            status, body = answer
            t.tg.calls.append(Call(method=api_method, params=params, files={}, failed=True))
            return status, json.dumps(body).encode()
        return await original(url, method, request_data, *args, **kwargs)

    t.tg.do_request = do_request  # type: ignore[method-assign]


def chat_of(params: dict) -> int:
    return int(params.get("chat_id") or 0)


# ==================================================================== announce
def test_announce_with_text_goes_to_everyone_except_owner_banned_and_unstarted(tmp_path):
    async def scenario(t: ShopTest) -> None:
        people(t)
        bold = {"type": "bold", "offset": 13, "length": 8}  # "Big sale" (🎉 is 2 UTF-16 units)
        calls = await t.send(OWNER, "/announce 🎉 Big sale today!", entities=[
            {"type": "bot_command", "offset": 0, "length": 9}, bold])
        preview = to(calls, OWNER)
        assert preview[0].params["text"] == "🎉 Big sale today!"
        assert jparam(preview[0], "entities") == [{"type": "bold", "offset": 3, "length": 8}]
        assert "Send the message above to 3 user(s)?" in preview[-1].params["text"]

        calls = await t.press(OWNER, t.button(calls, "Send to 3"))
        for user in (ALICE, BOB, ADMIN):
            assert texts_to(calls, user) == ["🎉 Big sale today!"], user
        for user in (CAROL, DAVE):
            assert to(calls, user) == []
        assert "Announcement sent</b> to all 3 user(s)" in texts_to(calls, OWNER)[-1]
        assert t.db.get_broadcast_job() is None  # finished: nothing left to resume

    run(tmp_path, scenario)


def test_formatting_that_starts_on_the_command_is_kept_for_the_rest(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        text = "/announce Answer: B"
        calls = await t.send(OWNER, text, entities=[
            {"type": "bot_command", "offset": 0, "length": 9},
            {"type": "spoiler", "offset": 0, "length": len(text)}])  # the whole message
        assert jparam(to(calls, OWNER)[0], "entities") == [{"type": "spoiler", "offset": 0, "length": 9}]

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
        assert to(calls, OWNER, "sendPhoto")[0].params["photo"] == "promo"
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


def test_cancelled_replaced_repeated_and_expired_buttons_send_nothing_more(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/announce first")
        first, first_cancel = t.button(calls, "Send to 1"), t.button(calls, "Cancel")
        calls = await t.press(OWNER, first_cancel)
        assert "Announcement cancelled. Nothing was sent." in t.last_text(calls)
        calls = await t.press(OWNER, first)
        assert "has expired" in t.alert(calls) and to(calls, ALICE) == []

        calls = await t.send(OWNER, "/announce second")
        older, older_cancel = t.button(calls, "Send to 1"), t.button(calls, "Cancel")
        calls = await t.send(OWNER, "/announce third")
        newer = t.button(calls, "Send to 1")
        calls = await t.press(OWNER, older_cancel)  # an old Cancel can't drop the newer preview
        assert "has expired" in t.alert(calls)
        calls = await t.press(OWNER, older)
        assert "has expired" in t.alert(calls) and to(calls, ALICE) == []
        calls = await t.press(OWNER, newer)
        assert texts_to(calls, ALICE) == ["third"]
        calls = await t.press(OWNER, newer)  # pressed again
        assert "has expired" in t.alert(calls) and to(calls, ALICE) == []

        calls = await t.send(OWNER, "/announce fourth")
        send = t.button(calls, "Send to 1")
        t.app.user_data[OWNER_ID].clear()  # what a restart does to a preview
        calls = await t.press(OWNER, send)
        assert "the bot restarted" in t.alert(calls) and to(calls, ALICE) == []

    run(tmp_path, scenario)


def test_editing_the_previewed_message_says_the_edit_is_not_used(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        await t.send(OWNER, "/announce")
        calls = await t.send(OWNER, "Sale: 50% off evrything")
        source = t._incoming_ids  # the id the harness gave that message is the last one used
        message_id = next(source) - 1
        edited = {"message_id": message_id, "date": 0, "edit_date": 1,
                  "chat": {"id": OWNER_ID, "type": "private"}, "from": OWNER,
                  "text": "Sale: 50% off everything"}
        calls = await t._process({"edited_message": edited})
        assert "That edit isn't used" in t.last_text(calls)
        assert calls

    run(tmp_path, scenario)


def test_only_the_owner_can_announce_or_stop(tmp_path):
    async def scenario(t: ShopTest) -> None:
        people(t)
        for user in (ADMIN, ALICE):
            calls = await t.send(user, "/announce hello")
            assert "Only the owner can use this command" in t.last_text(calls)
        calls = await t.send(OWNER, "/announce real")
        calls = await t.press(ADMIN, t.button(calls, "Send to"))
        assert "Only the owner can send announcements" in t.alert(calls)
        calls = await t.press(ADMIN, "ans:whatever")
        assert "Only the owner can stop announcements" in t.alert(calls)
        assert to(calls, ALICE) == []

    run(tmp_path, scenario)


def test_stop_sending_ends_the_announcement_where_it_is(tmp_path):
    async def scenario(t: ShopTest) -> None:
        people(t)
        calls = await t.send(OWNER, "/announce oops, a typo")

        def press_stop_after_alice(method: str, params: dict) -> None:
            if method == "sendMessage" and chat_of(params) == ALICE["id"]:
                job = broadcast_module.running_job(t.app)
                t.app.bot_data["broadcast_stop"] = job["id"]  # as ⏹ Stop sending does
            return None

        hook_sends(t, press_stop_after_alice)
        calls = await t.press(OWNER, t.button(calls, "Send to 3"))
        assert texts_to(calls, ALICE) == ["oops, a typo"]
        assert to(calls, BOB) == [] and to(calls, ADMIN) == []
        assert "Announcement stopped</b>: 1 of 3 user(s) got it" in texts_to(calls, OWNER)[-1]
        assert t.db.get_broadcast_job() is None

    run(tmp_path, scenario)


def test_someone_banned_while_it_goes_out_does_not_get_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        people(t)
        calls = await t.send(OWNER, "/announce hello")

        def ban_bob(method: str, params: dict) -> None:
            if method == "sendMessage" and chat_of(params) == ALICE["id"]:
                t.db.set_banned(BOB["id"], True)
            return None

        hook_sends(t, ban_bob)
        calls = await t.press(OWNER, t.button(calls, "Send to 3"))
        assert to(calls, BOB) == []
        assert "1 were banned in the meantime" in texts_to(calls, OWNER)[-1]

    run(tmp_path, scenario)


def test_users_who_blocked_the_bot_are_counted_not_retried(tmp_path):
    async def scenario(t: ShopTest) -> None:
        people(t)
        inject_failure(t, lambda method, p: method == "sendMessage" and chat_of(p) == BOB["id"],
                       status=403, description="Forbidden: bot was blocked by the user")
        calls = await t.send(OWNER, "/announce hi all")
        calls = await t.press(OWNER, t.button(calls, "Send to 3"))
        summary = texts_to(calls, OWNER)[-1]
        assert "sent</b> to 2 of 3 user(s)" in summary
        assert "1 blocked the bot or deleted their account" in summary
        assert len([c for c in calls if c.chat_id == BOB["id"]]) == 1  # tried once

    run(tmp_path, scenario)


def test_a_flood_wait_or_an_outage_pauses_instead_of_skipping_people(tmp_path, monkeypatch):
    monkeypatch.setattr(broadcast_module, "NETWORK_BACKOFF", 0.01)

    async def scenario(t: ShopTest) -> None:
        people(t)
        hits = {"flood": 0}

        def flood_then_ok(method: str, params: dict) -> Optional[tuple[int, dict]]:
            if method == "sendMessage" and chat_of(params) == BOB["id"] and hits["flood"] == 0:
                hits["flood"] += 1
                return 429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                             "parameters": {"retry_after": 1}}
            return None

        hook_sends(t, flood_then_ok)
        inject_failure(t, lambda method, p: method == "sendMessage" and chat_of(p) == ADMIN["id"]
                       and not hits.setdefault("net", 0) and not hits.update(net=1), error=NetworkError)
        calls = await t.send(OWNER, "/announce hello")
        calls = await t.press(OWNER, t.button(calls, "Send to 3"))
        for user in (ALICE, BOB, ADMIN):
            assert texts_to(calls, user) == ["hello"], user
        assert "Announcement sent</b> to all 3 user(s)" in texts_to(calls, OWNER)[-1]

    run(tmp_path, scenario)


def test_a_timed_out_message_counts_as_sent_and_is_never_sent_twice(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        inject_failure(t, lambda method, p: method == "sendMessage" and chat_of(p) == ALICE["id"],
                       error=TimedOut)
        calls = await t.send(OWNER, "/announce hi")
        calls = await t.press(OWNER, t.button(calls, "Send to 1"))
        assert len([c for c in calls if c.chat_id == ALICE["id"]]) == 1
        assert "to all 1 user(s)" in texts_to(calls, OWNER)[-1]

    run(tmp_path, scenario)


def test_send_is_refused_while_another_broadcast_is_going_out(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/announce hi")
        t.app.bot_data["broadcast_running"] = {"id": "x", "kind": "stock", "total": 50, "sent": 10,
                                               "unreachable": 0, "failed": 0, "skipped": 0}
        calls = await t.press(OWNER, t.button(calls, "Send to 1"))
        assert "A “New in the shop” message is still going out (10 of 50)" in t.alert(calls)
        assert to(calls, ALICE) == []

    run(tmp_path, scenario)


def test_an_announcement_a_restart_interrupted_goes_on_from_where_it_stopped(tmp_path):
    async def first(t: ShopTest) -> None:
        people(t)
        job = broadcast_module._new_job(
            "announce", 3, content={"kind": "text", "text": "hello", "entities": []},
            owner_chat=OWNER_ID, status_msg=None)
        job["after"], job["sent"] = ALICE["id"], 1  # Alice got it before the restart
        t.db.save_broadcast_job(job)

    async def after_restart(t: ShopTest) -> None:
        mark = len(t.tg.calls)
        broadcast_module.resume_broadcasts(t.app)
        await t.settle()
        calls = t.tg.calls[mark:]
        assert to(calls, ALICE) == []
        assert texts_to(calls, BOB) == ["hello"] and texts_to(calls, ADMIN) == ["hello"]
        owner = texts_to(calls, OWNER)
        assert "restarted while sending your announcement" in owner[0]
        assert "Announcement sent</b> to all 3 user(s)" in owner[-1]
        assert t.db.get_broadcast_job() is None

    run(tmp_path, first)
    run(tmp_path, after_restart)


def test_announce_with_nobody_to_send_to(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/announce hello?")
        assert "Nobody would get it yet" in t.last_text(calls)
        assert not t.has_button(calls, "Send to")

    run(tmp_path, scenario)


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
            assert "Esim › Oman › 5GB · <b>9.50$</b> · 2 new" in got[0].params["text"]
            assert f"sh:{five}:0" in [b.get("callback_data") for b in got[0].buttons]
        for user in (OWNER, CAROL, DAVE, ADMIN):
            assert [x for x in texts_to(calls, user) if NEW in x] == [], user

        calls = await t.press(ALICE, f"sh:{five}:0")  # the button in the message
        assert "In stock: <b>2</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_each_adder_hears_about_the_other_stock_only(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        make_admin(t, ADMIN)
        make_admin(t, ADMIN2)
        five, ten = make_label(t, "Esim/5GB", 950), make_label(t, "Esim/10GB", 1500)
        add_stock(t, five, "a", owner_id=ADMIN["id"])
        add_stock(t, ten, "b", owner_id=ADMIN2["id"])
        t.db.add_pending_stock(five, 1, ADMIN["id"])
        t.db.add_pending_stock(ten, 1, ADMIN2["id"])
        mark = len(t.tg.calls)
        job = await broadcast_module.send_new_stock(t.app)
        calls = t.tg.calls[mark:]
        assert job is not None and job["sent"] == 3
        alice = texts_to(calls, ALICE)[0]
        assert "5GB" in alice and "10GB" in alice
        adam, zara = texts_to(calls, ADMIN)[0], texts_to(calls, ADMIN2)[0]
        assert "10GB" in adam and "5GB" not in adam
        assert "5GB" in zara and "10GB" not in zara

    run(tmp_path, scenario)


def test_sellers_see_their_own_price_in_the_stock_message(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        register(t, BOB)
        t.db.set_seller(BOB["id"], True)
        make_label(t, "Esim/Oman/5GB", 950)
        await ui_add_products(t, OWNER, ["Esim", "Oman", "5GB"], [{"text": "X"}])
        alice = [x for x in texts_to(t.tg.calls, ALICE) if NEW in x]
        bob = [x for x in texts_to(t.tg.calls, BOB) if NEW in x]
        assert "<b>9.50$</b> · 1 new" in alice[-1]
        assert "<b>8.55$</b> ⭐ · 1 new" in bob[-1]

    run(tmp_path, scenario)


def test_no_stock_message_for_a_disabled_product_or_a_closed_shop(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        five = make_label(t, "Esim/Oman/5GB", 950)
        t.db.set_label_disabled(make_label(t, "Esim"), True)
        await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, "A")
        calls = await t.press(OWNER, "apd")
        assert "Customers aren't told: this product is disabled" in t.last_text(calls, OWNER_ID)
        assert to(calls, ALICE) == []

        t.db.set_label_disabled(make_label(t, "Esim"), False)
        t.db.set_shop_open(False)
        await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, "B")
        calls = await t.press(OWNER, "apd")
        assert "Customers aren't told: the shop is closed" in t.last_text(calls, OWNER_ID)
        assert to(calls, ALICE) == [] and not t.db.has_pending_stock()

    run(tmp_path, scenario)


def test_stock_noted_together_goes_out_as_one_message_without_sold_out_items(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        five, ten = make_label(t, "Esim/Oman/5GB", 950), make_label(t, "Esim/Oman/10GB", 1500)
        gone = make_label(t, "Netflix/1 Month", 1200)
        add_stock(t, five, "a", "b")
        add_stock(t, ten, "c")
        t.db.add_pending_stock(five, 1, OWNER_ID)
        t.db.add_pending_stock(ten, 1, OWNER_ID)
        t.db.add_pending_stock(five, 1, ADMIN["id"])
        t.db.add_pending_stock(gone, 3, OWNER_ID)  # its stock was sold or removed since
        mark = len(t.tg.calls)
        job = await broadcast_module.send_new_stock(t.app)
        got = texts_to(t.tg.calls[mark:], ALICE)
        assert job is not None and job["sent"] == 1 and len(got) == 1
        assert "5GB · <b>9.50$</b> · 2 new" in got[0] and "10GB · <b>15.00$</b> · 1 new" in got[0]
        assert "Netflix" not in got[0]
        assert not t.db.has_pending_stock()
        assert await broadcast_module.send_new_stock(t.app) is None  # nothing left

    run(tmp_path, scenario)


def test_stock_added_while_a_stock_message_goes_out_gets_its_own_message(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        register(t, BOB)
        five, ten = make_label(t, "Esim/5GB", 950), make_label(t, "Esim/10GB", 1500)
        add_stock(t, five, "a")
        add_stock(t, ten, "b")
        noted = {"done": False}

        def add_more_while_sending(method: str, params: dict) -> None:
            if method == "sendMessage" and NEW in (params.get("text") or "") and not noted["done"]:
                noted["done"] = True
                t.db.add_pending_stock(ten, 1, OWNER_ID)  # an admin presses Done meanwhile
                broadcast_module.schedule_new_stock(t.app)
            return None

        hook_sends(t, add_more_while_sending)
        t.db.add_pending_stock(five, 1, OWNER_ID)
        broadcast_module.schedule_new_stock(t.app)
        await t.settle()
        alice = [x for x in texts_to(t.tg.calls, ALICE) if NEW in x]
        assert len(alice) == 2 and "5GB" in alice[0] and "10GB" in alice[1]
        assert not t.db.has_pending_stock()

    run(tmp_path, scenario)


def test_stock_noted_before_a_restart_is_still_announced(tmp_path):
    async def first(t: ShopTest) -> None:
        register(t, ALICE)
        five = make_label(t, "Esim/Oman/5GB", 950)
        add_stock(t, five, "a")
        t.db.add_pending_stock(five, 1, OWNER_ID)  # the bot stopped before sending it

    async def after_restart(t: ShopTest) -> None:
        mark = len(t.tg.calls)
        broadcast_module.resume_broadcasts(t.app)
        await t.settle()
        got = texts_to(t.tg.calls[mark:], ALICE)
        assert len(got) == 1 and "5GB · <b>9.50$</b> · 1 new" in got[0]
        assert not t.db.has_pending_stock()

    run(tmp_path, first)
    run(tmp_path, after_restart)


def test_an_older_pending_record_is_still_read(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "a")
        t.db.set_setting("pending_stock_alert", json.dumps({"labels": {str(five): 2}, "adders": [OWNER_ID]}))
        mark = len(t.tg.calls)
        await broadcast_module.send_new_stock(t.app)
        assert "5GB · <b>9.50$</b> · 2 new" in texts_to(t.tg.calls[mark:], ALICE)[0]

    run(tmp_path, scenario)


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

    run(tmp_path, scenario)
