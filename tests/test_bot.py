import re
from types import SimpleNamespace

import pytest

from kdbot.bot import BotApp
from kdbot.store import DAY, Store

OWNER, ADMIN2, USER = 1, 2, 42


class FakeMsg:
    def __init__(self, text):
        self.text = text
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
        msg = FakeMsg(text)
        self.replies.append(msg)
        return msg

    @property
    def last(self):
        return self.replies[-1].text


def target_msg(text):
    """A message from the target bot (always id 1, so an update edits the same message)."""
    return SimpleNamespace(id=1, message=text, entities=None, media=None)


class FakeRelay:
    busy = False

    def __init__(self):
        self.sent = []
        self.first = "answer"  # the target bot's first answer
        self.update = None  # its text after a later edit, if any

    async def ask(self, text, deliver):
        self.sent.append(text)
        await deliver([target_msg(self.first)], False)
        if self.update is not None:
            await deliver([target_msg(self.update)], True)
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


async def make_code(app, args="25 0", uid=OWNER):
    ev = await send(app, uid, f"/code {args}")
    return re.findall(r"ANONYMOUS-[A-Za-z0-9]{11}", ev.replies[0].text)


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
    ev = await send(app, OWNER, "/code 25 0")
    assert ev.replies[0].text.startswith("✅ Code generated:\n<code>ANONYMOUS-")
    assert len(await make_code(app, "7 10 5")) == 5


async def test_only_admins_can_make_codes(app):
    assert (await send(app, USER, "/code 25 0")).replies == []
    assert len(await make_code(app, uid=ADMIN2)) == 1


async def test_code_bad_args(app):
    assert "Usage" in (await send(app, OWNER, "/code 25")).last


async def test_free_user_blocked(app):
    ev = await send(app, USER, "/kd hello")
    assert "needs Premium" in ev.last
    assert app.relay.sent == []


async def test_redeem_then_use_and_status(app, clock):
    [code] = await make_code(app, "25 0")
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
    [code] = await make_code(app, "25 60")
    await send(app, USER, f"/redeem {code}")
    await send(app, USER, "/kd one")
    ev = await send(app, USER, "/zz two")
    assert "Please wait" in ev.last
    assert app.relay.sent == ["/kd one"]


async def test_expired(app, clock):
    [code] = await make_code(app, "1 0")
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
    [a, b] = await make_code(app, "3 0 2")
    await send(app, USER, f"/redeem {a}")
    codes = (await send(app, OWNER, "/codes")).last
    assert b in codes and a not in codes
    assert "Code deleted" in (await send(app, OWNER, f"/delcode {b}")).last
    assert "Bob" in (await send(app, OWNER, "/users")).last
    assert "removed" in (await send(app, OWNER, f"/removepremium {USER}")).last
    assert "needs Premium" in (await send(app, USER, "/kd x")).last


# ---- what users see ----------------------------------------------------------

async def test_edit_updates_the_same_message_with_footer(app):
    app.relay.update = "final answer"
    await send(app, OWNER, "/kd hi", username="alice")
    assert app.bot.sent == [(OWNER, "answer")]  # one message only...
    assert app.bot.edits == [(OWNER, 501, "final answer" + footer("@alice"))]  # ...edited later


async def test_custom_texts_with_line_6(app):
    await send(app, OWNER, "/setresponsebef\nplease wait")
    await send(app, OWNER, "/setresponseaft\nhey, thanks for using our service\n<line_6>\nhave a good day")
    app.relay.first = answer_with_line6("• PROCESSED: 3 + 0 / 10")
    app.relay.update = answer_with_line6("• PROCESSED: 10 + 0 / 10")
    await send(app, OWNER, "/kd hi")  # this user has no @username
    assert app.bot.sent == [(OWNER, "please wait")]
    assert app.bot.edits[-1][2] == (
        "hey, thanks for using our service\n• PROCESSED: 10 + 0 / 10\nhave a good day"
        + footer("Unknown")
    )


async def test_zero_texts_when_nothing_was_processed(app):
    await send(app, OWNER, "/setresponse0bef\nstarting…")
    await send(app, OWNER, "/setresponse0aft\nfailed: <line_6>")
    await send(app, OWNER, "/setresponseaft\nnot used here")
    app.relay.first = answer_with_line6("• PROCESSED: 0 + 10 / 10")
    app.relay.update = answer_with_line6("• PROCESSED: 0 + 10 / 10")
    await send(app, OWNER, "/kd hi", username="bob")
    assert app.bot.sent == [(OWNER, "starting…")]
    assert app.bot.edits[-1][2] == "failed: • PROCESSED: 0 + 10 / 10" + footer("@bob")


async def test_setresponse_is_admin_only_and_can_be_reset(app):
    assert (await send(app, USER, "/setresponseaft\nhi")).replies == []
    assert app.store.get_setting("setresponseaft") is None
    assert "Saved" in (await send(app, OWNER, "/setresponseaft\nhi\n<line_6>")).last
    assert app.store.get_setting("setresponseaft") == "hi\n<line_6>"
    assert "reset" in (await send(app, OWNER, "/setresponseaft")).last
    assert app.store.get_setting("setresponseaft") is None


async def test_sends_new_message_if_edit_fails(app):
    async def broken_edit(*args, **kw):
        raise RuntimeError("can't edit")

    app.bot.edit_message = broken_edit
    app.relay.update = "final"
    await send(app, OWNER, "/kd hi")
    assert [t for _, t in app.bot.sent] == ["answer", "final" + footer("Unknown")]
