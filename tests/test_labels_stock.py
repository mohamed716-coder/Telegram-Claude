"""End-to-end tests: 🏷 Add Label (the label tree), ➕ Add Product and 📦 My Products.

Every test drives the real Application through the offline fake Telegram from
harness.py and checks both the database and what each person sees.
"""

from __future__ import annotations

import csv
import io
import json
import time
from typing import Any, Optional

import pytest

from harness import OWNER, OWNER_ID, ShopTest, run, tg_user
from shopbot.db import CODE_ALPHABET, CODE_LENGTH
from shopbot.handlers import stock as stock_module

ADMIN = tg_user(2001, "Adam", "adam_admin")
ADMIN2 = tg_user(2002, "Bella", "bella_admin")
ALICE = tg_user(1001, "Alice", "alice")
BOB = tg_user(1002, "Bob", "bob")

OWNER_ONLY = "Only the owner can manage labels."
STAFF_ONLY = "This section is for the owner and admins."
NOT_YOURS = "You can only manage your own products."
GONE = "This product is no longer in stock."
PRICE_HINT = "Please send a price like 9.99 (more than 0)."
UNSUPPORTED_HINT = "This kind of message can't be sold"
ADD_PROMPT_TITLE = "❗ <b>SEND THE PRODUCTS — ONE MESSAGE PER PRODUCT</b>"
NO_STATE_REPLY = "Send /start to open the shop menu."

# A product message with formatting, exactly as Telegram delivers it to the bot.
TEXT = "eSIM code: ABCD-1234 activate here, PIN 9876"
ENTITIES = [
    {"type": "bold", "offset": 0, "length": 9},
    {"type": "code", "offset": 11, "length": 9},
    {"type": "text_link", "offset": 21, "length": 13, "url": "https://example.com/activate"},
    {"type": "spoiler", "offset": 40, "length": 4},
]


# ============================================================== small helpers
def jparam(call: Any, key: str) -> Any:
    """A list/dict Bot API parameter (it may be recorded as a JSON string)."""
    value = call.params.get(key)
    return json.loads(value) if isinstance(value, str) else value


def datas(calls: list) -> list[str]:
    return [b.get("callback_data", "") for b in ShopTest.all_buttons(calls)]


def button_texts(calls: list) -> list[str]:
    return [b.get("text", "") for b in ShopTest.all_buttons(calls)]


def all_text(calls: list, chat_id: Optional[int] = None) -> str:
    return "\n".join(ShopTest.texts(calls, chat_id))


def last_answer(calls: list) -> dict:
    answers = ShopTest.answers(calls)
    assert answers, f"no answerCallbackQuery; calls: {[c.method for c in calls]}"
    return answers[-1]


def assert_alert(calls: list, text: str) -> None:
    answer = last_answer(calls)
    assert text in (answer.get("text") or ""), answer
    assert answer.get("show_alert") is True, answer


def sent_to(calls: list, chat_id: int, methods: Optional[set] = None) -> list:
    """Messages sent (not edited) to ``chat_id``."""
    return [
        c for c in calls
        if c.method.startswith("send") and c.chat_id == chat_id
        and (methods is None or c.method in methods)
    ]


def label_by_path(t: ShopTest, *names: str) -> Any:
    parent, row = None, None
    for name in names:
        matches = [r for r in t.db.children(parent) if r["name"] == name]
        assert matches, f"no label {name!r} under {parent}: {[r['name'] for r in t.db.children(parent)]}"
        row = matches[0]
        parent = row["id"]
    return row


def raw_label(t: ShopTest, label_id: int) -> Any:
    """The labels row, even when it is soft-deleted."""
    return t.db.conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()


def items_of(t: ShopTest, label_id: int) -> list:
    """Every item of a label (any status), oldest first."""
    return t.db.conn.execute(
        "SELECT * FROM items WHERE label_id = ? ORDER BY id", (label_id,)
    ).fetchall()


def content(row: Any) -> dict:
    return json.loads(row["content"])


def csv_rows(call: Any) -> list[list[str]]:
    return list(csv.reader(io.StringIO(ShopTest.document_text(call))))


def photo_msg(file_id: str = "qr-photo-1", caption: Optional[str] = "Scan this QR",
              caption_entities: Optional[list] = None, **extra: Any) -> dict:
    msg: dict[str, Any] = {"photo": [
        {"file_id": f"{file_id}-small", "file_unique_id": f"{file_id}-s", "width": 90, "height": 90},
        {"file_id": f"{file_id}-medium", "file_unique_id": f"{file_id}-m", "width": 320, "height": 320},
        {"file_id": file_id, "file_unique_id": f"{file_id}-l", "width": 1280, "height": 1280},
    ]}
    if caption is not None:
        msg["caption"] = caption
    if caption_entities:
        msg["caption_entities"] = caption_entities
    msg.update(extra)
    return msg


def media_samples() -> list[dict]:
    """One message of every other sellable kind + how the bot must send it again."""
    return [
        {"kind": "video", "method": "sendVideo", "file_id": "video-1", "caption": "Video guide",
         "caption_entities": [{"type": "italic", "offset": 0, "length": 5}],
         "msg": {"video": {"file_id": "video-1", "file_unique_id": "u-video-1", "width": 640,
                           "height": 360, "duration": 12},
                 "caption": "Video guide",
                 "caption_entities": [{"type": "italic", "offset": 0, "length": 5}]}},
        {"kind": "document", "method": "sendDocument", "file_id": "doc-1", "caption": "Your codes file",
         "caption_entities": [{"type": "bold", "offset": 5, "length": 5}],
         "msg": {"document": {"file_id": "doc-1", "file_unique_id": "u-doc-1",
                              "file_name": "codes.txt", "mime_type": "text/plain"},
                 "caption": "Your codes file",
                 "caption_entities": [{"type": "bold", "offset": 5, "length": 5}]}},
        # Telegram sends a GIF with both .animation and .document set.
        {"kind": "animation", "method": "sendAnimation", "file_id": "gif-1", "caption": "Thanks GIF",
         "caption_entities": [],
         "msg": {"animation": {"file_id": "gif-1", "file_unique_id": "u-gif-1", "width": 320,
                               "height": 240, "duration": 3},
                 "document": {"file_id": "gif-1", "file_unique_id": "u-gif-1",
                              "file_name": "thanks.mp4", "mime_type": "video/mp4"},
                 "caption": "Thanks GIF"}},
        {"kind": "sticker", "method": "sendSticker", "file_id": "sticker-1", "caption": None,
         "caption_entities": [],
         "msg": {"sticker": {"file_id": "sticker-1", "file_unique_id": "u-sticker-1", "type": "regular",
                             "width": 512, "height": 512, "is_animated": False, "is_video": False}}},
        {"kind": "voice", "method": "sendVoice", "file_id": "voice-1", "caption": "Listen to this",
         "caption_entities": [],
         "msg": {"voice": {"file_id": "voice-1", "file_unique_id": "u-voice-1", "duration": 4},
                 "caption": "Listen to this"}},
        {"kind": "audio", "method": "sendAudio", "file_id": "audio-1", "caption": "Full track",
         "caption_entities": [{"type": "underline", "offset": 0, "length": 4}],
         "msg": {"audio": {"file_id": "audio-1", "file_unique_id": "u-audio-1", "duration": 180,
                           "title": "Song"},
                 "caption": "Full track",
                 "caption_entities": [{"type": "underline", "offset": 0, "length": 4}]}},
        {"kind": "video_note", "method": "sendVideoNote", "file_id": "vnote-1", "caption": None,
         "caption_entities": [],
         "msg": {"video_note": {"file_id": "vnote-1", "file_unique_id": "u-vnote-1", "length": 240,
                                "duration": 5}}},
    ]


def assert_resent(call: Any, sample: dict) -> None:
    """``call`` re-sends ``sample`` with the same file_id and caption/entities."""
    assert call.method == sample["method"], (call.method, sample["kind"])
    assert call.params[sample["kind"]] == sample["file_id"]
    assert call.params.get("caption") == sample["caption"]
    assert (jparam(call, "caption_entities") or []) == sample["caption_entities"]
    assert "parse_mode" not in call.params


def assert_text_resent(call: Any, text: str = TEXT, entities: Optional[list] = None) -> None:
    assert call.method == "sendMessage"
    assert call.params["text"] == text
    assert jparam(call, "entities") == (ENTITIES if entities is None else entities)
    assert "parse_mode" not in call.params  # entities, never re-parsed HTML/Markdown


# ============================================================ flow helpers
async def new_labels(t: ShopTest, parent_id: int, names: str) -> list:
    """Owner opens a label (0 = top level), presses ➕ New label and sends ``names``."""
    calls = await t.press(OWNER, f"lb:{parent_id}:0")
    calls = await t.press(OWNER, t.button(calls, "New label"))
    assert "Send the label name" in t.last_text(calls)
    return await t.send(OWNER, names)


async def set_price(t: ShopTest, label_id: int, text: str) -> list:
    calls = await t.press(OWNER, f"lb:{label_id}:0")
    calls = await t.press(OWNER, t.button(calls, "Set price"))
    assert "Send the price in $" in t.last_text(calls)
    return await t.send(OWNER, text)


async def esim_tree(t: ShopTest, price: Optional[str] = "9.50") -> dict:
    """Esim › Oman › {Omantel › {5GB, 10GB}, Vodafone}. 5GB gets ``price``."""
    await t.send(OWNER, "/start")
    await new_labels(t, 0, "Esim")
    esim = label_by_path(t, "Esim")["id"]
    await new_labels(t, esim, "Oman")
    oman = label_by_path(t, "Esim", "Oman")["id"]
    await new_labels(t, oman, "Omantel\nVodafone")
    omantel = label_by_path(t, "Esim", "Oman", "Omantel")["id"]
    await new_labels(t, omantel, "5GB\n10GB")
    five = label_by_path(t, "Esim", "Oman", "Omantel", "5GB")["id"]
    ten = label_by_path(t, "Esim", "Oman", "Omantel", "10GB")["id"]
    if price:
        calls = await set_price(t, five, price)
        assert "✅ Price of <b>5GB</b> set to" in t.last_text(calls)
    return {"esim": esim, "oman": oman, "omantel": omantel, "five": five, "ten": ten,
            "vodafone": label_by_path(t, "Esim", "Oman", "Vodafone")["id"]}


async def send_product(t: ShopTest, user: dict, product: Any) -> list:
    if isinstance(product, str):
        return await t.send(user, product)
    return await t.send(user, **product)


async def add_products(t: ShopTest, user: dict, label_id: int, products: list) -> list[str]:
    """Add Product on ``label_id``: send each product message, press Done. Returns the codes."""
    calls = await t.press(user, f"ap:{label_id}:0")
    assert ADD_PROMPT_TITLE in t.last_text(calls), t.texts(calls)
    before = {r["id"] for r in items_of(t, label_id)}
    for product in products:
        await send_product(t, user, product)
    calls = await t.press(user, "apd")
    assert f"Saved {len(products)} product(s)" in t.last_text(calls), t.texts(calls)
    return [r["code"] for r in items_of(t, label_id) if r["id"] not in before]


