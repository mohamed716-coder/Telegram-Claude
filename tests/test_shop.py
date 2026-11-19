"""🛍 Shop browsing, buying, delivery and 🧾 My Orders, end to end through the real bot.

Fixtures (labels, prices, stock, balances) are mostly created straight in the database
so each test stays focused on one thing; the behaviour under test is driven with real
button presses and messages, and checked both in the database and in what users see.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

import pytest
from telegram.error import TimedOut

from harness import OWNER, OWNER_ID, Call, ShopTest, run, tg_user
from shopbot.db import ShopError

ALICE = tg_user(1001, "Alice", "alice")
BOB = tg_user(1002, "Bob", "bob")
ADMIN = tg_user(2001, "Adam", "adam")
ADMIN2 = tg_user(2002, "Zara", "zara")

CLOSED = "shop is closed right now"
EXPIRED = "This confirmation has expired"


# =================================================================== helpers
def make_label(t: ShopTest, path: str, price: Optional[int] = None) -> int:
    """Create every label of 'A/B/C' that doesn't exist yet; return the last one's id."""
    parent: Optional[int] = None
    for name in path.split("/"):
        found = [row for row in t.db.children(parent) if row["name"] == name]
        parent = found[0]["id"] if found else t.db.add_labels(parent, [name])[0][0]["id"]
    if price is not None:
        t.db.set_label_price(parent, price)
    assert parent is not None
    return parent


def text_item(text: str, entities: Optional[list] = None) -> dict:
    return {"kind": "text", "text": text, "entities": entities or []}


def add_stock(t: ShopTest, label_id: int, *texts: str, owner_id: int = OWNER_ID) -> list[str]:
    """Add text products; returns their product IDs (codes)."""
    return t.db.add_items(label_id, owner_id, [text_item(x) for x in texts])


def register(t: ShopTest, user: dict) -> None:
    t.db.touch_user(user["id"], user["first_name"], user.get("last_name"), user.get("username"))


def set_balance(t: ShopTest, user: dict, cents: int) -> None:
    register(t, user)
    t.db.set_balance(user["id"], cents)


def make_admin(t: ShopTest, user: dict) -> None:
    register(t, user)
    t.db.set_admin(user["id"], True)


def balance(t: ShopTest, user: dict) -> int:
    return t.db.get_user(user["id"])["balance"]


def earnings(t: ShopTest, user_id: int) -> int:
    return t.db.get_user(user_id)["earnings"]


def orders_of(t: ShopTest, user: dict) -> list:
    return t.db.user_orders(user["id"], 0, 1000)


def purchase_ledger(t: ShopTest, user: dict) -> list:
    return t.db.conn.execute(
        "SELECT * FROM ledger WHERE user_id = ? AND reason = 'purchase' ORDER BY id", (user["id"],)
    ).fetchall()


def names(calls: list[Call]) -> list[str]:
    return [b.get("text", "") for b in ShopTest.all_buttons(calls)]


def datas(calls: list[Call]) -> list[str]:
    return [b.get("callback_data", "") for b in ShopTest.all_buttons(calls)]


def exact(calls: list[Call], text: str) -> str:
    """callback_data of the last button whose text is exactly ``text``."""
    found = [b for b in ShopTest.all_buttons(calls) if b.get("text") == text]
    assert found, f"no button {text!r}; buttons: {names(calls)}"
    return found[-1]["callback_data"]


def qty_buttons(calls: list[Call]) -> list[str]:
    return [b["text"] for b in ShopTest.all_buttons(calls) if b.get("callback_data", "").startswith("sb:")]


def buy_buttons(calls: list[Call]) -> list[str]:
    """Buttons that would lead to buying (quantity, other amount, confirm)."""
    return [d for d in datas(calls) if d.startswith(("sb:", "sq:", "sc:"))]


def all_text(calls: list[Call], chat_id: Optional[int] = None) -> str:
    return "\n".join(ShopTest.texts(calls, chat_id))


def sent_to(calls: list[Call], chat_id: int, method: str) -> list[Call]:
    return [c for c in calls if c.method == method and c.chat_id == chat_id and not c.get("failed")]


def inject_failure(
    t: ShopTest,
    predicate: Callable[[str, dict], bool],
    *,
    error: Optional[type] = None,
    status: int = 400,
    description: str = "Bad Request: injected failure",
) -> None:
    """Make only the Bot API calls matching ``predicate(method, params)`` fail.

    With ``error`` (e.g. TimedOut) the call raises that network error, otherwise
    Telegram answers with HTTP ``status``. Failed calls are recorded with failed=True.
    """
    original = type(t.tg).do_request.__get__(t.tg)

    async def do_request(url, method, request_data=None, read_timeout=None, write_timeout=None,
                         connect_timeout=None, pool_timeout=None):
        api_method = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        if predicate(api_method, params):
            t.tg.calls.append(Call(method=api_method, params=params, files={}, failed=True))
            if error is not None:
                raise error()
            body = {"ok": False, "error_code": status, "description": description}
            return status, json.dumps(body).encode()
        return await original(url, method, request_data, read_timeout, write_timeout,
                              connect_timeout, pool_timeout)

    t.tg.do_request = do_request  # type: ignore[method-assign]


async def confirm_screen(t: ShopTest, user: dict, label_id: int, qty: int) -> list[Call]:
    """Press the quantity button (sb:<label>:<qty>) and expect the confirmation."""
    calls = await t.press(user, f"sb:{label_id}:{qty}")
    assert "Confirm your purchase" in t.last_text(calls), t.last_text(calls)
    return calls


async def buy(t: ShopTest, user: dict, label_id: int, qty: int) -> list[Call]:
    calls = await confirm_screen(t, user, label_id, qty)
    return await t.press(user, t.button(calls, "Confirm"))


async def ui_add_products(t: ShopTest, user: dict, path: list[str], messages: list[dict]) -> None:
    """Add Product through the real buttons: open the labels, send messages, press Done."""
    calls = await t.send(user, "/start")
    calls = await t.press(user, t.button(calls, "Add Product"))
    for name in path:
        calls = await t.press(user, t.button(calls, name))
    assert "ONE MESSAGE PER PRODUCT" in t.last_text(calls)
    for fields in messages:
        fields = dict(fields)
        await t.send(user, fields.pop("text", None), **fields)
    calls = await t.press(user, t.button(calls, "Done"))
    assert f"Saved {len(messages)} product(s)" in t.last_text(calls, user["id"]), t.last_text(calls, user["id"])


# ================================================================== browsing
def test_customer_browses_label_tree_down_to_the_product_page(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/Oman/Omantel/5GB", 950)
        make_label(t, "Esim/Oman/Omantel/10GB", 1500)
        add_stock(t, make_label(t, "Netflix/1 Month", 1200), "N1")
        add_stock(t, five, "A", "B", "C")
        set_balance(t, ALICE, 2000)

        calls = await t.send(ALICE, "/start")
        calls = await t.press(ALICE, t.button(calls, "Shop"))
        assert "Choose a category or product" in t.last_text(calls)
        assert {"📂 Esim", "📂 Netflix"} <= set(names(calls))
        calls = await t.press(ALICE, t.button(calls, "Esim"))
        assert "📂 Esim" in t.last_text(calls)
        calls = await t.press(ALICE, t.button(calls, "Oman"))
        calls = await t.press(ALICE, t.button(calls, "Omantel"))
        assert "Esim › Oman › Omantel" in t.last_text(calls)
        assert "🛒 5GB · 9.50$" in names(calls)
        assert "🛒 10GB · 15.00$ · sold out" in names(calls)

        calls = await t.press(ALICE, t.button(calls, "5GB"))
        text = t.last_text(calls)
        assert "Esim › Oman › Omantel › 5GB" in text
        assert "Price: <b>9.50$</b>" in text
        assert "In stock: <b>3</b>" in text
        assert "Your balance: <b>20.00$</b>" in text
        assert "How many do you want to buy?" in text

        # ⬅️ Back walks up the tree, and from the top level back to the menu.
        calls = await t.press(ALICE, exact(calls, "⬅️ Back"))
        assert "Esim › Oman › Omantel" in t.last_text(calls)
        assert exact(await t.press(ALICE, "sh:0:0"), "⬅️ Back") == "menu"
        assert orders_of(t, ALICE) == []

    run(tmp_path, scenario)


def test_products_without_price_hidden_from_customers_and_admins_but_shown_to_owner(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/Oman/5GB", 950)
        make_label(t, "Esim/Oman/10GB")  # no price yet
        oman = make_label(t, "Esim/Oman")
        add_stock(t, five, "A")
        make_admin(t, ADMIN)

        for user in (ALICE, ADMIN):
            calls = await t.press(user, f"sh:{oman}:0")
            assert "🛒 5GB · 9.50$" in names(calls)
            assert not any("10GB" in n for n in names(calls)), names(calls)

        calls = await t.press(OWNER, f"sh:{oman}:0")
        assert "🛒 5GB · 9.50$" in names(calls)
        assert "10GB · ⚠️ no price" in names(calls)
        calls = await t.press(OWNER, t.button(calls, "10GB"))
        assert "no price yet" in t.last_text(calls)
        assert buy_buttons(calls) == []

    run(tmp_path, scenario)


def test_branches_without_any_priced_product_are_hidden_from_customers(tmp_path):
    async def scenario(t: ShopTest) -> None:
        add_stock(t, make_label(t, "Esim/Oman/5GB", 950), "A")
        make_label(t, "Esim/UAE/Du/1GB")  # whole UAE branch unpriced
        make_label(t, "Gift Cards/Steam/20$")  # whole top-level branch unpriced
        make_label(t, "Coming soon")  # unpriced top-level product
        make_label(t, "Netflix/1 Month", 1200)  # priced, no stock: still shown (sold out)
        gone = make_label(t, "Old/Gone", 500)  # its only priced product gets deleted
        t.db.delete_label(gone)

        calls = await t.press(ALICE, "sh:0:0")
        assert {"📂 Esim", "📂 Netflix"} <= set(names(calls))
        assert not any("Gift Cards" in n or "Coming soon" in n for n in names(calls)), names(calls)
        assert not any("Old" in n for n in names(calls)), names(calls)
        calls = await t.press(ALICE, t.button(calls, "Esim"))
        assert "📂 Oman" in names(calls)
        assert not any("UAE" in n for n in names(calls))

        calls = await t.press(OWNER, "sh:0:0")
        assert {"📂 Esim", "📂 Netflix", "📂 Gift Cards", "Coming soon · ⚠️ no price",
                "Old · ⚠️ no price"} <= set(names(calls))
        calls = await t.press(OWNER, t.button(calls, "Esim"))
        assert "📂 UAE" in names(calls)

    run(tmp_path, scenario)


def test_shop_with_only_unpriced_labels_looks_empty_to_customers(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_label(t, "Esim/Oman/5GB")
        calls = await t.press(ALICE, "sh:0:0")
        assert "The shop is empty right now" in t.last_text(calls)
        assert not any("Esim" in n for n in names(calls))
        calls = await t.press(OWNER, "sh:0:0")
        assert "📂 Esim" in names(calls)

    run(tmp_path, scenario)


def test_sold_out_product_is_shown_but_cannot_be_bought(tmp_path):
    async def scenario(t: ShopTest) -> None:
        month = make_label(t, "Netflix/1 Month", 1200)
        netflix = make_label(t, "Netflix")
        set_balance(t, ALICE, 5000)

        calls = await t.press(ALICE, f"sh:{netflix}:0")
        assert "🛒 1 Month · 12.00$ · sold out" in names(calls)
        calls = await t.press(ALICE, t.button(calls, "1 Month"))
        text = t.last_text(calls)
        assert "In stock: <b>0</b>" in text and "Sold out" in text
        assert buy_buttons(calls) == []

        # Crafted quantity / other-amount buttons are refused as well.
        calls = await t.press(ALICE, f"sb:{month}:1")
        assert "Sold out" in t.last_text(calls)
        assert buy_buttons(calls) == []
        calls = await t.press(ALICE, f"sq:{month}")
        assert "sold out" in t.alert(calls).lower()
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000

    run(tmp_path, scenario)


def test_buying_the_last_item_turns_the_product_sold_out(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        esim = make_label(t, "Esim")
        add_stock(t, five, "ONLY-ONE")
        set_balance(t, ALICE, 5000)
        await buy(t, ALICE, five, 1)
        calls = await t.press(ALICE, f"sh:{esim}:0")
        assert "🛒 5GB · 9.50$ · sold out" in names(calls)
        calls = await t.press(ALICE, f"sh:{five}:0")
        assert "In stock: <b>0</b>" in t.last_text(calls) and "Sold out" in t.last_text(calls)
        assert buy_buttons(calls) == []

    run(tmp_path, scenario)


def test_shop_pages_hold_20_entries_and_hidden_labels_do_not_count(tmp_path):
    async def scenario(t: ShopTest) -> None:
        for i in range(23):
            make_label(t, f"P{i:02d}", 100)
        for i in range(5):
            make_label(t, f"Hidden{i}")  # no price

        calls = await t.press(ALICE, "sh:0:0")
        products = [n for n in names(calls) if n.startswith("🛒")]
        assert len(products) == 20
        assert "1/2" in names(calls)
        calls = await t.press(ALICE, exact(calls, "▶️"))
        products = [n for n in names(calls) if n.startswith("🛒")]
        assert products == ["🛒 P20 · 1.00$ · sold out", "🛒 P21 · 1.00$ · sold out", "🛒 P22 · 1.00$ · sold out"]
        assert "2/2" in names(calls) and "◀️" in names(calls)
        assert not any("Hidden" in n for n in names(calls))
        # A page number past the end shows the last page.
        calls = await t.press(ALICE, "sh:0:9")
        assert "2/2" in names(calls)

    run(tmp_path, scenario)


def test_seller_sees_10_percent_off_rounded_half_up(tmp_path):
    async def scenario(t: ShopTest) -> None:
        premium = make_label(t, "Codes/Premium", 999)
        mini = make_label(t, "Codes/Mini", 5)
        small = make_label(t, "Codes/Small", 15)
        codes = make_label(t, "Codes")
        for label_id in (premium, mini, small):
            add_stock(t, label_id, "X", "Y")
        set_balance(t, ALICE, 5000)
        set_balance(t, BOB, 5000)

        calls = await t.send(OWNER, f"/seller {ALICE['id']}")
        assert "is now a <b>seller</b>" in t.last_text(calls, OWNER_ID)

        calls = await t.press(ALICE, f"sh:{codes}:0")
        assert "🛒 Premium · 8.99$" in names(calls)  # 9.99 × 0.9 = 8.991 → 8.99
        assert "🛒 Mini · 0.05$" in names(calls)  # 0.045 → 0.05 (half up)
        assert "🛒 Small · 0.14$" in names(calls)  # 0.135 → 0.14 (half up)
        calls = await t.press(ALICE, f"sh:{premium}:0")
        assert "<s>9.99$</s> <b>8.99$</b> ⭐ seller price" in t.last_text(calls)
        calls = await confirm_screen(t, ALICE, premium, 2)
        assert "Price each: 8.99$" in t.last_text(calls) and "Total: <b>17.98$</b>" in t.last_text(calls)
        calls = await confirm_screen(t, ALICE, mini, 1)
        assert "Price each: 0.05$" in t.last_text(calls)

        # A normal customer sees and pays the full price.
        calls = await t.press(BOB, f"sh:{codes}:0")
        assert "🛒 Premium · 9.99$" in names(calls)
        calls = await t.press(BOB, f"sh:{premium}:0")
        assert "Price: <b>9.99$</b>" in t.last_text(calls) and "<s>" not in t.last_text(calls)

    run(tmp_path, scenario)


def test_seller_pays_the_discounted_price_and_the_adder_earns_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        premium = make_label(t, "Codes/Premium", 999)
        make_admin(t, ADMIN)
        add_stock(t, premium, "P1", "P2", "P3", owner_id=ADMIN["id"])
        await t.send(OWNER, f"/seller {ALICE['id']}")
        set_balance(t, ALICE, 3000)

        calls = await confirm_screen(t, ALICE, premium, 3)
        assert "Total: <b>26.97$</b>" in t.last_text(calls)
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        assert "Paid: <b>26.97$</b>" in all_text(calls, ALICE["id"])
        assert balance(t, ALICE) == 3000 - 2697
        (order,) = orders_of(t, ALICE)
        assert order["unit_price"] == 899 and order["total"] == 2697
        assert [i["sold_price"] for i in t.db.order_items(order["id"])] == [899, 899, 899]
        assert earnings(t, ADMIN["id"]) == 2697
        assert "+26.97$" in all_text(calls, ADMIN["id"])

    run(tmp_path, scenario)


def test_user_promoted_automatically_after_1000_deposits_gets_seller_price(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 1000)
        add_stock(t, five, "A")
        await t.send(ALICE, "/start")
        await t.send(OWNER, f"/addbal {ALICE['id']} 1000")
        assert t.db.get_user(ALICE["id"])["is_seller"] == 1
        calls = await t.press(ALICE, f"sh:{five}:0")
        assert "<s>10.00$</s> <b>9.00$</b>" in t.last_text(calls)
        await buy(t, ALICE, five, 1)
        assert balance(t, ALICE) == 100000 - 900

    run(tmp_path, scenario)


# ================================================================== quantity
def test_quick_quantity_buttons_never_exceed_the_stock(tmp_path):
    async def scenario(t: ShopTest) -> None:
        one = make_label(t, "S/One", 100)
        four = make_label(t, "S/Four", 100)
        many = make_label(t, "S/Many", 100)
        add_stock(t, one, "x")
        add_stock(t, four, *"abcd")
        add_stock(t, many, *[f"m{i}" for i in range(12)])
        set_balance(t, ALICE, 10_000)

        calls = await t.press(ALICE, f"sh:{one}:0")
        assert qty_buttons(calls) == ["1"]
        assert not t.has_button(calls, "Other amount")
        calls = await t.press(ALICE, f"sh:{four}:0")
        assert qty_buttons(calls) == ["1", "2", "3"]
        assert t.has_button(calls, "Other amount")
        calls = await t.press(ALICE, f"sh:{many}:0")
        assert qty_buttons(calls) == ["1", "2", "3", "5", "10"]

        calls = await t.press(ALICE, exact(calls, "10"))
        assert "Quantity: <b>10</b>" in t.last_text(calls)
        # Crafted quantities above the stock, or 0, are refused.
        for bad in (5, 0):
            calls = await t.press(ALICE, f"sb:{four}:{bad}")
            assert "Only 4 left in stock" in t.last_text(calls)
            assert not t.has_button(calls, "Confirm")
        assert orders_of(t, ALICE) == []

    run(tmp_path, scenario)


def test_other_amount_rejects_bad_numbers_then_buys_the_typed_quantity(tmp_path):
    async def scenario(t: ShopTest) -> None:
        seven = make_label(t, "S/Seven", 100)
        add_stock(t, seven, *[f"s{i}" for i in range(7)])
        set_balance(t, ALICE, 10_000)

        calls = await t.press(ALICE, f"sh:{seven}:0")
        calls = await t.press(ALICE, t.button(calls, "Other amount"))
        assert "How many <b>Seven</b>" in t.last_text(calls)
        assert "from 1 to 7" in t.last_text(calls)
        for bad in ("abc", "2.5", "-3", "0", "1e3"):
            calls = await t.send(ALICE, bad)
            assert "whole number" in t.last_text(calls), bad
            assert not t.has_button(calls, "Confirm")
        calls = await t.send_photo(ALICE, "random-photo")
        assert "text message" in t.last_text(calls)
        calls = await t.send(ALICE, "8")
        assert "at most 7" in t.last_text(calls)
        assert not t.has_button(calls, "Confirm")
        assert orders_of(t, ALICE) == []

        calls = await t.send(ALICE, "7")
        text = t.last_text(calls)
        assert "Quantity: <b>7</b>" in text and "Total: <b>7.00$</b>" in text
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        assert "Purchase successful" in all_text(calls, ALICE["id"])
        assert t.db.count_items(seven) == 0
        assert balance(t, ALICE) == 10_000 - 700
        assert orders_of(t, ALICE)[0]["quantity"] == 7

    run(tmp_path, scenario)


def test_other_amount_is_capped_at_200(tmp_path):
    async def scenario(t: ShopTest) -> None:
        bulk = make_label(t, "Bulk/Codes", 1)
        add_stock(t, bulk, *[f"c{i}" for i in range(205)])
        set_balance(t, ALICE, 100_000)

        calls = await t.press(ALICE, f"sq:{bulk}")
        assert "from 1 to 200" in t.last_text(calls)
        calls = await t.send(ALICE, "201")
        assert "at most 200" in t.last_text(calls)
        calls = await t.send(ALICE, "200")
        assert "Quantity: <b>200</b>" in t.last_text(calls)
        # A crafted quantity button above 200 is refused too.
        calls = await t.press(ALICE, f"sb:{bulk}:201")
        assert not t.has_button(calls, "Confirm")
        assert orders_of(t, ALICE) == []

    run(tmp_path, scenario)


def test_other_amount_rechecks_the_stock_when_the_number_arrives(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 100)
        add_stock(t, five, *"abcde")
        set_balance(t, ALICE, 5000)
        set_balance(t, BOB, 5000)
        calls = await t.press(ALICE, f"sq:{five}")
        assert "from 1 to 5" in t.last_text(calls)
        await buy(t, BOB, five, 4)  # meanwhile Bob buys 4 of the 5
        calls = await t.send(ALICE, "3")
        assert "at most 1" in t.last_text(calls)
        assert not t.has_button(calls, "Confirm")
        await buy(t, BOB, five, 1)  # and then the last one
        calls = await t.send(ALICE, "1")
        assert "Sold out" in t.last_text(calls)
        assert not t.has_button(calls, "Confirm")
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000

    run(tmp_path, scenario)


def test_cancel_on_other_amount_goes_back_and_ends_the_question(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 5000)

        prompt = await t.press(ALICE, f"sq:{five}")
        calls = await t.press(ALICE, t.button(prompt, "Cancel"))
        assert "In stock: <b>2</b>" in t.last_text(calls)
        calls = await t.send(ALICE, "2")
        assert "Send /start" in t.last_text(calls)
        assert not t.has_button(calls, "Confirm")
        assert orders_of(t, ALICE) == []

    run(tmp_path, scenario)


# ============================================================ balance checks
def test_not_enough_balance_offers_add_balance_and_buys_nothing(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 500)

        calls = await t.press(ALICE, f"sh:{five}:0")
        calls = await t.press(ALICE, exact(calls, "1"))
        text = t.last_text(calls)
        assert "Not enough balance" in text
        assert "Total: <b>9.50$</b>" in text and "Your balance: 5.00$" in text
        assert "You need <b>4.50$</b> more" in text
        assert not t.has_button(calls, "Confirm")
        add_balance = t.button(calls, "Add Balance")
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 500 and t.db.count_items(five) == 2

        calls = await t.press(ALICE, add_balance)
        assert "Add Balance" in t.last_text(calls) and "Current balance: <b>5.00$</b>" in t.last_text(calls)
        assert t.has_button(calls, "10$") and t.has_button(calls, "Custom amount")
        assert orders_of(t, ALICE) == []

    run(tmp_path, scenario)


def test_exact_balance_is_enough(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 1900)
        calls = await buy(t, ALICE, five, 2)
        assert "Purchase successful" in all_text(calls, ALICE["id"])
        assert balance(t, ALICE) == 0
        calls = await t.press(ALICE, f"sh:{five}:0")
        assert "Your balance: <b>0.00$</b>" in t.last_text(calls)

    run(tmp_path, scenario)


# ===================================================================== nonce
def test_pressing_confirm_twice_buys_only_once(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B", "C")
        set_balance(t, ALICE, 5000)

        calls = await confirm_screen(t, ALICE, five, 1)
        confirm = t.button(calls, "Confirm")
        first = await t.press(ALICE, confirm)
        assert "Purchase successful" in all_text(first, ALICE["id"])
        second = await t.press(ALICE, confirm)
        assert EXPIRED in t.alert(second)
        assert t.answers(second)[-1].get("show_alert") is True
        assert not sent_to(second, ALICE["id"], "sendMessage")  # nothing delivered again

        assert len(orders_of(t, ALICE)) == 1
        assert balance(t, ALICE) == 5000 - 950
        assert t.db.count_items(five) == 2
        assert len(purchase_ledger(t, ALICE)) == 1
        assert earnings(t, OWNER_ID) == 950

    run(tmp_path, scenario)


def test_an_older_confirmation_expires_when_a_newer_one_is_opened(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B", "C")
        set_balance(t, ALICE, 5000)

        old = t.button(await confirm_screen(t, ALICE, five, 1), "Confirm")
        new = t.button(await confirm_screen(t, ALICE, five, 2), "Confirm")
        assert old != new
        calls = await t.press(ALICE, old)
        assert EXPIRED in t.alert(calls)
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000

        await t.press(ALICE, new)
        (order,) = orders_of(t, ALICE)
        assert order["quantity"] == 2 and balance(t, ALICE) == 5000 - 1900

    run(tmp_path, scenario)


def test_someone_elses_confirm_button_does_not_buy_for_anyone(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 5000)
        set_balance(t, BOB, 5000)
        alice_confirm = t.button(await confirm_screen(t, ALICE, five, 1), "Confirm")
        calls = await t.press(BOB, alice_confirm)
        assert EXPIRED in t.alert(calls)
        assert orders_of(t, ALICE) == [] and orders_of(t, BOB) == []
        assert balance(t, ALICE) == 5000 and balance(t, BOB) == 5000
        await t.press(ALICE, alice_confirm)  # still valid for Alice herself
        assert len(orders_of(t, ALICE)) == 1

    run(tmp_path, scenario)


def test_crafted_confirm_without_a_confirmation_screen_is_refused(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A")
        set_balance(t, ALICE, 5000)
        calls = await t.press(ALICE, f"sc:{five}:1:deadbeef")
        assert EXPIRED in t.alert(calls)
        calls = await t.press(ALICE, f"sc:{five}:1")  # malformed: no nonce
        assert "no longer valid" in t.alert(calls)
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000 and t.db.count_items(five) == 1

    run(tmp_path, scenario)


def test_cancel_on_the_confirmation_buys_nothing(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 5000)
        calls = await confirm_screen(t, ALICE, five, 2)
        text = t.last_text(calls)
        assert "Quantity: <b>2</b>" in text and "Price each: 9.50$" in text
        assert "Total: <b>19.00$</b>" in text and "Balance after: 31.00$" in text
        calls = await t.press(ALICE, t.button(calls, "Cancel"))
        assert "In stock: <b>2</b>" in t.last_text(calls)
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000

    run(tmp_path, scenario)


def test_crafted_purchase_of_a_category_or_unpriced_product_is_refused(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        soon = make_label(t, "Esim/Soon")  # no price
        esim = make_label(t, "Esim")
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 5000)

        for target in (esim, soon):
            calls = await t.press(ALICE, f"sb:{target}:1")
            assert "isn't available" in t.last_text(calls)
            assert not t.has_button(calls, "Confirm")
            calls = await t.press(ALICE, f"sq:{target}")
            assert "isn't available" in t.alert(calls)
        # Even with a valid nonce taken from a real confirmation screen: a nonce only
        # works for the product and quantity it was shown for.
        for target in (soon, esim):
            nonce = t.button(await confirm_screen(t, ALICE, five, 1), "Confirm").split(":")[-1]
            calls = await t.press(ALICE, f"sc:{target}:1:{nonce}")
            assert "This confirmation has expired" in t.alert(calls), t.alert(calls)
            calls = await t.press(ALICE, f"sc:{five}:2:{nonce}")  # same product, other quantity
            assert "This confirmation has expired" in t.alert(calls), t.alert(calls)
        # The database refuses such purchases on its own as well.
        for target, reason in ((soon, "isn't for sale"), (esim, "category")):
            with pytest.raises(ShopError, match=reason):
                t.db.purchase(ALICE["id"], target, 1, 10)
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000 and t.db.count_items(five) == 2

    run(tmp_path, scenario)


# ================================================================== purchase
def test_purchase_charges_records_order_and_ledger_and_shows_codes(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/Oman/Omantel/5GB", 950)
        codes = add_stock(t, five, "CODE-1", "CODE-2", "CODE-3")
        set_balance(t, ALICE, 2000)

        calls = await buy(t, ALICE, five, 2)
        assert "Purchase successful" in t.alert(calls)

        (order,) = orders_of(t, ALICE)
        assert order["quantity"] == 2 and order["unit_price"] == 950 and order["total"] == 1900
        assert order["product_name"] == "Esim › Oman › Omantel › 5GB"
        assert order["label_id"] == five
        assert balance(t, ALICE) == 100
        items = t.db.order_items(order["id"])
        assert [i["code"] for i in items] == codes[:2]  # oldest first
        for item in items:
            assert item["status"] == "sold" and item["buyer_id"] == ALICE["id"]
            assert item["sold_price"] == 950 and item["order_id"] == order["id"]
        assert t.db.count_items(five) == 1
        assert earnings(t, OWNER_ID) == 1900
        (row,) = purchase_ledger(t, ALICE)
        assert row["delta"] == -1900 and row["balance_after"] == 100 and row["ref"] == order["code"]

        texts = t.texts(calls, ALICE["id"])
        summary = next(x for x in texts if "Purchase successful" in x)
        assert f"Order: <code>#{order['code']}</code>" in summary
        assert f"<code>{codes[0]}</code>, <code>{codes[1]}</code>" in summary
        assert codes[2] not in summary
        assert "Paid: <b>19.00$</b>" in summary and "New balance: <b>1.00$</b>" in summary
        assert texts.index("CODE-1") < texts.index("CODE-2")
        assert "CODE-3" not in texts
        final = t.messages(calls, ALICE["id"])[-1]
        assert "Delivered 2 product(s)" in final.text
        assert {"🛍 Shop", "🧾 My Orders", "🏠 Menu"} <= {b["text"] for b in final.buttons}

    run(tmp_path, scenario)


def test_big_order_lists_50_product_ids_and_delivers_every_item_in_order(tmp_path):
    async def scenario(t: ShopTest) -> None:
        bulk = make_label(t, "Bulk/Codes", 10)
        codes = add_stock(t, bulk, *[f"BULK-{i:02d}" for i in range(60)])
        set_balance(t, ALICE, 10_000)
        await t.press(ALICE, f"sq:{bulk}")
        calls = await t.send(ALICE, "60")
        assert "Total: <b>6.00$</b>" in t.last_text(calls)
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        summary = next(x for x in t.texts(calls, ALICE["id"]) if "Purchase successful" in x)
        assert all(code in summary for code in codes[:50])
        assert not any(code in summary for code in codes[50:])
        assert "(+10)" in summary
        delivered = [x for x in t.texts(calls, ALICE["id"]) if x.startswith("BULK-")]
        assert delivered == [f"BULK-{i:02d}" for i in range(60)]
        assert "Delivered 60 product(s)" in t.last_text(calls, ALICE["id"])
        assert balance(t, ALICE) == 10_000 - 600 and t.db.count_items(bulk) == 0

    run(tmp_path, scenario)


def test_customer_tops_up_from_the_not_enough_screen_and_then_buys(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "AFTER-TOPUP")
        calls = await t.send(ALICE, "/start")
        calls = await t.press(ALICE, t.button(calls, "Shop"))
        calls = await t.press(ALICE, t.button(calls, "Esim"))
        calls = await t.press(ALICE, t.button(calls, "5GB"))
        calls = await t.press(ALICE, exact(calls, "1"))
        assert "Not enough balance" in t.last_text(calls)
        assert "In stock: <b>1</b>" in t.last_text(await t.press(ALICE, exact(calls, "⬅️ Back")))

        calls = await t.press(ALICE, t.button(calls, "Add Balance"))
        calls = await t.press(ALICE, t.button(calls, "10$"))
        calls = await t.send_photo(ALICE, "alice-receipt")
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        request = [c for c in calls if c.chat_id == OWNER_ID and c.method == "sendPhoto"]
        await t.press(OWNER, t.button(request, "Approve"), photo=True)
        assert balance(t, ALICE) == 1000

        calls = await buy(t, ALICE, five, 1)
        assert "AFTER-TOPUP" in t.texts(calls, ALICE["id"])
        assert balance(t, ALICE) == 50

    run(tmp_path, scenario)


def test_fifo_mix_of_owner_and_admin_items_credits_each_adder(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_label(t, "Esim/5GB", 1000)
        calls = await t.send(OWNER, f"/addadmin {ADMIN['id']}")
        assert "is now an <b>admin</b>" in t.last_text(calls, OWNER_ID)
        path = ["Esim", "5GB"]
        # Stock arrives owner, admin, owner, admin — through the real Add Product flow.
        await ui_add_products(t, OWNER, path, [{"text": "OWN-1"}])
        await ui_add_products(t, ADMIN, path, [{"text": "ADM-1"}])
        await ui_add_products(t, OWNER, path, [{"text": "OWN-2"}])
        await ui_add_products(t, ADMIN, path, [{"text": "ADM-2"}])
        set_balance(t, ALICE, 5000)

        five = make_label(t, "Esim/5GB")
        calls = await buy(t, ALICE, five, 3)
        texts = t.texts(calls, ALICE["id"])
        delivered = [x for x in texts if x in ("OWN-1", "ADM-1", "OWN-2", "ADM-2")]
        assert delivered == ["OWN-1", "ADM-1", "OWN-2"]

        assert balance(t, ALICE) == 5000 - 3000
        assert earnings(t, OWNER_ID) == 2000
        assert earnings(t, ADMIN["id"]) == 1000
        remaining = t.db.list_items(five)
        assert [json.loads(i["content"])["text"] for i in remaining] == ["ADM-2"]
        assert remaining[0]["owner_id"] == ADMIN["id"]

        (order,) = orders_of(t, ALICE)
        admin_note = all_text(calls, ADMIN["id"])
        assert "New sale!" in admin_note and "1 product(s)" in admin_note
        assert "+10.00$" in admin_note and f"#{order['code']}" in admin_note
        owner_note = all_text(calls, OWNER_ID)
        assert "New sale!" in owner_note and "2 product(s)" in owner_note and "+20.00$" in owner_note
        assert "New sale" not in all_text(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_two_admins_and_owner_each_get_their_own_sale_notification(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 300)
        make_admin(t, ADMIN)
        make_admin(t, ADMIN2)
        add_stock(t, five, "A1", owner_id=ADMIN["id"])
        add_stock(t, five, "Z1", "Z2", owner_id=ADMIN2["id"])
        add_stock(t, five, "O1")
        set_balance(t, ALICE, 5000)
        calls = await buy(t, ALICE, five, 3)  # A1, Z1, Z2
        assert earnings(t, ADMIN["id"]) == 300 and earnings(t, ADMIN2["id"]) == 600
        assert earnings(t, OWNER_ID) == 0
        assert "1 product(s) · <b>+3.00$</b>" in all_text(calls, ADMIN["id"])
        assert "2 product(s) · <b>+6.00$</b>" in all_text(calls, ADMIN2["id"])
        assert "New sale" not in all_text(calls, OWNER_ID)

    run(tmp_path, scenario)


def test_buying_your_own_stock_sends_no_sale_notification_to_yourself(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 500)
        make_admin(t, ADMIN)
        add_stock(t, five, "MINE", owner_id=ADMIN["id"])
        set_balance(t, ADMIN, 1000)
        calls = await buy(t, ADMIN, five, 1)
        assert "MINE" in t.texts(calls, ADMIN["id"])
        assert "New sale" not in all_text(calls, ADMIN["id"])
        assert balance(t, ADMIN) == 500 and earnings(t, ADMIN["id"]) == 500

    run(tmp_path, scenario)


def test_products_are_delivered_exactly_as_sent_with_entities_and_photo_caption(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/Oman/5GB", 950)
        text = "Your eSIM: ACT-123 — activate at example 🔥"
        entities = [
            {"type": "bold", "offset": 0, "length": 10},
            {"type": "code", "offset": text.index("ACT"), "length": 7},
            {"type": "text_link", "offset": text.index("example"), "length": 7,
             "url": "https://example.com/activate"},
            {"type": "italic", "offset": text.index("activate"), "length": 8},
        ]
        caption = "Scan this QR to install"
        caption_entities = [{"type": "underline", "offset": 0, "length": 4},
                            {"type": "spoiler", "offset": 10, "length": 2}]
        photo = [
            {"file_id": "qr-small", "file_unique_id": "qs", "width": 90, "height": 90},
            {"file_id": "qr-large", "file_unique_id": "ql", "width": 800, "height": 800},
        ]
        await ui_add_products(t, OWNER, ["Esim", "Oman", "5GB"], [
            {"text": text, "entities": entities},
            {"photo": photo, "caption": caption, "caption_entities": caption_entities,
             "has_media_spoiler": True, "show_caption_above_media": True},
        ])
        set_balance(t, ALICE, 5000)

        calls = await buy(t, ALICE, five, 2)
        (product_text,) = [c for c in sent_to(calls, ALICE["id"], "sendMessage") if c.text == text]
        assert product_text.params.get("entities") == entities
        assert "parse_mode" not in product_text.params  # raw entities, not re-parsed HTML
        (product_photo,) = sent_to(calls, ALICE["id"], "sendPhoto")
        assert product_photo.params["photo"] == "qr-large"
        assert product_photo.params["caption"] == caption
        assert product_photo.params.get("caption_entities") == caption_entities
        assert product_photo.params.get("has_spoiler") is True
        assert product_photo.params.get("show_caption_above_media") is True
        assert not [c for c in calls if c.method in ("forwardMessage", "copyMessage", "forwardMessages")]

    run(tmp_path, scenario)


def test_every_media_kind_is_delivered_with_its_file_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        pack = make_label(t, "Media/Pack", 100)
        media = [
            {"document": {"file_id": "doc-file", "file_unique_id": "u-doc", "file_name": "key.txt"},
             "caption": "Your licence"},
            {"video": {"file_id": "video-file", "file_unique_id": "u-vid", "width": 64, "height": 48,
                       "duration": 5}, "caption": "Tutorial"},
            {"animation": {"file_id": "gif-file", "file_unique_id": "u-gif", "width": 1, "height": 1,
                           "duration": 1},
             "document": {"file_id": "gif-file", "file_unique_id": "u-gif"}},
            {"audio": {"file_id": "audio-file", "file_unique_id": "u-aud", "duration": 3}},
            {"voice": {"file_id": "voice-file", "file_unique_id": "u-voi", "duration": 2}},
            {"sticker": {"file_id": "sticker-file", "file_unique_id": "u-sti", "type": "regular",
                         "width": 512, "height": 512, "is_animated": False, "is_video": False}},
            {"video_note": {"file_id": "vnote-file", "file_unique_id": "u-vn", "length": 240,
                            "duration": 4}},
        ]
        await ui_add_products(t, OWNER, ["Media", "Pack"], media)
        set_balance(t, ALICE, 5000)
        calls = await buy(t, ALICE, pack, 7)
        expected = [
            ("sendDocument", "document", "doc-file", "Your licence"),
            ("sendVideo", "video", "video-file", "Tutorial"),
            ("sendAnimation", "animation", "gif-file", None),
            ("sendAudio", "audio", "audio-file", None),
            ("sendVoice", "voice", "voice-file", None),
            ("sendSticker", "sticker", "sticker-file", None),
            ("sendVideoNote", "video_note", "vnote-file", None),
        ]
        for method, field, file_id, caption in expected:
            (call,) = sent_to(calls, ALICE["id"], method)
            assert call.params[field] == file_id
            assert call.params.get("caption") == caption
        assert "Delivered 7 product(s)" in t.last_text(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_label_names_with_html_characters_are_escaped_everywhere(tmp_path):
    async def scenario(t: ShopTest) -> None:
        vip = make_label(t, "Tom & Jerry/<VIP> Pack", 950)
        make_admin(t, ADMIN)
        add_stock(t, vip, "X1", "X2", "X3", owner_id=ADMIN["id"])
        set_balance(t, ALICE, 5000)

        seen: list[Call] = []
        seen += await t.press(ALICE, "sh:0:0")
        seen += await t.press(ALICE, t.button(seen, "Tom & Jerry"))
        seen += await t.press(ALICE, f"sh:{vip}:0")
        seen += await t.press(ALICE, f"sq:{vip}")
        seen += await t.send(ALICE, "2")
        seen += await t.press(ALICE, t.button(seen, "Confirm"))
        seen += await t.press(ALICE, "mo:0")
        seen += await t.press(ALICE, t.button(seen, "<VIP> Pack"))

        html = [c for c in t.messages(seen) if c.params.get("parse_mode") == "HTML"]
        assert html
        for call in html:
            assert "<VIP>" not in (call.text or ""), call.text
            assert "Tom & Jerry" not in (call.text or ""), call.text
        joined = "\n".join(c.text or "" for c in html)
        assert "Tom &amp; Jerry › &lt;VIP&gt; Pack" in joined
        assert "New sale!" in all_text(seen, ADMIN["id"])
        assert len(orders_of(t, ALICE)) == 1

    run(tmp_path, scenario)


# ===================================================================== races
def test_stock_removed_between_confirmation_and_confirm_is_not_charged(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 5000)

        confirm = t.button(await confirm_screen(t, ALICE, five, 2), "Confirm")
        first = t.db.list_items(five)[0]
        calls = await t.press(OWNER, f"mr:{first['id']}")
        calls = await t.press(OWNER, t.button(calls, "Yes, remove"))
        assert t.db.count_items(five) == 1

        calls = await t.press(ALICE, confirm)
        assert "only 1 left" in t.alert(calls)
        assert "only 1 left" in t.last_text(calls, ALICE["id"])
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000
        assert t.db.count_items(five) == 1 and earnings(t, OWNER_ID) == 0
        assert "B" not in t.texts(calls, ALICE["id"])

        # The removed item is skipped: buying 1 now gets the next oldest one.
        calls = await buy(t, ALICE, five, 1)
        assert "B" in t.texts(calls, ALICE["id"]) and "A" not in t.texts(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_last_item_bought_by_someone_else_before_confirm_is_not_charged(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "ONLY")
        set_balance(t, ALICE, 5000)
        set_balance(t, BOB, 5000)

        alice_confirm = t.button(await confirm_screen(t, ALICE, five, 1), "Confirm")
        calls = await buy(t, BOB, five, 1)
        assert "ONLY" in t.texts(calls, BOB["id"])
        calls = await t.press(ALICE, alice_confirm)
        assert "sold out" in t.alert(calls)
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000
        assert balance(t, BOB) == 5000 - 950

    run(tmp_path, scenario)


def test_label_deleted_between_confirmation_and_confirm_is_not_charged(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/Oman/5GB", 950)
        add_stock(t, five, "ONLY")
        set_balance(t, ALICE, 5000)
        confirm = t.button(await confirm_screen(t, ALICE, five, 1), "Confirm")

        # The owner removes the stock, then deletes the label.
        item = t.db.list_items(five)[0]
        await t.press(OWNER, f"mr:{item['id']}")
        await t.press(OWNER, f"mry:{item['id']}")
        calls = await t.press(OWNER, f"ld:{five}")
        calls = await t.press(OWNER, t.button(calls, "Yes, delete"))
        assert t.db.get_label(five) is None

        calls = await t.press(ALICE, confirm)
        assert "no longer exists" in t.alert(calls)
        assert "no longer exists" in t.last_text(calls, ALICE["id"])
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000
        assert not [x for x in t.texts(calls, ALICE["id"]) if x == "ONLY"]
        # Old buttons pointing at the deleted product don't break anything.
        for data in (f"sh:{five}:0", f"sb:{five}:1", f"sq:{five}"):
            calls = await t.press(ALICE, data)
            shown = t.alert(calls) + all_text(calls, ALICE["id"])
            assert "no longer exists" in shown or "isn't available" in shown, data
            assert not t.has_button(calls, "Confirm")
        assert orders_of(t, ALICE) == []

    run(tmp_path, scenario)


def test_balance_lowered_between_confirmation_and_confirm_shows_not_enough(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A")
        set_balance(t, ALICE, 2000)
        confirm = t.button(await confirm_screen(t, ALICE, five, 1), "Confirm")
        calls = await t.send(OWNER, f"/modifybal {ALICE['id']} 5")
        assert "5.00$" in t.last_text(calls)
        calls = await t.press(ALICE, confirm)
        assert "Not enough balance" in t.last_text(calls, ALICE["id"])
        assert t.has_button(calls, "Add Balance")
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 500 and t.db.count_items(five) == 1

    run(tmp_path, scenario)


def test_price_raised_after_confirmation_does_not_overcharge(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 5000)
        calls = await confirm_screen(t, ALICE, five, 2)
        assert "Total: <b>19.00$</b>" in t.last_text(calls)
        confirm = t.button(calls, "Confirm")

        # Meanwhile the owner raises the price with Add Label › Set price.
        owner = await t.press(OWNER, f"lb:{five}:0")
        owner = await t.press(OWNER, t.button(owner, "Set price"))
        owner = await t.send(OWNER, "20")
        assert "20.00$" in t.last_text(owner)

        await t.press(ALICE, confirm)
        charged = 5000 - balance(t, ALICE)
        # The buyer must never pay more than the total shown on the screen they confirmed
        # (either the old price is honoured, or they are asked to confirm the new one).
        assert charged in (0, 1900), f"charged {charged} cents after confirming 1900"

    run(tmp_path, scenario)


def test_seller_removed_after_confirmation_does_not_overcharge(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 1000)
        add_stock(t, five, "A", "B")
        await t.send(OWNER, f"/seller {ALICE['id']}")
        set_balance(t, ALICE, 5000)
        calls = await confirm_screen(t, ALICE, five, 2)
        assert "Total: <b>18.00$</b>" in t.last_text(calls)
        confirm = t.button(calls, "Confirm")
        await t.send(OWNER, f"/removeseller {ALICE['id']}")
        await t.press(ALICE, confirm)
        charged = 5000 - balance(t, ALICE)
        assert charged in (0, 1800), f"charged {charged} cents after confirming 1800"

    run(tmp_path, scenario)


# =============================================================== closed shop
def test_closed_shop_blocks_customer_browsing_and_buying_even_with_crafted_buttons(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        esim = make_label(t, "Esim")
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 5000)

        calls = await t.send(OWNER, "/close")
        assert "now closed" in t.last_text(calls) and not t.db.shop_is_open()

        calls = await t.send(ALICE, "/start")
        calls = await t.press(ALICE, t.button(calls, "Shop"))
        assert CLOSED in t.last_text(calls)
        assert not any("Esim" in n for n in names(calls))
        for data in (f"sh:{esim}:0", f"sh:{five}:0", f"sb:{five}:1", f"sq:{five}"):
            calls = await t.press(ALICE, data)
            assert CLOSED in t.last_text(calls), data
            assert buy_buttons(calls) == [], data
        calls = await t.press(ALICE, f"sc:{five}:1:deadbeef")
        assert EXPIRED in t.alert(calls)
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000 and t.db.count_items(five) == 2

        await t.send(OWNER, "/open")
        calls = await t.press(ALICE, f"sh:{five}:0")
        assert "In stock: <b>2</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_closed_shop_blocks_a_confirmation_opened_before_closing(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B")
        make_admin(t, ADMIN)
        for user in (ALICE, ADMIN):
            set_balance(t, user, 5000)
        confirms = {u["id"]: t.button(await confirm_screen(t, u, five, 1), "Confirm") for u in (ALICE, ADMIN)}

        await t.send(OWNER, "/close")
        for user in (ALICE, ADMIN):
            calls = await t.press(user, confirms[user["id"]])
            assert CLOSED in t.last_text(calls, user["id"])
            assert orders_of(t, user) == [] and balance(t, user) == 5000
        assert t.db.count_items(five) == 2

        await t.send(OWNER, "/open")
        calls = await buy(t, ALICE, five, 1)
        assert "Purchase successful" in all_text(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_closed_shop_blocks_admins(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", owner_id=OWNER_ID)
        make_admin(t, ADMIN)
        set_balance(t, ADMIN, 5000)
        await t.send(OWNER, "/close")

        calls = await t.send(ADMIN, "/start")
        assert "Welcome Admin" in t.last_text(calls)
        calls = await t.press(ADMIN, t.button(calls, "Shop"))
        assert CLOSED in t.last_text(calls)
        for data in (f"sh:{five}:0", f"sb:{five}:1", f"sq:{five}"):
            calls = await t.press(ADMIN, data)
            assert CLOSED in t.last_text(calls), data
            assert buy_buttons(calls) == []
        assert orders_of(t, ADMIN) == [] and balance(t, ADMIN) == 5000

    run(tmp_path, scenario)


def test_owner_can_browse_and_buy_while_the_shop_is_closed(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        make_admin(t, ADMIN)
        add_stock(t, five, "ADM-1", owner_id=ADMIN["id"])
        set_balance(t, OWNER, 5000)
        await t.send(OWNER, "/close")

        calls = await t.send(OWNER, "/start")
        calls = await t.press(OWNER, t.button(calls, "Shop"))
        assert "closed for customers" in t.last_text(calls)
        assert "📂 Esim" in names(calls)
        calls = await t.press(OWNER, t.button(calls, "Esim"))
        calls = await t.press(OWNER, t.button(calls, "5GB"))
        calls = await t.press(OWNER, exact(calls, "1"))
        calls = await t.press(OWNER, t.button(calls, "Confirm"))
        assert "ADM-1" in t.texts(calls, OWNER_ID)
        assert balance(t, OWNER) == 5000 - 950
        assert earnings(t, ADMIN["id"]) == 950
        assert "New sale!" in all_text(calls, ADMIN["id"])
        assert not t.db.shop_is_open()

    run(tmp_path, scenario)


def test_closed_shop_typed_quantity_offers_no_confirmation(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B", "C")
        set_balance(t, ALICE, 5000)
        await t.press(ALICE, f"sq:{five}")  # Alice is asked "How many?"
        await t.send(OWNER, "/close")
        calls = await t.send(ALICE, "2")
        assert not t.has_button(calls, "Confirm"), t.last_text(calls)
        assert CLOSED in t.last_text(calls)

    run(tmp_path, scenario)


def test_closed_shop_typed_quantity_still_cannot_buy(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B", "C")
        set_balance(t, ALICE, 5000)
        await t.press(ALICE, f"sq:{five}")
        await t.send(OWNER, "/close")
        calls = await t.send(ALICE, "2")
        if t.has_button(calls, "Confirm"):
            calls = await t.press(ALICE, t.button(calls, "Confirm"))
            assert CLOSED in t.last_text(calls)
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000 and t.db.count_items(five) == 3

    run(tmp_path, scenario)


# ======================================================================= ban
def test_banned_customer_cannot_use_an_open_confirmation_and_unban_restores(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "A", "B")
        set_balance(t, ALICE, 5000)
        confirm = t.button(await confirm_screen(t, ALICE, five, 1), "Confirm")

        calls = await t.send(OWNER, f"/ban {ALICE['id']}")
        assert "banned" in t.last_text(calls)
        for data in (confirm, "sh:0:0", f"sb:{five}:1", "mo:0"):
            calls = await t.press(ALICE, data)
            assert "You are banned" in t.alert(calls), data
            assert not t.messages(calls, ALICE["id"])
        assert orders_of(t, ALICE) == [] and balance(t, ALICE) == 5000

        await t.send(OWNER, f"/unban {ALICE['id']}")
        calls = await t.press(ALICE, confirm)  # the ban threw the old confirmation away
        assert EXPIRED in t.alert(calls)
        calls = await buy(t, ALICE, five, 1)
        assert "Purchase successful" in all_text(calls, ALICE["id"])
        assert balance(t, ALICE) == 5000 - 950

    run(tmp_path, scenario)


# ================================================================= My Orders
def test_my_orders_lists_orders_newest_first_and_resends_their_items(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        ten = make_label(t, "Esim/10GB", 1500)
        add_stock(t, five, "CODE-A", "CODE-B", "CODE-C")
        ent = [{"type": "bold", "offset": 0, "length": 3}]
        t.db.add_items(ten, OWNER_ID, [text_item("BIG-1 secret", ent)])
        set_balance(t, ALICE, 5000)
        await buy(t, ALICE, five, 2)
        await buy(t, ALICE, ten, 1)
        first, second = sorted(orders_of(t, ALICE), key=lambda o: o["id"])

        calls = await t.send(ALICE, "/start")
        calls = await t.press(ALICE, t.button(calls, "My Orders"))
        assert "Tap an order to get its products again" in t.last_text(calls)
        listed = [n for n in names(calls) if n.startswith("#")]
        assert listed == [f"#{second['code']} · 10GB ×1 · 15.00$", f"#{first['code']} · 5GB ×2 · 19.00$"]

        stock_before = (t.db.count_items(five), t.db.count_items(ten), balance(t, ALICE))
        calls = await t.press(ALICE, t.button(calls, f"#{first['code']}"))
        texts = t.texts(calls, ALICE["id"])
        assert f"Order <code>#{first['code']}</code>" in texts[0]
        assert "Esim › 5GB" in texts[0] and "Quantity: 2 · 💰 19.00$" in texts[0]
        assert texts[1:3] == ["CODE-A", "CODE-B"]
        assert "CODE-C" not in texts
        assert texts[-1] == "✅ Done."
        assert {"🧾 My Orders", "🏠 Menu"} <= {b["text"] for b in t.messages(calls, ALICE["id"])[-1].buttons}

        calls = await t.press(ALICE, f"mov:{second['id']}")
        (resent,) = [c for c in sent_to(calls, ALICE["id"], "sendMessage") if c.text == "BIG-1 secret"]
        assert resent.params.get("entities") == ent
        # Re-sending changes nothing: no charge, no stock taken.
        assert (t.db.count_items(five), t.db.count_items(ten), balance(t, ALICE)) == stock_before
        assert len(orders_of(t, ALICE)) == 2

    run(tmp_path, scenario)


def test_my_orders_when_nothing_was_bought(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(ALICE, "/start")
        calls = await t.press(ALICE, t.button(calls, "My Orders"))
        assert "You haven't bought anything yet" in t.last_text(calls)
        assert not [n for n in names(calls) if n.startswith("#")]
        assert t.has_button(calls, "Shop")

    run(tmp_path, scenario)


def test_another_users_order_is_not_found(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "ALICE-SECRET")
        set_balance(t, ALICE, 5000)
        await buy(t, ALICE, five, 1)
        (order,) = orders_of(t, ALICE)

        for intruder in (BOB, OWNER):
            calls = await t.press(intruder, f"mov:{order['id']}")
            assert "Order not found" in t.alert(calls)
            assert t.answers(calls)[-1].get("show_alert") is True
            assert "ALICE-SECRET" not in all_text(calls)
        calls = await t.press(BOB, "mov:999999")
        assert "Order not found" in t.alert(calls)
        calls = await t.press(BOB, "mo:0")
        assert "You haven't bought anything yet" in t.last_text(calls)
        assert order["code"] not in " ".join(names(calls))

    run(tmp_path, scenario)


def test_my_orders_pages_8_orders_at_a_time(tmp_path):
    async def scenario(t: ShopTest) -> None:
        one = make_label(t, "Esim/1GB", 100)
        add_stock(t, one, *[f"K{i}" for i in range(10)])
        set_balance(t, ALICE, 5000)
        for _ in range(10):
            await buy(t, ALICE, one, 1)
        calls = await t.press(ALICE, "mo:0")
        assert len([n for n in names(calls) if n.startswith("#")]) == 8
        assert "1/2" in names(calls)
        calls = await t.press(ALICE, exact(calls, "▶️"))
        assert len([n for n in names(calls) if n.startswith("#")]) == 2
        assert "2/2" in names(calls)

    run(tmp_path, scenario)


def test_my_orders_still_work_while_the_shop_is_closed(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "BOUGHT-BEFORE-CLOSING")
        set_balance(t, ALICE, 5000)
        await buy(t, ALICE, five, 1)
        await t.send(OWNER, "/close")
        calls = await t.send(ALICE, "/start")
        calls = await t.press(ALICE, t.button(calls, "My Orders"))
        calls = await t.press(ALICE, t.button(calls, "5GB"))
        assert "BOUGHT-BEFORE-CLOSING" in t.texts(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_my_orders_still_resends_after_the_label_is_deleted(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "KEEP-ME")
        set_balance(t, ALICE, 5000)
        await buy(t, ALICE, five, 1)
        await t.press(OWNER, f"ld:{five}")
        calls = await t.press(OWNER, f"ldy:{five}")
        assert "Deleted" in t.alert(calls) and t.db.get_label(five) is None

        calls = await t.press(ALICE, "mo:0")
        calls = await t.press(ALICE, t.button(calls, "5GB"))
        assert "KEEP-ME" in t.texts(calls, ALICE["id"])

    run(tmp_path, scenario)


# ========================================================== delivery failure
def test_photo_that_cannot_be_sent_warns_buyer_and_order_is_kept(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        make_admin(t, ADMIN)
        t.db.add_items(five, ADMIN["id"], [
            text_item("CODE-TEXT"),
            {"kind": "photo", "file_id": "qr-photo", "caption": "QR code", "caption_entities": []},
        ])
        set_balance(t, ALICE, 5000)

        t.tg.fail["sendPhoto"] = "Bad Request: wrong file identifier/HTTP URL specified"
        calls = await buy(t, ALICE, five, 2)
        (order,) = orders_of(t, ALICE)
        assert balance(t, ALICE) == 5000 - 1900
        assert t.db.count_items(five) == 0
        assert "CODE-TEXT" in t.texts(calls, ALICE["id"])
        warning = t.messages(calls, ALICE["id"])[-1]
        assert "1 product(s) could not be sent" in warning.text
        assert "My Orders" in warning.text and f"#{order['code']}" in warning.text
        assert t.has_button([warning], "My Orders")
        assert "New sale!" in all_text(calls, ADMIN["id"])
        assert earnings(t, ADMIN["id"]) == 1900

        # Once Telegram accepts the photo again, My Orders delivers it.
        del t.tg.fail["sendPhoto"]
        calls = await t.press(ALICE, t.button([warning], "My Orders"))
        calls = await t.press(ALICE, t.button(calls, f"#{order['code']}"))
        (photo,) = sent_to(calls, ALICE["id"], "sendPhoto")
        assert photo.params["photo"] == "qr-photo" and photo.params["caption"] == "QR code"
        assert t.last_text(calls, ALICE["id"]) == "✅ Done."
        assert balance(t, ALICE) == 5000 - 1900

    run(tmp_path, scenario)


def test_text_product_that_cannot_be_sent_warns_buyer(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "GOOD-CODE", "BROKEN-CODE")
        set_balance(t, ALICE, 5000)
        inject_failure(t, lambda m, p: m == "sendMessage" and p.get("text") == "BROKEN-CODE",
                       description="Bad Request: message is too long")
        calls = await buy(t, ALICE, five, 2)
        (order,) = orders_of(t, ALICE)
        assert order["quantity"] == 2 and balance(t, ALICE) == 5000 - 1900
        assert [c.text for c in sent_to(calls, ALICE["id"], "sendMessage")][0] == "GOOD-CODE"
        warning = t.messages(calls, ALICE["id"])[-1]
        assert "1 product(s) could not be sent" in warning.text and f"#{order['code']}" in warning.text

        # My Orders also says what it could not send.
        calls = await t.press(ALICE, f"mov:{order['id']}")
        assert "1 product(s) could not be sent" in t.last_text(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_all_messages_to_buyer_failing_still_records_the_order_once(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        add_stock(t, five, "LOST-CODE", "SPARE")
        set_balance(t, ALICE, 5000)
        confirm = t.button(await confirm_screen(t, ALICE, five, 1), "Confirm")

        t.tg.fail["sendMessage"] = "Forbidden: bot was blocked by the user"
        calls = await t.press(ALICE, confirm)
        (order,) = orders_of(t, ALICE)
        assert balance(t, ALICE) == 5000 - 950 and t.db.count_items(five) == 1
        edits = [c for c in calls if c.method == "editMessageText" and c.chat_id == ALICE["id"]]
        assert edits and f"#{order['code']}" in edits[-1].text  # the confirm screen became the receipt
        calls = await t.press(ALICE, confirm)  # pressing again doesn't buy twice
        assert len(orders_of(t, ALICE)) == 1 and balance(t, ALICE) == 5000 - 950

        del t.tg.fail["sendMessage"]
        calls = await t.press(ALICE, f"mov:{order['id']}")
        assert "LOST-CODE" in t.texts(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_late_confirm_press_still_delivers_the_products(tmp_path):
    """Updates are handled one at a time, so a Confirm press can wait behind a long
    delivery to someone else; Telegram then refuses the late answer to the press
    ("query is too old"). The purchase is already committed at that point."""

    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        make_admin(t, ADMIN)
        add_stock(t, five, "PAID-CODE", owner_id=ADMIN["id"])
        set_balance(t, ALICE, 5000)
        inject_failure(
            t, lambda m, p: m == "answerCallbackQuery" and "Purchase successful" in str(p.get("text")),
            description="Bad Request: query is too old and response timeout expired or query ID is invalid",
        )
        calls = await buy(t, ALICE, five, 1)
        assert balance(t, ALICE) == 5000 - 950 and len(orders_of(t, ALICE)) == 1
        shown = [c.text or "" for c in t.messages(calls, ALICE["id"]) if not c.get("failed")]
        assert "PAID-CODE" in shown, shown
        assert any("Purchase successful" in x for x in shown), shown
        assert not any("try again" in x for x in shown), shown
        assert "New sale!" in all_text(calls, ADMIN["id"])

    run(tmp_path, scenario)


def test_timeout_on_buyers_final_message_still_notifies_the_seller(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        make_admin(t, ADMIN)
        add_stock(t, five, "ADM-CODE", owner_id=ADMIN["id"])
        set_balance(t, ALICE, 5000)
        inject_failure(t, lambda m, p: m == "sendMessage" and p.get("chat_id") == ALICE["id"]
                       and "Delivered" in str(p.get("text")), error=TimedOut)
        calls = await buy(t, ALICE, five, 1)
        assert "ADM-CODE" in t.texts(calls, ALICE["id"])
        assert earnings(t, ADMIN["id"]) == 950
        assert "New sale!" in all_text(calls, ADMIN["id"])

    run(tmp_path, scenario)


def test_timeout_showing_the_receipt_still_delivers_the_products(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_label(t, "Esim/5GB", 950)
        make_admin(t, ADMIN)
        add_stock(t, five, "PAID-CODE", owner_id=ADMIN["id"])
        set_balance(t, ALICE, 5000)
        inject_failure(t, lambda m, p: m == "editMessageText"
                       and "Purchase successful" in str(p.get("text")), error=TimedOut)
        calls = await buy(t, ALICE, five, 1)
        assert balance(t, ALICE) == 5000 - 950  # the money is taken either way
        texts = [c.text for c in sent_to(calls, ALICE["id"], "sendMessage")]
        assert "PAID-CODE" in texts, texts
        assert not any("try again" in (x or "") for x in texts), texts
        assert "New sale!" in all_text(calls, ADMIN["id"])

    run(tmp_path, scenario)
