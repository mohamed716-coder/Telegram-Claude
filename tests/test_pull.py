"""📤 /pull: the owner and admins take products out of stock, picked at random, as many
as they choose; they're sent to them. Admins pull their own; the owner anyone's (/pull ID)."""

from __future__ import annotations

import json
import random

from telegram.error import TimedOut

from harness import OWNER, OWNER_ID, Picks, ShopTest, run
from test_shop import (ADMIN, ADMIN2, ALICE, add_stock, all_text, datas, inject_failure, make_admin,
                       make_label, register)


def statuses(t: ShopTest, label_id: int) -> dict[str, str]:
    """Product text → status, for every product of a label."""
    rows = t.db.conn.execute("SELECT * FROM items WHERE label_id = ? ORDER BY id", (label_id,)).fetchall()
    return {json.loads(row["content"])["text"]: row["status"] for row in rows}


def delivered(t: ShopTest, calls: list, user: dict, prefix: str) -> list[str]:
    return [x for x in t.texts(calls, user["id"]) if x.startswith(prefix)]


async def pull(t: ShopTest, user: dict, seller: int, label_id: int, count: int) -> list:
    """Product → quantity → confirm, through the buttons."""
    await t.press(user, f"pl:{seller}:{label_id}")
    calls = await t.press(user, f"plq:{seller}:{label_id}:{count}")
    return await t.press(user, t.button(calls, f"Pull {count}"))


