import re
from types import SimpleNamespace

import pytest

from kdbot.bot import BotApp
from kdbot.store import DAY, Store

OWNER, ADMIN2, USER = 1, 2, 42


class FakeMsg:
    def __init__(self, text, buttons=None):
        self.text = text
        self.buttons = buttons
        self.deleted = False

    async def edit(self, text):
        self.text = text

    async def delete(self):
        self.deleted = True


class FakeEvent:
    def __init__(self, uid, text, first_name="Bob", username=None):
        self.sender_id = uid
        self.raw_text = text
        self.chat_id = uid
        self.id = 7
        self.replies = []
        self._sender = SimpleNamespace(first_name=first_name, last_name=None, username=username)

    async def get_sender(self):
        return self._sender

    async def reply(self, text, **kw):
        msg = FakeMsg(text, kw.get("buttons"))
        self.replies.append(msg)
        return msg

    @property
    def last(self):
        return self.replies[-1].text


class FakeButtonPress:
    """An admin tapping one of the inline buttons under a bot message."""

    def __init__(self, uid, data, username=None):
        self.sender_id = uid
        self.data = data
        self.answers = []
        self.edited = None
        self._sender = SimpleNamespace(first_name="Bob", last_name=None, username=username)

    async def get_sender(self):
        return self._sender

    async def answer(self, text=None, **kw):
        self.answers.append(text)

    async def edit(self, text, **kw):
        self.edited = text


def target_msg(text, mid=1):
    """A message from the target bot (id 1 by default, so an update edits the same message)."""
    return SimpleNamespace(id=mid, message=text, entities=None, media=None)


class FakeRelay:
    busy = False

    def __init__(self):
        self.sent = []
        self.batches = []  # every ask_many call: the texts sent together
        self.first = "answer"  # the target bot's first answer
        self.update = None  # its text after a later edit, if any

    async def ask(self, text, deliver):
        self.sent.append(text)
        await deliver([target_msg(self.first)], False)
        if self.update is not None:
            await deliver([target_msg(self.update)], True)
        return True

    async def ask_many(self, texts, deliver):
        self.sent.extend(texts)
        self.batches.append(list(texts))
        await deliver([target_msg(f"answer {t}", i) for i, t in enumerate(texts, 1)], False)
        return True


class FakeBot:
    def __init__(self):
        self.sent = []  # (chat, text) of every new message
        self.edits = []  # (chat, message id, new text)
        self.next_id = 500

    async def send_message(self, chat, text, **kw):
        self.next_id += 1
        self.sent.append((chat, text))
        return SimpleNamespace(id=self.next_id, text=text)

    async def edit_message(self, chat, msg, text, **kw):
        self.edits.append((chat, msg.id, text))
        return SimpleNamespace(id=msg.id, text=text)


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def app(tmp_path, clock):
    cfg = SimpleNamespace(
        commands=("kd", "ko", "zz"), admin_ids=frozenset({ADMIN2}),
        code_prefix="ANONYMOUS", reply_timeout=30,
    )
    store = Store(str(tmp_path / "t.db"), clock=lambda: clock[0])
    return BotApp(cfg, store, FakeRelay(), FakeBot(), user=None, owner_id=OWNER,
                  bot_username="MyBot", clock=lambda: clock[0])


async def send(app, uid, text, username=None):
    ev = FakeEvent(uid, text, username=username)
    await app.handle(ev)
    return ev


async def make_code(app, args="25 0 100", uid=OWNER):
    ev = await send(app, uid, f"/code {args}")
    return re.findall(r"ANONYMOUS-[A-Za-z0-9]{11}", ev.replies[0].text)


async def press(app, msg, label, uid=OWNER):
    """Tap the button called `label` under the bot's message `msg`."""
    [button] = [b for row in msg.buttons for b in row if b.text == label]
    ev = FakeButtonPress(uid, button.type.data)  # the bytes Telegram sends back on a tap
    await app.handle_button(ev)
    return ev


async def set_response(app, name, text, command="/kd"):
    """Send /<name> with `text` below it, then tap which command it's for."""
    ev = await send(app, OWNER, f"/{name}\n{text}" if text else f"/{name}")
    assert "Which command" in ev.last
    return await press(app, ev.replies[-1], command)


def footer(user):
    return f"\n\nUSER: {user}\nStatus: Premium\nOwner: @anony6916"


def answer_with_line6(line6):
    return "\n".join(["l1", "l2", "l3", "l4", "l5", line6, "l7", "l8", "l9", "l10"])


