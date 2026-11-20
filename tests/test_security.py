"""Permissions, robustness and Telegram limits — end to end through the real bot.

Covers:
* callback data crafted by people who must not use it: customers pressing staff/owner
  buttons, admins pressing owner buttons or other sellers' product buttons, banned and
  removed admins — refused, nothing changes in the database, nothing secret is shown;
* malformed, oversized and unknown callback data (an alert, never a crash);
* updates the bot must ignore (groups, channels, inline messages and queries);
* unknown commands, stray messages, /cancel, and several users in flows at once;
* HTML injection through names, label names and Binance IDs; emoji and Arabic labels;
* leaving ➕ Add Product with unsaved products;
* Telegram limits over big scenarios: callback_data <= 64 bytes, <= 100 buttons per
  keyboard (<= 8 per row), text <= 4096, caption <= 1024, alerts <= 200 characters and
  well-formed parse_mode=HTML;
* button presses that Telegram reports as too old to answer, and double presses.

Fixtures are mostly created straight in the database so each test stays focused; the
behaviour under test is driven with real messages and button presses.
"""

from __future__ import annotations

import csv
import html
import io
import itertools
import json
import re
import time
from typing import Any, Optional

import pytest

from harness import BOT_USER, OWNER, OWNER_ID, Call, Picks, ShopTest, run, tg_user
from shopbot.handlers.common import setup_bot_profile

ALICE = tg_user(1001, "Alice", "alice")
BOB = tg_user(1002, "Bob", "bob")
ADMIN = tg_user(2001, "Adam", "adam")
ADMIN2 = tg_user(2002, "Zara", "zara")
GUEST = tg_user(3001, "Guest", "guest")

LABELS_ONLY = "Only the owner can manage labels"
STAFF_ONLY = "This section is for the owner and admins"
OWNER_ONLY = "Only the owner can do that"
OWN_ITEMS_ONLY = "You can only manage your own products"
EARNINGS_ONLY = "Earnings are for the owner and admins"
NO_PRICE_YET = "has no price yet"
NOTHING_TO_SAVE = "There is nothing to save"
STALE = "This button is no longer valid"
UNKNOWN_BUTTON = "This button is no longer active"
EXPIRED = "expired"
BANNED = "You are banned"
OWNER_COMMAND_ONLY = "Only the owner can use this command"
NO_FLOW_HINT = "Send /start to open the shop menu"
UNKNOWN_COMMAND = "Unknown command"
CANCELLED = "Cancelled"
WENT_WRONG = "Something went wrong"
DISCARDED = "unsaved product(s) were discarded"
TOO_OLD = "Bad Request: query is too old and response timeout expired or query id is invalid"
HUGE = "99999999999999999999"  # > 2**63 - 1 (SQLite's largest integer), still a short callback_data

GROUP = {"id": -1001234567890, "type": "supergroup", "title": "eSIM fans"}
SMALL_GROUP = {"id": -4001, "type": "group", "title": "Friends"}
CHANNEL = {"id": -1009876543210, "type": "channel", "title": "Shop news"}
_ids = itertools.count(500_000)


# =================================================================== fixtures
def register(t: ShopTest, user: dict) -> None:
    t.db.touch_user(user["id"], user["first_name"], user.get("last_name"), user.get("username"))


def make_admin(t: ShopTest, user: dict) -> None:
    register(t, user)
    t.db.set_admin(user["id"], True)


def text_item(text: str) -> dict:
    return {"kind": "text", "text": text, "entities": []}


def make_product(t: ShopTest, path: str, price: Optional[int]) -> int:
    """Create the labels of 'A/B/C' (reusing existing ones); price the last one."""
    parent: Optional[int] = None
    for name in path.split("/"):
        found = [r for r in t.db.children(parent) if r["name"] == name]
        parent = found[0]["id"] if found else t.db.add_labels(parent, [name])[0][0]["id"]
    assert parent is not None
    if price is not None:
        t.db.set_label_price(parent, price)
    return parent


def item_ids(t: ShopTest, label_id: int) -> dict[str, int]:
    return {row["code"]: row["id"] for row in t.db.list_items(label_id, None, 0, 10_000)}


def give_balance(t: ShopTest, user: dict, cents: int) -> None:
    register(t, user)
    t.db.manual_deposit(user["id"], cents, 10**12)  # huge threshold: never auto-seller


def build_world(t: ShopTest) -> dict[str, Any]:
    """Esim › Oman › 5GB (9.50$, stock from the owner, Adam and Zara), an unpriced
    10GB, an unpriced top-level draft, Bob's pending deposit and Adam's pending
    withdrawal."""
    register(t, OWNER)
    register(t, ALICE)
    register(t, BOB)
    make_admin(t, ADMIN)
    make_admin(t, ADMIN2)
    five = make_product(t, "Esim/Oman/5GB", 950)
    oman = t.db.get_label(five)["parent_id"]
    esim = t.db.get_label(oman)["parent_id"]
    ten = make_product(t, "Esim/Oman/10GB", None)
    hidden = make_product(t, "Hidden Draft", None)
    owner_codes = t.db.add_items(five, OWNER_ID, [text_item("SECRET-OWNER-1"), text_item("SECRET-OWNER-2")])
    admin_codes = t.db.add_items(five, ADMIN["id"], [text_item("SECRET-ADMIN-1")])
    admin2_codes = t.db.add_items(five, ADMIN2["id"], [text_item("SECRET-ADMIN2-1")])
    ids = item_ids(t, five)
    deposit = t.db.create_deposit(BOB["id"], 2000, "bob-receipt-file", "photo")
    t.db.conn.execute("UPDATE users SET earnings = 5000 WHERE id = ?", (ADMIN["id"],))
    withdrawal = t.db.create_withdrawal(ADMIN["id"], 1000, "BINANCE-OF-ADAM", auto_approve=False)
    return {
        "esim": esim, "oman": oman, "five": five, "ten": ten, "hidden": hidden,
        "owner_item": ids[owner_codes[0]], "admin_item": ids[admin_codes[0]],
        "admin2_item": ids[admin2_codes[0]],
        "owner_codes": owner_codes, "admin_codes": admin_codes, "admin2_codes": admin2_codes,
        "deposit": deposit["id"], "withdrawal": withdrawal["id"], "HUGE": HUGE,
        "secrets": ["SECRET-", "bob-receipt-file", "BINANCE-OF-ADAM", "Hidden Draft",
                    *owner_codes, *admin_codes, *admin2_codes],
    }


TABLES = ("users", "labels", "orders", "items", "deposits", "withdrawals", "ledger", "settings")


def snapshot(t: ShopTest) -> dict[str, list[dict]]:
    """Every row of every table (users.last_seen left out: any update touches it)."""
    snap: dict[str, list[dict]] = {}
    for table in TABLES:
        cursor = t.db.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        columns = [d[0] for d in cursor.description]
        rows = []
        for row in cursor.fetchall():
            record = dict(zip(columns, row))
            record.pop("last_seen", None)
            rows.append(record)
        snap[table] = rows
    return snap


# =================================================================== call helpers
def all_text(calls: list[Call], chat_id: Optional[int] = None) -> str:
    return "\n".join(ShopTest.texts(calls, chat_id))


def blob(call: Call) -> str:
    """Everything a Bot API call carries, as one string (to look for leaked secrets)."""
    files = " ".join(repr(f.get("content", "")) for f in call["files"].values())
    return json.dumps(call.params, ensure_ascii=False, default=str) + " " + files


def leaks(calls: list[Call], secrets: list[str]) -> list[tuple[str, str]]:
    return [(c.method, s) for c in calls if c.method != "answerCallbackQuery"
            for s in secrets if s in blob(c)]


def datas(calls: list[Call]) -> list[str]:
    return [b.get("callback_data", "") for b in ShopTest.all_buttons(calls)]


def button_texts(calls: list[Call]) -> list[str]:
    return [b.get("text", "") for b in ShopTest.all_buttons(calls)]


def alerts(calls: list[Call]) -> list[dict]:
    return [a for a in ShopTest.answers(calls) if a.get("text")]


def other_chats(calls: list[Call], chat_id: int) -> list[Call]:
    return [c for c in calls if c.chat_id is not None and c.chat_id != chat_id]


def csv_rows(call: Call) -> list[list[str]]:
    return list(csv.reader(io.StringIO(ShopTest.document_text(call))))


def photo_sizes(file_id: str) -> list[dict]:
    return [
        {"file_id": f"{file_id}-s", "file_unique_id": f"{file_id}-us", "width": 90, "height": 90},
        {"file_id": file_id, "file_unique_id": f"{file_id}-u", "width": 800, "height": 800},
    ]


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def send_in(t: ShopTest, chat: dict, user: Optional[dict], text: Optional[str] = None,
                  **fields: Any) -> list[Call]:
    """A message in another chat (group, supergroup or channel)."""
    message: dict[str, Any] = {"message_id": next(_ids), "date": int(time.time()), "chat": chat}
    if user is not None:
        message["from"] = user
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    message.update(fields)
    key = "channel_post" if chat["type"] == "channel" else "message"
    return await t._process({key: message})


async def press_in(t: ShopTest, chat: dict, user: dict, data: str) -> list[Call]:
    """A button pressed on a bot message that sits in another chat (e.g. a group)."""
    query = {
        "id": f"g{next(_ids)}", "from": user, "chat_instance": "elsewhere", "data": data,
        "message": {"message_id": next(_ids), "date": int(time.time()), "chat": chat,
                    "from": BOT_USER, "text": "menu"},
    }
    return await t._process({"callback_query": query})


def nonce_of(calls: list[Call]) -> str:
    return ShopTest.button(calls, "Confirm").split(":")[3]


# ============================================================ Telegram limits
HTML_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler",
             "a", "tg-emoji", "code", "pre", "blockquote", "tg-time"}
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^<>]*)>")
_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});")
TEXT_METHODS = {"sendMessage", "editMessageText"}
CAPTION_METHODS = {"sendPhoto", "sendDocument", "sendVideo", "sendAnimation", "sendAudio",
                   "sendVoice", "editMessageCaption"}