def test_an_admin_pulls_as_many_of_their_products_as_they_choose_at_random(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        five = make_label(t, "Esim/Oman/5GB", 950)
        ten = make_label(t, "Esim/Oman/10GB", 1500)
        add_stock(t, five, *[f"A{i}" for i in range(10)], owner_id=ADMIN["id"])
        add_stock(t, five, "O0", "O1")  # the owner's: not the admin's to pull
        add_stock(t, ten, "AT0", owner_id=ADMIN["id"])
        add_stock(t, ten, "AT1", owner_id=ADMIN["id"])
        t.db.close_item(t.db.list_items(ten, ADMIN["id"])[0]["id"], "removed", ADMIN["id"])
        picks = t.db.rng = Picks()

        calls = await t.send(ADMIN, "/pull")
        text = t.last_text(calls)
        assert "Pull products" in text and "From your stock." in text
        assert "/pull <code>ID</code>" not in text  # only the owner pulls from others
        names = [b["text"] for b in t.all_buttons(calls)]
        assert "Esim › Oman › 5GB · 10" in names and "Esim › Oman › 10GB · 1" in names
        assert f"pl:{ADMIN['id']}:{five}" in datas(calls)

        calls = await t.press(ADMIN, f"pl:{ADMIN['id']}:{five}")
        assert "<b>10</b> in stock" in t.last_text(calls) and "picked at random" in t.last_text(calls)
        assert [b["text"] for b in t.all_buttons(calls) if b["callback_data"].startswith("plq:")] == [
            "1", "5", "All · 10"]
        calls = await t.press(ADMIN, f"plq:{ADMIN['id']}:{five}:5")
        assert "Pull 5 product(s)?" in t.last_text(calls)
        calls = await t.press(ADMIN, t.button(calls, "Pull 5"))

        # The draw was among exactly the admin's 10 products of 5GB in stock.
        mine = [row["id"] for row in t.db.conn.execute(
            "SELECT id FROM items WHERE label_id = ? AND owner_id = ? ORDER BY id", (five, ADMIN["id"]))]
        assert picks.samples == [(mine, 5)]
        assert "Pulled 5 product(s)" in all_text(calls, ADMIN["id"])
        assert delivered(t, calls, ADMIN, "A") == ["A0", "A1", "A2", "A3", "A4"]
        assert "✅ Sent 5 pulled product(s)." in all_text(calls, ADMIN["id"])
        rows = t.db.conn.execute("SELECT * FROM items WHERE status = 'pulled'").fetchall()
        assert len(rows) == 5 and {(r["owner_id"], r["buyer_id"]) for r in rows} == {(ADMIN["id"], ADMIN["id"])}
        assert t.db.count_items(five, ADMIN["id"]) == 5 and t.db.count_items(five, OWNER_ID) == 2
        assert "New sale" not in all_text(calls) and "pulled" not in all_text(calls, OWNER_ID)

    run(tmp_path, scenario)


def test_the_products_pulled_are_random(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, *[f"A{i}" for i in range(10)], owner_id=ADMIN["id"])
        t.db.rng = random.SystemRandom()
        seen = set()
        for _ in range(200):
            (item,) = t.db.pull_random(five, ADMIN["id"], 1, ADMIN["id"])
            seen.add(item["id"])
            assert t.db.restore_pulled([item["id"]]) == [item["id"]]
        assert len(seen) == 10  # every one of them came up (missing one: about 1 in 10^8)
        assert t.db.count_items(five) == 10

    run(tmp_path, scenario)


def test_the_owner_pulls_an_admins_products_by_id_and_the_admin_is_told(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "A0", "A1", "A2", owner_id=ADMIN["id"])
        add_stock(t, five, "O0")
        calls = await t.send(OWNER, "/pull")
        assert "/pull <code>ID</code>" in t.last_text(calls)
        assert [b["text"] for b in t.all_buttons(calls) if b["callback_data"].startswith("pl:")] == ["Esim › 5GB · 1"]
        calls = await t.send(OWNER, f"/pull {ADMIN['id']}")
        assert "From the stock of <b>Adam</b>" in t.last_text(calls)
        calls = await t.press(OWNER, f"pl:{ADMIN['id']}:{five}")
        assert "All · 3" in [b["text"] for b in t.all_buttons(calls)]
        calls = await pull(t, OWNER, ADMIN["id"], five, 3)
        assert sorted(delivered(t, calls, OWNER, "A")) == ["A0", "A1", "A2"]
        assert "The owner pulled <b>3</b> of your products out of the shop" in all_text(calls, ADMIN["id"])
        assert statuses(t, five) == {"A0": "pulled", "A1": "pulled", "A2": "pulled", "O0": "available"}
        assert {r["buyer_id"] for r in t.db.conn.execute("SELECT * FROM items WHERE status = 'pulled'")} == {OWNER_ID}
        calls = await t.send(OWNER, f"/pull {ADMIN['id']}")
        assert "They have no products in stock." in t.last_text(calls)

    run(tmp_path, scenario)


def test_only_the_owner_pulls_someone_elses_products(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        make_admin(t, ADMIN2)
        register(t, ALICE)
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "O0")
        add_stock(t, five, "Z0", owner_id=ADMIN2["id"])
        calls = await t.send(ADMIN, f"/pull {OWNER_ID}")
        assert "Only the owner can pull another seller's products" in t.last_text(calls)
        calls = await t.send(ADMIN, "/pull")
        assert "There are no products in stock." in t.last_text(calls)
        for data in (f"plp:{ADMIN2['id']}:0", f"pl:{ADMIN2['id']}:{five}", f"plq:{OWNER_ID}:{five}:1",
                     f"plo:{OWNER_ID}:{five}", f"ply:{ADMIN2['id']}:{five}:1:abcd1234"):
            calls = await t.press(ADMIN, data)
            assert "You can only pull your own products." in t.alert(calls), data
        calls = await t.send(ALICE, "/pull")
        assert "Only the owner and admins can use this command." in t.last_text(calls)
        calls = await t.press(ALICE, f"pl:{ALICE['id']}:{five}")
        assert "for the owner and admins" in t.alert(calls)
        calls = await t.send(ADMIN, "/pull abc")
        assert "isn't a user ID" in t.last_text(calls)
        assert statuses(t, five) == {"O0": "available", "Z0": "available"}

    run(tmp_path, scenario)


def test_other_amount_limits_and_a_confirmation_that_works_once(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, *[f"O{i}" for i in range(6)])
        await t.press(OWNER, f"pl:{OWNER_ID}:{five}")
        calls = await t.press(OWNER, f"plo:{OWNER_ID}:{five}")
        assert "Send a number from 1 to 6" in t.last_text(calls)
        for wrong in ("0", "7", "two"):
            calls = await t.send(OWNER, wrong)
            assert "Please send a number from 1 to 6" in t.last_text(calls), wrong
        calls = await t.send(OWNER, "2")
        confirm = t.button(calls, "Pull 2")
        calls = await t.press(OWNER, confirm)
        assert len(delivered(t, calls, OWNER, "O")) == 2
        calls = await t.press(OWNER, confirm)  # a second press
        assert "This confirmation has expired" in t.alert(calls)
        assert t.db.count_items(five) == 4
        # A pull asked for more than there is (or a stale number) shows what's there.
        calls = await t.press(OWNER, f"plq:{OWNER_ID}:{five}:9")
        assert "There are 4 in stock." in t.last_text(calls)
        # Stock taken meanwhile: only what's left is pulled.
        calls = await t.press(OWNER, f"plq:{OWNER_ID}:{five}:4")
        confirm = t.button(calls, "Pull 4")
        for item in t.db.list_items(five)[:3]:
            t.db.close_item(item["id"], "removed", OWNER_ID)
        calls = await t.press(OWNER, confirm)
        assert "Pulled 1 product(s)</b> (only 1 were left)" in all_text(calls)
        calls = await t.press(OWNER, f"pl:{OWNER_ID}:{five}")
        assert "There are no products left here." in t.last_text(calls)

    run(tmp_path, scenario)


def test_a_big_stock_is_pulled_up_to_200_at_a_time(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, *[f"O{i}" for i in range(250)])
        calls = await t.press(OWNER, f"pl:{OWNER_ID}:{five}")
        assert "Up to 200 at a time." in t.last_text(calls)
        assert [b["text"] for b in t.all_buttons(calls) if b["callback_data"].startswith("plq:")] == [
            "1", "5", "10", "50", "200"]
        calls = await t.press(OWNER, f"plq:{OWNER_ID}:{five}:201")
        assert "Pull 201" not in str(t.all_buttons(calls))

    run(tmp_path, scenario)


def test_products_that_could_not_be_sent_go_back_in_stock(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "O0", "O1", "O2")
        inject_failure(t, lambda method, params: method == "sendMessage" and params.get("text") == "O1")
        calls = await pull(t, OWNER, OWNER_ID, five, 3)
        assert delivered(t, [c for c in calls if not c.get("failed")], OWNER, "O") == ["O0", "O2"]
        code = t.db.conn.execute("SELECT code FROM items WHERE status = 'available'").fetchone()["code"]
        end = all_text(calls, OWNER_ID)
        assert f"1 product(s) couldn't be sent, so they're back in stock: <code>{code}</code>" in end
        assert statuses(t, five) == {"O0": "pulled", "O1": "available", "O2": "pulled"}
        back = t.db.conn.execute("SELECT * FROM items WHERE status = 'available'").fetchone()
        assert back["buyer_id"] is None and back["closed_at"] is None

    run(tmp_path, scenario)


def test_an_admin_removed_meanwhile_can_no_longer_pull(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "A0", owner_id=ADMIN["id"])
        await t.press(ADMIN, f"pl:{ADMIN['id']}:{five}")
        calls = await t.press(ADMIN, f"plq:{ADMIN['id']}:{five}:1")
        confirm = t.button(calls, "Pull 1")
        await t.press(ADMIN, f"plo:{ADMIN['id']}:{five}")  # and starts typing a number
        await t.send(OWNER, f"/removeadmin {ADMIN['id']}")
        calls = await t.send(ADMIN, "1")
        assert not t.has_button(calls, "Pull 1") and "state" not in t.app.user_data[ADMIN["id"]]
        calls = await t.press(ADMIN, confirm)
        assert "for the owner and admins" in t.alert(calls)
        assert statuses(t, five) == {"A0": "available"}

    run(tmp_path, scenario)


def test_a_product_telegram_did_not_answer_for_stays_pulled_and_can_be_sent_again(tmp_path):
    """It may have arrived: putting it back on sale could sell it twice."""
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "O0", "O1", "O2")
        failing = {"on": True}
        inject_failure(t, lambda method, params: failing["on"] and params.get("text") == "O1", error=TimedOut)
        calls = await pull(t, OWNER, OWNER_ID, five, 3)
        assert len([c for c in calls if c.get("failed")]) == 2  # sent, then once more
        end = all_text(calls, OWNER_ID)
        assert "1 product(s) may not have arrived" in end and "They stay pulled" in end
        assert "back in stock" not in end
        assert statuses(t, five) == {"O0": "pulled", "O1": "pulled", "O2": "pulled"}
        o1 = t.db.conn.execute("SELECT * FROM items WHERE status = 'pulled' ORDER BY id").fetchall()[1]
        assert t.button(calls, f"📤 {o1['code']}") == f"plr:{o1['id']}"
        calls = await t.press(OWNER, f"plr:{o1['id']}")  # still no answer from Telegram
        assert "Couldn't send it right now" in t.alert(calls)
        failing["on"] = False
        calls = await t.press(OWNER, f"plr:{o1['id']}")
        assert "O1" in t.texts(calls, OWNER_ID) and f"Sent {o1['code']} again" in t.answers(calls)[-1]["text"]
        assert statuses(t, five)["O1"] == "pulled"

    run(tmp_path, scenario)