async def make_admin(t: ShopTest, user: dict) -> None:
    await t.send(user, "/start")
    calls = await t.send(OWNER, f"/addadmin {user['id']}")
    assert "is now an <b>admin</b>" in all_text(calls, OWNER_ID)
    calls = await t.send(user, "/start")
    assert "Welcome Admin" in t.last_text(calls)


async def give_balance(t: ShopTest, user: dict, dollars: int) -> None:
    await t.send(user, "/start")
    calls = await t.send(OWNER, f"/addbal {user['id']} {dollars}")
    assert "✅ Added" in all_text(calls, OWNER_ID)


async def buy(t: ShopTest, user: dict, label_id: int, quantity: int) -> list:
    """Customer buys through the Shop buttons. Returns the calls of the Confirm press."""
    calls = await t.press(user, f"sh:{label_id}:0")
    quick = f"sb:{label_id}:{quantity}"
    if quick in datas(calls):
        calls = await t.press(user, quick)
    else:
        calls = await t.press(user, t.button(calls, "Other amount"))
        calls = await t.send(user, str(quantity))
    calls = await t.press(user, t.button(calls, "Confirm"))
    assert any("Purchase successful" in x for x in t.texts(calls, user["id"])), t.texts(calls)
    return calls


async def send_edit(t: ShopTest, user: dict, message_id: int, text: str) -> list:
    """The update Telegram sends when ``user`` edits their earlier message ``message_id``."""
    now = int(time.time())
    message = {
        "message_id": message_id,
        "date": now - 30,
        "edit_date": now,
        "chat": {"id": user["id"], "type": "private", "first_name": user.get("first_name", "")},
        "from": user,
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return await t._process({"edited_message": message})


async def open_items(t: ShopTest, user: dict, label_id: int) -> list:
    """📦 My Products list of a product label."""
    calls = await t.press(user, f"mp:{label_id}:0")
    assert "Price:" in t.last_text(calls), t.texts(calls)
    return calls


def item_buttons(calls: list) -> list[dict]:
    return [b for b in ShopTest.all_buttons(calls) if b.get("callback_data", "").startswith("mv:")]


def status_msg_id(t: ShopTest, user: dict) -> int:
    return t.app.user_data[user["id"]]["state"]["status_msg_id"]


# ======================================================================== labels
def test_add_label_button_only_on_owner_panel(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/start")
        assert "Welcome Owner" in t.last_text(calls)
        for name in ("Shop", "My Products", "Add Product", "Add Label", "Earnings", "Add Balance"):
            assert t.has_button(calls, name), name
        assert t.button(calls, "Add Label") == "lb:0:0"
        await make_admin(t, ADMIN)
        calls = await t.send(ADMIN, "/start")
        assert t.has_button(calls, "Add Product") and t.has_button(calls, "My Products")
        assert not t.has_button(calls, "Add Label")
        calls = await t.send(ALICE, "/start")
        for name in ("Add Label", "Add Product", "My Products"):
            assert not t.has_button(calls, name), name

    run(tmp_path, scenario)


def test_build_nested_label_tree_with_buttons(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/start")
        calls = await t.press(OWNER, t.button(calls, "Add Label"))
        assert "Top level (shown in 🛍 Shop)" in t.last_text(calls)
        path: list[str] = []
        for name in ("Esim", "Oman", "Omantel", "5GB"):
            calls = await t.press(OWNER, t.button(calls, "New label"))
            where = f"inside <b>{' › '.join(path)}</b>" if path else "at the top level"
            assert where in t.last_text(calls)
            calls = await t.send(OWNER, name)
            assert f"✅ Added: <b>{name}</b>" in t.last_text(calls)
            calls = await t.press(OWNER, t.button(calls, name))
            path.append(name)
            assert f"📂 {' › '.join(path)}" in t.last_text(calls)

        rows = [label_by_path(t, *path[: i + 1]) for i in range(len(path))]
        assert rows[0]["parent_id"] is None
        for parent, child in zip(rows, rows[1:]):
            assert child["parent_id"] == parent["id"]
        # The last label is the product: it needs a price and can hold stock.
        text = t.last_text(calls)
        assert "This label is a product" in text
        assert "Price: <b>⚠️ not set</b>" in text and "In stock: <b>0</b>" in text
        assert f"lp:{rows[-1]['id']}" in datas(calls)
        # Categories show 📂 and have no "Set price".
        calls = await t.press(OWNER, f"lb:{rows[2]['id']}:0")
        assert f"lp:{rows[2]['id']}" not in datas(calls)
        assert "5GB · no price" in button_texts(calls)
        calls = await t.press(OWNER, f"lb:{rows[1]['id']}:0")
        assert "📂 Omantel" in button_texts(calls)

        # The owner's 🛍 Shop shows the same tree.
        calls = await t.press(OWNER, "sh:0:0")
        for name in ("Esim", "Oman", "Omantel"):
            calls = await t.press(OWNER, t.button(calls, f"📂 {name}"))
        assert t.has_button(calls, "5GB")
        # Customers don't see a branch whose product has no price yet.
        calls = await t.press(ALICE, "sh:0:0")
        assert "The shop is empty right now" in t.last_text(calls)
        await set_price(t, rows[-1]["id"], "5")
        calls = await t.press(ALICE, "sh:0:0")
        assert t.has_button(calls, "📂 Esim")

    run(tmp_path, scenario)


def test_several_labels_one_per_line(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t, price=None)
        await new_labels(t, ids["vodafone"], "5GB\n10GB\n20GB\n50GB\n100GB")
        calls = t.tg.calls[-20:]
        names = [r["name"] for r in t.db.children(ids["vodafone"])]
        assert names == ["5GB", "10GB", "20GB", "50GB", "100GB"]
        note = t.last_text(calls)
        assert "✅ Added: <b>5GB</b>, <b>10GB</b>, <b>20GB</b>, <b>50GB</b>, <b>100GB</b>" in note
        assert "📂 Esim › Oman › Vodafone" in note
        for name in names:
            assert f"{name} · no price" in button_texts(calls)
        # Each new label is a product of Vodafone.
        for row in t.db.children(ids["vodafone"]):
            assert row["parent_id"] == ids["vodafone"] and row["price"] is None
            assert not t.db.has_children(row["id"])

    run(tmp_path, scenario)


def test_duplicate_label_names_case_insensitive_skipped(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        await new_labels(t, 0, "Esim")
        calls = await new_labels(t, 0, "esim\nESIM \nVodafone\nvodafone")
        assert [r["name"] for r in t.db.children(None)] == ["Esim", "Vodafone"]
        note = t.last_text(calls)
        assert "✅ Added: <b>Vodafone</b>" in note
        assert "ℹ️ Already existed: esim, ESIM, vodafone" in note
        # Only duplicates were sent: nothing is created, just the notice.
        calls = await new_labels(t, 0, "ESIM")
        assert "Already existed: ESIM" in t.last_text(calls) and "Added" not in t.last_text(calls)
        assert len(t.db.children(None)) == 2
        # The same name is fine under another parent.
        esim = label_by_path(t, "Esim")["id"]
        voda = label_by_path(t, "Vodafone")["id"]
        await new_labels(t, esim, "5GB")
        calls = await new_labels(t, voda, "5gb")
        assert "✅ Added: <b>5gb</b>" in t.last_text(calls)
        assert [r["name"] for r in t.db.children(esim)] == ["5GB"]
        assert [r["name"] for r in t.db.children(voda)] == ["5gb"]

    run(tmp_path, scenario)


def test_label_names_whitespace_cleaned_and_blank_rejected(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        calls = await new_labels(t, 0, "   \n  ")
        assert "Send at least one label name." in t.last_text(calls)
        assert t.db.children(None) == []
        # Still waiting for the names: the next message creates them.
        calls = await t.send(OWNER, "  Oman    Tel  \n\n   \n Vodafone ")
        assert [r["name"] for r in t.db.children(None)] == ["Oman Tel", "Vodafone"]
        assert "✅ Added: <b>Oman Tel</b>, <b>Vodafone</b>" in t.last_text(calls)
        # A photo instead of a name is refused.
        await t.press(OWNER, "lb:0:0")
        calls = await t.press(OWNER, "ln:0")
        calls = await t.send_photo(OWNER, "some-photo")
        assert "Please send this as a text message." in t.last_text(calls)
        assert len(t.db.children(None)) == 2

    run(tmp_path, scenario)


def test_label_name_over_40_chars_rejected(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        long_name = "X" * 41
        calls = await new_labels(t, 0, long_name)
        assert "too long" in t.last_text(calls) and "at most 40 characters" in t.last_text(calls)
        assert t.db.children(None) == []
        # One bad name in a batch: nothing from the batch is created.
        calls = await t.send(OWNER, f"Good\n{long_name}")
        assert "too long" in t.last_text(calls)
        assert t.db.children(None) == []
        # Exactly 40 characters is fine (the prompt is still waiting).
        calls = await t.send(OWNER, "Y" * 40)
        assert [r["name"] for r in t.db.children(None)] == ["Y" * 40]
        assert "✅ Added" in t.last_text(calls)

    run(tmp_path, scenario)


def test_product_label_with_stock_cannot_get_children(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await add_products(t, OWNER, ids["five"], ["CODE-1"])
        calls = await t.press(OWNER, f"lb:{ids['five']}:0")
        text = t.last_text(calls)
        assert "In stock: <b>1</b>" in text
        assert "This is a product with stock, so it can't contain other labels." in text
        assert f"ln:{ids['five']}" not in datas(calls)
        assert not t.has_button(calls, "New label")
        assert {f"lp:{ids['five']}", f"lr:{ids['five']}", f"ld:{ids['five']}"} <= set(datas(calls))
        # Pressing an old/crafted "New label inside 5GB" is refused.
        calls = await t.press(OWNER, f"ln:{ids['five']}")
        assert_alert(calls, "This product has stock, so it can't contain labels.")
        calls = await t.send(OWNER, "Mini")
        assert NO_STATE_REPLY in t.last_text(calls)
        assert t.db.children(ids["five"]) == []

    run(tmp_path, scenario)


def test_new_label_refused_when_stock_arrives_while_typing(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await make_admin(t, ADMIN)
        calls = await t.press(OWNER, f"lb:{ids['five']}:0")
        calls = await t.press(OWNER, t.button(calls, "New label inside 5GB"))
        assert "Send the label name" in t.last_text(calls)
        await add_products(t, ADMIN, ids["five"], ["ADMIN-CODE"])
        calls = await t.send(OWNER, "Mini")
        assert "already has 1 product(s) in stock" in t.last_text(calls)
        assert t.db.children(ids["five"]) == []
        assert t.db.count_items(ids["five"]) == 1

    run(tmp_path, scenario)


def test_set_price_only_on_last_label(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t, price=None)
        for category in ("esim", "oman", "omantel"):
            calls = await t.press(OWNER, f"lb:{ids[category]}:0")
            assert f"lp:{ids[category]}" not in datas(calls)
            assert not t.has_button(calls, "Set price")
        calls = await t.press(OWNER, f"lp:{ids['omantel']}")
        assert_alert(calls, "Only the last label (the product) has a price.")
        calls = await t.send(OWNER, "5")
        assert NO_STATE_REPLY in t.last_text(calls)
        assert raw_label(t, ids["omantel"])["price"] is None
        # The last labels do have it.
        for product in ("five", "ten", "vodafone"):
            calls = await t.press(OWNER, f"lb:{ids[product]}:0")
            assert f"lp:{ids[product]}" in datas(calls)

    run(tmp_path, scenario)


@pytest.mark.parametrize(
    "text, cents",
    [("9", 900), ("9.5", 950), ("9,50", 950), ("9,5", 950), ("$9", 900), ("12.99$", 1299),
     ("0.5", 50)],
)
def test_price_formats_accepted(tmp_path, text, cents):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        await new_labels(t, 0, "Netflix")
        label = label_by_path(t, "Netflix")["id"]
        calls = await set_price(t, label, text)
        shown = f"{cents // 100}.{cents % 100:02d}$"
        assert f"✅ Price of <b>Netflix</b> set to <b>{shown}</b>." in t.last_text(calls)
        assert f"Price: <b>{shown}</b>" in t.last_text(calls)
        assert raw_label(t, label)["price"] == cents

    run(tmp_path, scenario)


@pytest.mark.parametrize("text", ["0", "abc", "-5", "0.00", "9.999", "1,000"])
def test_price_invalid_inputs_rejected(tmp_path, text):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        await new_labels(t, 0, "Netflix")
        label = label_by_path(t, "Netflix")["id"]
        calls = await set_price(t, label, text)
        assert PRICE_HINT in t.last_text(calls)
        assert raw_label(t, label)["price"] is None
        # The bot keeps waiting for a valid price.
        calls = await t.send(OWNER, "7")
        assert "set to <b>7.00$</b>" in t.last_text(calls)
        assert raw_label(t, label)["price"] == 700

    run(tmp_path, scenario)


def test_change_existing_price(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        calls = await t.press(OWNER, f"lb:{ids['five']}:0")
        assert "Price: <b>9.50$</b>" in t.last_text(calls)
        calls = await t.press(OWNER, t.button(calls, "Set price"))
        assert "Current price: <b>9.50$</b>" in t.last_text(calls)
        calls = await t.send(OWNER, "11")
        assert raw_label(t, ids["five"])["price"] == 1100
        # The tree button shows the new price.
        calls = await t.press(OWNER, f"lb:{ids['omantel']}:0")
        assert "5GB · 11.00$" in button_texts(calls)
        # Cancel leaves the price alone.
        calls = await t.press(OWNER, f"lp:{ids['five']}")
        calls = await t.press(OWNER, t.button(calls, "Cancel"))
        calls = await t.send(OWNER, "1")
        assert NO_STATE_REPLY in t.last_text(calls)
        assert raw_label(t, ids["five"])["price"] == 1100

    run(tmp_path, scenario)


def test_rename_label(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        calls = await t.press(OWNER, f"lb:{ids['omantel']}:0")
        calls = await t.press(OWNER, t.button(calls, "Rename"))
        assert "Send the new name for <b>Omantel</b>" in t.last_text(calls)
        calls = await t.send(OWNER, "  Omantel   5G ")
        assert "✅ Renamed to <b>Omantel 5G</b>." in t.last_text(calls)
        assert raw_label(t, ids["omantel"])["name"] == "Omantel 5G"
        assert "📂 Esim › Oman › Omantel 5G" in t.last_text(calls)
        # Renaming itself with another case is fine.
        await t.press(OWNER, f"lr:{ids['five']}")
        calls = await t.send(OWNER, "5gb")
        assert raw_label(t, ids["five"])["name"] == "5gb"
        # Everyone sees the new path.
        calls = await t.press(ALICE, f"sh:{ids['five']}:0")
        assert "📂 Esim › Oman › Omantel 5G › 5gb" in t.last_text(calls)

    run(tmp_path, scenario)


def test_rename_to_duplicate_sibling_rejected(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        calls = await t.press(OWNER, f"lb:{ids['ten']}:0")
        calls = await t.press(OWNER, t.button(calls, "Rename"))
        calls = await t.send(OWNER, "5gb")
        assert "already exists there" in t.last_text(calls)
        assert raw_label(t, ids["ten"])["name"] == "10GB"
        # Still waiting for a name.
        calls = await t.send(OWNER, "20GB")
        assert "✅ Renamed to <b>20GB</b>." in t.last_text(calls)
        assert raw_label(t, ids["ten"])["name"] == "20GB"
        # A name used under ANOTHER parent is allowed.
        await t.press(OWNER, f"lr:{ids['vodafone']}")
        calls = await t.send(OWNER, "Esim")
        assert raw_label(t, ids["vodafone"])["name"] == "Esim"

    run(tmp_path, scenario)


def test_rename_too_long_or_blank_rejected(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"lr:{ids['oman']}")
        calls = await t.send(OWNER, "Z" * 41)
        assert "too long" in t.last_text(calls)
        calls = await t.send(OWNER, "   ")
        assert "Send a name for the label." in t.last_text(calls)
        assert raw_label(t, ids["oman"])["name"] == "Oman"

    run(tmp_path, scenario)


def test_delete_label_asks_for_confirmation(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        calls = await t.press(OWNER, f"lb:{ids['omantel']}:0")
        calls = await t.press(OWNER, t.button(calls, "Delete"))
        text = t.last_text(calls)
        assert "🗑 Delete <b>Esim › Oman › Omantel</b> and the 2 label(s) inside it?" in text
        assert "Past sales stay in the sales analysis." in text
        assert f"ldy:{ids['omantel']}" in datas(calls)
        assert raw_label(t, ids["omantel"])["deleted"] == 0
        # Back keeps everything.
        calls = await t.press(OWNER, t.button(calls, "Back"))
        assert "📂 Esim › Oman › Omantel" in t.last_text(calls)
        assert raw_label(t, ids["omantel"])["deleted"] == 0
        # Yes deletes the label and everything inside it.
        calls = await t.press(OWNER, f"ld:{ids['omantel']}")
        calls = await t.press(OWNER, t.button(calls, "Yes, delete"))
        assert last_answer(calls).get("text") == "🗑 Deleted"
        text = t.last_text(calls)
        assert "🗑 Deleted <b>Omantel</b>." in text and "📂 Esim › Oman" in text
        assert not t.has_button(calls, "Omantel")
        for key in ("omantel", "five", "ten"):
            assert raw_label(t, ids[key])["deleted"] == 1
        # A second press on the same "Yes" button.
        calls = await t.press(OWNER, f"ldy:{ids['omantel']}")
        assert_alert(calls, "This label was already deleted.")

    run(tmp_path, scenario)


def test_delete_refused_while_stock_inside(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await add_products(t, OWNER, ids["five"], ["CODE-1", "CODE-2"])
        for key in ("esim", "oman", "omantel", "five"):
            calls = await t.press(OWNER, f"ld:{ids[key]}")
            assert_alert(calls, "There are 2 product(s) in stock inside this label.")
            assert raw_label(t, ids[key])["deleted"] == 0
        # Confirmation was shown while empty, then stock arrived before "Yes".
        await set_price(t, ids["ten"], "12")
        calls = await t.press(OWNER, f"ld:{ids['ten']}")
        assert "Delete <b>Esim › Oman › Omantel › 10GB</b>?" in t.last_text(calls)
        await make_admin(t, ADMIN)
        await add_products(t, ADMIN, ids["ten"], ["ADMIN-CODE"])
        calls = await t.press(OWNER, f"ldy:{ids['ten']}")
        assert_alert(calls, "still 1 product(s) in stock")
        assert raw_label(t, ids["ten"])["deleted"] == 0
        assert t.db.count_items(ids["ten"]) == 1

    run(tmp_path, scenario)


def test_deleted_label_hidden_from_shop_tree_and_stock_screens(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await new_labels(t, ids["vodafone"], "1GB")
        one_gb = label_by_path(t, "Esim", "Oman", "Vodafone", "1GB")["id"]
        await set_price(t, one_gb, "3")
        await t.press(OWNER, f"ld:{ids['omantel']}")
        await t.press(OWNER, f"ldy:{ids['omantel']}")
        # Soft delete: rows stay in the database, flagged.
        assert raw_label(t, ids["omantel"]) is not None
        assert raw_label(t, ids["omantel"])["deleted"] == 1
        assert raw_label(t, ids["five"])["deleted"] == 1
        assert [r["name"] for r in t.db.children(ids["oman"])] == ["Vodafone"]
        # Add Label tree.
        calls = await t.press(OWNER, f"lb:{ids['oman']}:0")
        assert not t.has_button(calls, "Omantel")
        calls = await t.press(OWNER, f"lb:{ids['omantel']}:0")
        assert "This label no longer exists." in t.last_text(calls)
        # Shop, for customers and for the owner.
        for user in (ALICE, OWNER):
            calls = await t.press(user, f"sh:{ids['oman']}:0")
            assert t.has_button(calls, "Vodafone") and not t.has_button(calls, "Omantel")
            calls = await t.press(user, f"sh:{ids['five']}:0")
            assert "no longer exists" in t.last_text(calls)
            assert not any(d.startswith("sb:") for d in datas(calls))
        # Add Product and My Products.
        calls = await t.press(OWNER, f"ap:{ids['oman']}:0")
        assert not t.has_button(calls, "Omantel")
        calls = await t.press(OWNER, f"ap:{ids['five']}:0")
        assert_alert(calls, "This label no longer exists.")
        calls = await t.press(OWNER, f"mp:{ids['oman']}:0")
        assert not t.has_button(calls, "Omantel")
        calls = await t.press(OWNER, f"mp:{ids['five']}:0")
        assert "This label no longer exists." in t.last_text(calls)
        # A label with the same name can be created again (a new, separate label).
        calls = await new_labels(t, ids["oman"], "Omantel")
        assert "✅ Added: <b>Omantel</b>" in t.last_text(calls)
        assert label_by_path(t, "Esim", "Oman", "Omantel")["id"] != ids["omantel"]

    run(tmp_path, scenario)


def test_deleted_label_past_sales_stay_in_analysis(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        codes = await add_products(t, OWNER, ids["five"], ["SOLD-ONE", "LEFT-ONE"])
        await give_balance(t, ALICE, 20)
        await buy(t, ALICE, ids["five"], 1)
        # Remove what is left, then delete the label.
        unsold = [r for r in items_of(t, ids["five"]) if r["status"] == "available"][0]
        calls = await t.press(OWNER, f"mr:{unsold['id']}")
        await t.press(OWNER, t.button(calls, "Yes, remove"))
        await t.press(OWNER, f"ld:{ids['five']}")
        await t.press(OWNER, f"ldy:{ids['five']}")
        assert raw_label(t, ids["five"])["deleted"] == 1

        calls = await t.press(OWNER, "mp:0:0")
        calls = await t.press(OWNER, t.button(calls, "Download sales analysis"))
        docs = t.documents(calls)
        assert len(docs) == 1
        rows = csv_rows(docs[0])
        assert rows[1] == ["Esim > Oman > Omantel > 5GB (deleted)", "Owner", str(OWNER_ID),
                           "9.50", "1", "0", "9.50", "1", "0"]
        assert ["All products", "EVERYONE", "", "", "1", "0", "9.50", "1", "0"] in rows
        assert codes and t.db.count_items(ids["five"]) == 0
        # The buyer still has the order.
        calls = await t.press(ALICE, "mo:0")
        assert t.has_button(calls, "5GB ×1")

    run(tmp_path, scenario)


def test_label_management_is_owner_only(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await make_admin(t, ADMIN)
        await t.send(ALICE, "/start")
        five = ids["five"]
        for user in (ADMIN, ALICE):
            for data in ("lb:0:0", f"lb:{five}:0", "ln:0", f"ln:{ids['esim']}", f"lp:{five}",
                         f"lr:{five}", f"ld:{five}", f"ldy:{five}"):
                calls = await t.press(user, data)
                assert_alert(calls, OWNER_ONLY)
                assert not t.messages(calls), (data, t.texts(calls))
            calls = await t.send(user, "Hacked")
            assert NO_STATE_REPLY in t.last_text(calls)
        assert raw_label(t, five)["deleted"] == 0 and raw_label(t, five)["price"] == 950
        assert raw_label(t, five)["name"] == "5GB"
        assert [r["name"] for r in t.db.children(None)] == ["Esim"]

    run(tmp_path, scenario)


def test_label_names_with_html_are_escaped(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        calls = await new_labels(t, 0, "Tom & Jerry <VIP>")
        assert "✅ Added: <b>Tom &amp; Jerry &lt;VIP&gt;</b>" in t.last_text(calls)
        assert "Tom & Jerry <VIP> · no price" in button_texts(calls)
        label = label_by_path(t, "Tom & Jerry <VIP>")["id"]
        calls = await t.press(OWNER, f"lb:{label}:0")
        assert "📂 Tom &amp; Jerry &lt;VIP&gt;" in t.last_text(calls)
        calls = await set_price(t, label, "2")
        assert "Price of <b>Tom &amp; Jerry &lt;VIP&gt;</b>" in t.last_text(calls)
        calls = await t.press(OWNER, f"ap:{label}:0")
        assert "Product: <b>Tom &amp; Jerry &lt;VIP&gt;</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_label_tree_pagination(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        names = [f"L{i:02d}" for i in range(1, 32)]
        calls = await new_labels(t, 0, "\n".join(names))
        assert [r["name"] for r in t.db.children(None)] == names
        shown = [x for x in button_texts(calls) if x.endswith("· no price")]
        assert len(shown) == 30 and "L31 · no price" not in shown
        assert "1/2" in button_texts(calls)
        calls = await t.press(OWNER, t.button(calls, "▶️"))
        shown = [x for x in button_texts(calls) if x.endswith("· no price")]
        assert shown == ["L31 · no price"]
        assert "2/2" in button_texts(calls) and "◀️" in button_texts(calls)

    run(tmp_path, scenario)


# =================================================================== Add Product
def test_add_product_browse_to_last_label_shows_prompt(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await esim_tree(t)
        calls = await t.send(OWNER, "/start")
        calls = await t.press(OWNER, t.button(calls, "Add Product"))
        assert "Choose the product to add stock to" in t.last_text(calls)
        assert t.button(calls, "Back") == "menu"
        calls = await t.press(OWNER, t.button(calls, "📂 Esim"))
        calls = await t.press(OWNER, t.button(calls, "📂 Oman"))
        assert "📂 Esim › Oman" in t.last_text(calls)
        calls = await t.press(OWNER, t.button(calls, "📂 Omantel"))
        assert "🛒 5GB · 0 in stock" in button_texts(calls)
        assert "🛒 10GB · no price" in button_texts(calls)
        # Back goes one level up.
        back = await t.press(OWNER, t.button(calls, "Back"))
        assert "📂 Esim › Oman" in t.last_text(back) and "📂 Omantel" in button_texts(back)

        calls = await t.press(OWNER, t.button(calls, "5GB"))
        text = t.last_text(calls)
        assert text == (
            "❗ <b>SEND THE PRODUCTS — ONE MESSAGE PER PRODUCT</b>\n"
            "Each message is delivered to the buyer exactly as you send it.\n\n"
            "📦 Product: <b>Esim › Oman › Omantel › 5GB</b>\n💵 Price: <b>9.50$</b>\n\n"
            "Press <b>Done</b> when finished."
        )
        buttons = t.all_buttons(calls)
        assert [(b["text"], b["callback_data"], b.get("style")) for b in buttons] == [
            ("✅ Done", "apd", "success"), ("❌ Cancel", "apc", "danger")]
        assert t.app.user_data[OWNER_ID]["state"]["name"] == "add_items"

    run(tmp_path, scenario)


def test_owner_asked_for_price_first_then_add_mode_starts(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        calls = await t.press(OWNER, f"ap:{ids['omantel']}:0")
        calls = await t.press(OWNER, t.button(calls, "10GB · no price"))
        text = t.last_text(calls)
        assert "💲 <b>Price for 10GB</b>" in text and "⚠️ This product has no price yet." in text
        assert ADD_PROMPT_TITLE not in text
        calls = await t.send(OWNER, "abc")
        assert PRICE_HINT in t.last_text(calls)
        calls = await t.send(OWNER, "12")
        text = t.last_text(calls)
        assert text.startswith("✅ Price of <b>10GB</b> set to <b>12.00$</b>.")
        assert ADD_PROMPT_TITLE in text and "💵 Price: <b>12.00$</b>" in text
        assert datas(calls) == ["apd", "apc"]
        assert raw_label(t, ids["ten"])["price"] == 1200
        await t.send(OWNER, "TEN-GB-CODE")
        calls = await t.press(OWNER, "apd")
        assert "Saved 1 product(s)" in t.last_text(calls)
        assert [content(r)["text"] for r in items_of(t, ids["ten"])] == ["TEN-GB-CODE"]

    run(tmp_path, scenario)


def test_owner_price_prompt_cancel_from_add_product(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        calls = await t.press(OWNER, f"ap:{ids['ten']}:0")
        assert "Price for 10GB" in t.last_text(calls)
        calls = await t.press(OWNER, t.button(calls, "Cancel"))
        assert "❌ Cancelled." in t.last_text(calls)
        calls = await t.send(OWNER, "12")
        assert NO_STATE_REPLY in t.last_text(calls)
        assert raw_label(t, ids["ten"])["price"] is None
        assert t.db.count_items(ids["ten"]) == 0

    run(tmp_path, scenario)


def test_admin_blocked_when_product_has_no_price(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await make_admin(t, ADMIN)
        calls = await t.send(ADMIN, "/start")
        calls = await t.press(ADMIN, t.button(calls, "Add Product"))
        for name in ("Esim", "Oman", "Omantel"):
            calls = await t.press(ADMIN, t.button(calls, f"📂 {name}"))
        assert "🛒 10GB · no price" in button_texts(calls)
        calls = await t.press(ADMIN, t.button(calls, "10GB"))
        assert_alert(calls, "This product has no price yet. Ask the owner to set one first.")
        assert not t.messages(calls)
        assert "state" not in t.app.user_data[ADMIN["id"]]
        calls = await t.send(ADMIN, "SNEAKY-CODE")
        assert NO_STATE_REPLY in t.last_text(calls)
        # The "➕ Add products" button in My Products is blocked the same way.
        calls = await t.press(ADMIN, f"mp:{ids['ten']}:0")
        calls = await t.press(ADMIN, t.button(calls, "Add products"))
        assert_alert(calls, "no price yet")
        assert t.db.count_items(ids["ten"]) == 0
        assert raw_label(t, ids["ten"])["price"] is None

    run(tmp_path, scenario)


def test_admin_adds_products_to_priced_label(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await make_admin(t, ADMIN)
        calls = await t.send(ADMIN, "/start")
        calls = await t.press(ADMIN, t.button(calls, "Add Product"))
        for name in ("Esim", "Oman", "Omantel"):
            calls = await t.press(ADMIN, t.button(calls, f"📂 {name}"))
        calls = await t.press(ADMIN, t.button(calls, "5GB"))
        assert ADD_PROMPT_TITLE in t.last_text(calls)
        await t.send(ADMIN, "ADMIN-1")
        await t.send(ADMIN, "ADMIN-2")
        calls = await t.press(ADMIN, "apd")
        assert "Saved 2 product(s)" in t.last_text(calls)
        rows = items_of(t, ids["five"])
        assert [r["owner_id"] for r in rows] == [ADMIN["id"], ADMIN["id"]]
        assert [content(r)["text"] for r in rows] == ["ADMIN-1", "ADMIN-2"]
        assert all(r["status"] == "available" for r in rows)

    run(tmp_path, scenario)


def test_text_product_keeps_entities_in_purchase_and_view(tmp_path):
    async def scenario(t: ShopTest) -> None:
        # The sample is self-consistent.
        assert [TEXT[e["offset"]: e["offset"] + e["length"]] for e in ENTITIES] == [
            "eSIM code", "ABCD-1234", "activate here", "9876"]
        ids = await esim_tree(t)
        await add_products(t, OWNER, ids["five"], [{"text": TEXT, "entities": ENTITIES}, "PLAIN-CODE"])
        stored = [content(r) for r in items_of(t, ids["five"])]
        assert stored[0] == {"kind": "text", "text": TEXT, "entities": ENTITIES}
        assert stored[1] == {"kind": "text", "text": "PLAIN-CODE", "entities": []}

        # Viewing it in My Products re-sends it exactly.
        first = items_of(t, ids["five"])[0]
        calls = await t.press(OWNER, f"mv:{first['id']}")
        assert_text_resent(sent_to(calls, OWNER_ID)[0])

        # Buying delivers it exactly.
        await give_balance(t, ALICE, 50)
        calls = await buy(t, ALICE, ids["five"], 2)
        delivered = [c for c in sent_to(calls, ALICE["id"], {"sendMessage"})
                     if c.params["text"] in (TEXT, "PLAIN-CODE")]
        assert len(delivered) == 2
        assert_text_resent(delivered[0])
        assert delivered[1].params["text"] == "PLAIN-CODE"
        assert not jparam(delivered[1], "entities")
        assert "parse_mode" not in delivered[1].params

    run(tmp_path, scenario)


def test_photo_product_keeps_caption_and_largest_file(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        caption_entities = [{"type": "bold", "offset": 0, "length": 4}]
        product = photo_msg("qr-photo-1", "Scan this QR", caption_entities,
                            has_media_spoiler=True, show_caption_above_media=True)
        await add_products(t, OWNER, ids["five"], [product, photo_msg("plain-photo", caption=None)])
        rows = items_of(t, ids["five"])
        assert content(rows[0]) == {
            "kind": "photo", "file_id": "qr-photo-1", "caption": "Scan this QR",
            "caption_entities": caption_entities, "has_spoiler": True,
            "show_caption_above_media": True,
        }
        assert content(rows[1])["file_id"] == "plain-photo" and content(rows[1])["caption"] is None

        def check(call: Any) -> None:
            assert call.method == "sendPhoto"
            assert call.params["photo"] == "qr-photo-1"
            assert call.params["caption"] == "Scan this QR"
            assert jparam(call, "caption_entities") == caption_entities
            assert call.params.get("has_spoiler") is True
            assert call.params.get("show_caption_above_media") is True
            assert "parse_mode" not in call.params

        calls = await t.press(OWNER, f"mv:{rows[0]['id']}")
        check(sent_to(calls, OWNER_ID, {"sendPhoto"})[0])
        await give_balance(t, ALICE, 50)
        calls = await buy(t, ALICE, ids["five"], 2)
        photos = sent_to(calls, ALICE["id"], {"sendPhoto"})
        assert len(photos) == 2
        check(photos[0])
        assert photos[1].params["photo"] == "plain-photo" and "caption" not in photos[1].params

    run(tmp_path, scenario)


def test_every_media_kind_saved_and_delivered_on_purchase(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        samples = media_samples()
        await add_products(t, OWNER, ids["five"], [s["msg"] for s in samples])
        rows = items_of(t, ids["five"])
        assert [content(r)["kind"] for r in rows] == [s["kind"] for s in samples]
        for row, sample in zip(rows, samples):
            stored = content(row)
            assert stored["file_id"] == sample["file_id"], sample["kind"]
            if sample["kind"] not in ("sticker", "video_note"):
                assert stored["caption"] == sample["caption"]
                assert stored["caption_entities"] == sample["caption_entities"]

        await give_balance(t, ALICE, 100)
        calls = await buy(t, ALICE, ids["five"], len(samples))
        methods = {s["method"] for s in samples}
        delivered = sent_to(calls, ALICE["id"], methods)
        assert len(delivered) == len(samples)
        for call, sample in zip(delivered, samples):
            assert_resent(call, sample)
        assert "Delivered 7 product(s)" in all_text(calls, ALICE["id"])
        # Re-sent from the stored content, never forwarded/copied from the seller's chat.
        assert not [c for c in t.tg.calls if c.method in ("forwardMessage", "copyMessage",
                                                         "forwardMessages", "copyMessages")]
        assert all(r["status"] == "sold" and r["buyer_id"] == ALICE["id"]
                   for r in items_of(t, ids["five"]))

    run(tmp_path, scenario)


def test_every_media_kind_resent_when_viewed(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        samples = media_samples()
        await add_products(t, OWNER, ids["five"], [s["msg"] for s in samples])
        calls = await open_items(t, OWNER, ids["five"])
        buttons = item_buttons(calls)
        assert len(buttons) == len(samples)
        for button, sample in zip(buttons, samples):
            calls = await t.press(OWNER, button["callback_data"])
            sent = sent_to(calls, OWNER_ID)
            assert len(sent) == 2, [c.method for c in sent]
            assert_resent(sent[0], sample)
            assert sent[1].method == "sendMessage" and "🆔 Product ID" in sent[1].params["text"]

    run(tmp_path, scenario)


def test_unsupported_messages_rejected_with_hint(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        unsupported = [
            {"location": {"latitude": 23.6, "longitude": 58.5}},
            {"contact": {"phone_number": "+96812345678", "first_name": "Ali"}},
            {"poll": {"id": "p1", "question": "Which plan?", "options": [
                {"persistent_id": "o1", "text": "5GB", "voter_count": 0},
                {"persistent_id": "o2", "text": "10GB", "voter_count": 0}],
                "total_voter_count": 0, "is_closed": False, "is_anonymous": True,
                "type": "regular", "allows_multiple_answers": False,
                "allows_revoting": True, "members_only": False}},
            {"venue": {"location": {"latitude": 23.6, "longitude": 58.5}, "title": "Shop",
                       "address": "Muscat"},
             "location": {"latitude": 23.6, "longitude": 58.5}},
            {"dice": {"emoji": "🎲", "value": 3}},
        ]
        for message in unsupported:
            calls = await t.send(OWNER, **message)
            text = t.last_text(calls)
            assert UNSUPPORTED_HINT in text, message
            assert "Send text, a photo, video, GIF, file, audio, voice message or sticker." in text
            assert "Received" not in all_text(calls)
        assert t.app.user_data[OWNER_ID]["state"]["items"] == []
        # Still in add mode: a real product is accepted afterwards.
        calls = await t.send(OWNER, "REAL-CODE")
        assert "Received <b>1</b> product(s)" in t.last_text(calls)
        calls = await t.press(OWNER, "apd")
        assert "Saved 1 product(s)" in t.last_text(calls)
        assert [content(r)["text"] for r in items_of(t, ids["five"])] == ["REAL-CODE"]

    run(tmp_path, scenario)


def test_done_without_products_alerts(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        calls = await t.press(OWNER, "apd")
        assert_alert(calls, "Send at least one product message first.")
        assert not t.messages(calls)
        assert t.db.count_items(ids["five"]) == 0
        # Add mode is still on.
        await t.send(OWNER, "CODE-1")
        calls = await t.press(OWNER, "apd")
        assert "Saved 1 product(s)" in t.last_text(calls)

    run(tmp_path, scenario)


def test_done_saves_and_shows_unique_product_ids(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        for code in ("A-1", "A-2", "A-3"):
            await t.send(OWNER, code)
        calls = await t.press(OWNER, "apd")
        assert last_answer(calls).get("text") == "✅ Saved 3"
        text = t.last_text(calls)
        rows = items_of(t, ids["five"])
        codes = [r["code"] for r in rows]
        assert len(codes) == 3 and len(set(codes)) == 3
        for code in codes:
            assert len(code) == CODE_LENGTH and set(code) <= set(CODE_ALPHABET)
            assert f"<code>{code}</code>" in text
        assert "✅ <b>Saved 3 product(s)</b>" in text
        assert "📂 Esim › Oman › Omantel › 5GB" in text and "💵 Price: 9.50$" in text
        assert "📦 Now in stock: <b>3</b>" in text
        assert [r["owner_id"] for r in rows] == [OWNER_ID] * 3
        assert [content(r)["text"] for r in rows] == ["A-1", "A-2", "A-3"]
        assert f"ap:{ids['five']}:0" in datas(calls) and f"mi:{ids['five']}:0" in datas(calls)
        assert "state" not in t.app.user_data[OWNER_ID]

        # "Add more" starts a new batch; all IDs stay unique.
        calls = await t.press(OWNER, t.button(calls, "Add more"))
        assert ADD_PROMPT_TITLE in t.last_text(calls)
        for code in ("B-1", "B-2", "B-3"):
            await t.send(OWNER, code)
        calls = await t.press(OWNER, "apd")
        assert "📦 Now in stock: <b>6</b>" in t.last_text(calls)
        all_codes = [r["code"] for r in items_of(t, ids["five"])]
        assert len(set(all_codes)) == 6
        # The "My Products" button lists them.
        calls = await t.press(OWNER, t.button(calls, "My Products"))
        assert sorted(b["text"].removeprefix("🆔 ") for b in item_buttons(calls)) == sorted(all_codes)

    run(tmp_path, scenario)


def test_cancel_discards_products(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        await t.send(OWNER, "CODE-1")
        await t.send_photo(OWNER, "qr-1", caption="QR")
        status_id = status_msg_id(t, OWNER)
        calls = await t.press(OWNER, "apc")
        assert last_answer(calls).get("text") == "Cancelled"
        assert "❌ Cancelled. 2 product(s) were not saved." in t.last_text(calls)
        assert {"chat_id": OWNER_ID, "message_id": status_id} in [
            c.params for c in calls if c.method == "deleteMessage"]
        assert t.db.count_items(ids["five"]) == 0 and items_of(t, ids["five"]) == []
        calls = await t.send(OWNER, "CODE-2")
        assert NO_STATE_REPLY in t.last_text(calls)
        calls = await t.press(OWNER, "apd")
        assert_alert(calls, "There is nothing to save.")
        assert items_of(t, ids["five"]) == []

    run(tmp_path, scenario)


@pytest.mark.parametrize(
    "leave", ["menu", "sh:0:0", "mp:0:0", "ap:0:0", "lb:0:0", "ea", "/start", "/help", "/cancel"]
)
def test_leaving_add_product_discards_with_warning(tmp_path, leave):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        await t.send(OWNER, "CODE-1")
        await t.send(OWNER, "CODE-2")
        if leave.startswith("/"):
            calls = await t.send(OWNER, leave)
        else:
            calls = await t.press(OWNER, leave)
        warning = "⚠️ Add Product stopped — 2 unsaved product(s) were discarded."
        assert warning in t.texts(calls, OWNER_ID)
        assert t.db.count_items(ids["five"]) == 0
        calls = await t.press(OWNER, "apd")
        assert_alert(calls, "There is nothing to save.")
        assert items_of(t, ids["five"]) == []

    run(tmp_path, scenario)


def test_leaving_add_product_without_products_has_no_warning(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        calls = await t.press(OWNER, "menu")
        assert not any("Add Product stopped" in x for x in t.texts(calls))
        assert "Welcome Owner" in t.last_text(calls)

    run(tmp_path, scenario)


def test_status_message_shows_count_with_done_cancel(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        calls = await t.send(OWNER, "CODE-1")
        status = [c for c in sent_to(calls, OWNER_ID) if "Received" in (c.text or "")]
        assert len(status) == 1
        assert status[0].text == ("📥 Received <b>1</b> product(s) for <b>5GB</b>.\n"
                                  "Send more, or press <b>Done</b> to save them.")
        assert [(b["callback_data"], b.get("style")) for b in status[0].buttons] == [
            ("apd", "success"), ("apc", "danger")]
        first_id = status_msg_id(t, OWNER)

        calls = await t.send_photo(OWNER, "qr-2", caption="QR")
        assert "Received <b>2</b> product(s)" in t.last_text(calls)
        deleted = [c.params for c in calls if c.method == "deleteMessage"]
        assert deleted == [{"chat_id": OWNER_ID, "message_id": first_id}]
        second_id = status_msg_id(t, OWNER)
        assert second_id != first_id

        # Done on the original prompt removes the leftover status message.
        calls = await t.press(OWNER, "apd", message_id=1)
        assert {"chat_id": OWNER_ID, "message_id": second_id} in [
            c.params for c in calls if c.method == "deleteMessage"]
        assert "Saved 2 product(s)" in t.last_text(calls)

    run(tmp_path, scenario)


def test_done_pressed_on_status_message_edits_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        await t.send(OWNER, "CODE-1")
        status_id = status_msg_id(t, OWNER)
        calls = await t.press(OWNER, "apd", message_id=status_id,
                              message_text="📥 Received 1 product(s) for 5GB.")
        assert not [c for c in calls if c.method == "deleteMessage"]
        edits = [c for c in calls if c.method == "editMessageText"]
        assert edits and edits[-1].params["message_id"] == status_id
        assert "Saved 1 product(s)" in edits[-1].params["text"]
        assert t.db.count_items(ids["five"]) == 1

    run(tmp_path, scenario)


def test_double_done_saves_once(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        await t.send(OWNER, "CODE-1")
        await t.send(OWNER, "CODE-2")
        calls = await t.press(OWNER, "apd")
        assert "Saved 2 product(s)" in t.last_text(calls)
        calls = await t.press(OWNER, "apd")
        assert_alert(calls, "There is nothing to save.")
        assert t.db.count_items(ids["five"]) == 2

    run(tmp_path, scenario)


def test_owner_stays_in_add_mode_while_approving_a_deposit(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        await t.send(OWNER, "CODE-1")
        # A customer's deposit request arrives; the owner approves it mid-way.
        await t.send(ALICE, "/start")
        calls = await t.press(ALICE, "dp")
        calls = await t.press(ALICE, t.button(calls, "10$"))
        calls = await t.send_photo(ALICE, "alice-receipt")
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        to_owner = [c for c in calls if c.chat_id == OWNER_ID and c.method == "sendPhoto"]
        assert to_owner, t.texts(calls)
        calls = await t.press(OWNER, t.button(to_owner, "Approve"), photo=True)
        assert not any("Add Product stopped" in x for x in t.texts(calls))
        assert t.db.get_user(ALICE["id"])["balance"] == 1000
        assert t.app.user_data[OWNER_ID]["state"]["name"] == "add_items"
        await t.send(OWNER, "CODE-2")
        calls = await t.press(OWNER, "apd")
        assert "Saved 2 product(s)" in t.last_text(calls)
        assert [content(r)["text"] for r in items_of(t, ids["five"])] == ["CODE-1", "CODE-2"]

    run(tmp_path, scenario)


def test_edited_product_message_does_not_add_another_product(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        await t.send(OWNER, "CODE-TYPO", message_id=777)
        # The owner fixes a typo by editing the message they just sent.
        await send_edit(t, OWNER, 777, "CODE-FIXED")
        calls = await t.press(OWNER, "apd")
        assert "Saved 1 product(s)" in t.last_text(calls)
        assert t.db.count_items(ids["five"]) == 1

    run(tmp_path, scenario)


def test_edited_command_message_is_not_run_again(tmp_path):
    """Same root cause as the edited product message: edits are handled as new messages."""
    async def scenario(t: ShopTest) -> None:
        await t.send(ALICE, "/start")
        await t.send(OWNER, f"/addbal {ALICE['id']} 10", message_id=555)
        assert t.db.get_user(ALICE["id"])["balance"] == 1000
        # The owner edits the old command message, e.g. to fix the amount.
        await send_edit(t, OWNER, 555, f"/addbal {ALICE['id']} 100")
        assert t.db.get_user(ALICE["id"])["balance"] == 1000
        assert t.db.get_user(ALICE["id"])["total_deposits"] == 1000

    run(tmp_path, scenario)


def test_add_product_not_available_to_customers(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.send(ALICE, "/start")
        for data in ("ap:0:0", f"ap:{ids['omantel']}:0", f"ap:{ids['five']}:0"):
            calls = await t.press(ALICE, data)
            assert_alert(calls, STAFF_ONLY)
            assert not t.messages(calls)
        calls = await t.press(ALICE, "apd")
        assert_alert(calls, "There is nothing to save.")
        calls = await t.send(ALICE, "FAKE-CODE")
        assert NO_STATE_REPLY in t.last_text(calls)
        assert items_of(t, ids["five"]) == []

    run(tmp_path, scenario)


def test_batch_limit_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(stock_module, "MAX_BATCH", 2)

    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await t.press(OWNER, f"ap:{ids['five']}:0")
        await t.send(OWNER, "CODE-1")
        await t.send(OWNER, "CODE-2")
        calls = await t.send(OWNER, "CODE-3")
        assert "You can add up to 2 products at a time. Press Done to save these first." in t.last_text(calls)
        calls = await t.press(OWNER, "apd")
        assert "Saved 2 product(s)" in t.last_text(calls)
        assert [content(r)["text"] for r in items_of(t, ids["five"])] == ["CODE-1", "CODE-2"]

    run(tmp_path, scenario)


def test_done_after_label_deleted_meanwhile_saves_nothing(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await set_price(t, ids["ten"], "12")
        await make_admin(t, ADMIN)
        await t.press(ADMIN, f"ap:{ids['ten']}:0")
        await t.send(ADMIN, "ADMIN-CODE")
        await t.press(OWNER, f"ld:{ids['ten']}")
        await t.press(OWNER, f"ldy:{ids['ten']}")
        calls = await t.press(ADMIN, "apd")
        assert "This product no longer exists, so nothing was saved." in t.last_text(calls)
        assert items_of(t, ids["ten"]) == []
        assert "state" not in t.app.user_data[ADMIN["id"]]

    run(tmp_path, scenario)


def test_done_after_label_became_category_saves_nothing(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await set_price(t, ids["ten"], "12")
        await make_admin(t, ADMIN)
        await t.press(ADMIN, f"ap:{ids['ten']}:0")
        await t.send(ADMIN, "ADMIN-CODE")
        await new_labels(t, ids["ten"], "Weekly")
        calls = await t.press(ADMIN, "apd")
        assert "now contains other labels" in t.last_text(calls)
        assert items_of(t, ids["ten"]) == []

    run(tmp_path, scenario)


# =================================================================== My Products
async def stock_three_sellers(t: ShopTest) -> dict:
    """Owner: 3 × 5GB · Adam: 2 × 5GB · Bella: 1 × 10GB (10GB priced 12$)."""
    ids = await esim_tree(t)
    await set_price(t, ids["ten"], "12")
    await make_admin(t, ADMIN)
    await make_admin(t, ADMIN2)
    ids["owner_codes"] = await add_products(t, OWNER, ids["five"], ["O-1", "O-2", "O-3"])
    ids["adam_codes"] = await add_products(t, ADMIN, ids["five"], ["A-1", "A-2"])
    ids["bella_codes"] = await add_products(t, ADMIN2, ids["ten"], ["B-1"])
    return ids


def test_my_products_owner_sees_all_items(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await stock_three_sellers(t)
        calls = await t.send(OWNER, "/start")
        calls = await t.press(OWNER, t.button(calls, "My Products"))
        assert "In stock: <b>6</b> (yours: 3, admins: 3)" in t.last_text(calls)
        assert "ma" in datas(calls) and t.has_button(calls, "Download sales analysis")
        calls = await t.press(OWNER, t.button(calls, "📂 Esim · 6"))
        assert "In stock here: <b>6</b>" in t.last_text(calls)
        calls = await t.press(OWNER, t.button(calls, "📂 Oman · 6"))
        assert "Vodafone · 0" in button_texts(calls)
        calls = await t.press(OWNER, t.button(calls, "📂 Omantel · 6"))
        assert {"5GB · 5", "10GB · 1"} <= set(button_texts(calls))
        calls = await t.press(OWNER, t.button(calls, "5GB · 5"))
        text = t.last_text(calls)
        assert "📦 <b>5GB</b>" in text and "💵 Price: <b>9.50$</b>" in text
        assert "📦 In stock: <b>5</b> (yours: 3, admins: 2)" in text
        labels = [b["text"] for b in item_buttons(calls)]
        assert labels == [f"🆔 {c}" for c in ids["owner_codes"]] + [
            f"🆔 {c} · Adam" for c in ids["adam_codes"]]
        calls = await t.press(OWNER, f"mp:{ids['ten']}:0")
        assert [b["text"] for b in item_buttons(calls)] == [f"🆔 {ids['bella_codes'][0]} · Bella"]
        assert "(yours: 0, admins: 1)" in t.last_text(calls)

    run(tmp_path, scenario)


def test_my_products_admin_sees_only_own_items(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await stock_three_sellers(t)
        calls = await t.send(ADMIN, "/start")
        calls = await t.press(ADMIN, t.button(calls, "My Products"))
        assert "Your products in stock: <b>2</b>" in t.last_text(calls)
        assert "admins:" not in t.last_text(calls)
        calls = await t.press(ADMIN, t.button(calls, "📂 Esim · 2"))
        assert "In stock here: <b>2</b>" in t.last_text(calls)
        calls = await t.press(ADMIN, t.button(calls, "📂 Oman · 2"))
        calls = await t.press(ADMIN, t.button(calls, "📂 Omantel · 2"))
        assert {"5GB · 2", "10GB · 0"} <= set(button_texts(calls))
        calls = await t.press(ADMIN, t.button(calls, "5GB · 2"))
        assert "📦 Your stock: <b>2</b>" in t.last_text(calls)
        assert [b["text"] for b in item_buttons(calls)] == [f"🆔 {c}" for c in ids["adam_codes"]]
        for code in ids["owner_codes"]:
            assert not t.has_button(calls, code)
        calls = await t.press(ADMIN, f"mp:{ids['ten']}:0")
        assert "📦 Your stock: <b>0</b>" in t.last_text(calls)
        assert "No products in stock here." in t.last_text(calls)
        assert item_buttons(calls) == []
        # Bella sees only hers.
        calls = await t.press(ADMIN2, "mp:0:0")
        assert "Your products in stock: <b>1</b>" in t.last_text(calls)
        calls = await t.press(ADMIN2, f"mi:{ids['five']}:0")
        assert item_buttons(calls) == [] and "Your stock: <b>0</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_my_products_pagination_over_30_items(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await add_products(t, OWNER, ids["five"], [f"CODE-{i:02d}" for i in range(1, 36)])
        codes = [r["code"] for r in items_of(t, ids["five"])]
        assert len(codes) == 35
        calls = await open_items(t, OWNER, ids["five"])
        page1 = [b["text"].removeprefix("🆔 ") for b in item_buttons(calls)]
        assert page1 == codes[:30]
        assert "1/2" in button_texts(calls) and "◀️" not in button_texts(calls)
        assert f"mi:{ids['five']}:1" in datas(calls)
        calls = await t.press(OWNER, t.button(calls, "▶️"))
        page2 = [b["text"].removeprefix("🆔 ") for b in item_buttons(calls)]
        assert page2 == codes[30:]
        assert "2/2" in button_texts(calls) and "▶️" not in button_texts(calls)
        calls = await t.press(OWNER, t.button(calls, "◀️"))
        assert [b["text"].removeprefix("🆔 ") for b in item_buttons(calls)] == codes[:30]
        # A page past the end shows the last page.
        calls = await t.press(OWNER, f"mi:{ids['five']}:9")
        assert [b["text"].removeprefix("🆔 ") for b in item_buttons(calls)] == codes[30:]

    run(tmp_path, scenario)


def test_view_item_sends_content_and_info_with_remove_pull(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await stock_three_sellers(t)
        await add_products(t, OWNER, ids["five"], [{"text": TEXT, "entities": ENTITIES}])
        item = items_of(t, ids["five"])[-1]
        calls = await open_items(t, OWNER, ids["five"])
        calls = await t.press(OWNER, t.button(calls, item["code"]))
        sent = sent_to(calls, OWNER_ID)
        assert len(sent) == 2
        assert_text_resent(sent[0])
        info = sent[1].params["text"]
        assert f"🆔 Product ID: <code>{item['code']}</code>" in info
        assert "📂 Esim › Oman › Omantel › 5GB" in info and "💵 Price: 9.50$" in info
        assert f"👤 Added by: Owner (<code>{OWNER_ID}</code>)" in info
        assert [(b["text"], b["callback_data"]) for b in sent[1].buttons][:2] == [
            ("🗑 Remove", f"mr:{item['id']}"), ("📤 Pull", f"mu:{item['id']}")]
        assert f"mi:{ids['five']}:0" in [b["callback_data"] for b in sent[1].buttons]
        assert t.db.get_item(item["id"])["status"] == "available"
        # An admin's item shows who added it.
        adam_item = [r for r in items_of(t, ids["five"]) if r["owner_id"] == ADMIN["id"]][0]
        calls = await t.press(OWNER, f"mv:{adam_item['id']}")
        assert f"👤 Added by: Adam (<code>{ADMIN['id']}</code>)" in sent_to(calls, OWNER_ID)[-1].params["text"]

    run(tmp_path, scenario)


def test_view_item_when_resend_fails_still_shows_controls(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await add_products(t, OWNER, ids["five"], [photo_msg("broken-photo")])
        item = items_of(t, ids["five"])[0]
        t.tg.fail["sendPhoto"] = "Bad Request: wrong file identifier/HTTP URL specified"
        calls = await t.press(OWNER, f"mv:{item['id']}")
        texts = t.texts(calls, OWNER_ID)
        assert any("Couldn't show this product's message" in x for x in texts)
        assert f"mu:{item['id']}" in datas(calls) and f"mr:{item['id']}" in datas(calls)

    run(tmp_path, scenario)


def test_remove_asks_confirmation_then_marks_removed(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        codes = await add_products(t, OWNER, ids["five"], ["KEEP-1", "DROP-2", "KEEP-3"])
        target = items_of(t, ids["five"])[1]
        calls = await open_items(t, OWNER, ids["five"])
        calls = await t.press(OWNER, t.button(calls, target["code"]))
        calls = await t.press(OWNER, t.button(calls, "Remove"))
        text = t.last_text(calls)
        assert "🗑 <b>Remove this product?</b>" in text and target["code"] in text
        assert f"mry:{target['id']}" in datas(calls)
        assert t.db.get_item(target["id"])["status"] == "available"
        # Back keeps it.
        back = await t.press(OWNER, t.button(calls, "Back"))
        assert t.has_button(back, target["code"])
        assert t.db.get_item(target["id"])["status"] == "available"
        # Yes removes it.
        calls = await t.press(OWNER, t.button(calls, "Yes, remove"))
        assert last_answer(calls).get("text") == "🗑 Removed"
        row = t.db.get_item(target["id"])
        assert row["status"] == "removed" and row["closed_at"] and row["buyer_id"] is None
        text = t.last_text(calls)
        assert f"🗑 Product <code>{target['code']}</code> was removed." in text
        assert "In stock: <b>2</b>" in text
        assert not t.has_button(calls, target["code"])
        assert t.db.count_items(ids["five"]) == 2
        # Customers can't get it any more.
        calls = await t.press(ALICE, f"sh:{ids['five']}:0")
        assert "In stock: <b>2</b>" in t.last_text(calls)
        assert codes[1] == target["code"]

    run(tmp_path, scenario)


def test_pull_marks_pulled_and_sends_content_to_presser(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await add_products(t, OWNER, ids["five"], [photo_msg("qr-pull", "Pulled QR"), "OTHER"])
        target = items_of(t, ids["five"])[0]
        calls = await t.press(OWNER, f"mv:{target['id']}")
        calls = await t.press(OWNER, t.button(calls, "Pull"))
        assert last_answer(calls).get("text") == "📤 Pulled"
        row = t.db.get_item(target["id"])
        assert row["status"] == "pulled" and row["buyer_id"] == OWNER_ID and row["closed_at"]
        texts = t.texts(calls, OWNER_ID)
        assert any(f"Product <code>{target['code']}</code> was pulled from the shop" in x for x in texts)
        assert any("📤 <b>Pulled product</b>" in x and target["code"] in x for x in texts)
        photos = sent_to(calls, OWNER_ID, {"sendPhoto"})
        assert len(photos) == 1
        assert photos[0].params["photo"] == "qr-pull" and photos[0].params["caption"] == "Pulled QR"
        assert t.db.count_items(ids["five"]) == 1
        # Pulling it again is refused.
        calls = await t.press(OWNER, f"mu:{target['id']}")
        assert_alert(calls, GONE)
        assert not sent_to(calls, OWNER_ID, {"sendPhoto"})

    run(tmp_path, scenario)


def test_owner_can_remove_and_pull_admin_items(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await stock_three_sellers(t)
        adam_items = [r for r in items_of(t, ids["five"]) if r["owner_id"] == ADMIN["id"]]
        calls = await t.press(OWNER, f"mu:{adam_items[0]['id']}")
        assert t.db.get_item(adam_items[0]["id"])["status"] == "pulled"
        assert t.db.get_item(adam_items[0]["id"])["buyer_id"] == OWNER_ID
        delivered = [c for c in sent_to(calls, OWNER_ID, {"sendMessage"}) if c.params["text"] == "A-1"]
        assert len(delivered) == 1
        assert not sent_to(calls, ADMIN["id"])
        await t.press(OWNER, f"mr:{adam_items[1]['id']}")
        await t.press(OWNER, f"mry:{adam_items[1]['id']}")
        assert t.db.get_item(adam_items[1]["id"])["status"] == "removed"
        bella_item = items_of(t, ids["ten"])[0]
        calls = await t.press(OWNER, f"mv:{bella_item['id']}")
        assert any(c.params.get("text") == "B-1" for c in sent_to(calls, OWNER_ID))
        # The admin's own view reflects it.
        calls = await t.press(ADMIN, "mp:0:0")
        assert "Your products in stock: <b>0</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_admin_manages_own_items(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await stock_three_sellers(t)
        a1, a2 = [r for r in items_of(t, ids["five"]) if r["owner_id"] == ADMIN["id"]]
        calls = await open_items(t, ADMIN, ids["five"])
        calls = await t.press(ADMIN, t.button(calls, a1["code"]))
        sent = sent_to(calls, ADMIN["id"])
        assert sent[0].params["text"] == "A-1"
        assert "👤 Added by: Adam" in sent[1].params["text"]
        calls = await t.press(ADMIN, t.button(calls, "Remove"))
        calls = await t.press(ADMIN, t.button(calls, "Yes, remove"))
        assert t.db.get_item(a1["id"])["status"] == "removed"
        assert "Your stock: <b>1</b>" in t.last_text(calls)
        calls = await t.press(ADMIN, f"mu:{a2['id']}")
        assert t.db.get_item(a2["id"])["status"] == "pulled"
        assert t.db.get_item(a2["id"])["buyer_id"] == ADMIN["id"]
        assert any(c.params.get("text") == "A-2" for c in sent_to(calls, ADMIN["id"]))
        # The owner's items are untouched.
        assert t.db.count_items(ids["five"], OWNER_ID) == 3

    run(tmp_path, scenario)


def test_admin_cannot_touch_owner_or_other_admin_items(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await stock_three_sellers(t)
        owner_item = [r for r in items_of(t, ids["five"]) if r["owner_id"] == OWNER_ID][0]
        bella_item = items_of(t, ids["ten"])[0]
        for item in (owner_item, bella_item):
            for data in (f"mv:{item['id']}", f"mr:{item['id']}", f"mry:{item['id']}",
                         f"mu:{item['id']}"):
                calls = await t.press(ADMIN, data)
                assert_alert(calls, NOT_YOURS)
                assert not sent_to(calls, ADMIN["id"]), data
                assert not t.messages(calls), data
            assert t.db.get_item(item["id"])["status"] == "available"
            assert t.db.get_item(item["id"])["buyer_id"] is None
        # Listing another seller's product shows none of their codes.
        calls = await t.press(ADMIN, f"mi:{ids['ten']}:0")
        assert item_buttons(calls) == []
        assert not t.has_button(calls, bella_item["code"])

    run(tmp_path, scenario)


def test_actions_on_removed_sold_or_pulled_item_alert(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await add_products(t, OWNER, ids["five"], ["SOLD", "REMOVED", "PULLED", "LEFT"])
        sold, removed, pulled, left = items_of(t, ids["five"])
        await give_balance(t, ALICE, 20)
        await buy(t, ALICE, ids["five"], 1)
        assert t.db.get_item(sold["id"])["status"] == "sold"
        await t.press(OWNER, f"mr:{removed['id']}")
        await t.press(OWNER, f"mry:{removed['id']}")
        await t.press(OWNER, f"mu:{pulled['id']}")
        for item in (sold, removed, pulled):
            status = t.db.get_item(item["id"])["status"]
            for data in (f"mv:{item['id']}", f"mr:{item['id']}", f"mry:{item['id']}",
                         f"mu:{item['id']}"):
                calls = await t.press(OWNER, data)
                assert_alert(calls, GONE)
                assert not t.messages(calls), data
            assert t.db.get_item(item["id"])["status"] == status
        assert t.db.get_item(sold["id"])["buyer_id"] == ALICE["id"]
        assert t.db.get_item(left["id"])["status"] == "available"
        calls = await t.press(OWNER, "mv:999999")
        assert_alert(calls, GONE)

    run(tmp_path, scenario)


def test_my_products_not_available_to_customers(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await add_products(t, OWNER, ids["five"], ["SECRET"])
        item = items_of(t, ids["five"])[0]
        await t.send(ALICE, "/start")
        for data in ("mp:0:0", f"mp:{ids['five']}:0", f"mi:{ids['five']}:0", f"mv:{item['id']}",
                     f"mr:{item['id']}", f"mry:{item['id']}", f"mu:{item['id']}", "ma"):
            calls = await t.press(ALICE, data)
            assert_alert(calls, STAFF_ONLY)
            assert not t.messages(calls) and not t.documents(calls), data
        assert t.db.get_item(item["id"])["status"] == "available"

    run(tmp_path, scenario)


def test_removed_admin_cannot_save_or_manage_products(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await stock_three_sellers(t)
        await t.press(ADMIN, f"ap:{ids['five']}:0")
        await t.send(ADMIN, "LATE-CODE")
        calls = await t.send(OWNER, f"/removeadmin {ADMIN['id']}")
        assert "is no longer an admin" in all_text(calls, OWNER_ID)
        # The pending product can't be saved any more.
        calls = await t.press(ADMIN, "apd")
        assert_alert(calls, STAFF_ONLY)
        assert "Saved" not in all_text(calls)
        calls = await t.send(ADMIN, "LATE-CODE-2")
        assert "Received" not in all_text(calls)
        assert t.db.count_items(ids["five"], ADMIN["id"]) == 2
        a1 = [r for r in items_of(t, ids["five"]) if r["owner_id"] == ADMIN["id"]][0]
        for data in ("mp:0:0", f"mi:{ids['five']}:0", f"mv:{a1['id']}", f"mr:{a1['id']}",
                     f"mry:{a1['id']}", f"mu:{a1['id']}", "ma", f"ap:{ids['five']}:0"):
            calls = await t.press(ADMIN, data)
            assert_alert(calls, STAFF_ONLY)
            assert not t.messages(calls) and not t.documents(calls), data
        assert t.db.get_item(a1["id"])["status"] == "available"
        # Their products stay in the shop and the owner still manages them.
        calls = await open_items(t, OWNER, ids["five"])
        assert t.has_button(calls, f"{a1['code']} · Adam")

    run(tmp_path, scenario)


# ================================================================ sales analysis
def test_sales_analysis_owner_gets_all_sellers_with_totals(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await esim_tree(t)
        await set_price(t, ids["ten"], "12")
        await make_admin(t, ADMIN)
        await make_admin(t, ADMIN2)
        await add_products(t, OWNER, ids["five"], ["O-1", "O-2", "O-3"])
        await add_products(t, ADMIN, ids["five"], ["A-1", "A-2"])
        await add_products(t, ADMIN2, ids["ten"], ["B-1", "B-2", "B-3"])
        # Alice buys 4 × 5GB: the oldest first → O-1, O-2, O-3, A-1.
        await give_balance(t, ALICE, 100)
        calls = await buy(t, ALICE, ids["five"], 4)
        codes = {"O-1", "O-2", "O-3", "A-1", "A-2"}
        assert [c.params["text"] for c in sent_to(calls, ALICE["id"], {"sendMessage"})
                if c.params["text"] in codes] == ["O-1", "O-2", "O-3", "A-1"]
        # Bob is a seller (10% off): 1 × 10GB for 10.80$.
        await give_balance(t, BOB, 50)
        await t.send(OWNER, f"/seller {BOB['id']}")
        await buy(t, BOB, ids["ten"], 1)
        # More owner stock, one removal, one pull.
        await add_products(t, OWNER, ids["five"], ["O-4", "O-5"])
        b2 = items_of(t, ids["ten"])[1]
        await t.press(OWNER, f"mr:{b2['id']}")
        await t.press(OWNER, f"mry:{b2['id']}")
        a2 = [r for r in items_of(t, ids["five"]) if r["owner_id"] == ADMIN["id"]][1]
        await t.press(ADMIN, f"mu:{a2['id']}")

        calls = await t.send(OWNER, "/start")
        calls = await t.press(OWNER, t.button(calls, "My Products"))
        calls = await t.press(OWNER, t.button(calls, "Download sales analysis"))
        assert "Preparing" in last_answer(calls).get("text", "")
        docs = t.documents(calls)
        assert len(docs) == 1 and docs[0].chat_id == OWNER_ID
        assert docs[0]["files"] and next(iter(docs[0]["files"].values()))["filename"].endswith(".csv")
        assert csv_rows(docs[0]) == [
            ["Product", "Seller", "Seller ID", "Price ($)", "Sold", "Left", "Revenue ($)",
             "Removed", "Pulled"],
            ["Esim > Oman > Omantel > 10GB", "Bella", "2002", "12.00", "1", "1", "10.80", "1", "0"],
            ["Esim > Oman > Omantel > 5GB", "Owner", str(OWNER_ID), "9.50", "3", "2", "28.50", "0", "0"],
            ["Esim > Oman > Omantel > 5GB", "Adam", "2001", "9.50", "1", "0", "9.50", "0", "1"],
            [],
            ["TOTALS PER SELLER"],
            ["All products", "Bella", "2002", "", "1", "1", "10.80", "1", "0"],
            ["All products", "Owner", str(OWNER_ID), "", "3", "2", "28.50", "0", "0"],
            ["All products", "Adam", "2001", "", "1", "0", "9.50", "0", "1"],
            ["All products", "EVERYONE", "", "", "5", "3", "48.80", "1", "1"],
        ]
        caption = docs[0].params["caption"]
        assert "(all sellers)" in caption
        assert "✅ Sold: <b>5</b> · 📦 Left: <b>3</b> · 💰 Revenue: <b>48.80$</b>" in caption
        # The money matches what each seller earned and what the buyers paid.
        assert t.db.get_user(OWNER_ID)["earnings"] == 2850
        assert t.db.get_user(ADMIN["id"])["earnings"] == 950
        assert t.db.get_user(ADMIN2["id"])["earnings"] == 1080
        assert t.db.get_user(ALICE["id"])["balance"] == 10000 - 3800
        assert t.db.get_user(BOB["id"])["balance"] == 5000 - 1080

    run(tmp_path, scenario)


def test_sales_analysis_admin_gets_only_own_rows(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = await stock_three_sellers(t)
        await give_balance(t, ALICE, 100)
        await buy(t, ALICE, ids["five"], 5)  # O-1..O-3, A-1, A-2
        await buy(t, ALICE, ids["ten"], 1)  # B-1
        await add_products(t, ADMIN, ids["five"], ["A-3"])
        calls = await t.send(ADMIN, "/start")
        calls = await t.press(ADMIN, t.button(calls, "My Products"))
        calls = await t.press(ADMIN, t.button(calls, "Download sales analysis"))
        docs = t.documents(calls)
        assert len(docs) == 1 and docs[0].chat_id == ADMIN["id"]
        rows = csv_rows(docs[0])
        assert rows == [
            ["Product", "Seller", "Seller ID", "Price ($)", "Sold", "Left", "Revenue ($)",
             "Removed", "Pulled"],
            ["Esim > Oman > Omantel > 5GB", "Adam", "2001", "9.50", "2", "1", "19.00", "0", "0"],
            [],
            ["TOTALS PER SELLER"],
            ["All products", "Adam", "2001", "", "2", "1", "19.00", "0", "0"],
        ]
        caption = docs[0].params["caption"]
        assert "(your products)" in caption
        assert "✅ Sold: <b>2</b> · 📦 Left: <b>1</b> · 💰 Revenue: <b>19.00$</b>" in caption
        flat = ShopTest.document_text(docs[0])
        assert "Owner" not in flat and "Bella" not in flat and "EVERYONE" not in flat
        # Bella's file only has her 10GB sale.
        calls = await t.press(ADMIN2, "ma")
        rows = csv_rows(t.documents(calls)[0])
        assert rows[1] == ["Esim > Oman > Omantel > 10GB", "Bella", "2002", "12.00", "1", "0",
                           "12.00", "0", "0"]
        assert len([r for r in rows if r and r[0].startswith("Esim")]) == 1

    run(tmp_path, scenario)


def test_sales_analysis_without_products_alerts(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await esim_tree(t)
        await make_admin(t, ADMIN)
        await add_products(t, OWNER, label_by_path(t, "Esim", "Oman", "Omantel", "5GB")["id"], ["O-1"])
        calls = await t.press(ADMIN, "ma")
        assert_alert(calls, "There are no products yet.")
        assert not t.documents(calls)
        calls = await t.press(OWNER, "ma")
        assert len(t.documents(calls)) == 1

    run(tmp_path, scenario)


def test_sales_analysis_csv_neutralises_formulas(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        evil_label = '=HYPERLINK("http://evil.example","x")'
        await new_labels(t, 0, evil_label)
        label = label_by_path(t, evil_label)["id"]
        await set_price(t, label, "1")
        evil_admin = tg_user(2003, "=cmd|' /C calc'!A0")
        await make_admin(t, evil_admin)
        await add_products(t, evil_admin, label, ["X-1"])
        calls = await t.press(OWNER, "ma")
        rows = csv_rows(t.documents(calls)[0])
        assert rows[1][0] == "'" + evil_label
        assert rows[1][1] == "'=cmd|' /C calc'!A0"
        for row in rows:
            for cell in row:
                assert not cell.startswith(("=", "+", "-", "@")), row

    run(tmp_path, scenario)


def test_add_product_and_my_products_label_lists_paginate(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await t.send(OWNER, "/start")
        await new_labels(t, 0, "\n".join(f"P{i:02d}" for i in range(1, 32)))
        calls = await t.press(OWNER, "ap:0:0")
        products = [x for x in button_texts(calls) if x.startswith("🛒")]
        assert len(products) == 30 and "🛒 P31 · no price" not in products
        assert "1/2" in button_texts(calls)
        calls = await t.press(OWNER, t.button(calls, "▶️"))
        assert [x for x in button_texts(calls) if x.startswith("🛒")] == ["🛒 P31 · no price"]
        calls = await t.press(OWNER, "mp:0:0")
        leaves = [x for x in button_texts(calls) if x.startswith("P")]
        assert len(leaves) == 30 and "P31 · 0" not in leaves
        calls = await t.press(OWNER, t.button(calls, "▶️"))
        assert [x for x in button_texts(calls) if x.startswith("P")] == ["P31 · 0"]

    run(tmp_path, scenario)


def test_sales_analysis_with_no_items_at_all_alerts(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/start")
        calls = await t.press(OWNER, t.button(calls, "My Products"))
        assert "There are no labels yet." in t.last_text(calls)
        calls = await t.press(OWNER, t.button(calls, "Download sales analysis"))
        assert_alert(calls, "There are no products yet.")
        assert not t.documents(calls)

    run(tmp_path, scenario)
