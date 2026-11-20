"""💳 Add Balance (deposits), 📊 Earnings and 💸 /withdraw, end to end through the real bot.

Fixtures that are not what a test is about (labels, prices, stock, admins, a few sales)
are created straight in the database. Deposits, the owner's Approve/Reject, purchases,
the Earnings screens and withdrawals are driven with real button presses and messages,
and every test checks both the database and what the customer / admin / owner sees.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

import pytest
from telegram.error import TimedOut

from harness import OWNER, OWNER_ID, Call, Picks, ShopTest, run, tg_user

ALICE = tg_user(1001, "Alice", "alice")
BOB = tg_user(1002, "Bob", "bob")
ADMIN = tg_user(2001, "Adam", "adam")
ADMIN2 = tg_user(2002, "Zara", "zara")
BUYER = tg_user(3001, "Buyer", "buyer")  # fixture customer whose purchases create earnings

BINANCE_ID = "850940840"
EXPIRED = "This request has expired"
OWNER_ONLY = "Only the owner can do that."
NOT_A_RECEIPT = "Please send the payment receipt as a photo"
WHOLE_NUMBER = "Please send a whole number only"
OUT_OF_RANGE = "The amount must be between 1 and 100000."
BAD_AMOUNT = "Please send an amount like 25 or 25.50."
CONGRATS = "Congratulations"
BANNED = "You are banned"


# ===================================================================== helpers
def register(t: ShopTest, user: dict) -> None:
    t.db.touch_user(user["id"], user["first_name"], user.get("last_name"), user.get("username"))


def make_admin(t: ShopTest, user: dict) -> None:
    register(t, user)
    t.db.set_admin(user["id"], True)


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


def add_stock(t: ShopTest, label_id: int, *texts: str, owner_id: int = OWNER_ID) -> list[str]:
    contents = [{"kind": "text", "text": text, "entities": []} for text in texts]
    return t.db.add_items(label_id, owner_id, contents)


def row_of(t: ShopTest, who: dict) -> dict:
    row = t.db.get_user(who["id"])
    assert row is not None, who
    return row


def give_sales(t: ShopTest, seller_id: int, price: int, count: int = 1) -> int:
    """Fixture: ``count`` products of ``seller_id`` priced ``price`` cents are bought by
    BUYER, so the seller earns ``price * count``. Returns the product label id."""
    label = make_label(t, f"Fixture/Seller {seller_id}", price)
    add_stock(t, label, *(f"item-{seller_id}-{i}" for i in range(count)), owner_id=seller_id)
    register(t, BUYER)
    t.db.set_balance(BUYER["id"], row_of(t, BUYER)["balance"] + price * count)
    t.db.purchase(BUYER["id"], label, count, 10)
    return label


def deposits(t: ShopTest, who: Optional[dict] = None) -> list:
    if who is None:
        return t.db.conn.execute("SELECT * FROM deposits ORDER BY id").fetchall()
    return t.db.conn.execute(
        "SELECT * FROM deposits WHERE user_id = ? ORDER BY id", (who["id"],)
    ).fetchall()


def withdrawals(t: ShopTest, who: Optional[dict] = None) -> list:
    if who is None:
        return t.db.conn.execute("SELECT * FROM withdrawals ORDER BY id").fetchall()
    return t.db.conn.execute(
        "SELECT * FROM withdrawals WHERE user_id = ? ORDER BY id", (who["id"],)
    ).fetchall()


def ledger(t: ShopTest, who: dict) -> list:
    """Balance ledger rows written by deposits (approved or /addbal)."""
    return t.db.conn.execute(
        "SELECT * FROM ledger WHERE user_id = ? AND reason IN ('deposit', 'manual deposit')"
        " ORDER BY id",
        (who["id"],),
    ).fetchall()


def all_text(calls: list[Call], chat_id: Optional[int] = None) -> str:
    return "\n".join(ShopTest.texts(calls, chat_id))


def datas(calls: list[Call]) -> list[str]:
    return [b.get("callback_data", "") for b in ShopTest.all_buttons(calls)]


def exact_button(calls: list[Call], text: str) -> str:
    """callback_data of the last button whose text is exactly ``text``."""
    found = [b for b in ShopTest.all_buttons(calls) if b.get("text") == text]
    assert found, f"no button {text!r}; buttons: {[b.get('text') for b in ShopTest.all_buttons(calls)]}"
    return found[-1]["callback_data"]


def sent(calls: list[Call], chat_id: int, *methods: str) -> list[Call]:
    methods = methods or ("sendMessage",)
    return [c for c in calls if c.method in methods and c.chat_id == chat_id]


def edits(calls: list[Call], chat_id: int) -> list[Call]:
    return [c for c in calls
            if c.method in ("editMessageText", "editMessageCaption") and c.chat_id == chat_id]


def is_alert(calls: list[Call]) -> bool:
    answers = ShopTest.answers(calls)
    return bool(answers and answers[-1].get("show_alert"))


def chat_of(params: dict) -> int:
    return int(params.get("chat_id") or 0)


def inject_failure(t: ShopTest, predicate: Callable[[str, dict], bool], *,
                   error: Optional[type] = None,
                   description: str = "Bad Request: injected failure", times: int = 1) -> None:
    """Make the next ``times`` Bot API calls matching ``predicate(method, params)`` fail:
    with ``error`` (e.g. TimedOut) they raise that network error, otherwise Telegram
    answers HTTP 400 with ``description``. Failed calls are recorded with failed=True."""
    original = type(t.tg).do_request.__get__(t.tg)
    left = {"n": times}

    async def do_request(url, method, request_data=None, read_timeout=None, write_timeout=None,
                         connect_timeout=None, pool_timeout=None):
        api_method = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        if left["n"] > 0 and predicate(api_method, params):
            left["n"] -= 1
            t.tg.calls.append(Call(method=api_method, params=params, files={}, failed=True))
            if error is not None:
                raise error()
            body = {"ok": False, "error_code": 400, "description": description}
            return 400, json.dumps(body).encode()
        return await original(url, method, request_data, read_timeout, write_timeout,
                              connect_timeout, pool_timeout)

    t.tg.do_request = do_request  # type: ignore[method-assign]


def delivered(calls: list[Call]) -> list[Call]:
    return [c for c in calls if not c.get("failed")]


async def edit_message(t: ShopTest, user: dict, message_id: int, text: str) -> list[Call]:
    """The user edits a message they sent earlier (an ``edited_message`` update)."""
    now = int(time.time())
    message = {
        "message_id": message_id,
        "date": now - 30,
        "edit_date": now,
        "chat": {"id": user["id"], "type": "private", "first_name": user["first_name"]},
        "from": user,
        "text": text,
    }
    return await t._process({"edited_message": message})


# -------------------------------------------------------------- flow drivers
async def choose_amount(t: ShopTest, who: dict, amount: int) -> list[Call]:
    """Open 💳 Add Balance and pick ``amount``: its preset button, or Custom + typing it."""
    calls = await t.press(who, "dp")
    presets = {b["text"]: b["callback_data"] for b in t.all_buttons(calls)}
    if f"{amount}$" in presets:
        return await t.press(who, presets[f"{amount}$"])
    calls = await t.press(who, t.button(calls, "Custom"))
    return await t.send(who, str(amount))


async def request_deposit(t: ShopTest, who: dict, amount: int,
                          receipt: str = "receipt-photo") -> tuple[int, list[Call]]:
    """Customer side of a deposit: amount → receipt photo → Confirm.
    Returns (deposit id, calls made by the Confirm press)."""
    calls = await choose_amount(t, who, amount)
    assert BINANCE_ID in t.last_text(calls)
    calls = await t.send_photo(who, receipt)
    calls = await t.press(who, t.button(calls, "Confirm"))
    assert "Request sent" in all_text(calls, who["id"])
    return deposits(t, who)[-1]["id"], calls


async def approve_deposit(t: ShopTest, deposit_id: int, by: dict = OWNER, *, anyway: bool = False) -> list[Call]:
    """✅ Approve; ``anyway``: the customer deposited in the week before (a receipt lists it,
    so it isn't approved twice by mistake), so ⚠️ Approve anyway too."""
    calls = await t.press(by, f"dok:{deposit_id}", photo=True)
    if anyway:
        assert "may have been added already" in t.alert(calls), t.alert(calls)
        calls += await t.press(by, f"dokx:{deposit_id}", photo=True)
    return calls


async def reject_deposit(t: ShopTest, deposit_id: int, by: dict = OWNER) -> list[Call]:
    return await t.press(by, f"dno:{deposit_id}", photo=True)


async def buy(t: ShopTest, who: dict, label_id: int, quantity: int) -> list[Call]:
    """Open the product in the shop, pick the quantity and confirm."""
    await t.press(who, f"sh:{label_id}:0")
    calls = await t.press(who, f"sb:{label_id}:{quantity}")
    calls = await t.press(who, t.button(calls, "Confirm"))
    assert "Purchase successful" in all_text(calls, who["id"]), all_text(calls)
    return calls


async def request_withdrawal(t: ShopTest, who: dict, amount_text: str,
                             binance_id: str = "123456789") -> list[Call]:
    """/withdraw → amount → Binance ID. Returns the calls made by the Binance ID message."""
    calls = await t.send(who, "/withdraw")
    assert "Withdraw earnings" in t.last_text(calls)
    calls = await t.send(who, amount_text)
    assert "Binance ID" in t.last_text(calls), t.last_text(calls)
    return await t.send(who, binance_id)


# ================================================================ Add Balance
def test_add_balance_is_offered_to_owner_admin_and_customer(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        for who in (OWNER, ADMIN, ALICE):
            calls = await t.send(who, "/start")
            assert "dp" in datas(calls), who["first_name"]
            assert t.has_button(calls, "Add Balance"), who["first_name"]

    run(tmp_path, scenario)


def test_deposit_menu_lists_presets_and_custom(tmp_path):
    async def scenario(t: ShopTest):
        register(t, ALICE)
        t.db.set_balance(ALICE["id"], 750)
        calls = await t.send(ALICE, "/start")
        calls = await t.press(ALICE, t.button(calls, "Add Balance"))
        text = t.last_text(calls)
        assert "Add Balance" in text and "Current balance: <b>7.50$</b>" in text
        presets = [(b["text"], b["callback_data"]) for b in t.all_buttons(calls)
                   if b["callback_data"].startswith("da:")]
        assert presets == [("10$", "da:10"), ("20$", "da:20"), ("30$", "da:30"),
                           ("50$", "da:50"), ("100$", "da:100")]
        assert t.button(calls, "Custom") == "dc"
        assert t.has_button(calls, "Cancel")
        assert deposits(t) == []

    run(tmp_path, scenario)


@pytest.mark.parametrize("amount", [10, 20, 30, 50, 100])
def test_preset_amount_asks_for_binance_payment_and_receipt(tmp_path, amount):
    async def scenario(t: ShopTest):
        calls = await t.press(ALICE, "dp")
        calls = await t.press(ALICE, exact_button(calls, f"{amount}$"))
        text = t.last_text(calls)
        assert f"Add {amount}$ to your balance" in text
        assert f"Send <b>{amount}$</b> with Binance Pay to Binance ID: <code>{BINANCE_ID}</code>" in text
        assert "photo of the payment receipt" in text
        assert not t.has_button(calls, "Confirm")  # nothing to confirm before a receipt
        calls = await t.send_photo(ALICE, f"receipt-{amount}")
        assert f"Receipt received for <b>{amount}$</b>" in t.last_text(calls)
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        (row,) = deposits(t, ALICE)
        assert row["amount"] == amount * 100 and row["status"] == "pending"
        (photo,) = sent(calls, OWNER_ID, "sendPhoto")
        assert f"Amount: <b>{amount}.00$</b>" in photo.text

    run(tmp_path, scenario)


def test_custom_amount_rejects_decimals_text_zero_and_too_much(tmp_path):
    async def scenario(t: ShopTest):
        calls = await t.press(ALICE, "dp")
        calls = await t.press(ALICE, t.button(calls, "Custom"))
        assert "Whole numbers only" in t.last_text(calls)
        for bad in ("12.5", "12,50", "abc", "-5", "1e3", "ten"):
            calls = await t.send(ALICE, bad)
            assert WHOLE_NUMBER in t.last_text(calls), bad
            assert BINANCE_ID not in all_text(calls), bad
        for bad in ("0", "100001", "5000000"):
            calls = await t.send(ALICE, bad)
            assert OUT_OF_RANGE in t.last_text(calls), bad
            assert BINANCE_ID not in all_text(calls), bad
        # Still waiting for an amount: the largest allowed one goes through.
        calls = await t.send(ALICE, "100000")
        assert "Add 100000$ to your balance" in t.last_text(calls)
        assert f"<code>{BINANCE_ID}</code>" in t.last_text(calls)
        assert deposits(t) == []

    run(tmp_path, scenario)


def test_custom_whole_number_amount_goes_all_the_way(tmp_path):
    async def scenario(t: ShopTest):
        calls = await t.press(ALICE, "dp")
        calls = await t.press(ALICE, t.button(calls, "Custom"))
        calls = await t.send(ALICE, "25")
        assert "Add 25$ to your balance" in t.last_text(calls)
        assert f"<code>{BINANCE_ID}</code>" in t.last_text(calls)
        calls = await t.send_photo(ALICE, "custom-receipt")
        assert "Receipt received for <b>25$</b>" in t.last_text(calls)
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        (row,) = deposits(t, ALICE)
        assert row["amount"] == 2500 and row["receipt_file_id"] == "custom-receipt"
        await approve_deposit(t, row["id"])
        assert row_of(t, ALICE)["balance"] == 2500

    run(tmp_path, scenario)


def test_custom_amount_must_be_sent_as_text(tmp_path):
    async def scenario(t: ShopTest):
        calls = await t.press(ALICE, "dp")
        await t.press(ALICE, t.button(calls, "Custom"))
        calls = await t.send_photo(ALICE, "too-early-receipt")
        assert "send this as a text message" in t.last_text(calls)
        assert BINANCE_ID not in all_text(calls)
        calls = await t.send(ALICE, "15")
        assert "Add 15$ to your balance" in t.last_text(calls)

    run(tmp_path, scenario)


def test_crafted_deposit_amount_buttons_are_refused(tmp_path):
    async def scenario(t: ShopTest):
        for data in ("da:0", "da:100001", "da:-5", "da:abc", "da:"):
            calls = await t.press(ALICE, data)
            assert "no longer valid" in t.alert(calls), data
            assert BINANCE_ID not in all_text(calls), data
        calls = await t.press(ALICE, "dsend")
        assert EXPIRED in t.alert(calls)
        assert deposits(t) == []

    run(tmp_path, scenario)


# ----------------------------------------------------------------- receipts
def test_photo_receipt_gets_confirm_button_but_nothing_is_sent_yet(tmp_path):
    async def scenario(t: ShopTest):
        await choose_amount(t, ALICE, 20)
        calls = await t.send_photo(ALICE, "alice-receipt", caption="paid!")
        assert "Receipt received for <b>20$</b>" in t.last_text(calls)
        assert t.button(calls, "Confirm") == "dsend"
        assert t.button(calls, "Cancel") == "cancel"
        assert deposits(t) == []
        assert t.messages(calls, OWNER_ID) == []

    run(tmp_path, scenario)


def test_image_document_receipt_is_accepted(tmp_path):
    async def scenario(t: ShopTest):
        await choose_amount(t, ALICE, 30)
        document = {"file_id": "receipt-file", "file_unique_id": "u-receipt-file",
                    "file_name": "receipt.png", "mime_type": "image/png"}
        calls = await t.send(ALICE, document=document)
        assert "Receipt received for <b>30$</b>" in t.last_text(calls)
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        (row,) = deposits(t, ALICE)
        assert row["receipt_kind"] == "document" and row["receipt_file_id"] == "receipt-file"
        (to_owner,) = sent(calls, OWNER_ID, "sendDocument")
        assert to_owner.params["document"] == "receipt-file"
        assert f"Deposit request #{row['id']}" in to_owner.text
        assert "Amount: <b>30.00$</b>" in to_owner.text
        assert {b["callback_data"] for b in to_owner.buttons} == {f"dok:{row['id']}", f"dno:{row['id']}"}
        calls = await approve_deposit(t, row["id"])  # caption of a document message
        assert row_of(t, ALICE)["balance"] == 3000
        (edit,) = edits(calls, OWNER_ID)
        assert edit.method == "editMessageCaption" and "Approved" in edit.text

    run(tmp_path, scenario)


def test_text_and_non_image_files_are_not_receipts(tmp_path):
    async def scenario(t: ShopTest):
        await choose_amount(t, ALICE, 20)
        not_receipts = [
            {"text": "I paid already"},
            {"document": {"file_id": "pdf", "file_unique_id": "u-pdf",
                          "file_name": "receipt.pdf", "mime_type": "application/pdf"}},
            {"document": {"file_id": "plain", "file_unique_id": "u-plain", "file_name": "receipt"}},
            {"video": {"file_id": "vid", "file_unique_id": "u-vid",
                       "width": 1, "height": 1, "duration": 1}},
            {"sticker": {"file_id": "stk", "file_unique_id": "u-stk", "type": "regular",
                         "width": 512, "height": 512, "is_animated": False, "is_video": False}},
            {"voice": {"file_id": "voice", "file_unique_id": "u-voice", "duration": 1}},
        ]
        for fields in not_receipts:
            calls = await t.send(ALICE, **fields)
            assert NOT_A_RECEIPT in t.last_text(calls), fields
            assert not t.has_button(calls, "Confirm"), fields
        # Confirm (e.g. a crafted press) is refused: no receipt was stored.
        calls = await t.press(ALICE, "dsend")
        assert EXPIRED in t.alert(calls) and is_alert(calls)
        assert deposits(t) == []
        # A real photo still works after the wrong attempts.
        calls = await t.send_photo(ALICE, "real-receipt")
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        (row,) = deposits(t, ALICE)
        assert row["receipt_file_id"] == "real-receipt" and row["amount"] == 2000

    run(tmp_path, scenario)


def test_second_photo_replaces_the_first(tmp_path):
    async def scenario(t: ShopTest):
        await choose_amount(t, ALICE, 50)
        await t.send_photo(ALICE, "wrong-receipt")
        calls = await t.send_photo(ALICE, "right-receipt")
        assert "Receipt received for <b>50$</b>" in t.last_text(calls)
        calls = await t.press(ALICE, t.button(calls, "Confirm"))
        (row,) = deposits(t, ALICE)
        assert row["receipt_file_id"] == "right-receipt"
        assert [c.params["photo"] for c in sent(calls, OWNER_ID, "sendPhoto")] == ["right-receipt"]

    run(tmp_path, scenario)


def test_confirm_without_a_receipt_is_expired(tmp_path):
    async def scenario(t: ShopTest):
        await choose_amount(t, ALICE, 20)
        calls = await t.press(ALICE, "dsend")
        assert EXPIRED in t.alert(calls) and is_alert(calls)
        assert deposits(t) == []
        assert t.messages(calls) == []

    run(tmp_path, scenario)


def test_confirm_after_cancel_is_expired(tmp_path):
    async def scenario(t: ShopTest):
        await choose_amount(t, ALICE, 20)
        calls = await t.send_photo(ALICE, "receipt")
        confirm = t.button(calls, "Confirm")
        calls = await t.press(ALICE, t.button(calls, "Cancel"))
        assert "Cancelled" in t.last_text(calls)
        calls = await t.press(ALICE, confirm)
        assert EXPIRED in t.alert(calls) and is_alert(calls)
        assert deposits(t) == []
        assert t.messages(calls, OWNER_ID) == []

    run(tmp_path, scenario)


def test_confirm_after_leaving_the_flow_is_expired(tmp_path):
    async def scenario(t: ShopTest):
        # Pressing another menu button ends the deposit.
        await choose_amount(t, ALICE, 20)
        calls = await t.send_photo(ALICE, "receipt")
        confirm = t.button(calls, "Confirm")
        await t.press(ALICE, "menu")
        calls = await t.press(ALICE, confirm)
        assert EXPIRED in t.alert(calls)
        # So does sending a command.
        await choose_amount(t, ALICE, 30)
        await t.send_photo(ALICE, "receipt-2")
        await t.send(ALICE, "/start")
        calls = await t.press(ALICE, confirm)
        assert EXPIRED in t.alert(calls)
        assert deposits(t) == []

    run(tmp_path, scenario)


def test_double_confirm_sends_one_request(tmp_path):
    async def scenario(t: ShopTest):
        await choose_amount(t, ALICE, 20)
        calls = await t.send_photo(ALICE, "receipt")
        confirm = t.button(calls, "Confirm")
        first = await t.press(ALICE, confirm)
        second = await t.press(ALICE, confirm)
        assert EXPIRED in t.alert(second)
        assert len(deposits(t)) == 1
        assert len(sent(first, OWNER_ID, "sendPhoto")) == 1
        assert t.messages(second) == []

    run(tmp_path, scenario)


# ------------------------------------------------------- request to the owner
def test_confirm_creates_pending_deposit_and_sends_receipt_to_owner(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, calls = await request_deposit(t, ALICE, 20, receipt="alice-receipt")
        mine = all_text(calls, ALICE["id"])
        assert "Request sent" in mine and f"Deposit #{dep_id}" in mine and "20.00$" in mine
        (row,) = deposits(t, ALICE)
        assert row["status"] == "pending" and row["amount"] == 2000 and row["method"] == "binance"
        assert row["receipt_file_id"] == "alice-receipt" and row["receipt_kind"] == "photo"
        assert row["decided_at"] is None
        (photo,) = sent(calls, OWNER_ID, "sendPhoto")
        assert photo.params["photo"] == "alice-receipt"  # the largest size of the photo
        assert photo.params.get("parse_mode") == "HTML"
        assert f"Deposit request #{dep_id}" in photo.text
        assert "<b>Alice</b> · @alice · <code>1001</code>" in photo.text
        assert "Amount: <b>20.00$</b>" in photo.text
        buttons = {b["text"]: b["callback_data"] for b in photo.buttons}
        assert buttons == {"✅ Approve": f"dok:{dep_id}", "❌ Reject": f"dno:{dep_id}"}
        # Nothing is credited before the owner decides.
        user = row_of(t, ALICE)
        assert user["balance"] == 0 and user["total_deposits"] == 0
        assert ledger(t, ALICE) == []

    run(tmp_path, scenario)


def test_deposit_request_escapes_the_customers_name(tmp_path):
    async def scenario(t: ShopTest):
        tom = tg_user(1003, "<Tom & Jerry>", "tomjerry")
        _, calls = await request_deposit(t, tom, 10)
        (photo,) = sent(calls, OWNER_ID, "sendPhoto")
        assert "<b>&lt;Tom &amp; Jerry&gt;</b>" in photo.text
        assert "<Tom" not in photo.text

    run(tmp_path, scenario)


def test_receipt_falls_back_to_text_when_photo_cannot_be_sent(tmp_path):
    async def scenario(t: ShopTest):
        t.tg.fail["sendPhoto"] = "Bad Request: wrong file identifier/HTTP URL specified"
        dep_id, calls = await request_deposit(t, ALICE, 20)
        del t.tg.fail["sendPhoto"]
        (message,) = sent(calls, OWNER_ID, "sendMessage")
        assert f"Deposit request #{dep_id}" in message.text
        assert "couldn't be attached" in message.text
        assert {b["callback_data"] for b in message.buttons} == {f"dok:{dep_id}", f"dno:{dep_id}"}
        # Approving from that text message works and edits it.
        calls = await t.press(OWNER, f"dok:{dep_id}")
        assert row_of(t, ALICE)["balance"] == 2000
        (edit,) = edits(calls, OWNER_ID)
        assert edit.method == "editMessageText" and "Approved" in edit.text
        assert edit.buttons == []

    run(tmp_path, scenario)


# ------------------------------------------------------------ owner decides
def test_approve_credits_balance_and_deposits_and_notifies(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, _ = await request_deposit(t, ALICE, 20)
        calls = await approve_deposit(t, dep_id)
        assert "Approved" in t.alert(calls)
        user = row_of(t, ALICE)
        assert user["balance"] == 2000 and user["total_deposits"] == 2000
        dep = t.db.get_deposit(dep_id)
        assert dep["status"] == "approved" and dep["decided_at"]
        (entry,) = ledger(t, ALICE)
        assert entry["delta"] == 2000 and entry["balance_after"] == 2000
        assert entry["reason"] == "deposit" and entry["ref"] == f"deposit#{dep_id}"
        note = t.last_text(calls, ALICE["id"])
        assert "Deposit approved" in note and "+20.00$" in note
        assert "New balance: <b>20.00$</b>" in note
        (edit,) = edits(calls, OWNER_ID)
        assert edit.method == "editMessageCaption"
        assert f"Deposit request #{dep_id}" in edit.text
        assert "Approved" in edit.text and "new balance 20.00$" in edit.text
        assert edit.buttons == []  # Approve/Reject removed
        calls = await t.send(ALICE, "/start")
        assert "Balance: <b>20.00$</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_approving_twice_credits_once(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, _ = await request_deposit(t, ALICE, 20)
        await approve_deposit(t, dep_id)
        for again in (approve_deposit, reject_deposit):
            calls = await again(t, dep_id)
            assert "already approved" in t.alert(calls) and is_alert(calls)
            assert t.messages(calls) == []
        user = row_of(t, ALICE)
        assert user["balance"] == 2000 and user["total_deposits"] == 2000
        assert len(ledger(t, ALICE)) == 1
        assert t.db.get_deposit(dep_id)["status"] == "approved"

    run(tmp_path, scenario)


def test_reject_keeps_balance_and_notifies(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, _ = await request_deposit(t, ALICE, 20)
        calls = await reject_deposit(t, dep_id)
        assert "Rejected" in t.alert(calls)
        assert t.db.get_deposit(dep_id)["status"] == "rejected"
        user = row_of(t, ALICE)
        assert user["balance"] == 0 and user["total_deposits"] == 0
        assert ledger(t, ALICE) == []
        note = t.last_text(calls, ALICE["id"])
        assert f"deposit request #{dep_id} of 20.00$ was rejected" in note
        (edit,) = edits(calls, OWNER_ID)
        assert "Rejected" in edit.text and edit.buttons == []
        calls = await approve_deposit(t, dep_id)
        assert "already rejected" in t.alert(calls)
        assert row_of(t, ALICE)["balance"] == 0

    run(tmp_path, scenario)


def test_only_the_owner_can_decide_deposits(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        dep_id, _ = await request_deposit(t, ALICE, 20)
        for who, prefix in ((ALICE, "dok"), (ALICE, "dno"), (ADMIN, "dok"), (BOB, "dno")):
            calls = await t.press(who, f"{prefix}:{dep_id}", photo=True)
            assert t.alert(calls) == OWNER_ONLY, (who["first_name"], prefix)
            assert t.messages(calls) == []
        assert t.db.get_deposit(dep_id)["status"] == "pending"
        assert row_of(t, ALICE)["balance"] == 0
        await approve_deposit(t, dep_id)
        assert row_of(t, ALICE)["balance"] == 2000

    run(tmp_path, scenario)


def test_unknown_or_malformed_deposit_buttons(tmp_path):
    async def scenario(t: ShopTest):
        calls = await t.press(OWNER, "dok:999", photo=True)
        assert "doesn't exist" in t.alert(calls)
        calls = await t.press(OWNER, "dno:abc", photo=True)
        assert "no longer valid" in t.alert(calls)
        assert t.messages(calls) == []

    run(tmp_path, scenario)


def test_pending_deposits_are_decided_independently(tmp_path):
    async def scenario(t: ShopTest):
        first, _ = await request_deposit(t, ALICE, 10)
        second, _ = await request_deposit(t, ALICE, 30)
        bobs, _ = await request_deposit(t, BOB, 50)
        await approve_deposit(t, second)
        await reject_deposit(t, first)
        await approve_deposit(t, bobs)
        assert row_of(t, ALICE)["balance"] == 3000 and row_of(t, ALICE)["total_deposits"] == 3000
        assert row_of(t, BOB)["balance"] == 5000 and row_of(t, BOB)["total_deposits"] == 5000
        statuses = {row["id"]: row["status"] for row in deposits(t)}
        assert statuses == {first: "rejected", second: "approved", bobs: "approved"}

    run(tmp_path, scenario)


def test_approval_still_credits_when_the_customer_blocked_the_bot(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, _ = await request_deposit(t, ALICE, 20)
        t.tg.fail["sendMessage"] = "Forbidden: bot was blocked by the user"
        calls = await approve_deposit(t, dep_id)
        del t.tg.fail["sendMessage"]
        assert "Approved" in t.alert(calls)
        assert row_of(t, ALICE)["balance"] == 2000
        assert t.db.get_deposit(dep_id)["status"] == "approved"
        (edit,) = edits(calls, OWNER_ID)
        assert "Approved" in edit.text

    run(tmp_path, scenario)


def test_owner_deciding_a_deposit_keeps_the_owners_own_action(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, _ = await request_deposit(t, ALICE, 20)
        calls = await t.press(OWNER, "dp")
        await t.press(OWNER, t.button(calls, "Custom"))  # the owner is typing an amount
        await approve_deposit(t, dep_id)
        calls = await t.send(OWNER, "15")
        assert "Add 15$ to your balance" in t.last_text(calls)
        assert row_of(t, ALICE)["balance"] == 2000

    run(tmp_path, scenario)


def test_owner_and_admin_can_add_balance_too(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        dep_id, calls = await request_deposit(t, ADMIN, 30)
        assert sent(calls, OWNER_ID, "sendPhoto")
        await approve_deposit(t, dep_id)
        assert row_of(t, ADMIN)["balance"] == 3000
        own_id, calls = await request_deposit(t, OWNER, 10)
        (photo,) = sent(calls, OWNER_ID, "sendPhoto")
        assert f"dok:{own_id}" in datas([photo])
        await approve_deposit(t, own_id)
        assert row_of(t, OWNER)["balance"] == 1000

    run(tmp_path, scenario)


def test_not_enough_balance_leads_to_add_balance_then_purchase(tmp_path):
    async def scenario(t: ShopTest):
        card = make_label(t, "Gift/Card", 2500)
        add_stock(t, card, "CARD-1")
        calls = await t.press(ALICE, f"sb:{card}:1")
        assert "Not enough balance" in t.last_text(calls)
        calls = await t.press(ALICE, t.button(calls, "Add Balance"))
        assert "Choose how much you want to add" in t.last_text(calls)
        calls = await t.press(ALICE, exact_button(calls, "30$"))
        calls = await t.send_photo(ALICE, "receipt")
        await t.press(ALICE, t.button(calls, "Confirm"))
        (row,) = deposits(t, ALICE)
        await approve_deposit(t, row["id"])
        calls = await buy(t, ALICE, card, 1)
        assert "CARD-1" in t.texts(calls, ALICE["id"])
        assert row_of(t, ALICE)["balance"] == 500
        assert row_of(t, OWNER)["earnings"] == 2500

    run(tmp_path, scenario)


# ================================================================ auto seller
def test_deposits_of_600_and_400_make_a_seller(tmp_path):
    async def scenario(t: ShopTest):
        card = make_label(t, "Gift/Card", 1000)
        add_stock(t, card, "CARD-1")
        first, _ = await request_deposit(t, ALICE, 600)
        calls = await approve_deposit(t, first)
        assert not row_of(t, ALICE)["is_seller"]
        assert CONGRATS not in all_text(calls)
        second, _ = await request_deposit(t, ALICE, 400)
        calls = await approve_deposit(t, second, anyway=True)  # her first is listed
        user = row_of(t, ALICE)
        assert user["is_seller"] and user["total_deposits"] == 100000
        note = t.last_text(calls, ALICE["id"])
        assert CONGRATS in note and "1000$" in note
        assert "<b>Seller</b>" in note and "<b>10% off</b>" in note
        edit = edits(calls, OWNER_ID)[-1]  # after the one showing her first deposit
        assert "now a Seller" in edit.text
        # The discount really applies.
        calls = await t.send(ALICE, "/start")
        assert "Seller" in t.last_text(calls) and "10% off" in t.last_text(calls)
        calls = await t.press(ALICE, f"sh:{card}:0")
        assert "<b>9.00$</b>" in t.last_text(calls) and "seller price" in t.last_text(calls)
        # Later deposits don't congratulate again.
        third, _ = await request_deposit(t, ALICE, 10)
        calls = await approve_deposit(t, third)
        assert CONGRATS not in all_text(calls) and "now a Seller" not in all_text(calls)

    run(tmp_path, scenario)


def test_seller_threshold_is_reached_at_exactly_1000(tmp_path):
    async def scenario(t: ShopTest):
        first, _ = await request_deposit(t, ALICE, 999)
        await approve_deposit(t, first)
        assert not row_of(t, ALICE)["is_seller"]
        second, _ = await request_deposit(t, ALICE, 1)
        calls = await approve_deposit(t, second, anyway=True)  # her first is listed
        assert row_of(t, ALICE)["is_seller"]
        assert CONGRATS in t.last_text(calls, ALICE["id"])

    run(tmp_path, scenario)


def test_rejected_deposits_do_not_count_toward_seller(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, _ = await request_deposit(t, ALICE, 1000)
        calls = await reject_deposit(t, dep_id)
        user = row_of(t, ALICE)
        assert not user["is_seller"] and user["total_deposits"] == 0
        assert CONGRATS not in all_text(calls)

    run(tmp_path, scenario)


def test_removed_seller_is_not_promoted_again(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, _ = await request_deposit(t, ALICE, 1000)
        await approve_deposit(t, dep_id)
        assert row_of(t, ALICE)["is_seller"]
        calls = await t.send(OWNER, f"/removeseller {ALICE['id']}")
        assert "no longer a seller" in t.last_text(calls, OWNER_ID)
        assert not row_of(t, ALICE)["is_seller"]
        # More deposits (still above the threshold) don't bring it back.
        dep_id, _ = await request_deposit(t, ALICE, 10)
        calls = await approve_deposit(t, dep_id, anyway=True)
        user = row_of(t, ALICE)
        assert not user["is_seller"] and user["total_deposits"] == 101000
        assert CONGRATS not in all_text(calls) and "Seller" not in all_text(calls)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 5")
        assert not row_of(t, ALICE)["is_seller"]
        assert "seller" not in all_text(calls).lower()
        # /seller brings it back.
        await t.send(OWNER, f"/seller {ALICE['id']}")
        assert row_of(t, ALICE)["is_seller"]

    run(tmp_path, scenario)


def test_seller_removed_before_threshold_is_not_auto_promoted(tmp_path):
    async def scenario(t: ShopTest):
        register(t, ALICE)
        await t.send(OWNER, f"/seller {ALICE['id']}")
        await t.send(OWNER, f"/removeseller {ALICE['id']}")
        dep_id, _ = await request_deposit(t, ALICE, 1000)
        calls = await approve_deposit(t, dep_id)
        assert not row_of(t, ALICE)["is_seller"]
        assert CONGRATS not in all_text(calls)

    run(tmp_path, scenario)


def test_addbal_counts_toward_seller_threshold(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, _ = await request_deposit(t, ALICE, 600)
        await approve_deposit(t, dep_id)
        calls = await t.send(OWNER, f"/addbal {ALICE['id']} 400")
        user = row_of(t, ALICE)
        assert user["is_seller"]
        assert user["total_deposits"] == 100000 and user["balance"] == 100000
        assert "now a seller" in t.last_text(calls, OWNER_ID)
        note = t.last_text(calls, ALICE["id"])
        assert "400.00$" in note and "<b>Seller</b>" in note and "10% off" in note
        manual = [row for row in deposits(t, ALICE) if row["method"] == "manual"]
        assert len(manual) == 1 and manual[0]["status"] == "approved" and manual[0]["amount"] == 40000
        assert [entry["reason"] for entry in ledger(t, ALICE)] == ["deposit", "manual deposit"]
        # /addbal alone can reach it too.
        register(t, BOB)
        await t.send(OWNER, f"/addbal {BOB['id']} 1000")
        assert row_of(t, BOB)["is_seller"]

    run(tmp_path, scenario)


def test_modifybal_does_not_count_toward_seller(tmp_path):
    async def scenario(t: ShopTest):
        register(t, ALICE)
        calls = await t.send(OWNER, f"/modifybal {ALICE['id']} 1500")
        user = row_of(t, ALICE)
        assert user["balance"] == 150000
        assert user["total_deposits"] == 0 and not user["is_seller"]
        assert t.messages(calls, ALICE["id"]) == []

    run(tmp_path, scenario)


def test_existing_seller_is_not_congratulated_again(tmp_path):
    async def scenario(t: ShopTest):
        register(t, ALICE)
        await t.send(OWNER, f"/seller {ALICE['id']}")
        dep_id, _ = await request_deposit(t, ALICE, 1000)
        calls = await approve_deposit(t, dep_id)
        assert row_of(t, ALICE)["is_seller"]
        assert CONGRATS not in all_text(calls)

    run(tmp_path, scenario)


# =================================================================== earnings
async def build_sales(t: ShopTest) -> tuple[int, int]:
    """Owner and two admins with stock; Alice and Bob deposit and buy through the bot.

    Result: owner earned 28.50$ (3 sold), Adam 29.50$ (2 sold), Zara nothing yet;
    2 orders / 5 products / 58.00$; approved deposits 150.00$; 2 products left."""
    make_admin(t, ADMIN)
    make_admin(t, ADMIN2)
    five = make_label(t, "Esim/Oman/5GB", 950)
    steam = make_label(t, "Codes/Steam", 2000)
    add_stock(t, five, "O1", "O2", "O3")
    add_stock(t, five, "A1", "A2", owner_id=ADMIN["id"])
    add_stock(t, steam, "S1", owner_id=ADMIN["id"])
    add_stock(t, steam, "Z1", owner_id=ADMIN2["id"])
    dep_id, _ = await request_deposit(t, ALICE, 100)
    await approve_deposit(t, dep_id)
    await t.send(BOB, "/start")
    await t.send(OWNER, f"/addbal {BOB['id']} 50")
    t.db.rng = Picks(OWNER_ID, OWNER_ID, OWNER_ID, ADMIN["id"], ADMIN["id"])
    await buy(t, ALICE, five, 4)  # O1 O2 O3 (owner) + A1 (Adam)
    await buy(t, BOB, steam, 1)  # S1 (Adam), not Zara's Z1
    return five, steam


def test_earnings_button_only_for_owner_and_admins(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        for who, expected in ((OWNER, True), (ADMIN, True), (ALICE, False)):
            calls = await t.send(who, "/start")
            assert ("ea" in datas(calls)) is expected, who["first_name"]

    run(tmp_path, scenario)


def test_owner_earnings_overview(tmp_path):
    async def scenario(t: ShopTest):
        await build_sales(t)
        await request_withdrawal(t, ADMIN, "10")  # pending
        await request_withdrawal(t, OWNER, "8.50", "999888777")  # recorded at once
        calls = await t.send(OWNER, "/start")
        calls = await t.press(OWNER, t.button(calls, "Earnings"))
        text = t.last_text(calls)
        # The owner's own numbers.
        assert "You (Owner)" in text
        assert "Earned: <b>28.50$</b> (3 sold)" in text
        assert "Withdrawn: 8.50$" in text
        assert "Available: <b>20.00$</b>" in text
        # Each admin.
        assert "<b>Adam</b> <code>2001</code>" in text
        assert "Earned 29.50$ (2 sold) · Available 19.50$ · Pending withdrawal 10.00$" in text
        assert "<b>Zara</b> <code>2002</code>" in text
        assert "Earned 0.00$ (0 sold) · Available 0.00$" in text
        assert text.index("Adam") < text.index("Zara")  # best seller first
        # The whole shop.
        assert "Total earnings: <b>58.00$</b>" in text
        assert "Total purchases: <b>2</b> orders · 5 products" in text
        assert "Approved deposits: 150.00$" in text
        assert "Balances held: 92.00$" in text
        assert "Products in stock: 2" in text
        assert t.button(calls, "Withdraw") == "ew"
        assert row_of(t, OWNER)["earnings"] == 2000 and row_of(t, ADMIN)["earnings"] == 1950

    run(tmp_path, scenario)


def test_owner_earnings_with_no_sales_and_no_admins(tmp_path):
    async def scenario(t: ShopTest):
        calls = await t.press(OWNER, "ea")
        text = t.last_text(calls)
        assert "Earned: <b>0.00$</b> (0 sold)" in text
        assert "No admins yet" in text
        assert "Total earnings: <b>0.00$</b>" in text
        assert "Total purchases: <b>0</b> orders · 0 products" in text

    run(tmp_path, scenario)


def test_admin_sees_only_own_earnings(tmp_path):
    async def scenario(t: ShopTest):
        await build_sales(t)
        await request_withdrawal(t, ADMIN, "10")
        calls = await t.send(ADMIN, "/start")
        calls = await t.press(ADMIN, t.button(calls, "Earnings"))
        text = t.last_text(calls)
        assert "Your Earnings" in text
        assert "Earned: <b>29.50$</b> (2 sold)" in text
        assert "Withdrawn: 0.00$" in text
        assert "Waiting for approval: 10.00$" in text
        assert "Available: <b>19.50$</b>" in text
        assert "Your products in stock: 1" in text
        for other in ("Owner", "Zara", "28.50$", "Whole shop", "Total earnings", "58.00$"):
            assert other not in text, other
        assert t.has_button(calls, "Withdraw")
        # After the owner approves, the amount moves to "Withdrawn".
        (wd,) = withdrawals(t, ADMIN)
        await t.press(OWNER, f"wok:{wd['id']}")
        calls = await t.press(ADMIN, "ea")
        text = t.last_text(calls)
        assert "Withdrawn: 10.00$" in text and "Waiting for approval: 0.00$" in text
        assert "Available: <b>19.50$</b>" in text

    run(tmp_path, scenario)


def test_customer_cannot_open_earnings(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 1500)
        give_sales(t, OWNER_ID, 1000)
        calls = await t.press(ALICE, "ea")
        assert t.alert(calls) == "Earnings are for the owner and admins." and is_alert(calls)
        assert t.messages(calls) == []

    run(tmp_path, scenario)


def test_removed_admin_keeps_access_to_earned_money(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 1500, 2)  # 30.00$
        await t.send(OWNER, f"/removeadmin {ADMIN['id']}")
        calls = await t.press(OWNER, "ea")
        assert "<b>Adam</b> <code>2001</code> (not admin anymore)" in t.last_text(calls)
        calls = await t.press(ADMIN, "ea")
        assert "Your Earnings" in t.last_text(calls)
        assert "Available: <b>30.00$</b>" in t.last_text(calls)
        calls = await request_withdrawal(t, ADMIN, "30")
        assert "Withdrawal request sent" in all_text(calls, ADMIN["id"])
        assert row_of(t, ADMIN)["earnings"] == 0
        assert sent(calls, OWNER_ID, "sendMessage")

    run(tmp_path, scenario)


def test_earnings_keep_sales_of_a_deleted_label(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        label = give_sales(t, ADMIN["id"], 1200, 2)  # sold out, so it can be deleted
        calls = await t.press(OWNER, f"ld:{label}")
        calls = await t.press(OWNER, t.button(calls, "Yes, delete"))
        assert "Deleted" in t.last_text(calls)
        assert t.db.get_label(label) is None
        calls = await t.press(OWNER, "ea")
        text = t.last_text(calls)
        assert "Earned 24.00$ (2 sold) · Available 24.00$" in text
        assert "Total earnings: <b>24.00$</b>" in text
        assert "Total purchases: <b>1</b> orders · 2 products" in text
        calls = await t.press(ADMIN, "ea")
        assert "Earned: <b>24.00$</b> (2 sold)" in t.last_text(calls)

    run(tmp_path, scenario)


# ================================================================== /withdraw
def test_withdraw_for_a_customer_without_earnings(tmp_path):
    async def scenario(t: ShopTest):
        calls = await t.send(ALICE, "/withdraw")
        assert "/withdraw is for the owner and admins" in t.last_text(calls)
        calls = await t.send(ALICE, "10")
        assert "Send /start" in t.last_text(calls)
        calls = await t.press(ALICE, "wall")
        assert EXPIRED in t.alert(calls)
        assert withdrawals(t) == []

    run(tmp_path, scenario)


def test_withdraw_for_an_admin_without_earnings(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        calls = await t.send(ADMIN, "/withdraw")
        assert "no earnings to withdraw" in t.last_text(calls)
        assert "Available: <b>0.00$</b>" in t.last_text(calls)
        assert not t.has_button(calls, "Withdraw all")
        calls = await t.send(ADMIN, "10")
        assert "Send /start" in t.last_text(calls)
        assert withdrawals(t) == []

    run(tmp_path, scenario)


def test_withdraw_amount_validation(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2950)
        calls = await t.send(ADMIN, "/withdraw")
        text = t.last_text(calls)
        assert "Withdraw earnings" in text and "Available: <b>29.50$</b>" in text
        assert t.button(calls, "Withdraw all (29.50$)") == "wall"
        assert t.has_button(calls, "Cancel")
        for bad in ("abc", "0", "0.00", "-5", "1.234", "ten dollars"):
            calls = await t.send(ADMIN, bad)
            assert BAD_AMOUNT in t.last_text(calls), bad
        for bad in ("29.51", "30", "1000"):
            calls = await t.send(ADMIN, bad)
            assert "You only have 29.50$ available" in t.last_text(calls), bad
        assert withdrawals(t) == [] and row_of(t, ADMIN)["earnings"] == 2950
        calls = await t.send(ADMIN, "29.50")
        assert "Send your <b>Binance ID</b> to receive <b>29.50$</b>" in t.last_text(calls)
        assert row_of(t, ADMIN)["earnings"] == 2950  # nothing reserved before the Binance ID

    run(tmp_path, scenario)


def test_withdraw_accepts_comma_decimals_and_dollar_sign(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2000)
        await request_withdrawal(t, ADMIN, "12,25")
        await request_withdrawal(t, ADMIN, "$3")
        assert [row["amount"] for row in withdrawals(t, ADMIN)] == [1225, 300]
        assert row_of(t, ADMIN)["earnings"] == 2000 - 1225 - 300

    run(tmp_path, scenario)


def test_withdraw_request_reserves_earnings_and_reaches_the_owner(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2950)
        calls = await request_withdrawal(t, ADMIN, "10", "123456789")
        mine = t.last_text(calls, ADMIN["id"])
        assert "Withdrawal request sent" in mine and "10.00$" in mine
        assert "<code>123456789</code>" in mine and "Earnings left: 19.50$" in mine
        (wd,) = withdrawals(t, ADMIN)
        assert wd["status"] == "pending" and wd["amount"] == 1000
        assert wd["binance_id"] == "123456789" and wd["decided_at"] is None
        assert row_of(t, ADMIN)["earnings"] == 1950  # reserved straight away
        (request,) = sent(calls, OWNER_ID)
        assert f"Withdrawal request #{wd['id']}" in request.text
        assert "<b>Adam</b> · @adam · <code>2001</code>" in request.text
        assert "Amount: <b>10.00$</b>" in request.text
        assert "Binance ID: <code>123456789</code>" in request.text
        assert "Earnings left: 19.50$" in request.text
        buttons = {b["text"]: b["callback_data"] for b in request.buttons}
        assert buttons == {"✅ Approve": f"wok:{wd['id']}", "❌ Reject": f"wno:{wd['id']}"}
        calls = await t.send(ADMIN, "/start")
        assert "Earnings: <b>19.50$</b>" in t.last_text(calls)

    run(tmp_path, scenario)


def test_withdraw_all_button(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2950)
        calls = await t.send(ADMIN, "/withdraw")
        calls = await t.press(ADMIN, t.button(calls, "Withdraw all"))
        assert "to receive <b>29.50$</b>" in t.last_text(calls)
        calls = await t.send(ADMIN, "123456789")
        (wd,) = withdrawals(t, ADMIN)
        assert wd["amount"] == 2950 and wd["status"] == "pending"
        assert row_of(t, ADMIN)["earnings"] == 0
        (request,) = sent(calls, OWNER_ID)
        assert "Amount: <b>29.50$</b>" in request.text
        # Nothing left: a new /withdraw says so.
        calls = await t.send(ADMIN, "/withdraw")
        assert "no earnings to withdraw" in t.last_text(calls)

    run(tmp_path, scenario)


def test_withdraw_approve(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2950)
        await request_withdrawal(t, ADMIN, "10", "123456789")
        (wd,) = withdrawals(t, ADMIN)
        calls = await t.press(OWNER, f"wok:{wd['id']}")
        assert "Approved" in t.alert(calls)
        wd = t.db.get_withdrawal(wd["id"])
        assert wd["status"] == "approved" and wd["decided_at"]
        assert row_of(t, ADMIN)["earnings"] == 1950  # stays withdrawn
        note = t.last_text(calls, ADMIN["id"])
        assert "Withdrawal approved" in note and "10.00$" in note and "<code>123456789</code>" in note
        (edit,) = edits(calls, OWNER_ID)
        assert f"Withdrawal request #{wd['id']}" in edit.text and "Approved" in edit.text
        assert edit.buttons == []

    run(tmp_path, scenario)


def test_withdraw_reject_refunds_earnings(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2950)
        await request_withdrawal(t, ADMIN, "10")
        (wd,) = withdrawals(t, ADMIN)
        calls = await t.press(OWNER, f"wno:{wd['id']}")
        assert "Rejected" in t.alert(calls)
        assert t.db.get_withdrawal(wd["id"])["status"] == "rejected"
        assert row_of(t, ADMIN)["earnings"] == 2950
        note = t.last_text(calls, ADMIN["id"])
        assert "withdrawal request of 10.00$ was rejected" in note and "available: 29.50$" in note
        (edit,) = edits(calls, OWNER_ID)
        assert "Rejected" in edit.text and edit.buttons == []
        # A rejected request doesn't count as withdrawn.
        calls = await t.press(ADMIN, "ea")
        text = t.last_text(calls)
        assert "Withdrawn: 0.00$" in text and "Available: <b>29.50$</b>" in text

    run(tmp_path, scenario)


def test_reject_refund_is_added_to_current_earnings(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2950)
        await request_withdrawal(t, ADMIN, "10")
        give_sales(t, ADMIN["id"], 2950)  # another sale while the request waits
        (wd,) = withdrawals(t, ADMIN)
        calls = await t.press(OWNER, f"wno:{wd['id']}")
        assert row_of(t, ADMIN)["earnings"] == 5900
        assert "available: 59.00$" in t.last_text(calls, ADMIN["id"])

    run(tmp_path, scenario)


def test_deciding_a_withdrawal_twice(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 3000)
        await request_withdrawal(t, ADMIN, "10")
        await request_withdrawal(t, ADMIN, "5")
        first, second = withdrawals(t, ADMIN)
        await t.press(OWNER, f"wok:{first['id']}")
        for data in (f"wok:{first['id']}", f"wno:{first['id']}"):
            calls = await t.press(OWNER, data)
            assert "already approved" in t.alert(calls) and is_alert(calls), data
            assert t.messages(calls) == []
        await t.press(OWNER, f"wno:{second['id']}")
        for data in (f"wno:{second['id']}", f"wok:{second['id']}"):
            calls = await t.press(OWNER, data)
            assert "already rejected" in t.alert(calls), data
            assert t.messages(calls) == []
        # 30 - 10 (approved); the rejected 5 came back exactly once.
        assert row_of(t, ADMIN)["earnings"] == 2000
        assert t.db.get_withdrawal(first["id"])["status"] == "approved"
        assert t.db.get_withdrawal(second["id"])["status"] == "rejected"

    run(tmp_path, scenario)


def test_only_the_owner_can_decide_withdrawals(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        make_admin(t, ADMIN2)
        give_sales(t, ADMIN["id"], 3000)
        await request_withdrawal(t, ADMIN, "10")
        (wd,) = withdrawals(t, ADMIN)
        for who, prefix in ((ADMIN, "wok"), (ADMIN, "wno"), (ADMIN2, "wok"), (ALICE, "wno")):
            calls = await t.press(who, f"{prefix}:{wd['id']}")
            assert t.alert(calls) == OWNER_ONLY, (who["first_name"], prefix)
            assert t.messages(calls) == []
        assert t.db.get_withdrawal(wd["id"])["status"] == "pending"
        assert row_of(t, ADMIN)["earnings"] == 2000
        calls = await t.press(OWNER, "wok:999")
        assert "doesn't exist" in t.alert(calls)

    run(tmp_path, scenario)


def test_owner_withdrawal_is_recorded_instantly(tmp_path):
    async def scenario(t: ShopTest):
        give_sales(t, OWNER_ID, 950, 3)  # 28.50$
        calls = await t.send(OWNER, "/withdraw")
        assert "Available: <b>28.50$</b>" in t.last_text(calls)
        calls = await t.send(OWNER, "8.50")
        assert "Binance ID" in t.last_text(calls)  # the owner is still asked for it
        calls = await t.send(OWNER, "987654321")
        text = t.last_text(calls)
        assert "Withdrawal recorded" in text and "8.50$" in text
        assert "<code>987654321</code>" in text and "Earnings left: 20.00$" in text
        (wd,) = withdrawals(t, OWNER)
        assert wd["status"] == "approved" and wd["decided_at"] and wd["amount"] == 850
        assert row_of(t, OWNER)["earnings"] == 2000
        assert not any(d.startswith(("wok:", "wno:")) for d in datas(calls))
        assert "Withdrawal request" not in all_text(calls)
        assert len(t.messages(calls)) == 1
        calls = await t.press(OWNER, "ea")
        text = t.last_text(calls)
        assert "Withdrawn: 8.50$" in text and "Available: <b>20.00$</b>" in text

    run(tmp_path, scenario)


def test_withdraw_button_on_the_earnings_screen(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2000)
        give_sales(t, OWNER_ID, 1000)
        for who, binance_id in ((ADMIN, "111111111"), (OWNER, "222222222")):
            calls = await t.send(who, "/start")
            calls = await t.press(who, t.button(calls, "Earnings"))
            calls = await t.press(who, t.button(calls, "Withdraw"))
            assert "Withdraw earnings" in t.last_text(calls), who["first_name"]
            calls = await t.press(who, t.button(calls, "Withdraw all"))
            assert "Binance ID" in t.last_text(calls)
            await t.send(who, binance_id)
        found = {row["user_id"]: (row["amount"], row["status"], row["binance_id"])
                 for row in withdrawals(t)}
        assert found == {ADMIN["id"]: (2000, "pending", "111111111"),
                         OWNER_ID: (1000, "approved", "222222222")}
        assert row_of(t, ADMIN)["earnings"] == 0 and row_of(t, OWNER)["earnings"] == 0

    run(tmp_path, scenario)


def test_withdraw_cancel_and_stale_withdraw_all(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2000)
        calls = await t.send(ADMIN, "/withdraw")
        withdraw_all = t.button(calls, "Withdraw all")
        calls = await t.press(ADMIN, t.button(calls, "Cancel"))
        assert "Cancelled" in t.last_text(calls)
        calls = await t.press(ADMIN, withdraw_all)
        assert EXPIRED in t.alert(calls) and is_alert(calls)
        calls = await t.send(ADMIN, "10")
        assert "Send /start" in t.last_text(calls)
        # Cancel at the Binance ID step.
        await t.send(ADMIN, "/withdraw")
        calls = await t.send(ADMIN, "10")
        await t.press(ADMIN, t.button(calls, "Cancel"))
        calls = await t.send(ADMIN, "123456789")
        assert "Send /start" in t.last_text(calls)
        assert withdrawals(t) == [] and row_of(t, ADMIN)["earnings"] == 2000

    run(tmp_path, scenario)


def test_withdraw_binance_id_is_validated_and_escaped(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2000)
        await t.send(ADMIN, "/withdraw")
        await t.send(ADMIN, "10")
        calls = await t.send(ADMIN, "9" * 65)
        assert "valid Binance ID" in t.last_text(calls)
        calls = await t.send_photo(ADMIN, "qr-code")
        assert "send this as a text message" in t.last_text(calls)
        assert withdrawals(t) == []
        calls = await t.send(ADMIN, "ID <12> & co")
        (wd,) = withdrawals(t, ADMIN)
        assert wd["binance_id"] == "ID <12> & co" and wd["amount"] == 1000
        assert "<code>ID &lt;12&gt; &amp; co</code>" in t.last_text(calls, ADMIN["id"])
        (request,) = sent(calls, OWNER_ID)
        assert "<code>ID &lt;12&gt; &amp; co</code>" in request.text

    run(tmp_path, scenario)


def test_owner_deciding_a_withdrawal_keeps_add_product_going(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2000)
        await request_withdrawal(t, ADMIN, "10")
        (wd,) = withdrawals(t, ADMIN)
        five = make_label(t, "Esim/5GB", 500)
        calls = await t.press(OWNER, f"ap:{five}:0")
        assert "ONE MESSAGE PER PRODUCT" in t.last_text(calls)
        await t.send(OWNER, "CODE-1")
        await t.press(OWNER, f"wok:{wd['id']}")
        calls = await t.press(OWNER, "apd")
        assert "Saved 1 product(s)" in t.last_text(calls, OWNER["id"])
        assert t.db.count_items(five) == 1
        assert t.db.get_withdrawal(wd["id"])["status"] == "approved"

    run(tmp_path, scenario)


def test_banned_users_cannot_deposit_or_withdraw(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 2000)
        await choose_amount(t, ALICE, 20)
        calls = await t.send_photo(ALICE, "receipt")
        confirm = t.button(calls, "Confirm")
        await t.send(OWNER, f"/ban {ALICE['id']}")
        calls = await t.press(ALICE, confirm)
        assert BANNED in t.alert(calls)
        calls = await t.press(ALICE, "dp")
        assert BANNED in t.alert(calls)
        assert deposits(t) == []
        await t.send(OWNER, f"/ban {ADMIN['id']}")
        calls = await t.send(ADMIN, "/withdraw")
        assert BANNED in t.last_text(calls)
        calls = await t.press(ADMIN, "ea")
        assert BANNED in t.alert(calls)
        assert withdrawals(t) == [] and row_of(t, ADMIN)["earnings"] == 2000
        # After /unban the money is still there and works again.
        await t.send(OWNER, f"/unban {ADMIN['id']}")
        await request_withdrawal(t, ADMIN, "20")
        assert row_of(t, ADMIN)["earnings"] == 0

    run(tmp_path, scenario)


def test_editing_an_old_message_is_not_taken_as_the_binance_id(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 5000)
        await t.send(ADMIN, "/withdraw")
        calls = await t.send(ADMIN, "20", message_id=500)
        assert "to receive <b>20.00$</b>" in t.last_text(calls)
        # The admin corrects the amount they already sent (20 → 10) by editing it.
        await edit_message(t, ADMIN, 500, "10")
        assert withdrawals(t) == [], "the edited amount message became a withdrawal request"
        assert row_of(t, ADMIN)["earnings"] == 5000
        await t.send(ADMIN, "123456789")
        (wd,) = withdrawals(t, ADMIN)
        assert wd["binance_id"] == "123456789"

    run(tmp_path, scenario)


def test_editing_the_user_id_during_addbal_does_not_credit_it_as_dollars(tmp_path):
    async def scenario(t: ShopTest):
        register(t, ALICE)
        register(t, BOB)
        await t.send(OWNER, "/addbal")
        calls = await t.send(OWNER, str(ALICE["id"]), message_id=700)
        assert "Send the amount to add" in t.last_text(calls)
        # The owner meant Bob and fixes the ID by editing the message they already sent.
        calls = await edit_message(t, OWNER, 700, str(BOB["id"]))
        for who in (ALICE, BOB):
            user = row_of(t, who)
            assert user["balance"] == 0 and user["total_deposits"] == 0, who["first_name"]
            assert not user["is_seller"], who["first_name"]
        assert deposits(t) == []
        assert t.messages(calls, ALICE["id"]) == []

    run(tmp_path, scenario)


# ===================================================== Telegram hiccups mid-way
def test_withdrawal_reaches_the_owner_even_if_the_admins_confirmation_fails(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 3000)
        await t.send(ADMIN, "/withdraw")
        await t.send(ADMIN, "10")
        inject_failure(
            t,
            lambda method, params: method == "sendMessage" and chat_of(params) == ADMIN["id"]
            and "Withdrawal request sent" in params.get("text", ""),
            error=TimedOut,
        )
        calls = await t.send(ADMIN, "123456789")
        asked = set(datas(delivered(sent(calls, OWNER_ID))))
        pending = [row for row in withdrawals(t, ADMIN) if row["status"] == "pending"]
        # Either the request was not created (earnings intact) or the owner can decide it.
        for row in pending:
            assert f"wok:{row['id']}" in asked, "earnings reserved by a request the owner never got"
        assert row_of(t, ADMIN)["earnings"] + sum(row["amount"] for row in pending) == 3000

    run(tmp_path, scenario)


def test_deposit_approval_is_announced_even_if_answering_the_press_fails(tmp_path):
    async def scenario(t: ShopTest):
        dep_id, _ = await request_deposit(t, ALICE, 20)
        inject_failure(
            t, lambda method, params: method == "answerCallbackQuery",
            description="Bad Request: query is too old and response timeout expired or query ID is invalid",
        )
        calls = await approve_deposit(t, dep_id)
        assert row_of(t, ALICE)["balance"] == 2000  # the money did move
        assert "Deposit approved" in all_text(delivered(calls), ALICE["id"])
        assert "Something went wrong" not in all_text(calls, OWNER_ID)
        (edit,) = edits(calls, OWNER_ID)
        assert "Approved" in edit.text and edit.buttons == []

    run(tmp_path, scenario)


def test_withdrawal_decision_is_announced_even_if_editing_the_request_times_out(tmp_path):
    async def scenario(t: ShopTest):
        make_admin(t, ADMIN)
        give_sales(t, ADMIN["id"], 3000)
        await request_withdrawal(t, ADMIN, "10")
        (wd,) = withdrawals(t, ADMIN)
        inject_failure(
            t, lambda method, params: method == "editMessageText" and chat_of(params) == OWNER_ID,
            error=TimedOut,
        )
        calls = await t.press(OWNER, f"wno:{wd['id']}")
        assert row_of(t, ADMIN)["earnings"] == 3000  # refunded
        assert "was rejected" in all_text(delivered(calls), ADMIN["id"])

    run(tmp_path, scenario)
