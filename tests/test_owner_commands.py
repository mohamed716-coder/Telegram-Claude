"""Owner commands, panels, /help and /start — end to end through the real bot.

Covers /addbal /modifybal /addadmin /removeadmin /seller /removeseller /ban /unban
/close /open /status (and that nobody but the owner can use them), the role-based
/help, the /start panels (welcome text, balance, buttons and their colours) and the
bot profile (description and command menus).

Fixtures (labels, stock, roles) are mostly created straight in the database so each
test stays focused; the behaviour under test is driven with real commands, messages
and button presses, and checked both in the database and in what users see.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from typing import Any, Callable, Optional

import pytest

from harness import OWNER, OWNER_ID, Call, ShopTest, run, tg_user
from shopbot.config import Settings
from shopbot.handlers.common import setup_bot_profile

ALICE = tg_user(1001, "Alice", "alice")
BOB = tg_user(1002, "Bob", "bob")
CAROL = tg_user(1003, "Carol", "carol")
DAVE = tg_user(1004, "Dave", "dave")
ADMIN = tg_user(2001, "Adam", "adam")
ADMIN2 = tg_user(2002, "Zara", "zara")

OWNER_ONLY = "Only the owner can use this command"
BANNED = "You are banned"
CLOSED = "shop is closed right now"
NO_STATE = "Send /start to open the shop menu"
ASK_ID = "Send the user's Telegram ID"

OWNER_COMMAND_NAMES = {
    "addbal", "modifybal", "addadmin", "removeadmin", "seller", "removeseller",
    "ban", "unban", "close", "open", "status", "pending", "announce",
}


# =================================================================== helpers
def register(t: ShopTest, user: dict) -> None:
    t.db.touch_user(user["id"], user["first_name"], user.get("last_name"), user.get("username"))


def make_admin(t: ShopTest, user: dict) -> None:
    register(t, user)
    t.db.set_admin(user["id"], True)


def uid_of(user: Any) -> int:
    return user["id"] if isinstance(user, dict) else int(user)


def row(t: ShopTest, user: Any):
    return t.db.get_user(uid_of(user))


def balance(t: ShopTest, user: Any) -> int:
    return row(t, user)["balance"]


def deposits_of(t: ShopTest, user: Any) -> list:
    return t.db.conn.execute(
        "SELECT * FROM deposits WHERE user_id = ? ORDER BY id", (uid_of(user),)
    ).fetchall()


def ledger_of(t: ShopTest, user: Any) -> list[tuple[str, int]]:
    rows = t.db.conn.execute(
        "SELECT reason, delta FROM ledger WHERE user_id = ? ORDER BY id", (uid_of(user),)
    ).fetchall()
    return [(r["reason"], r["delta"]) for r in rows]


def orders_of(t: ShopTest, user: Any) -> list:
    return t.db.user_orders(uid_of(user), 0, 1000)


def make_product(t: ShopTest, path: str = "Esim/Oman/5GB", price: int = 950,
                 stock: int = 3, owner_id: int = OWNER_ID) -> int:
    """Create the labels of 'A/B/C', price the last one, add text stock; return its id."""
    parent: Optional[int] = None
    for name in path.split("/"):
        found = [r for r in t.db.children(parent) if r["name"] == name]
        parent = found[0]["id"] if found else t.db.add_labels(parent, [name])[0][0]["id"]
    assert parent is not None
    t.db.set_label_price(parent, price)
    if stock:
        t.db.add_items(parent, owner_id, [
            {"kind": "text", "text": f"CODE-{i}", "entities": []} for i in range(stock)
        ])
    return parent


def parent_of(t: ShopTest, label_id: int) -> int:
    return t.db.get_label(label_id)["parent_id"] or 0


def texts_to(calls: list[Call], user: Any) -> list[str]:
    return ShopTest.texts(calls, uid_of(user))


def button_texts(calls: list[Call]) -> list[str]:
    return [b.get("text", "") for b in ShopTest.all_buttons(calls)]


def datas(calls: list[Call]) -> list[str]:
    return [b.get("callback_data", "") for b in ShopTest.all_buttons(calls)]


def last_message(calls: list[Call], user: Any = None) -> Call:
    msgs = ShopTest.messages(calls, None if user is None else uid_of(user))
    assert msgs, f"no message; calls: {[c.method for c in calls]}"
    return msgs[-1]


def styles_of(message: Call) -> dict[str, Optional[str]]:
    return {b.get("text", ""): b.get("style") for b in message.buttons}


def assert_panel_buttons(message: Call, expected: dict[str, str]) -> None:
    """The panel has exactly these buttons (matched by substring) with these styles."""
    styles = styles_of(message)
    assert len(styles) == len(expected), f"panel buttons: {list(styles)}"
    for label, style in expected.items():
        matches = [s for text, s in styles.items() if label in text]
        assert matches == [style], f"{label!r}: {matches} in {styles}"
    for unwanted in ("Language", "Bulk"):
        assert not any(unwanted in text for text in styles), f"{unwanted} button: {list(styles)}"


def scoped_commands(calls: list[Call], chat_id: int) -> Optional[list[str]]:
    """Command names of the last setMyCommands call scoped to ``chat_id`` (None if none)."""
    found = []
    for c in calls:
        if c.method != "setMyCommands":
            continue
        scope = c.params.get("scope") or {}
        if isinstance(scope, str):
            scope = json.loads(scope)
        if scope.get("type") == "chat" and int(scope.get("chat_id", 0)) == chat_id:
            found.append(c)
    if not found:
        return None
    commands = found[-1].params["commands"]
    if isinstance(commands, str):
        commands = json.loads(commands)
    return [cmd["command"] for cmd in commands]


def utf16_len(text: str) -> int:
    """Telegram counts message length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def help_commands(text: str) -> set[str]:
    """Every /command mentioned in a help text (not HTML closing tags)."""
    return set(re.findall(r"(?:^|\s)/([a-z_]+)\b", text, flags=re.M))


def status_messages(calls: list[Call]) -> list[Call]:
    return [c for c in calls if c.method == "sendMessage" and c.chat_id == OWNER_ID]