def test_a_product_that_arrives_on_the_second_try_needs_nothing_more(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "O0", "O1")
        tries = []

        def first_send_of_o1(method: str, params: dict) -> bool:
            if params.get("text") != "O1":
                return False
            tries.append(1)
            return len(tries) == 1

        inject_failure(t, first_send_of_o1, error=TimedOut)
        calls = await pull(t, OWNER, OWNER_ID, five, 2)
        assert "✅ Sent 2 pulled product(s)." in all_text(calls, OWNER_ID)
        assert delivered(t, [c for c in calls if not c.get("failed")], OWNER, "O") == ["O0", "O1"]

    run(tmp_path, scenario)


def test_products_whose_product_is_gone_stay_pulled_and_can_be_sent_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "O0", "O1")

        def delete_then_fail(method: str, params: dict) -> bool:
            if params.get("text") != "O1":
                return False
            t.db.delete_label(five)  # it shows no stock while they're being sent
            return True

        inject_failure(t, delete_then_fail)
        calls = await pull(t, OWNER, OWNER_ID, five, 2)
        end = all_text(calls, OWNER_ID)
        assert "1 product(s) may not have arrived (or their product was deleted meanwhile)" in end
        assert statuses(t, five) == {"O0": "pulled", "O1": "pulled"}
        o1 = t.db.conn.execute("SELECT * FROM items WHERE status = 'pulled' ORDER BY id").fetchall()[1]
        t.tg.do_request = type(t.tg).do_request.__get__(t.tg)  # Telegram works again
        calls = await t.press(OWNER, f"plr:{o1['id']}")
        assert "O1" in t.texts(calls, OWNER_ID)

    run(tmp_path, scenario)