def parse_telegram_html(text: str) -> tuple[str, list[str]]:
    """The visible text after Telegram's HTML parsing, plus every rule it breaks:
    tags must be supported and balanced, and every '<' and '&' that isn't part of a tag
    or an entity must be escaped."""
    out: list[str] = []
    stack: list[str] = []
    problems: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "<":
            match = _TAG.match(text, i)
            if match is None:
                problems.append(f"unescaped '<' in {text[max(0, i - 15):i + 15]!r}")
                out.append(ch)
                i += 1
                continue
            closing, name = match.group(1) == "/", match.group(2).lower()
            if name not in HTML_TAGS:
                problems.append(f"unsupported tag {match.group(0)!r}")
            elif closing:
                if stack and stack[-1] == name:
                    stack.pop()
                else:
                    problems.append(f"unbalanced </{name}> (open: {stack})")
            else:
                stack.append(name)
            i = match.end()
            continue
        if ch == "&":
            match = _ENTITY.match(text, i)
            if match is None:
                problems.append(f"unescaped '&' in {text[max(0, i - 15):i + 15]!r}")
                out.append(ch)
                i += 1
                continue
            out.append(html.unescape(match.group(0)))
            i = match.end()
            continue
        out.append(ch)
        i += 1
    if stack:
        problems.append(f"unclosed tags {stack}")
    return "".join(out), problems


def keyboard(call: Call) -> list[list[dict]]:
    markup = call.params.get("reply_markup") or {}
    if isinstance(markup, str):
        markup = json.loads(markup)
    return markup.get("inline_keyboard", []) if isinstance(markup, dict) else []


def limit_problems(calls: list[Call]) -> list[str]:
    """Everything in these Bot API calls that Telegram would reject or render wrongly."""
    problems: list[str] = []
    for number, call in enumerate(calls):
        where = f"call #{number} {call.method}"
        params = call.params
        is_html = str(params.get("parse_mode") or "").upper() == "HTML"
        if call.method in TEXT_METHODS or call.method in CAPTION_METHODS:
            key = "text" if call.method in TEXT_METHODS else "caption"
            raw = params.get(key)
            if raw is None:
                if key == "text":
                    problems.append(f"{where}: no text")
            else:
                visible = str(raw)
                if is_html:
                    visible, errors = parse_telegram_html(visible)
                    problems += [f"{where}: {e}" for e in errors]
                limit = 4096 if key == "text" else 1024
                if utf16_len(visible) > limit:
                    problems.append(f"{where}: {key} is {utf16_len(visible)} long (> {limit})")
                if key == "text" and not visible.strip():
                    problems.append(f"{where}: empty text")
        rows = keyboard(call)
        buttons = [b for row in rows for b in row]
        if len(buttons) > 100:
            problems.append(f"{where}: {len(buttons)} buttons (> 100)")
        for row in rows:
            if len(row) > 8:
                problems.append(f"{where}: {len(row)} buttons in one row (> 8)")
        for button in buttons:
            if not str(button.get("text", "")).strip():
                problems.append(f"{where}: button without text {button}")
            data = button.get("callback_data")
            if data is not None and not 1 <= len(data.encode("utf-8")) <= 64:
                problems.append(f"{where}: callback_data {data!r} is {len(data.encode())} bytes")
        if call.method == "answerCallbackQuery" and len(params.get("text") or "") > 200:
            problems.append(f"{where}: alert is {len(params['text'])} characters (> 200)")
    return problems


def assert_within_limits(calls: list[Call]) -> None:
    problems = limit_problems(calls)
    assert not problems, "\n".join(problems[:40])


# ================================================ customers crafting staff buttons
# (callback data template, ("alert" | "message", expected text))
CUSTOMER_CRAFTED = [
    ("lb:0:0", ("alert", LABELS_ONLY)),
    ("lb:{esim}:0", ("alert", LABELS_ONLY)),
    ("ln:0", ("alert", LABELS_ONLY)),
    ("ln:{oman}", ("alert", LABELS_ONLY)),
    ("lp:{five}", ("alert", LABELS_ONLY)),
    ("lr:{five}", ("alert", LABELS_ONLY)),
    ("ld:{ten}", ("alert", LABELS_ONLY)),
    ("ldy:{ten}", ("alert", LABELS_ONLY)),
    ("ap:0:0", ("alert", STAFF_ONLY)),
    ("ap:{five}:0", ("alert", STAFF_ONLY)),
    ("apd", ("alert", NOTHING_TO_SAVE)),
    ("apc", ("message", CANCELLED)),  # harmless: nothing to cancel
    ("mp:0:0", ("alert", STAFF_ONLY)),
    ("mi:{five}:0", ("alert", STAFF_ONLY)),
    ("mv:{owner_item}", ("alert", STAFF_ONLY)),
    ("mr:{owner_item}", ("alert", STAFF_ONLY)),
    ("mry:{owner_item}", ("alert", STAFF_ONLY)),
    ("mu:{owner_item}", ("alert", STAFF_ONLY)),
    ("mu:{admin_item}", ("alert", STAFF_ONLY)),
    ("ma", ("alert", STAFF_ONLY)),
    ("dok:{deposit}", ("alert", OWNER_ONLY)),
    ("dno:{deposit}", ("alert", OWNER_ONLY)),
    ("wok:{withdrawal}", ("alert", OWNER_ONLY)),
    ("wno:{withdrawal}", ("alert", OWNER_ONLY)),
    ("ea", ("alert", EARNINGS_ONLY)),
    ("ew", ("message", "/withdraw is for the owner and admins")),
]


async def check_refused(t: ShopTest, user: dict, data: str, expected: tuple[str, str],
                        world: dict[str, Any]) -> None:
    before = snapshot(t)
    calls = await t.press(user, data)
    kind, text = expected
    if kind == "alert":
        assert text in t.alert(calls), (data, t.answers(calls))
        assert t.answers(calls)[-1].get("show_alert"), data
        assert t.messages(calls) == [], (data, t.texts(calls))
    else:
        assert text in all_text(calls, user["id"]), (data, t.texts(calls))
    assert other_chats(calls, user["id"]) == [], (data, [c.method for c in other_chats(calls, user["id"])])
    assert t.documents(calls) == []
    assert leaks(calls, world["secrets"]) == []
    assert WENT_WRONG not in all_text(calls)
    assert snapshot(t) == before, data
    # The refused press must not have opened a flow: the next message isn't taken as input
    # (a label name, a price, a product, ...).
    calls = await t.send(user, "0.01")
    assert NO_FLOW_HINT in t.last_text(calls), (data, t.texts(calls))
    assert snapshot(t) == before, data


@pytest.mark.parametrize("template, expected", CUSTOMER_CRAFTED, ids=[c[0] for c in CUSTOMER_CRAFTED])
def test_customer_crafting_a_staff_or_owner_button_is_refused(tmp_path, template, expected):
    async def scenario(t: ShopTest):
        world = build_world(t)
        await t.send(ALICE, "/start")
        await check_refused(t, ALICE, template.format(**world), expected, world)

    run(tmp_path, scenario)


# ============================================ admins crafting owner-only buttons
ADMIN_CRAFTED = [
    ("lb:0:0", ("alert", LABELS_ONLY)),
    ("lb:{five}:0", ("alert", LABELS_ONLY)),
    ("ln:0", ("alert", LABELS_ONLY)),
    ("ln:{oman}", ("alert", LABELS_ONLY)),
    ("lp:{five}", ("alert", LABELS_ONLY)),
    ("lp:{ten}", ("alert", LABELS_ONLY)),
    ("lr:{five}", ("alert", LABELS_ONLY)),
    ("ld:{ten}", ("alert", LABELS_ONLY)),
    ("ldy:{ten}", ("alert", LABELS_ONLY)),
    ("ldy:{hidden}", ("alert", LABELS_ONLY)),
    ("dok:{deposit}", ("alert", OWNER_ONLY)),
    ("dno:{deposit}", ("alert", OWNER_ONLY)),
    ("wok:{withdrawal}", ("alert", OWNER_ONLY)),  # approving their own withdrawal
    ("wno:{withdrawal}", ("alert", OWNER_ONLY)),
    ("mv:{owner_item}", ("alert", OWN_ITEMS_ONLY)),
    ("mr:{owner_item}", ("alert", OWN_ITEMS_ONLY)),
    ("mry:{owner_item}", ("alert", OWN_ITEMS_ONLY)),
    ("mu:{owner_item}", ("alert", OWN_ITEMS_ONLY)),
    ("mv:{admin2_item}", ("alert", OWN_ITEMS_ONLY)),
    ("mry:{admin2_item}", ("alert", OWN_ITEMS_ONLY)),
    ("mu:{admin2_item}", ("alert", OWN_ITEMS_ONLY)),
    ("ap:{ten}:0", ("alert", NO_PRICE_YET)),  # admins stock only priced products
]


@pytest.mark.parametrize("template, expected", ADMIN_CRAFTED, ids=[c[0] for c in ADMIN_CRAFTED])
def test_admin_crafting_an_owner_button_or_another_sellers_item_is_refused(tmp_path, template, expected):
    async def scenario(t: ShopTest):
        world = build_world(t)
        await t.send(ADMIN, "/start")
        world = dict(world, secrets=[s for s in world["secrets"]
                                     if s not in world["admin_codes"] and s != "BINANCE-OF-ADAM"]
                     + ["SECRET-OWNER", "SECRET-ADMIN2"])
        world["secrets"].remove("SECRET-")
        await check_refused(t, ADMIN, template.format(**world), expected, world)

    run(tmp_path, scenario)


def test_the_right_people_still_get_in(tmp_path):
    """Positive control for the refusals above."""
    async def scenario(t: ShopTest):
        world = build_world(t)
        calls = await t.press(ADMIN, f"mv:{world['admin_item']}")
        assert "SECRET-ADMIN-1" in all_text(calls, ADMIN["id"])
        calls = await t.press(OWNER, f"mv:{world['admin_item']}")
        assert "SECRET-ADMIN-1" in all_text(calls, OWNER_ID)
        calls = await t.press(OWNER, "lb:0:0")
        assert "Add Label" in t.last_text(calls)
        calls = await t.press(ADMIN, f"ap:{world['five']}:0")
        assert "ONE MESSAGE PER PRODUCT" in t.last_text(calls)
        await t.send(ADMIN, "/cancel")
        calls = await t.press(OWNER, f"dok:{world['deposit']}", photo=True)
        assert t.db.get_user(BOB["id"])["balance"] == 2000
        calls = await t.press(OWNER, f"wok:{world['withdrawal']}")
        assert t.db.get_withdrawal(world["withdrawal"])["status"] == "approved"

    run(tmp_path, scenario)