async def test_start_shows_plan_and_commands(app):
    text = (await send(app, USER, "/start")).last
    assert "Plan:</b> Free" in text
    assert "/kd\n/ko\n/zz" in text
    assert "/code" not in text  # admin help hidden from normal users
    admin_text = (await send(app, OWNER, "/start")).last
    assert "/code" in admin_text and "/setresponse0aft" in admin_text


async def test_code_output(app):
    ev = await send(app, OWNER, "/code 25 0 100")
    assert ev.replies[0].text.startswith("✅ Code generated:\n<code>ANONYMOUS-")
    assert len(await make_code(app, "7 10 500 5")) == 5


async def test_only_admins_can_make_codes(app):
    assert (await send(app, USER, "/code 25 0 100")).replies == []
    assert len(await make_code(app, uid=ADMIN2)) == 1


async def test_code_bad_args(app):
    assert "Usage" in (await send(app, OWNER, "/code 25 0")).last


async def test_free_user_blocked(app):
    ev = await send(app, USER, "/kd hello")
    assert "needs Premium" in ev.last
    assert app.relay.sent == []


async def test_redeem_then_use_and_status(app, clock):
    [code] = await make_code(app, "25 0 100")
    ev = await send(app, USER, f"/redeem {code}")
    assert "Premium activated" in ev.last
    assert any("redeemed" in t for _, t in app.bot.sent)  # admins notified
    assert "Plan:</b> Premium" in (await send(app, USER, "/start")).last

    clock[0] += 5 * 3600
    status = (await send(app, USER, "/status")).last
    assert "24 days, 19 hours" in status

    ev = await send(app, USER, "/kd hello there")
    assert app.relay.sent == ["/kd hello there"]
    assert ev.replies[0].deleted  # "Working on it…" removed after the answer

    assert "already been used" in (await send(app, 99, f"/redeem {code}")).last
    assert "not valid" in (await send(app, 99, "/redeem ANONYMOUS-wrong")).last


async def test_antispam_from_code(app):
    [code] = await make_code(app, "25 60 100")
    await send(app, USER, f"/redeem {code}")
    await send(app, USER, "/kd one")
    ev = await send(app, USER, "/zz two")
    assert "Please wait" in ev.last
    assert app.relay.sent == ["/kd one"]


async def test_expired(app, clock):
    [code] = await make_code(app, "1 0 100")
    await send(app, USER, f"/redeem {code}")
    clock[0] += DAY + 1
    assert "expired" in (await send(app, USER, "/kd hi")).last
    assert "Plan:</b> Free" in (await send(app, USER, "/status")).last


async def test_relay_needs_message(app):
    assert "Usage: /ko" in (await send(app, OWNER, "/ko")).last


async def test_commands_for_other_bots_ignored(app):
    assert (await send(app, OWNER, "/start@OtherBot")).replies == []
    assert (await send(app, OWNER, "/start@mybot")).replies != []


async def test_admin_lists(app):
    [a, b] = await make_code(app, "3 0 100 2")
    await send(app, USER, f"/redeem {a}")
    codes = (await send(app, OWNER, "/codes")).last
    assert b in codes and a not in codes
    assert "Code deleted" in (await send(app, OWNER, f"/delcode {b}")).last
    assert "Bob" in (await send(app, OWNER, "/users")).last
    assert "removed" in (await send(app, OWNER, f"/removepremium {USER}")).last
    assert "needs Premium" in (await send(app, USER, "/kd x")).last


async def test_owner_is_told_what_other_users_send(app):
    [code] = await make_code(app, "25 0 100")
    await send(app, USER, f"/redeem {code}")
    await send(app, USER, "/kd hello 123", username="bob")
    assert (OWNER, "USER: @bob\nhello 123") in app.bot.sent  # command itself removed
    await send(app, ADMIN2, "/zz hi there")  # another admin, no @username
    assert (OWNER, "USER: Unknown\nhi there") in app.bot.sent
    before = len(app.bot.sent)
    await send(app, OWNER, "/ko mine")  # the owner's own commands: no notice
    assert not any(t.startswith("USER:") for _, t in app.bot.sent[before:])


async def test_no_notice_when_request_is_refused(app):
    await send(app, USER, "/kd hello")  # not Premium: nothing is relayed
    assert not any(t.startswith("USER:") for _, t in app.bot.sent)


# ---- what users see ----------------------------------------------------------

async def test_edit_updates_the_same_message_with_footer(app):
    app.relay.update = "final answer"
    await send(app, OWNER, "/kd hi", username="alice")
    assert app.bot.sent == [(OWNER, "answer")]  # one message only...
    assert app.bot.edits == [(OWNER, 501, "final answer" + footer("@alice"))]  # ...edited later


