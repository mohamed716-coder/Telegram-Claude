"""End-to-end happy path: owner builds the shop, a customer deposits and buys."""

from harness import OWNER, OWNER_ID, ShopTest, run, tg_user

ALICE = tg_user(1001, "Alice", "alice")


async def build_esim_shop(t: ShopTest) -> int:
    """Owner creates Esim › Oman › Omantel › 5GB/10GB, prices 5GB, adds 3 products.
    Returns the 5GB label id."""
    calls = await t.send(OWNER, "/start")
    assert "Welcome Owner" in t.last_text(calls)

    calls = await t.press(OWNER, t.button(calls, "Add Label"))
    calls = await t.press(OWNER, t.button(calls, "New label"))
    calls = await t.send(OWNER, "Esim")
    assert "Added" in t.last_text(calls)
    calls = await t.press(OWNER, t.button(calls, "Esim"))
    calls = await t.press(OWNER, t.button(calls, "New label inside"))
    calls = await t.send(OWNER, "Oman")
    calls = await t.press(OWNER, t.button(calls, "Oman"))
    calls = await t.press(OWNER, t.button(calls, "New label inside"))
    calls = await t.send(OWNER, "Omantel")
    calls = await t.press(OWNER, t.button(calls, "Omantel"))
    calls = await t.press(OWNER, t.button(calls, "New label inside"))
    calls = await t.send(OWNER, "5GB\n10GB")
    assert "5GB" in t.last_text(calls) and "10GB" in t.last_text(calls)

    five = [r for r in t.db.children(t.db.children(t.db.children(t.db.children(None)[0]["id"])[0]["id"])[0]["id"]) if r["name"] == "5GB"][0]
    calls = await t.press(OWNER, f"lb:{five['id']}:0")
    calls = await t.press(OWNER, t.button(calls, "Set price"))
    calls = await t.send(OWNER, "9.50")
    assert "9.50$" in t.last_text(calls)

    calls = await t.press(OWNER, "ap:0:0")
    calls = await t.press(OWNER, t.button(calls, "Esim"))
    calls = await t.press(OWNER, t.button(calls, "Oman"))
    calls = await t.press(OWNER, t.button(calls, "Omantel"))
    calls = await t.press(OWNER, t.button(calls, "5GB"))
    assert "ONE MESSAGE PER PRODUCT" in t.last_text(calls)
    await t.send(OWNER, "CODE-AAA-111")
    await t.send(OWNER, "CODE-BBB-222")
    calls = await t.send_photo(OWNER, "esim-qr-photo", caption="QR for eSIM")
    assert "Received <b>3</b>" in t.last_text(calls)
    calls = await t.press(OWNER, "apd")
    assert "Saved 3 product(s)" in t.last_text(calls)
    assert t.db.count_items(five["id"]) == 3
    return five["id"]


async def happy_path(t: ShopTest) -> None:
    five_id = await build_esim_shop(t)

    # Customer deposits 20$ with a receipt; owner approves.
    calls = await t.send(ALICE, "/start")
    assert "Welcome Alice" in t.last_text(calls)
    calls = await t.press(ALICE, "dp")
    calls = await t.press(ALICE, t.button(calls, "20$"))
    assert "850940840" in t.last_text(calls)
    calls = await t.send_photo(ALICE, "alice-receipt")
    calls = await t.press(ALICE, t.button(calls, "Confirm"))
    to_owner = [c for c in calls if c.chat_id == OWNER_ID and c.method == "sendPhoto"]
    assert to_owner and to_owner[0].params["photo"] == "alice-receipt"
    calls = await t.press(OWNER, t.button(to_owner, "Approve"), photo=True)
    assert t.db.get_user(ALICE["id"])["balance"] == 2000

    # Customer buys 2 × 5GB.
    calls = await t.press(ALICE, "sh:0:0")
    calls = await t.press(ALICE, t.button(calls, "Esim"))
    calls = await t.press(ALICE, t.button(calls, "Oman"))
    calls = await t.press(ALICE, t.button(calls, "Omantel"))
    calls = await t.press(ALICE, t.button(calls, "5GB"))
    assert "In stock: <b>3</b>" in t.last_text(calls)
    calls = await t.press(ALICE, f"sb:{five_id}:2")
    calls = await t.press(ALICE, t.button(calls, "Confirm"))
    texts = t.texts(calls, ALICE["id"])
    assert any("Purchase successful" in x for x in texts)
    assert "CODE-AAA-111" in texts and "CODE-BBB-222" in texts
    assert t.db.get_user(ALICE["id"])["balance"] == 2000 - 1900
    assert t.db.get_user(OWNER_ID)["earnings"] == 1900
    assert t.db.count_items(five_id) == 1


def test_happy_path(tmp_path):
    run(tmp_path, happy_path)
