"""🚫 Disable / ✅ Enable a label (owner): customers don't see a disabled label or
anything inside it, can't reach it with old or crafted buttons, and can't buy there.
Nothing is deleted, and enabling it shows everything again (except labels inside that
were disabled on their own)."""

from __future__ import annotations

import sqlite3

from harness import OWNER, ShopTest, run
from test_shop import (
    ADMIN,
    ALICE,
    add_stock,
    balance,
    buy_buttons,
    confirm_screen,
    make_admin,
    make_label,
    names,
    orders_of,
    set_balance,
)

OWNER_ONLY = "Only the owner can manage labels."
UNAVAILABLE = "isn't available"


async def disable(t: ShopTest, label_id: int) -> list:
    calls = await t.press(OWNER, f"lb:{label_id}:0")
    return await t.press(OWNER, t.button(calls, "🚫 Disable"))


async def enable(t: ShopTest, label_id: int) -> list:
    calls = await t.press(OWNER, f"lb:{label_id}:0")
    return await t.press(OWNER, t.button(calls, "✅ Enable"))


def test_owner_disables_a_category_and_customers_see_nothing_inside_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/Oman/5GB", 950)
        add_stock(t, five, "A", "B")
        add_stock(t, make_label(t, "Netflix/1 Month", 1200), "N")
        esim, oman = make_label(t, "Esim"), make_label(t, "Esim/Oman")
        set_balance(t, ALICE, 10_000)

        calls = await disable(t, esim)
        text = t.last_text(calls)
        assert "Disabled <b>Esim</b> and the 2 label(s) inside it" in text, text
        assert "<b>Disabled</b>: customers don't see it or anything inside it" in text
        assert t.has_button(calls, "✅ Enable") and not t.has_button(calls, "🚫 Disable")
        assert t.db.get_label(esim)["disabled"] == 1
        assert t.db.get_label(oman)["disabled"] == 0  # hidden through Esim; own setting kept
        assert t.db.count_items(five) == 2  # nothing deleted

        # Customers don't see it, and old or crafted buttons don't get them in.
        calls = await t.press(ALICE, "sh:0:0")
        assert "📂 Netflix" in names(calls)
        assert not any("Esim" in n for n in names(calls)), names(calls)
        for data in (f"sh:{esim}:0", f"sh:{oman}:0", f"sh:{five}:0", f"sb:{five}:1"):
            calls = await t.press(ALICE, data)
            assert UNAVAILABLE in t.last_text(calls), (data, t.last_text(calls))
            assert "5GB" not in t.last_text(calls)
            assert buy_buttons(calls) == []
        calls = await t.press(ALICE, f"sq:{five}")
        assert UNAVAILABLE in t.alert(calls)
        assert balance(t, ALICE) == 10_000 and orders_of(t, ALICE) == []

        # The owner still sees it, marked, but can't buy there either.
        calls = await t.press(OWNER, "sh:0:0")
        assert "🚫 📂 Esim" in names(calls)
        calls = await t.press(OWNER, f"sh:{oman}:0")
        assert "“Esim” is disabled" in t.last_text(calls)
        calls = await t.press(OWNER, f"sh:{five}:0")
        assert "Price: <b>9.50$</b>" in t.last_text(calls)
        assert "can't be bought until it is enabled again" in t.last_text(calls)
        assert buy_buttons(calls) == []

    run(tmp_path, scenario)