def test_only_who_pulled_a_product_gets_it_sent_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "A0", owner_id=ADMIN["id"])
        add_stock(t, five, "O0")
        await pull(t, ADMIN, ADMIN["id"], five, 1)
        (pulled,) = t.db.conn.execute("SELECT * FROM items WHERE status = 'pulled'").fetchall()
        available = t.db.conn.execute("SELECT * FROM items WHERE status = 'available'").fetchone()
        for user, item_id in ((OWNER, pulled["id"]), (ALICE, pulled["id"]), (ADMIN, available["id"])):
            calls = await t.press(user, f"plr:{item_id}")
            assert "isn't one you pulled" in t.alert(calls)
            assert "A0" not in t.texts(calls) and "O0" not in t.texts(calls)

    run(tmp_path, scenario)


def test_the_admin_hears_how_many_were_really_taken_out(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, "A0", "A1", "A2", owner_id=ADMIN["id"])
        inject_failure(t, lambda method, params: params.get("text") == "A1")
        calls = await pull(t, OWNER, ADMIN["id"], five, 3)
        note = all_text(calls, ADMIN["id"])
        assert "The owner pulled <b>2</b> of your products out of the shop" in note
        assert statuses(t, five)["A1"] == "available"

    run(tmp_path, scenario)


def test_long_paths_keep_the_products_own_name_on_the_buttons(tmp_path):
    async def scenario(t: ShopTest) -> None:
        for size in ("5GB", "10GB"):
            label = make_label(t, f"Esim/Middle East/United Arab Emirates/{size}", 100)
            add_stock(t, label, f"P-{size}")
        calls = await t.send(OWNER, "/pull")
        names = [b["text"] for b in t.all_buttons(calls) if b["callback_data"].startswith("pl:")]
        assert sorted(names) == ["… › United Arab Emirates › 10GB · 1", "… › United Arab Emirates › 5GB · 1"]

    run(tmp_path, scenario)
