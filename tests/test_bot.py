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
    def __init__(self, uid, text, first_name="Bob"):
        self.sender_id = uid
        self.raw_text = text
        self.chat_id = uid
        self.id = 7
        self.replies = []
        self._sender = SimpleNamespace(first_name=first_name, last_name=None, username=None)

    async def get_sender(self):
        return self._sender

    async def reply(self, text, **kw):
        msg = FakeMsg(text)
        self.replies.append(msg)
        return msg

    @property
    def last(self):
        return self.replies[-1].text


class FakeRelay:
    busy = False

    def __init__(self, update=None):
        self.sent = []
        self.update = update  # text of a later edit, if any

    async def ask(self, text, deliver):
        self.sent.append(text)
        await deliver([SimpleNamespace(id=1, message="answer", entities=None, media=None)], False)
        if self.update is not None:
            await deliver([SimpleNamespace(id=1, message=self.update, entities=None, media=None)], True)
        return True


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))


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


async def send(app, uid, text):
    ev = FakeEvent(uid, text)
    await app.handle(ev)
    return ev


async def make_code(app, args="25 0", uid=OWNER):
    ev = await send(app, uid, f"/code {args}")
    return re.findall(r"ANONYMOUS-[A-Za-z0-9]{11}", ev.replies[0].text)


async def test_start_shows_plan_and_commands(app):
    text = (await send(app, USER, "/start")).last
    assert "Plan:</b> Free" in text
    assert "/kd\n/ko\n/zz" in text
    assert "/code" not in text  # admin help hidden from normal users
    assert "/code" in (await send(app, OWNER, "/start")).last


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


async def test_update_only_shows_first_8_lines(app):
    app.relay.update = "\n".join(["a", "b"] * 6)  # 12 lines
    await send(app, OWNER, "/kd hi")
    texts = [t for _, t in app.bot.sent]
    assert texts == ["answer", "a\nb\na\nb\na\nb\na\nb"]


def test_first_lines_clips_formatting():
    from kdbot.bot import first_lines

    text = "line1\nline2\nline3"
    bold_all = SimpleNamespace(offset=0, length=len(text))
    after_cut = SimpleNamespace(offset=12, length=5)
    out, ents = first_lines(text, [bold_all, after_cut], 2)
    assert out == "line1\nline2"
    assert [(e.offset, e.length) for e in ents] == [(0, 11)]
    assert bold_all.length == len(text)  # original untouched
    assert first_lines("x\ny", None, 8) == ("x\ny", None)