async def test_custom_texts_with_line_6(app):
    await set_response(app, "setresponsebef", "please wait")
    await set_response(app, "setresponseaft", "hey, thanks for using our service\n<line_6>\nhave a good day")
    app.relay.first = answer_with_line6("• PROCESSED: 3 + 0 / 10")
    app.relay.update = answer_with_line6("• PROCESSED: 10 + 0 / 10")
    await send(app, OWNER, "/kd hi")  # this user has no @username
    assert app.bot.sent == [(OWNER, "please wait")]
    assert app.bot.edits[-1][2] == (
        "hey, thanks for using our service\n• PROCESSED: 10 + 0 / 10\nhave a good day"
        + footer("Unknown")
    )


async def test_any_line_tag(app):
    await set_response(app, "setresponseaft", "a: <line_4>\nb: <line_6>\nmissing: [<line_20>]")
    app.relay.update = answer_with_line6("• PROCESSED: 10 + 0 / 10")
    await send(app, OWNER, "/kd hi")
    assert app.bot.edits[-1][2] == (
        "a: l4\nb: • PROCESSED: 10 + 0 / 10\nmissing: []" + footer("Unknown")
    )


def test_line_of_edges():
    from kdbot.bot import line_of

    assert line_of("a\nb", 1) == "a"
    assert line_of("a\nb", 0) == "" and line_of("a\nb", 3) == ""


async def test_zero_texts_when_nothing_was_processed(app):
    await set_response(app, "setresponse0bef", "starting…")
    await set_response(app, "setresponse0aft", "failed: <line_6>")
    await set_response(app, "setresponseaft", "not used here")
    app.relay.first = answer_with_line6("• PROCESSED: 0 + 10 / 10")
    app.relay.update = answer_with_line6("• PROCESSED: 0 + 10 / 10")
    await send(app, OWNER, "/kd hi", username="bob")
    assert app.bot.sent == [(OWNER, "starting…")]
    assert app.bot.edits[-1][2] == "failed: • PROCESSED: 0 + 10 / 10" + footer("@bob")


async def test_each_command_has_its_own_texts(app):
    await set_response(app, "setresponsebef", "zz text", command="/zz")
    await send(app, OWNER, "/zz hi")
    await send(app, OWNER, "/kd hi")  # nothing set for /kd: the target bot's message as it is
    assert [t for _, t in app.bot.sent] == ["zz text", "answer"]
    assert app.store.get_setting("setresponsebef:zz") == "zz text"
    assert app.store.get_setting("setresponsebef:kd") is None


async def test_all_commands_button(app):
    ev = await set_response(app, "setresponseaft", "same for all", command="All commands")
    assert "/kd /ko /zz" in ev.edited
    for c in ("kd", "ko", "zz"):
        assert app.store.get_setting(f"setresponseaft:{c}") == "same for all"


async def test_setresponse_is_admin_only_and_can_be_reset(app):
    assert (await send(app, USER, "/setresponseaft\nhi")).replies == []
    ev = await set_response(app, "setresponseaft", "hi\n<line_6>")
    assert "Saved /setresponseaft for /kd" in ev.edited
    assert app.store.get_setting("setresponseaft:kd") == "hi\n<line_6>"
    ev = await set_response(app, "setresponseaft", "")
    assert "reset for /kd" in ev.edited
    assert app.store.get_setting("setresponseaft:kd") is None


async def test_buttons_are_admin_only_and_single_use(app):
    question = (await send(app, OWNER, "/setresponsebef\nhello")).replies[-1]
    stranger = await press(app, question, "/kd", uid=USER)
    assert stranger.answers == ["Only admins can do this."] and stranger.edited is None
    assert app.store.get_setting("setresponsebef:kd") is None
    assert "Saved" in (await press(app, question, "/kd")).edited
    again = await press(app, question, "/zz")
    assert "expired" in again.answers[0]
    assert app.store.get_setting("setresponsebef:zz") is None


async def test_sends_new_message_if_edit_fails(app):
    async def broken_edit(*args, **kw):
        raise RuntimeError("can't edit")

    app.bot.edit_message = broken_edit
    app.relay.update = "final"
    await send(app, OWNER, "/kd hi")
    assert [t for _, t in app.bot.sent] == ["answer", "final" + footer("Unknown")]


# ---- credits ----------------------------------------------------------------

async def test_credits_deducted_on_use(app):
    [code] = await make_code(app, "25 0 50")
    await send(app, USER, f"/redeem {code}")
    assert app.store.get_credits(USER) == 50
    await send(app, USER, "/kd hello")
    assert app.store.get_credits(USER) == 45  # 50 - 5