def status_entry(text: str, user_id: int) -> str:
    """The two lines of /status describing ``user_id``."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if f"<code>{user_id}</code>" in line:
            return line + "\n" + (lines[i + 1] if i + 1 < len(lines) else "")
    raise AssertionError(f"user {user_id} not listed in /status:\n{text}")


def csv_rows(call: Call) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(ShopTest.document_text(call))))


async def edit_message(t: ShopTest, user: dict, message_id: int, text: str) -> list[Call]:
    """The user edits one of their earlier messages (an edited_message update)."""
    stamp = int(time.time())
    msg: dict[str, Any] = {
        "message_id": message_id,
        "date": stamp - 10,
        "edit_date": stamp,
        "chat": {"id": user["id"], "type": "private", "first_name": user.get("first_name", "")},
        "from": user,
        "text": text,
    }
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return await t._process({"edited_message": msg})


def block_chat(t: ShopTest, chat_id: int) -> None:
    """Every call addressed to ``chat_id`` fails as if the user blocked the bot (403)."""
    original = t.tg.do_request

    async def do_request(url, method, request_data=None, **kwargs):
        params = dict(request_data.parameters) if request_data is not None else {}
        target = params.get("chat_id")
        if target is not None and int(target) == chat_id:
            t.tg.calls.append(Call(method=url.rsplit("/", 1)[-1], params=params, files={},
                                   blocked=True))
            body = {"ok": False, "error_code": 403,
                    "description": "Forbidden: bot was blocked by the user"}
            return 403, json.dumps(body).encode()
        return await original(url=url, method=method, request_data=request_data, **kwargs)

    t.tg.do_request = do_request  # type: ignore[method-assign]


def setup_roles(t: ShopTest) -> None:
    """Alice (user), Adam + Zara (admins), Carol (seller), Dave (banned)."""
    register(t, ALICE)
    make_admin(t, ADMIN)
    make_admin(t, ADMIN2)
    register(t, CAROL)
    t.db.set_seller(CAROL["id"], True)
    register(t, DAVE)
    t.db.set_banned(DAVE["id"], True)


# ====================================================== owner-only commands
# command -> (text sent by the non-owner, check that nothing changed)
REFUSED: dict[str, tuple[str, Callable[[ShopTest, int], bool]]] = {
    "addbal": ("/addbal {me} 50", lambda t, me: balance(t, me) == 0 and not deposits_of(t, me)),
    "modifybal": ("/modifybal {me} 999", lambda t, me: balance(t, me) == 0),
    "addadmin": ("/addadmin 1001", lambda t, me: not t.db.is_admin(1001)),
    "removeadmin": ("/removeadmin 2002", lambda t, me: t.db.is_admin(2002)),
    "seller": ("/seller {me}", lambda t, me: not row(t, me)["is_seller"]),
    "removeseller": ("/removeseller 1003", lambda t, me: bool(row(t, 1003)["is_seller"])),
    "ban": ("/ban 1001", lambda t, me: not t.db.is_banned(1001)),
    "unban": ("/unban 1004", lambda t, me: t.db.is_banned(1004)),
    "close": ("/close", lambda t, me: t.db.shop_is_open()),
    "open": ("/open", lambda t, me: not t.db.shop_is_open()),
    "status": ("/status", lambda t, me: True),
}


@pytest.mark.parametrize("command", sorted(REFUSED))
def test_owner_only_command_refused_for_admin_and_user(tmp_path, command):
    template, unchanged = REFUSED[command]

    async def scenario(t: ShopTest) -> None:
        setup_roles(t)
        if command == "open":
            t.db.set_shop_open(False)
        for sender in (ADMIN, BOB):
            calls = await t.send(sender, template.format(me=sender["id"]))
            msgs = t.messages(calls)
            assert len(msgs) == 1, [(c.method, c.chat_id, c.text) for c in msgs]
            assert msgs[0].chat_id == sender["id"]
            assert OWNER_ONLY in msgs[0].text
            assert not t.documents(calls)
            assert "Users:" not in msgs[0].text
            assert unchanged(t, sender["id"]), f"{command} by {sender['first_name']} changed data"
            # nothing was started: a follow-up number is not taken as an ID or amount
            calls = await t.send(sender, "1001")
            assert NO_STATE in t.last_text(calls)
        assert balance(t, ALICE) == 0 and not deposits_of(t, ALICE)

    run(tmp_path, scenario)


def test_owner_prompts_cannot_be_opened_by_others(tmp_path):
    """/addbal, /ban … without arguments don't open the ID prompt for a non-owner."""

    async def scenario(t: ShopTest) -> None:
        setup_roles(t)
        for sender in (ADMIN, BOB):
            for command in ("addbal", "modifybal", "addadmin", "removeadmin", "seller",
                            "removeseller", "ban", "unban"):
                calls = await t.send(sender, f"/{command}")
                assert OWNER_ONLY in t.last_text(calls), command
                assert ASK_ID not in t.last_text(calls)
                calls = await t.send(sender, "1001")
                assert NO_STATE in t.last_text(calls), command
                calls = await t.send(sender, "50")
                assert NO_STATE in t.last_text(calls), command
        alice = row(t, ALICE)
        assert alice["balance"] == 0 and not alice["is_admin"] and not alice["is_seller"]
        assert not alice["is_banned"]

    run(tmp_path, scenario)


# ===================================================================== /addbal
def test_addbal_with_id_and_amount(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/addbal 1001 50")
        owner_text = t.last_text(calls, OWNER_ID)
        assert "Added <b>50.00$</b>" in owner_text
        assert "Alice" in owner_text and "<code>1001</code>" in owner_text
        assert "New balance: <b>50.00$</b>" in owner_text
        assert "Total deposits: 50.00$" in owner_text
        to_alice = texts_to(calls, ALICE)
        assert len(to_alice) == 1
        assert "<b>50.00$</b> was added to your balance" in to_alice[0]
        assert "New balance: <b>50.00$</b>" in to_alice[0]
        alice = row(t, ALICE)
        assert (alice["balance"], alice["total_deposits"], alice["is_seller"]) == (5000, 5000, 0)
        deps = deposits_of(t, ALICE)
        assert [(d["amount"], d["method"], d["status"]) for d in deps] == [(5000, "manual", "approved")]

        calls = await t.send(OWNER, "/addbal 1001 20")
        assert "New balance: <b>70.00$</b>" in t.last_text(calls, OWNER_ID)
        assert "Total deposits: 70.00$" in t.last_text(calls, OWNER_ID)
        alice = row(t, ALICE)
        assert (alice["balance"], alice["total_deposits"]) == (7000, 7000)
        assert ledger_of(t, ALICE) == [("manual deposit", 5000), ("manual deposit", 2000)]
        assert t.db.totals()["deposits"] == 7000

        calls = await t.send(ALICE, "/start")
        assert "Balance: <b>70.00$</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_addbal_accepts_cents_and_dollar_signs(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/addbal 1001 12.5")
        assert "Added <b>12.50$</b>" in t.last_text(calls, OWNER_ID)
        assert balance(t, ALICE) == 1250
        await t.send(OWNER, "/addbal 1001 $0.75")
        assert balance(t, ALICE) == 1325
        calls = await t.send(OWNER, "/addbal 1001 3,25$")
        assert "New balance: <b>16.50$</b>" in t.last_text(calls, OWNER_ID)
        assert row(t, ALICE)["total_deposits"] == 1650
        assert [d["amount"] for d in deposits_of(t, ALICE)] == [1250, 75, 325]

    run(tmp_path, scenario)


def test_addbal_with_only_an_id_asks_for_the_amount(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 300)
        calls = await t.send(OWNER, "/addbal 1001")
        prompt = t.last_text(calls)
        assert "Add balance" in prompt and "Alice" in prompt and "<code>1001</code>" in prompt
        assert "Current balance: 3.00$" in prompt
        assert "Send the amount to add" in prompt
        assert t.has_button(calls, "Cancel")
        assert balance(t, ALICE) == 300 and not deposits_of(t, ALICE)
        assert not texts_to(calls, ALICE)

        calls = await t.send(OWNER, "25")
        assert "Added <b>25.00$</b>" in t.last_text(calls, OWNER_ID)
        assert "New balance: <b>28.00$</b>" in t.last_text(calls, OWNER_ID)
        assert any("25.00$</b> was added" in x for x in texts_to(calls, ALICE))
        alice = row(t, ALICE)
        assert (alice["balance"], alice["total_deposits"]) == (2800, 2500)

        # the prompt is over: another number does nothing
        calls = await t.send(OWNER, "25")
        assert NO_STATE in t.last_text(calls)
        assert balance(t, ALICE) == 2800 and len(deposits_of(t, ALICE)) == 1

    run(tmp_path, scenario)


