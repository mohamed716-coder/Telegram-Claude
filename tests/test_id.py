"""/id and the 🆔 My ID button: everyone sees their own Telegram ID; the owner can look
up someone who has started the bot with /id @username."""

from __future__ import annotations

from harness import OWNER, OWNER_ID, ShopTest, run, tg_user
from test_shop import ALICE, BOB, make_admin, register

ADMIN = tg_user(2001, "Adam", "adam")


def test_everyone_gets_their_own_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        for user in (ALICE, ADMIN, OWNER):
            calls = await t.send(user, "/id")
            assert f"Your Telegram ID: <code>{user['id']}</code>" in t.last_text(calls), user

    run(tmp_path, scenario)


def test_the_owner_looks_up_a_user_by_username(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        for asked in ("/id @alice", "/id @ALICE", "/id alice"):
            calls = await t.send(OWNER, asked)
            text = t.last_text(calls)
            assert f"ID: <code>{ALICE['id']}</code>" in text and "Alice" in text, asked
        calls = await t.send(OWNER, "/id @nobody_here")
        assert "Nobody with the username @nobody_here has used this bot" in t.last_text(calls)
        t.db.set_banned(ALICE["id"], True)
        assert "🚫 Banned" in t.last_text(await t.send(OWNER, "/id @alice"))

    run(tmp_path, scenario)


def test_others_cannot_look_up_ids(tmp_path):
    async def scenario(t: ShopTest) -> None:
        register(t, ALICE)
        make_admin(t, ADMIN)
        for user in (BOB, ADMIN):
            text = t.last_text(await t.send(user, "/id @alice"))
            assert f"<code>{user['id']}</code>" in text and str(ALICE["id"]) not in text
            assert "Only the owner can look up" in text

    run(tmp_path, scenario)


def test_the_my_id_button_is_on_every_menu_and_shows_the_id(tmp_path):
    async def scenario(t: ShopTest) -> None:
        make_admin(t, ADMIN)
        for user in (ALICE, ADMIN, OWNER):
            calls = await t.send(user, "/start")
            assert t.has_button(calls, "🆔 My ID"), user
            calls = await t.press(user, t.button(calls, "🆔 My ID"))
            assert f"<code>{user['id']}</code>" in t.last_text(calls)
        assert "/id @username" in t.last_text(await t.press(OWNER, "myid"))
        assert OWNER_ID

    run(tmp_path, scenario)