async def test_zero_credits_blocks_use(app):
    [code] = await make_code(app, "25 0 3")  # only 3 credits, need 5
    await send(app, USER, f"/redeem {code}")
    ev = await send(app, USER, "/kd hello")
    assert "Not enough credits" in ev.last
    assert app.relay.sent == []


async def test_admins_dont_use_credits(app):
    await send(app, OWNER, "/kd hello")
    # owner has no credits row, should work fine


async def test_credits_refunded_on_no_answer(app):
    class NoAnswerRelay:
        busy = False
        sent = []
        async def ask(self, text, deliver):
            self.sent.append(text)
            return False  # no answer
    app.relay = NoAnswerRelay()
    [code] = await make_code(app, "25 0 50")
    await send(app, USER, f"/redeem {code}")
    await send(app, USER, "/kd hello")
    assert app.store.get_credits(USER) == 50  # refunded


async def test_credits_only_code(app):
    [premium_code] = await make_code(app, "25 0 0")
    await send(app, USER, f"/redeem {premium_code}")
    assert app.store.get_credits(USER) == 0
    [credits_code] = await make_code(app, "500")  # credits-only
    ev = await send(app, USER, f"/redeem {credits_code}")
    assert "500 credits added" in ev.last
    assert app.store.get_credits(USER) == 500


async def test_credits_only_code_needs_premium(app):
    [credits_code] = await make_code(app, "500")
    ev = await send(app, USER, f"/redeem {credits_code}")
    assert "need Premium first" in ev.last
    # code should not be consumed
    ev2 = await send(app, USER, f"/redeem {credits_code}")
    assert "need Premium first" in ev2.last  # still there, not "already been used"


async def test_status_shows_credits(app):
    [code] = await make_code(app, "25 0 100")
    await send(app, USER, f"/redeem {code}")
    status = (await send(app, USER, "/status")).last
    assert "Credits:</b> 100" in status


async def test_admin_status_shows_all_users(app):
    [code] = await make_code(app, "25 0 100")
    await send(app, USER, f"/redeem {code}")
    status = (await send(app, OWNER, "/status")).last
    assert "Users (1)" in status
    assert "100 credits" in status


# ---- /on /off per command ---------------------------------------------------

async def test_off_blocks_command(app):
    await send(app, OWNER, "/off /kd")
    ev = await send(app, OWNER, "/kd hello")  # owner is exempt
    assert app.relay.sent == ["/kd hello"]
    [code] = await make_code(app, "25 0 100")
    await send(app, USER, f"/redeem {code}")
    ev = await send(app, USER, "/kd test")
    assert "currently off" in ev.last
    # /zz should still work
    ev2 = await send(app, USER, "/zz test")
    assert "/zz test" in app.relay.sent


async def test_on_reenables_command(app):
    await send(app, OWNER, "/off /kd")
    await send(app, OWNER, "/on /kd")
    [code] = await make_code(app, "25 0 100")
    await send(app, USER, f"/redeem {code}")
    await send(app, USER, "/kd test")
    assert "/kd test" in app.relay.sent


async def test_off_all_commands(app):
    await send(app, OWNER, "/off")
    [code] = await make_code(app, "25 0 100")
    await send(app, USER, f"/redeem {code}")
    for cmd in ("kd", "ko", "zz"):
        ev = await send(app, USER, f"/{cmd} test")
        assert "currently off" in ev.last
    await send(app, OWNER, "/on")
    ev = await send(app, USER, "/kd test")
    assert "/kd test" in app.relay.sent


async def test_owner_multiline_sends_all_lines_together(app):
    ev = await send(app, OWNER, "/kd hi\nho\nhe")
    assert app.relay.batches == [["/kd hi", "/kd ho", "/kd he"]]  # one batch, sent at once
    assert ev.replies[0].text == "Sending 3 messages…" and ev.replies[0].deleted
    assert [t for _, t in app.bot.sent] == ["answer /kd hi", "answer /kd ho", "answer /kd he"]


async def test_multiline_is_owner_only(app):
    [code] = await make_code(app, "25 0 100")
    await send(app, USER, f"/redeem {code}")
    ev = await send(app, USER, "/kd hi\nho\nhe")
    assert app.relay.sent == ["/kd hi\nho\nhe"]


async def test_owner_multiline_skips_blank_lines(app):
    await send(app, OWNER, "/kd hi\n\nhe")
    assert app.relay.batches == [["/kd hi", "/kd he"]]


async def test_off_is_owner_only(app):
    ev = await send(app, ADMIN2, "/off /kd")
    assert "Only the owner" in ev.last