def test_addbal_without_arguments_asks_for_the_id_then_the_amount(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/addbal")
        assert "Add balance" in t.last_text(calls) and ASK_ID in t.last_text(calls)
        assert t.has_button(calls, "Cancel")
        for bad in ("alice", "0", "-1001", "10.5"):
            calls = await t.send(OWNER, bad)
            assert "numeric Telegram user ID" in t.last_text(calls), bad
        calls = await t.send_photo(OWNER, "not-an-id")
        assert "text message" in t.last_text(calls)

        calls = await t.send(OWNER, "1001")
        prompt = t.last_text(calls)
        assert "Send the amount to add" in prompt and "Alice" in prompt
        for bad in ("0", "-3", "ten", "1.234"):
            calls = await t.send(OWNER, bad)
            assert "Please send an amount" in t.last_text(calls), bad
        assert balance(t, ALICE) == 0 and not deposits_of(t, ALICE)

        calls = await t.send(OWNER, "7.25")
        assert "Added <b>7.25$</b>" in t.last_text(calls, OWNER_ID)
        assert any("7.25$</b> was added" in x for x in texts_to(calls, ALICE))
        alice = row(t, ALICE)
        assert (alice["balance"], alice["total_deposits"]) == (725, 725)

    run(tmp_path, scenario)


def test_addbal_rejects_invalid_id_or_amount(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        cases = [
            ("/addbal abc 5", "isn't a valid user ID"),
            ("/addbal 0 5", "isn't a valid user ID"),
            ("/addbal -1001 5", "isn't a valid user ID"),
            ("/addbal 1001 abc", "isn't a valid amount"),
            ("/addbal 1001 0", "isn't a valid amount"),
            ("/addbal 1001 0.00", "isn't a valid amount"),
            ("/addbal 1001 -5", "isn't a valid amount"),
            ("/addbal 1001 1.234", "isn't a valid amount"),
            ("/addbal 1001 1e3", "isn't a valid amount"),
        ]
        for text, expected in cases:
            calls = await t.send(OWNER, text)
            assert expected in t.last_text(calls), text
            assert not texts_to(calls, ALICE), text
            # no prompt was left open
            calls = await t.send(OWNER, "5")
            assert NO_STATE in t.last_text(calls), text
        assert balance(t, ALICE) == 0 and not deposits_of(t, ALICE)
        assert t.db.get_user(0) is None
        assert t.db.totals()["deposits"] == 0

    run(tmp_path, scenario)


def test_addbal_unknown_id_creates_a_user_whose_balance_is_waiting(tmp_path):
    newcomer = tg_user(555000, "Nadia", "nadia")

    async def scenario(t: ShopTest) -> None:
        assert t.db.get_user(555000) is None
        calls = await t.send(OWNER, "/addbal 555000 30")
        assert "Added <b>30.00$</b>" in t.last_text(calls, OWNER_ID)
        waiting = t.db.get_user(555000)
        assert waiting is not None
        assert (waiting["balance"], waiting["total_deposits"], waiting["started"]) == (3000, 3000, 0)

        calls = await t.send(newcomer, "/start")
        panel = t.last_text(calls)
        assert "Welcome Nadia" in panel and "Balance: <b>30.00$</b>" in panel
        joined = t.db.get_user(555000)
        assert (joined["started"], joined["first_name"], joined["balance"]) == (1, "Nadia", 3000)

    run(tmp_path, scenario)


def test_addbal_reaching_the_threshold_makes_a_seller(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/addbal 1001 999.99")
        assert "seller" not in t.last_text(calls, OWNER_ID).lower()
        assert not row(t, ALICE)["is_seller"]

        calls = await t.send(OWNER, "/addbal 1001 0.01")
        owner_text = t.last_text(calls, OWNER_ID)
        assert "1000$" in owner_text and "now a seller" in owner_text
        to_alice = "\n".join(texts_to(calls, ALICE))
        assert "Seller" in to_alice and "10% off" in to_alice
        alice = row(t, ALICE)
        assert alice["is_seller"] == 1 and alice["total_deposits"] == 100000

        calls = await t.send(ALICE, "/start")
        assert "Seller — <b>10% off</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_addbal_prompt_ends_on_cancel_button_command_or_menu(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        # ❌ Cancel button
        calls = await t.send(OWNER, "/addbal 1001")
        calls = await t.press(OWNER, t.button(calls, "Cancel"))
        assert "Cancelled" in t.last_text(calls)
        calls = await t.send(OWNER, "50")
        assert NO_STATE in t.last_text(calls)
        # another command
        await t.send(OWNER, "/addbal 1001")
        await t.send(OWNER, "/help")
        calls = await t.send(OWNER, "50")
        assert NO_STATE in t.last_text(calls)
        # /cancel while the ID is asked
        await t.send(OWNER, "/addbal")
        calls = await t.send(OWNER, "/cancel")
        assert "Cancelled" in t.last_text(calls)
        calls = await t.send(OWNER, "1001")
        assert NO_STATE in t.last_text(calls)
        # a menu button
        await t.send(OWNER, "/addbal 1001")
        await t.press(OWNER, "menu")
        calls = await t.send(OWNER, "50")
        assert NO_STATE in t.last_text(calls)
        assert balance(t, ALICE) == 0 and not deposits_of(t, ALICE)

    run(tmp_path, scenario)


def test_addbal_still_adds_when_the_user_blocked_the_bot(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        block_chat(t, ALICE["id"])
        calls = await t.send(OWNER, "/addbal 1001 50")
        owner_texts = t.texts(calls, OWNER_ID)
        assert any("Added <b>50.00$</b>" in x for x in owner_texts)
        assert not any("Something went wrong" in x for x in owner_texts)
        assert any(c.get("blocked") for c in calls), "the user should have been notified"
        assert balance(t, ALICE) == 5000

    run(tmp_path, scenario)


def test_editing_an_addbal_message_does_not_add_money_again(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        await t.send(OWNER, "/addbal 1001 50", message_id=900)
        assert balance(t, ALICE) == 5000
        # The owner fixes a "typo" in the message they already sent.
        await edit_message(t, OWNER, 900, "/addbal 1001 5")
        alice = row(t, ALICE)
        assert len(deposits_of(t, ALICE)) == 1
        assert (alice["balance"], alice["total_deposits"]) == (5000, 5000)

    run(tmp_path, scenario)


# ================================================================== /modifybal
def test_modifybal_shows_balance_and_deposits_then_sets_it(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        await t.send(OWNER, "/addbal 1001 100")
        calls = await t.send(OWNER, "/modifybal 1001")
        prompt = t.last_text(calls)
        assert "Modify balance" in prompt and "Alice" in prompt and "<code>1001</code>" in prompt
        assert "Balance: <b>100.00$</b>" in prompt
        assert "Total deposits: 100.00$" in prompt
        assert "What should the balance be?" in prompt
        assert t.has_button(calls, "Cancel")

        calls = await t.send(OWNER, "40")
        assert "changed from 100.00$ to <b>40.00$</b>" in t.last_text(calls, OWNER_ID)
        assert not texts_to(calls, ALICE)  # a correction: the user isn't notified
        alice = row(t, ALICE)
        assert (alice["balance"], alice["total_deposits"]) == (4000, 10000)
        assert ledger_of(t, ALICE)[-1] == ("balance set by owner", -6000)

        calls = await t.send(ALICE, "/start")
        assert "Balance: <b>40.00$</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_modifybal_prompt_allows_zero_and_rejects_negative_or_text(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 1500)
        await t.send(OWNER, "/modifybal 1001")
        for bad in ("-5", "abc", "12.345", "1e3"):
            calls = await t.send(OWNER, bad)
            assert "Please send an amount" in t.last_text(calls), bad
            assert balance(t, ALICE) == 1500
        calls = await t.send(OWNER, "0")
        assert "changed from 15.00$ to <b>0.00$</b>" in t.last_text(calls)
        assert balance(t, ALICE) == 0

    run(tmp_path, scenario)


def test_modifybal_with_an_amount_sets_it_directly(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        await t.send(OWNER, "/addbal 1001 10")
        calls = await t.send(OWNER, "/modifybal 1001 12.34")
        assert "changed from 10.00$ to <b>12.34$</b>" in t.last_text(calls)
        assert not texts_to(calls, ALICE)
        assert balance(t, ALICE) == 1234
        calls = await t.send(OWNER, "/modifybal 1001 0")
        assert "to <b>0.00$</b>" in t.last_text(calls)
        assert balance(t, ALICE) == 0
        for bad in ("-1", "abc", "5.555"):
            calls = await t.send(OWNER, f"/modifybal 1001 {bad}")
            assert "isn't a valid amount" in t.last_text(calls), bad
        assert balance(t, ALICE) == 0
        assert row(t, ALICE)["total_deposits"] == 1000
        calls = await t.send(OWNER, "5")  # no prompt was left open
        assert NO_STATE in t.last_text(calls)

    run(tmp_path, scenario)


def test_modifybal_without_arguments_asks_for_the_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 200)
        calls = await t.send(OWNER, "/modifybal")
        assert "Modify balance" in t.last_text(calls) and ASK_ID in t.last_text(calls)
        calls = await t.send(OWNER, "1001")
        assert "Balance: <b>2.00$</b>" in t.last_text(calls)
        calls = await t.send(OWNER, "15")
        assert "changed from 2.00$ to <b>15.00$</b>" in t.last_text(calls)
        assert balance(t, ALICE) == 1500

    run(tmp_path, scenario)


def test_modifybal_unknown_id_prepares_a_waiting_balance(tmp_path):
    newcomer = tg_user(555000, "Nadia")

    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/modifybal 555000")
        prompt = t.last_text(calls)
        assert "Balance: <b>0.00$</b>" in prompt and "hasn't started the bot yet" in prompt
        calls = await t.send(OWNER, "12")
        assert "to <b>12.00$</b>" in t.last_text(calls)
        assert (row(t, 555000)["balance"], row(t, 555000)["total_deposits"]) == (1200, 0)
        calls = await t.send(newcomer, "/start")
        assert "Welcome Nadia" in t.last_text(calls)
        assert "Balance: <b>12.00$</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_id_prompt_cancel_button(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        for command in ("/addbal", "/modifybal", "/ban", "/addadmin", "/seller"):
            calls = await t.send(OWNER, command)
            assert ASK_ID in t.last_text(calls), command
            calls = await t.press(OWNER, t.button(calls, "Cancel"))
            assert "Cancelled" in t.last_text(calls) and "Welcome Owner" in t.last_text(calls)
            calls = await t.send(OWNER, "1001")
            assert NO_STATE in t.last_text(calls), command
        alice = row(t, ALICE)
        assert (alice["balance"], alice["is_banned"], alice["is_admin"], alice["is_seller"]) == (
            0, 0, 0, 0)

    run(tmp_path, scenario)


def test_modifybal_is_not_a_deposit(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/modifybal 1001 5000")
        assert "to <b>5000.00$</b>" in t.last_text(calls)
        assert not texts_to(calls, ALICE)
        alice = row(t, ALICE)
        assert (alice["balance"], alice["total_deposits"], alice["is_seller"]) == (500000, 0, 0)
        assert not deposits_of(t, ALICE)
        assert t.db.totals()["deposits"] == 0

    run(tmp_path, scenario)


# ===================================================== /addadmin /removeadmin
def test_addadmin_gives_the_admin_panel_and_command_menu(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, BOB)
        calls = await t.send(OWNER, "/addadmin 1002")
        assert "Bob" in t.last_text(calls, OWNER_ID)
        assert "is now an <b>admin</b>" in t.last_text(calls, OWNER_ID)
        assert t.db.is_admin(BOB["id"])
        menu = scoped_commands(calls, BOB["id"])
        assert menu is not None, "setMyCommands with a chat scope for the new admin"
        assert {"start", "withdraw", "help"} <= set(menu)
        assert not (OWNER_COMMAND_NAMES & set(menu))
        assert any("You're now an <b>admin</b>" in x for x in texts_to(calls, BOB))

        calls = await t.send(BOB, "/start")
        panel = last_message(calls, BOB)
        assert "Welcome Admin" in panel.text
        assert "Balance: <b>0.00$</b>" in panel.text and "Earnings: <b>0.00$</b>" in panel.text
        assert_panel_buttons(panel, {
            "Shop": "success", "My Products": "primary", "Add Product": "success",
            "Earnings": "primary", "Add Balance": "success", "My ID": None})
        assert not t.has_button(calls, "Add Label") and not t.has_button(calls, "My Orders")

        # the admin sections open; label management stays owner-only
        calls = await t.press(BOB, t.button(calls, "Add Product"))
        assert "Add Product" in t.last_text(calls) and not t.alert(calls)
        calls = await t.press(BOB, "mp:0:0")
        assert "My Products" in t.last_text(calls)
        calls = await t.press(BOB, "lb:0:0")
        assert "Only the owner" in t.alert(calls)

    run(tmp_path, scenario)


def test_addadmin_for_the_owner_or_an_existing_admin(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, f"/addadmin {OWNER_ID}")
        assert "That's you" in t.last_text(calls)
        assert len(t.messages(calls)) == 1
        assert not row(t, OWNER_ID)["is_admin"]
        calls = await t.send(OWNER, "/start")
        assert "Welcome Owner" in t.last_text(calls) and t.has_button(calls, "Add Label")

        make_admin(t, BOB)
        calls = await t.send(OWNER, "/addadmin 1002")
        assert "already an admin" in t.last_text(calls)
        assert not texts_to(calls, BOB)
        assert scoped_commands(calls, BOB["id"]) is None
        assert t.db.is_admin(BOB["id"])

    run(tmp_path, scenario)


def test_addadmin_unknown_id_and_id_prompt(tmp_path):
    stranger = tg_user(777000, "Nora")

    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/addadmin 777000")
        text = t.last_text(calls, OWNER_ID)
        assert "is now an <b>admin</b>" in text and "hasn't started the bot yet" in text
        assert t.db.is_admin(777000)
        calls = await t.send(stranger, "/start")
        assert "Welcome Admin" in t.last_text(calls)
        assert scoped_commands(calls, 777000) is not None

        register(t, BOB)
        calls = await t.send(OWNER, "/addadmin")
        assert "Add admin" in t.last_text(calls) and ASK_ID in t.last_text(calls)
        calls = await t.send(OWNER, "1002")
        assert "is now an <b>admin</b>" in t.last_text(calls, OWNER_ID)
        assert t.db.is_admin(BOB["id"])

    run(tmp_path, scenario)


def test_removeadmin_turns_the_admin_back_into_a_user(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, BOB)
        await t.send(OWNER, "/addadmin 1002")
        await t.send(BOB, "/start")
        calls = await t.send(OWNER, "/removeadmin 1002")
        assert "no longer an admin" in t.last_text(calls, OWNER_ID)
        assert not t.db.is_admin(BOB["id"])
        reset = [c for c in calls if c.method == "deleteMyCommands"
                 and int((c.params.get("scope") or {}).get("chat_id", 0)) == BOB["id"]]
        assert reset, "the admin command menu should be removed from their chat"
        assert any("admin access to this shop was removed" in x for x in texts_to(calls, BOB))

        calls = await t.send(BOB, "/start")
        panel = last_message(calls, BOB)
        assert "Welcome Bob" in panel.text and "Welcome Admin" not in panel.text
        assert "Earnings" not in panel.text
        assert_panel_buttons(panel, {"Shop": "success", "Add Balance": "success",
                                     "My Orders": "primary", "My ID": None})
        for data in ("mp:0:0", "ap:0:0"):
            calls = await t.press(BOB, data)
            assert "for the owner and admins" in t.alert(calls), data
        calls = await t.send(BOB, "/help")
        assert "Admin panel" not in t.last_text(calls)

    run(tmp_path, scenario)


def test_removeadmin_for_someone_who_is_not_an_admin(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/removeadmin 1001")
        assert "isn't an admin" in t.last_text(calls)
        assert len(t.messages(calls)) == 1
        assert not [c for c in calls if c.method in ("deleteMyCommands", "setMyCommands")]
        calls = await t.send(OWNER, "/removeadmin")
        assert "Remove admin" in t.last_text(calls) and ASK_ID in t.last_text(calls)
        calls = await t.send(OWNER, "1001")
        assert "isn't an admin" in t.last_text(calls)

    run(tmp_path, scenario)


# ===================================================== /seller /removeseller
def test_seller_sees_and_pays_the_discounted_price(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_product(t, "Esim/Oman/5GB", price=950, stock=3)
        oman = parent_of(t, five)
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 5000)
        calls = await t.press(ALICE, f"sh:{oman}:0")
        assert t.has_button(calls, "5GB · 9.50$")

        calls = await t.send(OWNER, "/seller 1001")
        owner_text = t.last_text(calls, OWNER_ID)
        assert "is now a <b>seller</b>" in owner_text and "10% off" in owner_text
        to_alice = "\n".join(texts_to(calls, ALICE))
        assert "Seller" in to_alice and "10% off" in to_alice
        assert row(t, ALICE)["is_seller"] == 1

        calls = await t.send(ALICE, "/start")
        assert "Seller — <b>10% off</b>" in t.last_text(calls)
        calls = await t.press(ALICE, f"sh:{oman}:0")
        assert t.has_button(calls, "5GB · 8.55$") and not t.has_button(calls, "9.50$")
        calls = await t.press(ALICE, f"sh:{five}:0")
        assert "<s>9.50$</s> <b>8.55$</b>" in t.last_text(calls)
        calls = await t.press(ALICE, f"sb:{five}:2")
        confirm = t.last_text(calls)
        assert "Price each: 8.55$" in confirm and "Total: <b>17.10$</b>" in confirm
        await t.press(ALICE, t.button(calls, "Confirm"))
        assert balance(t, ALICE) == 5000 - 1710
        (order,) = orders_of(t, ALICE)
        assert (order["unit_price"], order["total"], order["quantity"]) == (855, 1710, 2)
        assert row(t, OWNER_ID)["earnings"] == 1710

    run(tmp_path, scenario)


def test_removeseller_restores_the_price_and_blocks_auto_promotion(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_product(t, "Esim/Oman/5GB", price=950, stock=3)
        oman = parent_of(t, five)
        register(t, ALICE)
        await t.send(OWNER, "/seller 1001")
        calls = await t.send(OWNER, "/removeseller 1001")
        assert "no longer a seller" in t.last_text(calls, OWNER_ID)
        assert any("seller discount was removed" in x for x in texts_to(calls, ALICE))
        alice = row(t, ALICE)
        assert (alice["is_seller"], alice["seller_locked"]) == (0, 1)
        calls = await t.press(ALICE, f"sh:{oman}:0")
        assert t.has_button(calls, "5GB · 9.50$")
        calls = await t.send(ALICE, "/start")
        assert "Seller" not in t.last_text(calls)

        # no automatic re-promotion after the owner removed it…
        calls = await t.send(OWNER, "/addbal 1001 1000")
        assert "now a seller" not in t.last_text(calls, OWNER_ID)
        assert row(t, ALICE)["is_seller"] == 0
        # …until /seller is used again
        await t.send(OWNER, "/seller 1001")
        alice = row(t, ALICE)
        assert (alice["is_seller"], alice["seller_locked"]) == (1, 0)

    run(tmp_path, scenario)


def test_seller_repeats_and_id_prompts(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        register(t, BOB)
        await t.send(OWNER, "/seller 1001")
        calls = await t.send(OWNER, "/seller 1001")
        assert "already a seller" in t.last_text(calls)
        assert not texts_to(calls, ALICE)
        calls = await t.send(OWNER, "/removeseller 1002")
        assert "isn't a seller" in t.last_text(calls)
        assert not texts_to(calls, BOB)
        assert row(t, BOB)["seller_locked"] == 0

        calls = await t.send(OWNER, "/seller")
        assert "Add seller" in t.last_text(calls) and ASK_ID in t.last_text(calls)
        await t.send(OWNER, "1002")
        assert row(t, BOB)["is_seller"] == 1
        calls = await t.send(OWNER, "/removeseller")
        assert "Remove seller" in t.last_text(calls) and ASK_ID in t.last_text(calls)
        await t.send(OWNER, "1002")
        assert row(t, BOB)["is_seller"] == 0

    run(tmp_path, scenario)


# ============================================================== /ban /unban
def test_ban_blocks_messages_commands_and_buttons(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/ban 1001")
        assert "is now <b>banned</b>" in t.last_text(calls, OWNER_ID)
        assert any("You have been banned" in x for x in texts_to(calls, ALICE))
        assert t.db.is_banned(ALICE["id"])

        for text in ("/start", "hello", "/help", "/withdraw", "/cancel"):
            calls = await t.send(ALICE, text)
            msgs = t.messages(calls)
            assert len(msgs) == 1 and BANNED in msgs[0].text, text
        calls = await t.send_photo(ALICE, "some-photo")
        assert len(t.messages(calls)) == 1 and BANNED in t.last_text(calls)
        for data in ("menu", "sh:0:0", "dp", "mo:0", "da:10"):
            calls = await t.press(ALICE, data)
            answers = t.answers(calls)
            assert answers and BANNED in answers[-1]["text"], data
            assert answers[-1].get("show_alert") is True, data
            assert not t.messages(calls), data

    run(tmp_path, scenario)


def test_banned_user_cannot_buy(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_product(t, price=500, stock=3)
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 5000)
        calls = await t.press(ALICE, f"sb:{five}:1")
        confirm = t.button(calls, "Confirm")
        await t.send(OWNER, "/ban 1001")

        calls = await t.press(ALICE, confirm)
        assert BANNED in t.alert(calls) and not t.messages(calls)
        calls = await t.press(ALICE, f"sb:{five}:1")
        assert BANNED in t.alert(calls)
        assert balance(t, ALICE) == 5000 and t.db.count_items(five) == 3
        assert not orders_of(t, ALICE)

        # after an unban the old confirmation is no longer valid either
        await t.send(OWNER, "/unban 1001")
        calls = await t.press(ALICE, confirm)
        assert "expired" in t.alert(calls)
        assert balance(t, ALICE) == 5000 and t.db.count_items(five) == 3
        assert not orders_of(t, ALICE)

    run(tmp_path, scenario)


def test_banned_user_cannot_deposit(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.press(ALICE, "dp")
        calls = await t.press(ALICE, t.button(calls, "20$"))
        calls = await t.send_photo(ALICE, "alice-receipt")
        confirm = t.button(calls, "Confirm")
        await t.send(OWNER, "/ban 1001")

        calls = await t.press(ALICE, confirm)
        assert BANNED in t.alert(calls)
        assert not [c for c in calls if c.chat_id == OWNER_ID]
        for data in ("dp", "da:20", "dc"):
            calls = await t.press(ALICE, data)
            assert BANNED in t.alert(calls), data
        assert not deposits_of(t, ALICE)

        await t.send(OWNER, "/unban 1001")
        calls = await t.press(ALICE, confirm)
        assert "expired" in t.alert(calls)
        assert not [c for c in calls if c.chat_id == OWNER_ID]
        assert not deposits_of(t, ALICE)

    run(tmp_path, scenario)


def test_the_owner_cannot_be_banned(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, f"/ban {OWNER_ID}")
        assert "can't ban yourself" in t.last_text(calls)
        calls = await t.send(OWNER, "/ban")
        calls = await t.send(OWNER, str(OWNER_ID))
        assert "can't ban yourself" in t.last_text(calls)
        assert not row(t, OWNER_ID)["is_banned"]
        calls = await t.send(OWNER, "/start")
        assert "Welcome Owner" in t.last_text(calls)

        # even a stray flag in the database never locks the owner out
        t.db.conn.execute("UPDATE users SET is_banned = 1 WHERE id = ?", (OWNER_ID,))
        calls = await t.send(OWNER, "/start")
        assert "Welcome Owner" in t.last_text(calls)
        calls = await t.press(OWNER, "menu")
        assert BANNED not in t.alert(calls) and "Welcome Owner" in t.last_text(calls)

    run(tmp_path, scenario)


def test_ban_ends_the_users_action_in_progress(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.press(ALICE, "dp")
        calls = await t.press(ALICE, t.button(calls, "Custom"))
        assert "Send the amount" in t.last_text(calls)
        assert t.app.user_data[ALICE["id"]]["state"]["name"] == "deposit_amount"

        await t.send(OWNER, "/ban 1001")
        assert "state" not in t.app.user_data.get(ALICE["id"], {})
        await t.send(OWNER, "/unban 1001")
        calls = await t.send(ALICE, "25")
        assert NO_STATE in t.last_text(calls)
        assert "850940840" not in t.last_text(calls)

    run(tmp_path, scenario)


def test_ban_discards_an_admins_unsaved_products(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_product(t, price=500, stock=0)
        make_admin(t, ADMIN)
        calls = await t.press(ADMIN, f"ap:{five}:0")
        assert "ONE MESSAGE PER PRODUCT" in t.last_text(calls)
        await t.send(ADMIN, "CODE-1")
        await t.send(ADMIN, "CODE-2")
        calls = await t.send(OWNER, "/ban 2001")
        assert "They're an admin" in t.last_text(calls, OWNER_ID)

        await t.send(OWNER, "/unban 2001")
        calls = await t.press(ADMIN, "apd")
        assert "nothing to save" in t.alert(calls)
        assert t.db.count_items(five) == 0

    run(tmp_path, scenario)


def test_unban_restores_access(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_product(t, price=500, stock=2)
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 1000)
        await t.send(OWNER, "/ban 1001")
        calls = await t.send(OWNER, "/unban 1001")
        assert "no longer banned" in t.last_text(calls, OWNER_ID)
        assert any("You have been unbanned" in x for x in texts_to(calls, ALICE))
        assert not t.db.is_banned(ALICE["id"])

        calls = await t.send(ALICE, "/start")
        assert "Welcome Alice" in t.last_text(calls)
        calls = await t.press(ALICE, f"sb:{five}:1")
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        assert "CODE-0" in t.texts(calls, ALICE["id"])
        assert balance(t, ALICE) == 500 and len(orders_of(t, ALICE)) == 1

    run(tmp_path, scenario)


def test_banned_admin_keeps_the_role_but_loses_access(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        calls = await t.send(OWNER, "/ban 2001")
        assert "They're an admin" in t.last_text(calls, OWNER_ID)
        assert t.db.is_admin(ADMIN["id"]) and t.db.is_banned(ADMIN["id"])
        for text in ("/start", "/withdraw"):
            calls = await t.send(ADMIN, text)
            assert BANNED in t.last_text(calls), text
        for data in ("ap:0:0", "mp:0:0", "ea"):
            calls = await t.press(ADMIN, data)
            assert BANNED in t.alert(calls) and not t.messages(calls), data

        await t.send(OWNER, "/unban 2001")
        calls = await t.send(ADMIN, "/start")
        assert "Welcome Admin" in t.last_text(calls)

    run(tmp_path, scenario)


def test_ban_unknown_id_and_repeats(tmp_path):
    stranger = tg_user(888000, "Sam")

    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/ban 888000")
        text = t.last_text(calls, OWNER_ID)
        assert "is now <b>banned</b>" in text and "hasn't started the bot yet" in text
        calls = await t.send(OWNER, "/ban 888000")
        assert "already banned" in t.last_text(calls)
        assert t.db.is_banned(888000)
        calls = await t.send(stranger, "/start")
        assert BANNED in t.last_text(calls) and len(t.messages(calls)) == 1

        register(t, ALICE)
        calls = await t.send(OWNER, "/unban 1001")
        assert "isn't banned" in t.last_text(calls)
        assert not texts_to(calls, ALICE)

    run(tmp_path, scenario)


def test_ban_and_unban_ask_for_the_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        calls = await t.send(OWNER, "/ban")
        assert "Ban user" in t.last_text(calls) and ASK_ID in t.last_text(calls)
        calls = await t.send(OWNER, "1001")
        assert "is now <b>banned</b>" in t.last_text(calls, OWNER_ID)
        assert t.db.is_banned(ALICE["id"])
        calls = await t.send(OWNER, "/unban")
        assert "Unban user" in t.last_text(calls) and ASK_ID in t.last_text(calls)
        calls = await t.send(OWNER, "1001")
        assert "no longer banned" in t.last_text(calls, OWNER_ID)
        assert not t.db.is_banned(ALICE["id"])

    run(tmp_path, scenario)


# =============================================================== /close /open
def test_close_and_open_toggle_and_reply(tmp_path):
    async def scenario(t: ShopTest) -> None:
        assert t.db.shop_is_open()
        calls = await t.send(OWNER, "/close")
        assert "The shop is now closed" in t.last_text(calls)
        assert not t.db.shop_is_open()
        calls = await t.send(OWNER, "/close")
        assert "already closed" in t.last_text(calls)
        calls = await t.send(OWNER, "/start")
        assert "Shop: 🔴 Closed" in t.last_text(calls)

        calls = await t.send(OWNER, "/open")
        assert "The shop is now open" in t.last_text(calls)
        assert t.db.shop_is_open()
        calls = await t.send(OWNER, "/open")
        assert "already open" in t.last_text(calls)
        calls = await t.send(OWNER, "/start")
        assert "Shop: 🟢 Open" in t.last_text(calls)

    run(tmp_path, scenario)


def test_closed_shop_blocks_customers_and_admins_but_not_the_owner(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_product(t, "Esim/Oman/5GB", price=500, stock=2)
        oman = parent_of(t, five)
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 5000)
        make_admin(t, ADMIN)
        await t.send(OWNER, "/close")

        for user in (ALICE, ADMIN):
            for data in ("sh:0:0", f"sh:{oman}:0", f"sh:{five}:0", f"sb:{five}:1", f"sq:{five}"):
                calls = await t.press(user, data)
                assert CLOSED in t.last_text(calls), (user["first_name"], data)
                assert not [d for d in datas(calls) if d.startswith(("sb:", "sq:", "sc:"))]
                assert not t.has_button(calls, "Esim")
        assert "state" not in t.app.user_data.get(ALICE["id"], {})

        calls = await t.press(OWNER, "sh:0:0")
        assert "closed for customers" in t.last_text(calls) and t.has_button(calls, "Esim")
        calls = await t.press(OWNER, f"sh:{five}:0")
        assert any(d.startswith("sb:") for d in datas(calls))

        await t.send(OWNER, "/open")
        calls = await t.press(ALICE, "sh:0:0")
        assert CLOSED not in t.last_text(calls) and t.has_button(calls, "Esim")

    run(tmp_path, scenario)


def test_closing_the_shop_stops_a_pending_confirmation(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_product(t, price=500, stock=2)
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 5000)
        calls = await t.press(ALICE, f"sb:{five}:1")
        confirm = t.button(calls, "Confirm")
        await t.send(OWNER, "/close")
        calls = await t.press(ALICE, confirm)
        assert CLOSED in t.last_text(calls)
        assert "CODE-0" not in t.texts(calls)
        assert balance(t, ALICE) == 5000 and t.db.count_items(five) == 2
        assert not orders_of(t, ALICE)

    run(tmp_path, scenario)


def test_closed_shop_custom_quantity_reply_is_refused(tmp_path):
    async def scenario(t: ShopTest) -> None:
        five = make_product(t, price=500, stock=3)
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 5000)
        calls = await t.press(ALICE, f"sq:{five}")
        assert "How many" in t.last_text(calls)
        await t.send(OWNER, "/close")
        calls = await t.send(ALICE, "2")
        assert CLOSED in t.last_text(calls)
        assert not [d for d in datas(calls) if d.startswith("sc:")]

    run(tmp_path, scenario)


# ===================================================================== /status
async def _status_population(t: ShopTest) -> None:
    for user in (ALICE, BOB, CAROL, DAVE):
        register(t, user)
    t.db.set_admin(BOB["id"], True)
    t.db.set_seller(CAROL["id"], True)
    t.db.set_banned(DAVE["id"], True)
    await t.send(OWNER, "/addbal 1001 25")
    await t.send(OWNER, "/addbal 555000 10")  # never started the bot


def test_status_lists_every_user_with_details_and_badges(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await _status_population(t)
        calls = await t.send(OWNER, "/status")
        msgs = status_messages(calls)
        assert msgs and all(m.params.get("parse_mode") == "HTML" for m in msgs)
        text = "\n".join(m.text for m in msgs)
        assert "Users: 6" in text
        assert "Balances held: 35.00$" in text and "Deposits: 35.00$" in text

        alice = status_entry(text, 1001)
        assert "Alice" in alice and "@alice" in alice
        assert "💰 25.00$" in alice and "📥 25.00$" in alice
        assert alice.split("\n")[0].endswith("</code>")  # no badge
        assert "</code> 👮" in status_entry(text, 1002)
        assert "</code> ⭐" in status_entry(text, 1003)
        assert "</code> 🚫" in status_entry(text, 1004)
        waiting = status_entry(text, 555000)
        assert "User 555000" in waiting and "</code> ⏳" in waiting
        assert "💰 10.00$" in waiting and "📥 10.00$" in waiting
        owner = status_entry(text, OWNER_ID)
        assert "Mohamed" in owner and "</code> 👑" in owner
        # the biggest depositor comes first
        assert text.index("<code>1001</code>") < text.index("<code>555000</code>")

    run(tmp_path, scenario)


def test_status_sends_a_csv_with_everyone(tmp_path):
    async def scenario(t: ShopTest) -> None:
        await _status_population(t)
        calls = await t.send(OWNER, "/status")
        docs = t.documents(calls)
        assert len(docs) == 1 and docs[0].chat_id == OWNER_ID
        assert next(iter(docs[0]["files"].values()))["filename"].endswith(".csv")
        assert "All users" in docs[0].params["caption"]
        by_id = {int(r["ID"]): r for r in csv_rows(docs[0])}
        assert set(by_id) == {OWNER_ID, 1001, 1002, 1003, 1004, 555000}
        alice = by_id[1001]
        assert (alice["Name"], alice["Username"], alice["Role"]) == ("Alice", "alice", "user")
        assert (alice["Balance ($)"], alice["Total deposits ($)"]) == ("25.00", "25.00")
        assert by_id[1002]["Role"] == "admin"
        assert by_id[1003]["Seller"] == "yes" and by_id[1001]["Seller"] == "no"
        assert by_id[1004]["Banned"] == "yes" and by_id[1001]["Banned"] == "no"
        waiting = by_id[555000]
        assert (waiting["Started bot"], waiting["Name"], waiting["Balance ($)"]) == (
            "no", "User 555000", "10.00")
        assert by_id[OWNER_ID]["Role"] == "owner"

    run(tmp_path, scenario)


def test_status_splits_long_output_into_messages_under_the_limit(tmp_path):
    async def scenario(t: ShopTest) -> None:
        ids = list(range(300000, 300060))
        for i, uid in enumerate(ids):
            t.db.touch_user(uid, f"Customer{i:02d}" + "x" * 50, "Lastname" + "y" * 50,
                            f"user_{i:02d}_" + "z" * 20)
            t.db.manual_deposit(uid, (i + 1) * 100, 10 ** 12)
        calls = await t.send(OWNER, "/status")
        msgs = status_messages(calls)
        assert len(msgs) >= 2, "a long list must be split"
        for m in msgs:
            assert utf16_len(m.text) <= 4096
            assert m.text.count("<b>") == m.text.count("</b>")
            assert m.text.count("<code>") == m.text.count("</code>")
        text = "\n".join(m.text for m in msgs)
        assert "Users: 61" in text
        # the 50 biggest depositors are listed once each; the file has everyone
        listed = [uid for uid in ids if f"<code>{uid}</code>" in text]
        assert listed == ids[10:]
        assert all(text.count(f"<code>{uid}</code>") == 1 for uid in listed)
        assert "💰 60.00$ · 📥 60.00$" in status_entry(text, 300059)
        (doc,) = t.documents(calls)
        assert "50 of 61" in doc.params["caption"]
        assert {int(r["ID"]) for r in csv_rows(doc)} == set(ids) | {OWNER_ID}

    run(tmp_path, scenario)


def test_status_escapes_names_and_guards_the_csv(tmp_path):
    tricky = tg_user(1666, '=HYPERLINK("http://x")', "tricky", "<i>&</i>")

    async def scenario(t: ShopTest) -> None:
        register(t, tricky)
        calls = await t.send(OWNER, "/status")
        text = "\n".join(m.text for m in status_messages(calls))
        entry = status_entry(text, 1666)
        assert "&lt;i&gt;&amp;&lt;/i&gt;" in entry and "<i>" not in entry
        (doc,) = t.documents(calls)
        name = {int(r["ID"]): r for r in csv_rows(doc)}[1666]["Name"]
        assert name.startswith("'=HYPERLINK"), name

    run(tmp_path, scenario)


def test_removal_commands_with_unknown_ids_do_not_create_users(tmp_path):
    async def scenario(t: ShopTest) -> None:
        for command in ("/removeadmin 555001", "/removeseller 555002", "/unban 555003"):
            calls = await t.send(OWNER, command)
            assert "isn't" in t.last_text(calls), command
        for uid in (555001, 555002, 555003):
            assert t.db.get_user(uid) is None, uid
        calls = await t.send(OWNER, "/status")
        assert "Users: 1" in "\n".join(m.text for m in status_messages(calls))

    run(tmp_path, scenario)


# ======================================================================= /help
def test_help_owner_sees_every_section(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/help")
        text = t.last_text(calls)
        assert "Owner panel" in text and "Admin panel" in text and "Everyone" in text
        assert help_commands(text) == OWNER_COMMAND_NAMES | {"withdraw", "start", "help", "cancel", "id"}
        assert "Add Label" in text
        assert utf16_len(text) <= 4096

    run(tmp_path, scenario)


def test_help_admin_sees_admin_and_everyone_sections(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        calls = await t.send(ADMIN, "/help")
        text = t.last_text(calls)
        assert "Admin panel" in text and "Everyone" in text
        assert "Owner panel" not in text and "Add Label" not in text
        assert help_commands(text) == {"withdraw", "start", "help", "cancel", "id"}

    run(tmp_path, scenario)


def test_help_user_sees_only_the_everyone_section(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(ALICE, "/help")
        text = t.last_text(calls)
        assert "Everyone" in text
        assert "Owner panel" not in text and "Admin panel" not in text
        assert help_commands(text) == {"start", "help", "cancel", "id"}
        assert "withdraw" not in text.lower()

    run(tmp_path, scenario)


def test_help_follows_role_changes(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, BOB)
        calls = await t.send(BOB, "/help")
        assert help_commands(t.last_text(calls)) == {"start", "help", "cancel", "id"}
        await t.send(OWNER, "/addadmin 1002")
        calls = await t.send(BOB, "/help")
        assert "withdraw" in help_commands(t.last_text(calls))
        await t.send(OWNER, "/removeadmin 1002")
        calls = await t.send(BOB, "/help")
        assert help_commands(t.last_text(calls)) == {"start", "help", "cancel", "id"}

    run(tmp_path, scenario)


# ====================================================================== /start
def test_start_owner_panel(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(OWNER, "/start")
        panel = last_message(calls, OWNER_ID)
        assert panel.method == "sendMessage"
        assert "Welcome Owner" in panel.text and "Balance: <b>0.00$</b>" in panel.text
        assert "Shop: 🟢 Open" in panel.text
        assert_panel_buttons(panel, {
            "Shop": "success", "My Products": "primary", "Add Product": "success",
            "Add Label": "success", "Earnings": "primary", "Add Balance": "success", "My ID": None})
        menu = scoped_commands(calls, OWNER_ID)
        assert menu is not None and set(menu) == OWNER_COMMAND_NAMES | {
            "start", "help", "withdraw", "cancel", "id"}

    run(tmp_path, scenario)


def test_start_admin_panel(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        calls = await t.send(ADMIN, "/start")
        panel = last_message(calls, ADMIN)
        assert "Welcome Admin" in panel.text and "Welcome Adam" not in panel.text
        assert "Balance: <b>0.00$</b>" in panel.text
        assert_panel_buttons(panel, {
            "Shop": "success", "My Products": "primary", "Add Product": "success",
            "Earnings": "primary", "Add Balance": "success", "My ID": None})
        assert scoped_commands(calls, ADMIN["id"]) == ["start", "withdraw", "id", "help", "cancel"]

    run(tmp_path, scenario)


def test_start_user_panel(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(ALICE, "/start")
        assert len(t.messages(calls)) == 1
        panel = last_message(calls, ALICE)
        assert "Welcome Alice" in panel.text and "Balance: <b>0.00$</b>" in panel.text
        assert "Earnings" not in panel.text and "Shop: " not in panel.text
        assert_panel_buttons(panel, {"Shop": "success", "Add Balance": "success",
                                     "My Orders": "primary", "My ID": None})
        assert not [c for c in calls if c.method in ("setMyCommands", "deleteMyCommands")]

    run(tmp_path, scenario)


def test_start_panel_shows_the_current_balance_and_seller_status(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        await t.send(OWNER, "/addbal 1001 12.34")
        calls = await t.send(ALICE, "/start")
        assert "Balance: <b>12.34$</b>" in t.last_text(calls)
        assert "Seller" not in t.last_text(calls)
        await t.send(OWNER, "/seller 1001")
        calls = await t.send(ALICE, "/start")
        assert "Seller — <b>10% off</b>" in t.last_text(calls)
        await t.send(OWNER, f"/modifybal {OWNER_ID} 7")
        calls = await t.send(OWNER, "/start")
        assert "Balance: <b>7.00$</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_start_welcome_uses_the_escaped_current_first_name(tmp_path):
    async def scenario(t: ShopTest) -> None:
        calls = await t.send(tg_user(1500, "<i>Eve</i> & co"), "/start")
        assert "Welcome &lt;i&gt;Eve&lt;/i&gt; &amp; co" in t.last_text(calls)
        calls = await t.send(tg_user(1500, "Evelyn"), "/start")
        assert "Welcome Evelyn" in t.last_text(calls)

    run(tmp_path, scenario)


def test_owner_changes_survive_a_restart(tmp_path):
    async def before(t: ShopTest) -> None:
        for user in (ALICE, BOB, CAROL):
            register(t, user)
        await t.send(OWNER, "/close")
        await t.send(OWNER, "/ban 1001")
        await t.send(OWNER, "/addadmin 1002")
        await t.send(OWNER, "/seller 1003")
        await t.send(OWNER, "/addbal 1003 5")

    async def after(t: ShopTest) -> None:
        assert not t.db.shop_is_open()
        calls = await t.send(ALICE, "/start")
        assert BANNED in t.last_text(calls)
        calls = await t.send(BOB, "/start")
        assert "Welcome Admin" in t.last_text(calls)
        calls = await t.send(CAROL, "/start")
        assert "Seller" in t.last_text(calls) and "Balance: <b>5.00$</b>" in t.last_text(calls)
        calls = await t.press(CAROL, "sh:0:0")
        assert CLOSED in t.last_text(calls)

    run(tmp_path, before)
    run(tmp_path, after)


# ================================================================ bot profile
def test_bot_profile_description_and_command_menus(tmp_path):
    async def scenario(t: ShopTest) -> None:
        assert Settings().owner_id == OWNER_ID == 6883493877  # the owner from the spec
        make_admin(t, ADMIN)
        mark = len(t.tg.calls)
        await setup_bot_profile(t.app)
        calls = t.tg.calls[mark:]

        (desc,) = [c for c in calls if c.method == "setMyDescription"]
        description = desc.params["description"]
        assert "/start" in description and "start the shop" in description
        assert len(description) <= 512
        (short,) = [c for c in calls if c.method == "setMyShortDescription"]
        assert "/start" in short.params["short_description"]
        assert 0 < len(short.params["short_description"]) <= 120

        defaults = [c for c in calls if c.method == "setMyCommands" and not c.params.get("scope")]
        assert len(defaults) == 1
        default_names = [cmd["command"] for cmd in defaults[0].params["commands"]]
        assert default_names[0] == "start" and "help" in default_names
        assert not (OWNER_COMMAND_NAMES & set(default_names))

        owner_menu = scoped_commands(calls, OWNER_ID)
        assert owner_menu is not None and OWNER_COMMAND_NAMES <= set(owner_menu)
        assert "start" in owner_menu
        assert scoped_commands(calls, ADMIN["id"]) == ["start", "withdraw", "id", "help", "cancel"]
        for c in calls:
            if c.method == "setMyCommands":
                for cmd in c.params["commands"]:
                    assert re.fullmatch(r"[a-z0-9_]{1,32}", cmd["command"])
                    assert 1 <= len(cmd["description"]) <= 256

    run(tmp_path, scenario)


def test_bot_profile_sets_the_menu_even_if_the_description_fails(tmp_path):
    async def scenario(t: ShopTest) -> None:
        t.tg.fail["setMyDescription"] = "Bad Request: injected failure"
        mark = len(t.tg.calls)
        await setup_bot_profile(t.app)
        calls = t.tg.calls[mark:]
        assert [c for c in calls if c.method == "setMyShortDescription"]
        defaults = [c for c in calls if c.method == "setMyCommands" and not c.params.get("scope")]
        assert defaults and defaults[0].params["commands"][0]["command"] == "start"

    run(tmp_path, scenario)