def test_a_purchase_confirmed_after_the_product_was_disabled_buys_nothing(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/Oman/5GB", 950)
        add_stock(t, five, "A")
        oman = make_label(t, "Esim/Oman")
        set_balance(t, ALICE, 10_000)

        calls = await confirm_screen(t, ALICE, five, 1)
        await disable(t, oman)
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        assert "isn't available right now" in t.alert(calls)
        assert UNAVAILABLE in t.last_text(calls) and buy_buttons(calls) == []
        assert balance(t, ALICE) == 10_000 and orders_of(t, ALICE) == []
        assert t.db.count_items(five) == 1

    run(tmp_path, scenario)


def test_a_typed_quantity_after_the_product_was_disabled_buys_nothing(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/Oman/5GB", 950)
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 10_000)

        calls = await t.press(ALICE, f"sq:{five}")
        assert "How many" in t.last_text(calls)
        await disable(t, five)
        calls = await t.send(ALICE, "2")
        assert UNAVAILABLE in t.last_text(calls)
        assert buy_buttons(calls) == []
        assert balance(t, ALICE) == 10_000 and t.db.count_items(five) == 2

    run(tmp_path, scenario)


def test_enable_shows_everything_again_except_labels_disabled_on_their_own(tmp_path):
    async def scenario(t: ShopTest) -> None:
        add_stock(t, make_label(t, "Esim/Oman/5GB", 950), "A")
        add_stock(t, make_label(t, "Esim/UAE/1GB", 300), "B")
        esim, uae = make_label(t, "Esim"), make_label(t, "Esim/UAE")

        await disable(t, uae)
        await disable(t, esim)
        calls = await t.press(OWNER, f"lb:{esim}:0")
        assert "🚫 📂 UAE" in names(calls) and "📂 Oman" in names(calls)
        assert "🚫 = disabled" in t.last_text(calls)

        # Enabling the inside label alone changes nothing for customers yet.
        calls = await enable(t, uae)
        assert "but customers still don't see it: “Esim” is disabled" in t.last_text(calls)
        await disable(t, uae)

        calls = await enable(t, esim)
        text = t.last_text(calls)
        assert "Enabled <b>Esim</b>: customers see it again" in text, text
        assert "1 label(s) inside it stay disabled" in text
        calls = await t.press(ALICE, f"sh:{esim}:0")
        assert "📂 Oman" in names(calls)
        assert not any("UAE" in n for n in names(calls)), names(calls)

        await enable(t, uae)
        calls = await t.press(ALICE, f"sh:{esim}:0")
        assert {"📂 Oman", "📂 UAE"} <= set(names(calls))

    run(tmp_path, scenario)


def test_a_category_whose_products_are_all_disabled_is_hidden_from_customers(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/Oman/5GB", 950)
        add_stock(t, five, "A")
        await disable(t, five)
        calls = await t.press(ALICE, "sh:0:0")
        assert "The shop is empty right now" in t.last_text(calls)
        assert not any("Esim" in n for n in names(calls))

        ten = make_label(t, "Esim/Oman/10GB", 1500)
        calls = await t.press(ALICE, f"sh:{make_label(t, 'Esim/Oman')}:0")
        assert "🛒 10GB · 15.00$ · sold out" in names(calls)
        assert not any("5GB" in n for n in names(calls))
        assert ten

    run(tmp_path, scenario)


def test_only_the_owner_can_disable_or_enable_labels(tmp_path):
    async def scenario(t: ShopTest) -> None:
        esim = make_label(t, "Esim/Oman/5GB", 950) and make_label(t, "Esim")
        make_admin(t, ADMIN)
        for user in (ALICE, ADMIN):
            calls = await t.press(user, f"lx:{esim}:1")
            assert OWNER_ONLY in t.alert(calls)
        assert t.db.get_label(esim)["disabled"] == 0
        t.db.set_label_disabled(esim, True)
        calls = await t.press(ADMIN, f"lx:{esim}:0")
        assert OWNER_ONLY in t.alert(calls)
        assert t.db.get_label(esim)["disabled"] == 1

    run(tmp_path, scenario)


def test_old_and_crafted_disable_buttons_are_harmless(tmp_path):
    async def scenario(t: ShopTest) -> None:
        esim = make_label(t, "Esim/Oman/5GB", 950) and make_label(t, "Esim")
        calls = await t.press(OWNER, f"lb:{esim}:0")
        old_disable = t.button(calls, "🚫 Disable")
        await t.press(OWNER, old_disable)
        await t.press(OWNER, old_disable)  # pressed again: still disabled, not switched back
        assert t.db.get_label(esim)["disabled"] == 1
        await t.press(OWNER, f"lx:{esim}:0")
        await t.press(OWNER, f"lx:{esim}:0")
        assert t.db.get_label(esim)["disabled"] == 0

        calls = await t.press(OWNER, f"lx:{esim}:2")  # neither 0 nor 1
        assert t.db.get_label(esim)["disabled"] == 0
        assert calls  # answered (as an old button), nothing changed

        t.db.delete_label(esim)
        calls = await t.press(OWNER, old_disable)
        assert "no longer exists" in t.alert(calls)

    run(tmp_path, scenario)


def test_admins_can_still_stock_a_disabled_product_and_see_it_marked(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_label(t, "Esim/Oman/5GB", 950)
        oman = make_label(t, "Esim/Oman")
        make_admin(t, ADMIN)
        await disable(t, oman)

        calls = await t.press(ADMIN, "ap:0:0")
        calls = await t.press(ADMIN, t.button(calls, "Esim"))
        assert "🚫 📂 Oman" in names(calls)
        calls = await t.press(ADMIN, t.button(calls, "Oman"))
        calls = await t.press(ADMIN, t.button(calls, "5GB"))
        assert "This product is disabled, so customers don't see it" in t.last_text(calls)
        await t.send(ADMIN, "code-123")
        calls = await t.press(ADMIN, t.button(calls, "Done"))
        assert "Saved 1 product(s)" in t.last_text(calls)

        calls = await t.press(ADMIN, "mp:0:0")
        calls = await t.press(ADMIN, t.button(calls, "Esim"))
        assert "🚫 📂 Oman · 1" in names(calls)

        calls = await t.press(ALICE, "sh:0:0")
        assert "The shop is empty right now" in t.last_text(calls)

    run(tmp_path, scenario)


def test_labels_added_inside_a_disabled_label_stay_hidden_and_it_can_still_be_deleted(tmp_path):
    async def scenario(t: ShopTest) -> None:
        esim = make_label(t, "Esim")
        await disable(t, esim)
        calls = await t.press(OWNER, f"ln:{esim}")
        calls = await t.send(OWNER, "Oman")
        oman = make_label(t, "Esim/Oman")
        assert "“Esim” is disabled" in t.last_text(await t.press(OWNER, f"lb:{oman}:0"))
        add_stock(t, make_label(t, "Esim/Oman/5GB", 950), "A")
        calls = await t.press(ALICE, "sh:0:0")
        assert "The shop is empty right now" in t.last_text(calls)

        t.db.conn.execute("UPDATE items SET status = 'removed'")
        calls = await t.press(OWNER, f"ld:{esim}")
        calls = await t.press(OWNER, t.button(calls, "Yes, delete"))
        assert t.db.get_label(esim) is None

    run(tmp_path, scenario)


def test_a_database_from_the_previous_version_gets_the_new_setting(tmp_path):
    # The shop's database on a server made before this feature: labels has no column
    # "disabled". Opening it must add it, with everything enabled.
    path = tmp_path / "shop.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE labels (id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER"
        " REFERENCES labels(id), name TEXT NOT NULL, price INTEGER, deleted INTEGER NOT NULL"
        " DEFAULT 0, created_at TEXT NOT NULL);"
        "INSERT INTO labels (parent_id, name, price, created_at) VALUES (NULL, 'Netflix', 1200, 'x');"
    )
    old.close()

    async def scenario(t: ShopTest) -> None:
        netflix = make_label(t, "Netflix")
        assert t.db.get_label(netflix)["disabled"] == 0
        add_stock(t, netflix, "N")
        calls = await t.press(ALICE, "sh:0:0")
        assert any("Netflix" in n for n in names(calls))
        await disable(t, netflix)
        calls = await t.press(ALICE, "sh:0:0")
        assert "The shop is empty right now" in t.last_text(calls)

    run(tmp_path, scenario)