def test_admin_sees_only_their_own_stock_sales_and_earnings(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        foreign = world["owner_codes"] + world["admin2_codes"] + ["SECRET-OWNER", "SECRET-ADMIN2", "Zara"]
        calls = await t.press(ADMIN, "mp:0:0")
        assert "Your products in stock: <b>1</b>" in t.last_text(calls)

        calls = await t.press(ADMIN, f"mi:{world['five']}:0")
        assert f"mv:{world['admin_item']}" in datas(calls)
        assert f"mv:{world['owner_item']}" not in datas(calls)
        assert f"mv:{world['admin2_item']}" not in datas(calls)
        assert leaks(calls, foreign) == []

        calls = await t.press(ADMIN, "ma")
        (document,) = t.documents(calls)
        rows = [r for r in csv_rows(document) if len(r) > 2 and r[2].isdigit()]
        assert rows and {r[2] for r in rows} == {str(ADMIN["id"])}
        assert "EVERYONE" not in t.document_text(document)
        assert leaks(calls, foreign + ["Owner"]) == []

        calls = await t.press(ADMIN, "ea")
        assert "Your Earnings" in t.last_text(calls)
        assert leaks(calls, foreign + ["Whole shop", "Owner"]) == []

    run(tmp_path, scenario)


def test_customer_cannot_open_someone_elses_order(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        give_balance(t, ALICE, 5000)
        t.db.rng = Picks(OWNER_ID)  # the owner's SECRET-OWNER-1 is sold
        calls = await t.press(ALICE, f"sb:{world['five']}:1")
        await t.press(ALICE, t.button(calls, "Confirm"))
        (order,) = t.db.user_orders(ALICE["id"], 0, 10)
        for intruder in (BOB, ADMIN, OWNER):
            calls = await t.press(intruder, f"mov:{order['id']}")
            assert "Order not found" in t.alert(calls)
            assert t.messages(calls) == []
            assert leaks(calls, ["SECRET-"]) == []
        calls = await t.press(ALICE, f"mov:{order['id']}")
        assert "SECRET-OWNER-1" in t.texts(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_a_confirm_button_only_works_for_the_user_who_got_it(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        give_balance(t, ALICE, 5000)
        give_balance(t, BOB, 5000)
        calls = await t.press(ALICE, f"sb:{world['five']}:2")
        confirm = t.button(calls, "Confirm")
        before = snapshot(t)
        calls = await t.press(BOB, confirm)
        assert EXPIRED in t.alert(calls)
        assert snapshot(t) == before
        assert leaks(calls, ["SECRET-"]) == []
        calls = await t.press(ALICE, confirm)
        assert any("Purchase successful" in x for x in t.texts(calls, ALICE["id"]))
        assert t.db.get_user(ALICE["id"])["balance"] == 5000 - 1900
        assert t.db.get_user(BOB["id"])["balance"] == 5000

    run(tmp_path, scenario)


def test_request_buttons_without_a_flow_are_refused(tmp_path):
    async def scenario(t: ShopTest):
        build_world(t)
        before = snapshot(t)
        for user, data, expected in [
            (ALICE, "dsend", EXPIRED), (ALICE, "wall", EXPIRED), (ADMIN, "wall", EXPIRED),
            (ALICE, "da:0", STALE), (ALICE, "da:-5", STALE), (ALICE, "da:100001", STALE),
        ]:
            calls = await t.press(user, data)
            assert expected in t.alert(calls), (data, t.answers(calls))
            assert t.messages(calls) == [], data
            calls = await t.send_photo(user, "random-photo")
            assert NO_FLOW_HINT in t.last_text(calls), data
        assert snapshot(t) == before

    run(tmp_path, scenario)


def test_only_the_owner_can_ban_or_unban(tmp_path):
    async def scenario(t: ShopTest):
        build_world(t)
        calls = await t.send(ADMIN, f"/ban {ALICE['id']}")
        assert OWNER_COMMAND_ONLY in t.last_text(calls)
        calls = await t.send(ALICE, f"/ban {ADMIN['id']}")
        assert OWNER_COMMAND_ONLY in t.last_text(calls)
        assert not t.db.is_banned(ALICE["id"]) and not t.db.is_banned(ADMIN["id"])
        calls = await t.send(OWNER, f"/ban {ALICE['id']}")
        assert "is now <b>banned</b>" in t.last_text(calls, OWNER_ID) and t.db.is_banned(ALICE["id"])
        calls = await t.send(ADMIN, f"/unban {ALICE['id']}")
        assert OWNER_COMMAND_ONLY in t.last_text(calls)
        assert t.db.is_banned(ALICE["id"])

    run(tmp_path, scenario)


def test_banned_admin_cannot_reach_stock_money_or_reports(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        await t.send(ADMIN, "/start")
        calls = await t.send(OWNER, f"/ban {ADMIN['id']}")
        assert "is now <b>banned</b>" in t.last_text(calls, OWNER_ID)
        before = snapshot(t)
        five, mine = world["five"], world["admin_item"]
        for data in ("mp:0:0", f"mi:{five}:0", f"mv:{mine}", f"mr:{mine}", f"mry:{mine}", f"mu:{mine}",
                     "ma", "ea", "ew", "wall", f"ap:{five}:0", "apd", "sh:0:0", "dp", "mo:0", "menu"):
            calls = await t.press(ADMIN, data)
            assert BANNED in t.alert(calls), data
            assert t.messages(calls) == [] and t.documents(calls) == [], data
            assert leaks(calls, world["secrets"]) == [], data
        for text in ("/start", "/withdraw", "/help", "hello"):
            calls = await t.send(ADMIN, text)
            assert [BANNED in x for x in t.texts(calls)] == [True], text
        assert snapshot(t) == before
        # /unban gives the admin their own product back.
        await t.send(OWNER, f"/unban {ADMIN['id']}")
        calls = await t.press(ADMIN, f"mv:{mine}")
        assert "SECRET-ADMIN-1" in t.texts(calls, ADMIN["id"])

    run(tmp_path, scenario)


def test_removed_admin_cannot_save_or_manage_stock(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        five = world["five"]
        await t.press(ADMIN, f"ap:{five}:0")
        await t.send(ADMIN, "LATE-1")
        await t.send(ADMIN, "LATE-2")
        await t.send(OWNER, f"/removeadmin {ADMIN['id']}")
        stock = t.db.count_items(five)
        calls = await t.press(ADMIN, "apd")
        assert STAFF_ONLY in t.alert(calls) or NOTHING_TO_SAVE in t.alert(calls)
        await t.send(ADMIN, "LATE-3")
        calls = await t.press(ADMIN, "apd")
        assert NOTHING_TO_SAVE in t.alert(calls)
        for data in (f"mu:{world['admin_item']}", f"mry:{world['admin_item']}", f"ap:{five}:0", "mp:0:0"):
            calls = await t.press(ADMIN, data)
            assert STAFF_ONLY in t.alert(calls), data
        assert t.db.count_items(five) == stock
        assert t.db.get_item(world["admin_item"])["status"] == "available"
        assert "LATE-" not in json.dumps([dict(r) for r in t.db.conn.execute("SELECT content FROM items")])

    run(tmp_path, scenario)


# ===================================================== malformed callback data
CUSTOMER_MALFORMED = ["sh:abc", "sh:", "sh:1", "sh:1.5:0", "sh:-1:0", "sb:1", "sb:x:1", "sb:1:-2", "sq:",
                      "sq:abc", "sc:1:2", "sc:1", "sc:x:1:abcd", "sc:1:1:", "mo:abc", "mov:-3", "mov:",
                      "da:abc", "da:", "da:1.5"]
OWNER_MALFORMED = ["lb:abc:0", "lb:1:-2", "lb:1", "ln:", "ln:-1", "lp:x", "lr:", "ld:1.0", "ldy:-1",
                   "ap:1", "ap:x:0", "mp:", "mi:1", "mv:-1", "mv:x", "mr:", "mry:1e3", "mu:",
                   "dok:", "dok:-1", "dno:x", "wok:x", "wno:1.5"]


@pytest.mark.parametrize("who, malformed", [("customer", CUSTOMER_MALFORMED), ("owner", OWNER_MALFORMED)])
def test_malformed_callback_data_gets_an_alert_not_a_crash(tmp_path, who, malformed):
    async def scenario(t: ShopTest):
        build_world(t)
        user = ALICE if who == "customer" else OWNER
        before = snapshot(t)
        failures = []
        for data in malformed:
            calls = await t.press(user, data)
            if WENT_WRONG in all_text(calls) or not alerts(calls) or t.messages(calls):
                failures.append((data, [c.method for c in calls], t.alert(calls), t.texts(calls)))
        assert not failures, failures
        assert snapshot(t) == before

    run(tmp_path, scenario)


HUGE_ID_CASES = [
    ("customer", "sh:{HUGE}:0"),
    ("customer", "sq:{HUGE}"),
    ("customer", "mov:{HUGE}"),
    ("customer", "sc:{five}:{HUGE}:{nonce}"),
    ("owner", "lb:{HUGE}:0"),
    ("owner", "mv:{HUGE}"),
    ("owner", "dok:{HUGE}"),
    ("owner", "wno:{HUGE}"),
]


@pytest.mark.parametrize("who, template", HUGE_ID_CASES,
                         ids=[c[1].format(HUGE="HUGE", five="5GB", nonce="nonce") for c in HUGE_ID_CASES])
def test_huge_ids_in_callback_data_get_an_alert_not_a_crash(tmp_path, who, template):
    async def scenario(t: ShopTest):
        world = build_world(t)
        give_balance(t, ALICE, 5000)
        user = ALICE if who == "customer" else OWNER
        nonce = ""
        if "{nonce}" in template:
            nonce = nonce_of(await t.press(ALICE, f"sb:{world['five']}:1"))
        before = snapshot(t)
        calls = await t.press(user, template.format(nonce=nonce, **world))
        assert WENT_WRONG not in all_text(calls)
        assert alerts(calls) or t.messages(calls, user["id"])
        assert snapshot(t) == before

    run(tmp_path, scenario)


def test_huge_page_numbers_and_quantities_are_handled(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        for user, data in [(OWNER, f"lb:0:{HUGE}"), (OWNER, f"ap:0:{HUGE}"), (OWNER, f"mp:0:{HUGE}"),
                           (OWNER, f"mi:{world['five']}:{HUGE}"), (ALICE, f"sh:0:{HUGE}"),
                           (ALICE, f"mo:{HUGE}"), (ALICE, f"da:{HUGE}"), (ALICE, f"sb:{world['five']}:{HUGE}"),
                           (ALICE, "sh:9223372036854775807:0"), (OWNER, "mv:9223372036854775807")]:
            calls = await t.press(user, data)
            assert WENT_WRONG not in all_text(calls), data
            assert t.answers(calls), data

    run(tmp_path, scenario)


def test_empty_callback_data_does_not_crash(tmp_path):
    async def scenario(t: ShopTest):
        build_world(t)
        before = snapshot(t)
        calls = await t.press(ALICE, "")
        assert WENT_WRONG not in all_text(calls)
        assert t.answers(calls)
        assert snapshot(t) == before

    run(tmp_path, scenario)


def test_unknown_callback_data_gets_an_alert(tmp_path):
    async def scenario(t: ShopTest):
        build_world(t)
        before = snapshot(t)
        for user in (ALICE, ADMIN, OWNER):
            for data in ("xyz", "foo:1:2", "menu:1", "apd:1", "shop", "SH:0:0", "noop:1", " sh:0:0"):
                calls = await t.press(user, data)
                assert UNKNOWN_BUTTON in t.alert(calls), (user["first_name"], data, t.answers(calls))
                assert t.messages(calls) == [], data
        assert snapshot(t) == before

    run(tmp_path, scenario)


# ================================================= groups, channels and inline
def test_group_messages_are_ignored_and_store_nothing(tmp_path):
    async def scenario(t: ShopTest):
        calls: list[Call] = []
        calls += await send_in(t, GROUP, GUEST, "/start")
        calls += await send_in(t, GROUP, GUEST, "/start@test_shop_bot")
        calls += await send_in(t, GROUP, GUEST, "/help")
        calls += await send_in(t, GROUP, GUEST, "hello shop")
        calls += await send_in(t, SMALL_GROUP, GUEST, "/withdraw")
        calls += await send_in(t, GROUP, GUEST, photo=photo_sizes("group-photo"))
        calls += await t._process({"edited_message": {
            "message_id": next(_ids), "date": int(time.time()), "edit_date": int(time.time()),
            "chat": GROUP, "from": GUEST, "text": "/start"}})
        calls += await t._process({"my_chat_member": {
            "chat": GROUP, "from": GUEST, "date": int(time.time()),
            "old_chat_member": {"status": "left", "user": BOT_USER},
            "new_chat_member": {"status": "member", "user": BOT_USER}}})
        assert calls == []
        assert t.db.get_user(GUEST["id"]) is None

    run(tmp_path, scenario)


def test_owner_commands_sent_in_a_group_do_nothing(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        before = snapshot(t)
        for text in ("/status", f"/ban {ALICE['id']}", f"/addbal {ALICE['id']} 50",
                     f"/modifybal {BOB['id']} 0", f"/addadmin {ALICE['id']}", f"/seller {ALICE['id']}",
                     "/close", "/withdraw", "/start", "/status@test_shop_bot"):
            calls = await send_in(t, GROUP, OWNER, text)
            assert calls == [], text
        calls = await press_in(t, GROUP, OWNER, f"dok:{world['deposit']}")
        assert calls == []
        assert snapshot(t) == before
        assert t.db.shop_is_open()

    run(tmp_path, scenario)


def test_a_group_message_does_not_disturb_a_private_flow(tmp_path):
    async def scenario(t: ShopTest):
        await t.press(OWNER, "lb:0:0")
        await t.press(OWNER, "ln:0")
        calls = await send_in(t, GROUP, OWNER, "Group Label")
        assert calls == [] and t.db.children(None) == []
        calls = await send_in(t, GROUP, OWNER, "/cancel")
        assert calls == []
        calls = await t.send(OWNER, "Private Label")
        assert "Added" in t.last_text(calls)
        assert [r["name"] for r in t.db.children(None)] == ["Private Label"]
        # A banned user writing in a group doesn't make the bot talk in the group either.
        register(t, ALICE)
        t.db.set_banned(ALICE["id"], True)
        assert await send_in(t, GROUP, ALICE, "hi") == []

    run(tmp_path, scenario)


def test_buttons_outside_private_chats_channel_posts_and_inline_queries_are_ignored(tmp_path):
    async def scenario(t: ShopTest):
        build_world(t)
        await t.press(ALICE, "dp")
        await t.press(ALICE, "dc")  # Alice is typing a custom deposit amount
        before = snapshot(t)
        calls: list[Call] = []
        calls += await press_in(t, GROUP, ALICE, "sh:0:0")
        calls += await press_in(t, SMALL_GROUP, ALICE, "menu")
        calls += await t._process({"callback_query": {
            "id": "inline-1", "from": ALICE, "chat_instance": "inline", "data": "sh:0:0",
            "inline_message_id": "AAAAAQ"}})
        calls += await send_in(t, CHANNEL, None, "/start")
        calls += await send_in(t, CHANNEL, None, "/status")
        calls += await t._process({"inline_query": {"id": "iq-1", "from": ALICE, "query": "esim", "offset": ""}})
        calls += await t._process({"my_chat_member": {  # Alice blocks and unblocks the bot
            "chat": {"id": ALICE["id"], "type": "private", "first_name": "Alice"}, "from": ALICE,
            "date": int(time.time()),
            "old_chat_member": {"status": "member", "user": BOT_USER},
            "new_chat_member": {"status": "kicked", "user": BOT_USER, "until_date": 0}}})
        assert calls == []
        assert snapshot(t) == before
        # Alice's private flow survived all of that.
        calls = await t.send(ALICE, "25")
        assert "Add 25$ to your balance" in t.last_text(calls)

    run(tmp_path, scenario)


# ============================================ commands, stray messages, /cancel
def test_unknown_commands_get_a_hint_and_end_the_flow(tmp_path):
    async def scenario(t: ShopTest):
        for text in ("/foo", "/foo bar", "/start@some_other_bot", "/addproduct"):
            calls = await t.send(ALICE, text)
            assert UNKNOWN_COMMAND in t.last_text(calls), text
        await t.press(ALICE, "dp")
        await t.press(ALICE, "dc")
        calls = await t.send(ALICE, "/foo")
        assert UNKNOWN_COMMAND in t.last_text(calls)
        calls = await t.send(ALICE, "25")
        assert NO_FLOW_HINT in t.last_text(calls)

    run(tmp_path, scenario)


def test_messages_without_a_flow_get_a_hint_and_change_nothing(tmp_path):
    async def scenario(t: ShopTest):
        build_world(t)
        before = snapshot(t)
        sticker = {"file_id": "stk", "file_unique_id": "ustk", "type": "regular", "width": 512,
                   "height": 512, "is_animated": False, "is_video": False}
        document = {"file_id": "doc", "file_unique_id": "udoc", "file_name": "a.txt", "mime_type": "text/plain"}
        for user in (ALICE, ADMIN, OWNER):
            for send in (lambda u: t.send(u, "hello"), lambda u: t.send(u, "Esim"),
                         lambda u: t.send(u, "9.50"), lambda u: t.send_photo(u, "stray-photo"),
                         lambda u: t.send(u, sticker=sticker), lambda u: t.send(u, document=document)):
                calls = await send(user)
                assert [NO_FLOW_HINT in x for x in t.texts(calls)] == [True], t.texts(calls)
        assert snapshot(t) == before

    run(tmp_path, scenario)


async def _flow_deposit_amount(t: ShopTest, w: dict) -> dict:
    await t.press(ALICE, "dp")
    await t.press(ALICE, "dc")
    return ALICE


async def _flow_deposit_receipt(t: ShopTest, w: dict) -> dict:
    await t.press(ALICE, "dp")
    await t.press(ALICE, "da:20")
    return ALICE


async def _flow_label_name(t: ShopTest, w: dict) -> dict:
    await t.press(OWNER, "lb:0:0")
    await t.press(OWNER, "ln:0")
    return OWNER


async def _flow_label_price(t: ShopTest, w: dict) -> dict:
    await t.press(OWNER, f"lp:{w['ten']}")
    return OWNER


async def _flow_label_rename(t: ShopTest, w: dict) -> dict:
    await t.press(OWNER, f"lr:{w['ten']}")
    return OWNER


async def _flow_ban_id(t: ShopTest, w: dict) -> dict:
    calls = await t.send(OWNER, "/ban")
    assert "Send the user's Telegram ID" in t.last_text(calls)
    return OWNER


async def _flow_addbal_amount(t: ShopTest, w: dict) -> dict:
    await t.send(OWNER, f"/addbal {BOB['id']}")
    return OWNER


async def _flow_quantity(t: ShopTest, w: dict) -> dict:
    await t.press(BOB, f"sh:{w['five']}:0")
    await t.press(BOB, f"sq:{w['five']}")
    return BOB


async def _flow_withdraw_amount(t: ShopTest, w: dict) -> dict:
    await t.send(ADMIN, "/withdraw")
    return ADMIN


async def _flow_add_product(t: ShopTest, w: dict) -> dict:
    await t.press(OWNER, f"ap:{w['five']}:0")
    await t.send(OWNER, "UNSAVED-1")
    return OWNER


CANCEL_FLOWS = {
    "deposit-amount": (_flow_deposit_amount, "25"),
    "deposit-receipt": (_flow_deposit_receipt, "photo"),
    "label-name": (_flow_label_name, "1001"),
    "label-price": (_flow_label_price, "1001"),
    "label-rename": (_flow_label_rename, "1001"),
    "ban-id": (_flow_ban_id, "1001"),
    "addbal-amount": (_flow_addbal_amount, "1001"),
    "custom-quantity": (_flow_quantity, "2"),
    "withdraw-amount": (_flow_withdraw_amount, "10"),
    "add-product": (_flow_add_product, "1001"),
}


@pytest.mark.parametrize("flow", list(CANCEL_FLOWS))
def test_cancel_command_ends_the_flow(tmp_path, flow):
    async def scenario(t: ShopTest):
        world = build_world(t)
        start, answer = CANCEL_FLOWS[flow]
        before = snapshot(t)
        user = await start(t, world)
        calls = await t.send(user, "/cancel")
        assert CANCELLED in all_text(calls, user["id"])
        if flow == "add-product":
            assert any(DISCARDED in x for x in t.texts(calls, user["id"]))
        if answer == "photo":
            calls = await t.send_photo(user, "late-receipt")
        else:
            calls = await t.send(user, answer)
        assert NO_FLOW_HINT in t.last_text(calls), t.texts(calls)
        assert other_chats(calls, user["id"]) == []
        assert snapshot(t) == before
        assert not t.db.is_banned(ALICE["id"])

    run(tmp_path, scenario)


def test_cancel_button_ends_the_flow(tmp_path):
    async def scenario(t: ShopTest):
        build_world(t)
        await t.press(ALICE, "dp")
        await t.press(ALICE, "dc")
        calls = await t.press(ALICE, "cancel")
        assert CANCELLED in t.last_text(calls)
        calls = await t.send(ALICE, "25")
        assert NO_FLOW_HINT in t.last_text(calls)

    run(tmp_path, scenario)


# ========================================================== parallel users
def test_several_users_in_different_flows_do_not_interfere(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        five, oman = world["five"], world["oman"]
        give_balance(t, BOB, 5000)
        stock_before = t.db.count_items(five)
        for user in (OWNER, ALICE, BOB, ADMIN):
            await t.send(user, "/start")
        await t.press(OWNER, f"ln:{oman}")              # owner: naming a label inside Oman
        await t.press(ALICE, "dp")
        await t.press(ALICE, "dc")                      # Alice: typing a deposit amount
        await t.press(BOB, f"sh:{five}:0")
        await t.press(BOB, f"sq:{five}")                # Bob: typing a quantity
        await t.press(ADMIN, f"ap:{five}:0")            # Adam: sending products

        calls = await t.send(ADMIN, "ADMIN-NEW-1")
        assert "Received <b>1</b>" in t.last_text(calls, ADMIN["id"])
        calls = await t.send(ALICE, "25")
        assert "Add 25$ to your balance" in t.last_text(calls)
        calls = await t.send(OWNER, "20GB")
        assert "Added: <b>20GB</b>" in t.last_text(calls)
        bob_calls = await t.send(BOB, "2")
        assert "Confirm your purchase" in t.last_text(bob_calls)
        calls = await t.send(ADMIN, "ADMIN-NEW-2")
        assert "Received <b>2</b>" in t.last_text(calls, ADMIN["id"])
        calls = await t.send_photo(ALICE, "alice-receipt")
        assert "Receipt received for <b>25$</b>" in t.last_text(calls)

        calls = await t.press(BOB, t.button(bob_calls, "Confirm"))
        assert any("Purchase successful" in x for x in t.texts(calls, BOB["id"]))
        calls = await t.press(ADMIN, "apd")
        assert "Saved 2 product(s)" in t.last_text(calls, ADMIN["id"])
        calls = await t.press(ALICE, "dsend")
        to_owner = [c for c in calls if c.chat_id == OWNER_ID and c.method == "sendPhoto"]
        assert len(to_owner) == 1 and to_owner[0].params["photo"] == "alice-receipt"
        # Alice can't use Bob's (already used) confirmation either.
        calls = await t.press(ALICE, t.button(bob_calls, "Confirm"))
        assert EXPIRED in t.alert(calls)

        names = sorted(r["name"] for r in t.db.children(oman))
        assert names == ["10GB", "20GB", "5GB"]
        admin_items = [json.loads(r["content"])["text"] for r in t.db.conn.execute(
            "SELECT content FROM items WHERE owner_id = ? AND content LIKE '%ADMIN-NEW%' ORDER BY id",
            (ADMIN["id"],))]
        assert admin_items == ["ADMIN-NEW-1", "ADMIN-NEW-2"]
        (order,) = t.db.user_orders(BOB["id"], 0, 10)
        assert order["quantity"] == 2 and t.db.get_user(BOB["id"])["balance"] == 5000 - 1900
        assert t.db.count_items(five) == stock_before + 2 - 2
        deposits = t.db.conn.execute(
            "SELECT * FROM deposits WHERE user_id = ?", (ALICE["id"],)).fetchall()
        assert [(d["amount"], d["status"], d["receipt_file_id"]) for d in deposits] == [
            (2500, "pending", "alice-receipt")]
        for user in (OWNER, ALICE, BOB, ADMIN):
            calls = await t.send(user, "leftover")
            assert NO_FLOW_HINT in t.last_text(calls), user["first_name"]

    run(tmp_path, scenario)


def test_two_admins_adding_products_at_the_same_time(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        five = world["five"]
        await t.press(ADMIN, f"ap:{five}:0")
        await t.press(ADMIN2, f"ap:{five}:0")
        for user, text in [(ADMIN, "A-1"), (ADMIN2, "Z-1"), (ADMIN, "A-2"), (ADMIN2, "Z-2"), (ADMIN2, "Z-3")]:
            calls = await t.send(user, text)
            assert t.messages(calls) and all(c.chat_id == user["id"] for c in t.messages(calls))
        calls = await t.press(ADMIN2, "apd")
        assert "Saved 3 product(s)" in t.last_text(calls, ADMIN2["id"])
        calls = await t.press(ADMIN, "apd")
        assert "Saved 2 product(s)" in t.last_text(calls, ADMIN["id"])

        def contents(owner: int) -> list[str]:
            rows = t.db.conn.execute("SELECT content FROM items WHERE owner_id = ? ORDER BY id", (owner,))
            return [json.loads(r["content"])["text"] for r in rows]

        assert contents(ADMIN["id"]) == ["SECRET-ADMIN-1", "A-1", "A-2"]
        assert contents(ADMIN2["id"]) == ["SECRET-ADMIN2-1", "Z-1", "Z-2", "Z-3"]

    run(tmp_path, scenario)


def test_owner_approving_requests_keeps_their_own_add_product_batch(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        five = world["five"]
        stock = t.db.count_items(five)
        await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, "BATCH-1")
        calls = await t.press(OWNER, f"dok:{world['deposit']}", photo=True)
        assert DISCARDED not in all_text(calls)
        await t.send(OWNER, "BATCH-2")
        calls = await t.press(OWNER, f"wno:{world['withdrawal']}")
        assert DISCARDED not in all_text(calls)
        await t.send(OWNER, "BATCH-3")
        calls = await t.press(OWNER, "apd")
        assert "Saved 3 product(s)" in t.last_text(calls, OWNER["id"])
        assert t.db.count_items(five) == stock + 3
        assert t.db.get_user(BOB["id"])["balance"] == 2000
        assert t.db.get_user(ADMIN["id"])["earnings"] == 5000

    run(tmp_path, scenario)


# ============================================== leaving Add Product unsaved
def test_leaving_add_product_by_a_button_warns_and_the_old_done_saves_nothing(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        five = world["five"]
        stock = t.db.count_items(five)
        await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, "UNSAVED-1")
        calls = await t.send(OWNER, "UNSAVED-2")
        assert "Received <b>2</b>" in t.last_text(calls)
        status_done = t.button(calls, "Done")

        calls = await t.press(OWNER, "sh:0:0")
        texts = t.texts(calls, OWNER_ID)
        assert any(f"2 {DISCARDED}" in x for x in texts), texts
        assert any("🛍 <b>Shop</b>" in x for x in texts)

        for data in (status_done, "apd"):
            calls = await t.press(OWNER, data)
            assert NOTHING_TO_SAVE in t.alert(calls)
            assert t.messages(calls) == []
        calls = await t.press(OWNER, "apc")
        assert WENT_WRONG not in all_text(calls)
        assert t.db.count_items(five) == stock
        assert "UNSAVED" not in json.dumps([r["content"] for r in t.db.conn.execute("SELECT content FROM items")])

    run(tmp_path, scenario)


@pytest.mark.parametrize("leave", ["/start", "/help", "/withdraw", "dp", "mp:0:0", "menu"])
def test_leaving_add_product_any_other_way_warns_too(tmp_path, leave):
    async def scenario(t: ShopTest):
        world = build_world(t)
        await t.press(ADMIN, f"ap:{world['five']}:0")
        for n in range(3):
            await t.send(ADMIN, f"UNSAVED-{n}")
        calls = await (t.send(ADMIN, leave) if leave.startswith("/") else t.press(ADMIN, leave))
        assert any(f"3 {DISCARDED}" in x for x in t.texts(calls, ADMIN["id"])), t.texts(calls)
        calls = await t.press(ADMIN, "apd")
        assert NOTHING_TO_SAVE in t.alert(calls)
        assert t.db.count_items(world["five"], ADMIN["id"]) == 1

    run(tmp_path, scenario)


def test_leaving_add_product_without_products_has_no_warning(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        await t.press(OWNER, f"ap:{world['five']}:0")
        calls = await t.press(OWNER, "menu")
        assert DISCARDED not in all_text(calls)
        assert "Welcome Owner" in t.last_text(calls)

    run(tmp_path, scenario)


# ================================================================ HTML safety
NASTY = tg_user(1009, "<b>x</b> & <i>", "nasty_user", last_name="</code><a href='x'>")
NASTY_HTML = "&lt;b&gt;x&lt;/b&gt; &amp; &lt;i&gt;"
EVIL_ADMIN = tg_user(2009, "<i>Adm & Co</i>", "evil_admin")
EVIL_ADMIN_HTML = "&lt;i&gt;Adm &amp; Co&lt;/i&gt;"


def test_html_special_characters_are_escaped_in_every_message(tmp_path):
    async def scenario(t: ShopTest):
        def check(calls: list[Call], *expected: str, chat_id: Optional[int] = None) -> None:
            text = all_text(calls, chat_id)
            for piece in expected:
                assert piece in text, (piece, t.texts(calls, chat_id))

        # Owner builds  <Esim> & Co › Oman <b> › 5GB > 3GB & <x>  through the bot.
        await t.send(OWNER, "/start")
        await t.press(OWNER, "lb:0:0")
        await t.press(OWNER, "ln:0")
        calls = await t.send(OWNER, "<Esim> & Co")
        check(calls, "Added: <b>&lt;Esim&gt; &amp; Co</b>")
        assert "<Esim> & Co · no price" in button_texts(calls)  # buttons show the raw name
        top = t.db.children(None)[0]["id"]
        calls = await t.press(OWNER, t.button(calls, "<Esim> & Co"))
        check(calls, "📂 &lt;Esim&gt; &amp; Co")
        calls = await t.press(OWNER, t.button(calls, "New label inside"))
        check(calls, "inside <b>&lt;Esim&gt; &amp; Co</b>")
        await t.send(OWNER, "Oman <b>")
        mid = t.db.children(top)[0]["id"]
        await t.press(OWNER, f"lb:{mid}:0")
        await t.press(OWNER, f"ln:{mid}")
        calls = await t.send(OWNER, "5GB > 3GB & <x>")
        check(calls, "Added: <b>5GB &gt; 3GB &amp; &lt;x&gt;</b>")
        leaf = t.db.children(mid)[0]["id"]
        calls = await t.press(OWNER, f"lp:{leaf}")
        check(calls, "Price for 5GB &gt; 3GB &amp; &lt;x&gt;", "&lt;Esim&gt; &amp; Co › Oman &lt;b&gt; › 5GB")
        calls = await t.send(OWNER, "9.50")
        check(calls, "Price of <b>5GB &gt; 3GB &amp; &lt;x&gt;</b> set to <b>9.50$</b>")
        await t.press(OWNER, f"lr:{mid}")
        calls = await t.send(OWNER, "Oman </b><i>")
        check(calls, "Renamed to <b>Oman &lt;/b&gt;&lt;i&gt;</b>")
        calls = await t.press(OWNER, f"ld:{mid}")
        check(calls, "Delete <b>&lt;Esim&gt; &amp; Co › Oman &lt;/b&gt;&lt;i&gt;</b>")
        await t.press(OWNER, f"lb:{mid}:0")

        # An admin with an HTML-looking name stocks the product.
        await t.send(EVIL_ADMIN, "/start")
        calls = await t.send(OWNER, f"/addadmin {EVIL_ADMIN['id']}")
        check(calls, EVIL_ADMIN_HTML, chat_id=OWNER_ID)
        calls = await t.press(EVIL_ADMIN, f"ap:{leaf}:0")
        check(calls, "Product: <b>&lt;Esim&gt; &amp; Co › Oman &lt;/b&gt;&lt;i&gt; › 5GB &gt; 3GB &amp; &lt;x&gt;</b>")
        await t.send(EVIL_ADMIN, "<b>not bold</b> & co")
        await t.send(EVIL_ADMIN, "second <code>")
        calls = await t.send(EVIL_ADMIN, "third & last")
        check(calls, "for <b>5GB &gt; 3GB &amp; &lt;x&gt;</b>")
        calls = await t.press(EVIL_ADMIN, "apd")
        check(calls, "Saved 3 product(s)")
        await t.press(OWNER, f"ap:{leaf}:0")
        await t.send(OWNER, "owner & <pre>")
        await t.press(OWNER, "apd")

        # A customer with an HTML-looking name deposits and buys.
        calls = await t.send(NASTY, "/start")
        check(calls, f"Welcome {NASTY_HTML}")
        await t.press(NASTY, "dp")
        await t.press(NASTY, "da:20")
        await t.send_photo(NASTY, "nasty-receipt")
        calls = await t.press(NASTY, "dsend")
        (request,) = [c for c in calls if c.chat_id == OWNER_ID and c.method == "sendPhoto"]
        assert f"{NASTY_HTML} &lt;/code&gt;&lt;a href='x'&gt;" in request.text
        calls = await t.press(OWNER, t.button([request], "Approve"), photo=True)
        check(calls, NASTY_HTML, chat_id=OWNER_ID)

        calls = await t.press(NASTY, "sh:0:0")
        calls = await t.press(NASTY, t.button(calls, "<Esim> & Co"))
        calls = await t.press(NASTY, t.button(calls, "Oman </b><i>"))
        calls = await t.press(NASTY, t.button(calls, "5GB > 3GB & <x>"))
        check(calls, "🛒 <b>5GB &gt; 3GB &amp; &lt;x&gt;</b>")
        calls = await t.press(NASTY, f"sb:{leaf}:2")
        check(calls, "&lt;Esim&gt; &amp; Co")
        calls = await t.press(NASTY, t.button(calls, "Confirm"))
        check(calls, "Purchase successful", "📦 &lt;Esim&gt; &amp; Co", chat_id=NASTY["id"])
        delivered = [c for c in calls if c.chat_id == NASTY["id"] and c.method == "sendMessage"
                     and c.params.get("text") in ("<b>not bold</b> & co", "second <code>")]
        assert len(delivered) == 2
        assert all(not c.params.get("parse_mode") for c in delivered)  # sent exactly as typed
        check(calls, "New sale!", "&lt;Esim&gt; &amp; Co", chat_id=EVIL_ADMIN["id"])

        calls = await t.press(NASTY, "mo:0")
        order = t.db.user_orders(NASTY["id"], 0, 1)[0]
        calls = await t.press(NASTY, f"mov:{order['id']}")
        check(calls, "📦 &lt;Esim&gt; &amp; Co", chat_id=NASTY["id"])

        # Owner reports and commands that show names.
        calls = await t.send(OWNER, "/status")
        check(calls, NASTY_HTML, EVIL_ADMIN_HTML, "@nasty_user")
        calls = await t.press(OWNER, "ea")
        check(calls, EVIL_ADMIN_HTML)
        calls = await t.press(OWNER, f"mi:{leaf}:0")
        assert any(b.startswith("🆔 ") and "<i>Adm" in b for b in button_texts(calls))
        calls = await t.press(OWNER, t.button(calls, "<i>Adm"))
        check(calls, f"Added by: {EVIL_ADMIN_HTML}", "third & last")
        calls = await t.send(OWNER, f"/addbal {NASTY['id']} 5")
        check(calls, NASTY_HTML)
        calls = await t.send(OWNER, f"/modifybal {NASTY['id']}")
        check(calls, NASTY_HTML)
        await t.send(OWNER, "/cancel")
        calls = await t.send(OWNER, f"/seller {NASTY['id']}")
        check(calls, NASTY_HTML)
        calls = await t.send(OWNER, f"/ban {NASTY['id']}")
        check(calls, NASTY_HTML, "is now <b>banned</b>")
        calls = await t.send(OWNER, f"/unban {NASTY['id']}")
        check(calls, NASTY_HTML)
        calls = await t.send(OWNER, "/ban <b>&</b>")
        check(calls, "&lt;b&gt;&amp;&lt;/b&gt;")

        # The admin withdraws to an HTML-looking Binance ID.
        await t.send(EVIL_ADMIN, "/withdraw")
        await t.send(EVIL_ADMIN, "5")
        calls = await t.send(EVIL_ADMIN, "<b>id</b>&1")
        check(calls, "<code>&lt;b&gt;id&lt;/b&gt;&amp;1</code>", chat_id=EVIL_ADMIN["id"])
        check(calls, EVIL_ADMIN_HTML, "<code>&lt;b&gt;id&lt;/b&gt;&amp;1</code>", chat_id=OWNER_ID)
        calls = await t.press(OWNER, t.button(calls, "Approve"))
        check(calls, "<code>&lt;b&gt;id&lt;/b&gt;&amp;1</code>", chat_id=EVIL_ADMIN["id"])

        # Every message of the whole session: valid HTML, names escaped, never doubled.
        assert_within_limits(t.tg.calls)
        for call in t.messages(t.tg.calls):
            text = call.text or ""
            if str(call.params.get("parse_mode") or "").upper() == "HTML":
                assert NASTY["first_name"] not in text and EVIL_ADMIN["first_name"] not in text, text
            else:
                assert "&lt;" not in text and "&amp;" not in text, text  # no double escaping
        assert not [b for b in button_texts(t.tg.calls) if "&lt;" in b or "&amp;" in b]

    run(tmp_path, scenario)


def test_emoji_and_arabic_labels_work_end_to_end(tmp_path):
    async def scenario(t: ShopTest):
        country, provider, plan = "🇴🇲 عُمان", "📶 فودافون", "👨‍👩‍👧 باقة العائلة 10GB"
        buyer = tg_user(1050, "محمد", "mohd", last_name="البوسعيدي")
        await t.send(OWNER, "/start")
        await t.press(OWNER, "lb:0:0")
        await t.press(OWNER, "ln:0")
        calls = await t.send(OWNER, country)
        assert f"Added: <b>{country}</b>" in t.last_text(calls)
        calls = await t.press(OWNER, t.button(calls, country))
        calls = await t.press(OWNER, t.button(calls, "New label inside"))
        await t.send(OWNER, provider)
        top = t.db.children(None)[0]["id"]
        mid = t.db.children(top)[0]["id"]
        await t.press(OWNER, f"lb:{mid}:0")
        await t.press(OWNER, f"ln:{mid}")
        calls = await t.send(OWNER, f"{plan}\n{country}")
        assert plan in t.last_text(calls)
        leaf = [r for r in t.db.children(mid) if r["name"] == plan][0]["id"]
        # The same name again at the top level is a duplicate.
        await t.press(OWNER, "ln:0")
        calls = await t.send(OWNER, country)
        assert "Already existed" in t.last_text(calls) and len(t.db.children(None)) == 1
        await t.press(OWNER, f"lp:{leaf}")
        calls = await t.send(OWNER, "7.25")
        assert "7.25$" in t.last_text(calls)

        # A product in Arabic with formatting (UTF-16 offsets).
        prefix = "رمز التفعيل 🔑: "
        code = "ABC-123"
        product = f"{prefix}{code} ✅"
        entities = [{"type": "bold", "offset": utf16_len(prefix), "length": len(code)}]
        await t.press(OWNER, f"ap:{leaf}:0")
        await t.send(OWNER, product, entities=entities)
        calls = await t.press(OWNER, "apd")
        assert "Saved 1 product(s)" in t.last_text(calls, OWNER["id"])
        assert f"{country} › {provider} › {plan}" in t.last_text(calls)

        # A customer with an Arabic name buys it by pressing the emoji/Arabic buttons.
        give_balance(t, buyer, 1000)
        calls = await t.send(buyer, "/start")
        assert "Welcome محمد" in t.last_text(calls)
        calls = await t.press(buyer, "sh:0:0")
        calls = await t.press(buyer, t.button(calls, country))
        calls = await t.press(buyer, t.button(calls, provider))
        calls = await t.press(buyer, t.button(calls, plan))
        assert f"🛒 <b>{plan}</b>" in t.last_text(calls)
        calls = await t.press(buyer, f"sb:{leaf}:1")
        calls = await t.press(buyer, t.button(calls, "Confirm"))
        (delivered,) = [c for c in calls if c.chat_id == buyer["id"] and c.params.get("text") == product]
        assert [(e["type"], e["offset"], e["length"]) for e in delivered.params["entities"]] == [
            ("bold", utf16_len(prefix), len(code))]
        (order,) = t.db.user_orders(buyer["id"], 0, 10)
        assert order["product_name"] == f"{country} › {provider} › {plan}"
        assert t.db.get_user(buyer["id"])["balance"] == 1000 - 725
        calls = await t.press(buyer, "mo:0")
        assert t.has_button(calls, "#")

        # Sales analysis keeps the names intact.
        calls = await t.press(OWNER, "ma")
        (document,) = t.documents(calls)
        assert f"{country} > {provider} > {plan}" in t.document_text(document)
        # Renaming to emoji only works too.
        await t.press(OWNER, f"lr:{leaf}")
        calls = await t.send(OWNER, "🔥🔥🔥")
        assert "Renamed to <b>🔥🔥🔥</b>" in t.last_text(calls)
        assert_within_limits(t.tg.calls)

    run(tmp_path, scenario)


def test_sales_analysis_csv_neutralises_formulas_in_names(tmp_path):
    async def scenario(t: ShopTest):
        evil = tg_user(2010, "=HYPERLINK(\"http://x\")", None, last_name="+cmd")
        make_admin(t, evil)
        five = make_product(t, "=SUM(A1)/@5GB", 100)
        t.db.add_items(five, evil["id"], [text_item("x")])
        calls = await t.press(OWNER, "ma")
        (document,) = t.documents(calls)
        for row in csv_rows(document):
            for cell in row:
                assert not cell[:1] in ("=", "+", "-", "@"), row

    run(tmp_path, scenario)


# ===================================================== Telegram limits at scale
FLAGS = ["🇴🇲", "🇦🇪", "🇸🇦", "🇶🇦", "🇰🇼", "🇧🇭", "🇪🇬", "🇯🇴"]


def test_big_catalog_stays_within_telegram_limits(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        await t.send(OWNER, "/start")
        # 60 top-level labels in one message.
        countries = [f"{FLAGS[i % len(FLAGS)]} Country {i:02d} <&> بلد" for i in range(60)]
        await t.press(OWNER, "lb:0:0")
        await t.press(OWNER, "ln:0")
        calls = await t.send(OWNER, "\n".join(countries))
        assert len(t.db.children(None)) == 60
        calls = await t.press(OWNER, t.button(calls, "▶️"))
        assert "2/2" in button_texts(calls)
        first = t.db.children(None)[0]["id"]
        # 40 providers inside the first country, then the plans.
        await t.press(OWNER, f"lb:{first}:0")
        await t.press(OWNER, f"ln:{first}")
        await t.send(OWNER, "\n".join(f"Provider {i:02d} & <Co> مزود" for i in range(40)))
        provider = t.db.children(first)[0]["id"]
        await t.press(OWNER, f"lb:{first}:1")
        await t.press(OWNER, f"lb:{provider}:0")
        await t.press(OWNER, f"ln:{provider}")
        await t.send(OWNER, "5GB\n10GB\n20GB\n50GB\n100GB")
        plans = {r["name"]: r["id"] for r in t.db.children(provider)}
        for name, price in zip(plans, ("9.50", "15", "25.99", "49", "89.90")):
            await t.press(OWNER, f"lp:{plans[name]}")
            await t.send(OWNER, price)
        # 40 more countries become priced products, so customers get several shop pages.
        for row in t.db.children(None)[1:41]:
            t.db.set_label_price(row["id"], 100 + row["id"])

        # 100 products through Add Product, browsing the label pages on the way.
        calls = await t.press(OWNER, "ap:0:0")
        calls = await t.press(OWNER, t.button(calls, "▶️"))
        calls = await t.press(OWNER, f"ap:{first}:0")
        calls = await t.press(OWNER, t.button(calls, "▶️"))
        calls = await t.press(OWNER, f"ap:{provider}:0")
        calls = await t.press(OWNER, t.button(calls, "5GB"))
        assert "ONE MESSAGE PER PRODUCT" in t.last_text(calls)
        for i in range(100):
            if i % 10 == 9:
                caption = f"QR {i:03d} — صورة <b>&"
                await t.send(OWNER, photo=photo_sizes(f"qr-{i}"), caption=caption,
                             caption_entities=[{"type": "italic", "offset": 0, "length": 6}])
            else:
                text = f"CODE-{i:03d} 🔑 كود <b>raw</b> & more"
                await t.send(OWNER, text, entities=[{"type": "bold", "offset": 0, "length": 8}])
        calls = await t.press(OWNER, "apd")
        assert "Saved 100 product(s)" in t.last_text(calls, OWNER["id"]) and "(+40 more)" in t.last_text(calls, OWNER["id"])
        # An admin stocks another plan.
        await t.press(ADMIN, f"ap:{plans['10GB']}:0")
        for i in range(20):
            await t.send(ADMIN, f"ADMIN-CODE-{i:02d}")
        await t.press(ADMIN, "apd")

        # Browse every page everyone can reach.
        presses = [(OWNER, "sh:0:0"), (OWNER, "sh:0:1"), (OWNER, "sh:0:2"), (OWNER, f"sh:{first}:0"),
                   (OWNER, f"sh:{first}:1"), (OWNER, f"sh:{provider}:0"), (OWNER, f"sh:{plans['5GB']}:0"),
                   (ALICE, "sh:0:0"), (ALICE, "sh:0:1"), (ALICE, "sh:0:2"), (ALICE, f"sh:{first}:0"),
                   (ALICE, f"sh:{provider}:0"), (ALICE, f"sh:{plans['5GB']}:0"),
                   (OWNER, "lb:0:0"), (OWNER, "lb:0:1"), (OWNER, f"lb:{first}:0"), (OWNER, f"lb:{first}:1"),
                   (OWNER, f"lb:{plans['5GB']}:0"), (OWNER, f"ld:{first}"),
                   (OWNER, "ap:0:0"), (OWNER, "ap:0:1"), (OWNER, f"ap:{first}:1"),
                   (OWNER, "mp:0:0"), (OWNER, "mp:0:1"), (OWNER, f"mp:{first}:0"), (OWNER, f"mp:{first}:1"),
                   (OWNER, f"mp:{provider}:0"), (OWNER, f"mi:{plans['5GB']}:0"), (OWNER, f"mi:{plans['5GB']}:1"),
                   (OWNER, f"mi:{plans['5GB']}:3"), (OWNER, f"mi:{plans['10GB']}:0"),
                   (ADMIN, "mp:0:0"), (ADMIN, f"mp:{provider}:0"), (ADMIN, f"mi:{plans['10GB']}:0"),
                   (ADMIN, "ap:0:0"), (ADMIN, "ap:0:1")]
        for user, data in presses:
            calls = await t.press(user, data)
            assert WENT_WRONG not in all_text(calls), data
        calls = await t.press(OWNER, f"mi:{plans['5GB']}:0")
        item = t.button(calls, "🆔")
        calls = await t.press(OWNER, item)
        assert "Product ID" in t.last_text(calls)
        calls = await t.press(OWNER, "ma")
        assert t.documents(calls)

        assert_within_limits(t.tg.calls)
        produced = set(datas(t.tg.calls))
        assert {"lb:0:1", "sh:0:2", "ap:0:1", f"mi:{plans['5GB']}:2"} <= produced  # paging happened

    run(tmp_path, scenario)


def test_status_with_120_users_stays_within_telegram_limits(tmp_path):
    async def scenario(t: ShopTest):
        patterns = [
            lambda i: (f"Mohammed {i}", "Al-Habsi", f"mohd_{i}"),
            lambda i: ("Abdulrahman " + "N" * 52, "Al-" + "M" * 61, f"long_name_user_{i:03d}x" + "y" * 10),
            lambda i: (f"🌟 Star {i} 🚀🔥", "💎", None),
            lambda i: ("محمد بن سعيد", f"البوسعيدي {i}", f"arabic_{i}"),
            lambda i: ("<b>x</b> & <i>", f"&amp; {i}", f"html_{i}"),
            lambda i: (f"Plain{i}", None, None),
        ]
        for i in range(120):
            first, last, username = patterns[i % len(patterns)](i)
            uid = 10_000 + i
            t.db.touch_user(uid, first, last, username)
            if i % 3 == 0:
                t.db.manual_deposit(uid, 100 * (i + 1), 10**12)
            if i % 17 == 0:
                t.db.set_admin(uid, True)
            if i % 19 == 0:
                t.db.set_banned(uid, True)
        for i in range(3):
            await t.send(tg_user(20_000 + i, f"Late {i}", f"late_{i}"), "/start")
        calls = await t.send(OWNER, "/status")
        messages = [c for c in t.messages(calls) if c.method == "sendMessage"]
        assert len(messages) >= 2
        (document,) = t.documents(calls)
        rows = csv_rows(document)
        assert len(rows) == 1 + 120 + 3 + 1  # header + users + late starters + owner
        assert_within_limits(calls)
        assert WENT_WRONG not in all_text(calls)

    run(tmp_path, scenario)


def test_big_purchase_orders_and_reports_stay_within_telegram_limits(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        make_admin(t, ADMIN2)
        five = make_product(t, "Esim/Oman <&>/5GB", 950)
        one = make_product(t, "Esim/Oman <&>/1GB", 150)
        t.db.add_items(five, OWNER_ID, [text_item(f"OWNER-{i:02d}") for i in range(40)])
        t.db.add_items(five, ADMIN["id"], [text_item(f"ADAM-{i:02d}") for i in range(20)])
        t.db.add_items(five, ADMIN2["id"], [text_item(f"ZARA-{i:02d}") for i in range(10)])
        t.db.add_items(one, OWNER_ID, [text_item(f"ONE-{i:02d}") for i in range(12)])
        buyer = tg_user(1077, "Big Buyer " + "B" * 40, "big_buyer")
        await t.send(buyer, "/start")
        await t.send(OWNER, f"/addbal {buyer['id']} 1000")

        await t.press(buyer, "sh:0:0")
        calls = await t.press(buyer, f"sh:{five}:0")
        calls = await t.press(buyer, t.button(calls, "Other amount"))
        assert "from 1 to 70" in t.last_text(calls)
        calls = await t.send(buyer, "65")  # all of Adam's 20 and Zara's 10, 35 of the owner's
        calls = await t.press(buyer, t.button(calls, "Confirm"))
        summary = [x for x in t.texts(calls, buyer["id"]) if "Purchase successful" in x]
        assert summary and "(+15)" in summary[0]
        delivered = [c for c in calls if c.chat_id == buyer["id"] and c.method == "sendMessage"
                     and re.match(r"(OWNER|ADAM|ZARA)-\d\d$", c.params.get("text", ""))]
        assert len(delivered) == 65
        assert any("New sale!" in x for x in t.texts(calls, ADMIN["id"]))
        assert any("New sale!" in x for x in t.texts(calls, ADMIN2["id"]))
        for _ in range(9):  # nine small orders: My Orders gets a second page
            await t.press(buyer, f"sh:{one}:0")
            calls = await t.press(buyer, f"sb:{one}:1")
            await t.press(buyer, t.button(calls, "Confirm"))
        assert t.db.count_user_orders(buyer["id"]) == 10
        calls = await t.press(buyer, "mo:0")
        assert t.has_button(calls, "▶️")
        await t.press(buyer, "mo:1")
        big = [o for o in t.db.user_orders(buyer["id"], 0, 20) if o["quantity"] == 65][0]
        calls = await t.press(buyer, f"mov:{big['id']}")
        assert len([c for c in calls if c.chat_id == buyer["id"] and c.method == "sendMessage"]) == 67

        for user in (OWNER, ADMIN, ADMIN2):
            await t.press(user, "ea")
            await t.press(user, "ma")
            await t.send(user, "/help")
        await t.send(buyer, "/help")
        await t.send(OWNER, "/status")
        await t.press(OWNER, f"mi:{five}:0")
        assert_within_limits(t.tg.calls)
        assert WENT_WRONG not in all_text(t.tg.calls)

    run(tmp_path, scenario)


def test_pasting_many_labels_in_one_message_gets_a_confirmation_telegram_accepts(tmp_path):
    async def scenario(t: ShopTest):
        await t.press(OWNER, "lb:0:0")
        await t.press(OWNER, "ln:0")
        names = [f"Europe {i:03d} · 30 days · 10GB" for i in range(145)]
        paste = "\n".join(names)
        assert utf16_len(paste) <= 4096  # one ordinary Telegram message
        calls = await t.send(OWNER, paste)
        assert len(t.db.children(None)) == 145
        assert_within_limits(calls)

    run(tmp_path, scenario)


def test_owner_earnings_with_many_admins_fits_one_message(tmp_path):
    async def scenario(t: ShopTest):
        five = make_product(t, "Esim/5GB", 12345)
        buyer = tg_user(1090, "Buyer")
        give_balance(t, buyer, 10**9)
        for i in range(30):
            admin = tg_user(5000 + i, f"Abdulrahman Mohammed {i:02d}", f"admin_{i:02d}", last_name="Al-Habsi Reseller")
            make_admin(t, admin)
            t.db.add_items(five, admin["id"], [text_item(f"A{i}-{n}") for n in range(3)])
        t.db.purchase(buyer["id"], five, 90, 10)
        for i in range(30):
            t.db.create_withdrawal(5000 + i, 1234, f"binance-{i}", auto_approve=False)
        calls = await t.press(OWNER, "ea")
        assert "…and 5 more" in t.last_text(calls)
        assert_within_limits(calls)

    run(tmp_path, scenario)


def test_bot_profile_and_command_menus_respect_telegram_limits(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        mark = len(t.tg.calls)
        await setup_bot_profile(t.app)
        await t.send(OWNER, "/start")
        await t.send(OWNER, f"/addadmin {ALICE['id']}")
        calls = t.tg.calls[mark:]
        descriptions = [c.params.get("description", "") for c in calls if c.method == "setMyDescription"]
        shorts = [c.params.get("short_description", "") for c in calls if c.method == "setMyShortDescription"]
        assert descriptions and all(len(d) <= 512 for d in descriptions)
        assert shorts and all(len(s) <= 120 for s in shorts)
        menus = [c for c in calls if c.method == "setMyCommands"]
        assert len(menus) >= 3
        for menu in menus:
            commands = menu.params["commands"]
            if isinstance(commands, str):
                commands = json.loads(commands)
            assert 1 <= len(commands) <= 100
            for command in commands:
                assert re.fullmatch(r"[a-z0-9_]{1,32}", command["command"]), command
                assert 1 <= len(command["description"]) <= 256, command
        assert_within_limits(calls)

    run(tmp_path, scenario)


# ===================================================== presses Telegram can't answer
def test_a_press_that_cannot_be_answered_still_shows_the_page(tmp_path):
    async def scenario(t: ShopTest):
        build_world(t)
        await t.send(ALICE, "/start")
        t.tg.fail["answerCallbackQuery"] = TOO_OLD
        calls = await t.press(ALICE, "sh:0:0")
        assert WENT_WRONG not in all_text(calls)
        assert "🛍 <b>Shop</b>" in all_text(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_a_pull_press_that_cannot_be_answered_does_not_lose_the_product(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        t.tg.fail["answerCallbackQuery"] = TOO_OLD
        calls = await t.press(OWNER, f"mu:{world['owner_item']}")
        status = t.db.get_item(world["owner_item"])["status"]
        delivered = "SECRET-OWNER-1" in t.texts(calls, OWNER_ID)
        assert status == "available" or delivered, (status, t.texts(calls))

    run(tmp_path, scenario)


def test_a_deposit_confirm_press_that_cannot_be_answered_still_reaches_the_owner(tmp_path):
    async def scenario(t: ShopTest):
        build_world(t)
        await t.press(ALICE, "dp")
        await t.press(ALICE, "da:20")
        await t.send_photo(ALICE, "alice-receipt")
        t.tg.fail["answerCallbackQuery"] = TOO_OLD
        calls = await t.press(ALICE, "dsend")
        pending = t.db.conn.execute(
            "SELECT id FROM deposits WHERE user_id = ? AND status = 'pending'", (ALICE["id"],)).fetchall()
        offered = set(datas([c for c in calls if c.chat_id == OWNER_ID]))
        for row in pending:
            assert f"dok:{row['id']}" in offered, "deposit request stored but never shown to the owner"

    run(tmp_path, scenario)


def test_a_done_press_that_cannot_be_answered_still_reports_the_saved_products(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        stock = t.db.count_items(world["five"])
        await t.press(OWNER, f"ap:{world['five']}:0")
        await t.send(OWNER, "NEW-1")
        await t.send(OWNER, "NEW-2")
        t.tg.fail["answerCallbackQuery"] = TOO_OLD
        calls = await t.press(OWNER, "apd")
        assert t.db.count_items(world["five"]) == stock + 2
        assert WENT_WRONG not in all_text(calls)
        assert "Saved 2 product(s)" in all_text(calls, OWNER_ID)

    run(tmp_path, scenario)


# ================================================================ double presses
def test_double_presses_do_not_repeat_one_shot_actions(tmp_path):
    async def scenario(t: ShopTest):
        world = build_world(t)
        give_balance(t, ALICE, 5000)
        five = world["five"]
        t.db.rng = Picks(OWNER_ID)  # Adam's item stays in stock for the pull below

        calls = await t.press(ALICE, f"sb:{five}:1")
        confirm = t.button(calls, "Confirm")
        await t.press(ALICE, confirm)
        calls = await t.press(ALICE, confirm)
        assert EXPIRED in t.alert(calls)
        assert len(t.db.user_orders(ALICE["id"], 0, 10)) == 1
        assert t.db.get_user(ALICE["id"])["balance"] == 5000 - 950

        await t.press(OWNER, f"dok:{world['deposit']}", photo=True)
        for data in (f"dok:{world['deposit']}", f"dno:{world['deposit']}"):
            calls = await t.press(OWNER, data, photo=True)
            assert "already approved" in t.alert(calls), data
        assert t.db.get_user(BOB["id"])["balance"] == 2000

        await t.press(OWNER, f"wno:{world['withdrawal']}")
        for data in (f"wno:{world['withdrawal']}", f"wok:{world['withdrawal']}"):
            calls = await t.press(OWNER, data)
            assert "already rejected" in t.alert(calls), data
        assert t.db.get_user(ADMIN["id"])["earnings"] == 5000

        item = world["admin_item"]
        await t.press(ADMIN, f"mu:{item}")
        calls = await t.press(ADMIN, f"mu:{item}")
        assert "no longer in stock" in t.alert(calls)
        assert "SECRET-ADMIN-1" not in t.texts(calls)
        calls = await t.press(ADMIN, f"mry:{item}")
        assert "no longer in stock" in t.alert(calls)

        await t.press(OWNER, f"ldy:{world['ten']}")
        calls = await t.press(OWNER, f"ldy:{world['ten']}")
        assert "already deleted" in t.alert(calls)

        stock = t.db.count_items(five)
        await t.press(OWNER, f"ap:{five}:0")
        await t.send(OWNER, "ONCE")
        await t.press(OWNER, "apd")
        calls = await t.press(OWNER, "apd")
        assert NOTHING_TO_SAVE in t.alert(calls)
        assert t.db.count_items(five) == stock + 1

        await t.press(ALICE, "dp")
        await t.press(ALICE, "da:10")
        await t.send_photo(ALICE, "alice-receipt")
        await t.press(ALICE, "dsend")
        calls = await t.press(ALICE, "dsend")
        assert EXPIRED in t.alert(calls)
        assert t.db.conn.execute("SELECT COUNT(*) FROM deposits WHERE user_id = ?",
                                 (ALICE["id"],)).fetchone()[0] == 2  # the /addbal-style one + this one

    run(tmp_path, scenario)
