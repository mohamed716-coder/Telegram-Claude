import re

import pytest

from kdbot.store import DAY, CodeAlreadyUsed, CodeNotFound, Store


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def store(tmp_path, clock):
    return Store(str(tmp_path / "t.db"), clock=lambda: clock[0])


def test_code_format(store):
    code = store.create_code(25, 0, created_by=1, prefix="ANONYMOUS")
    assert re.fullmatch(r"ANONYMOUS-[A-Za-z0-9]{11}", code)
    assert len({store.create_code(1, 0, 1, "X") for _ in range(200)}) == 200


def test_redeem_once(store, clock):
    code = store.create_code(25, 10, 1, "ANONYMOUS")
    r = store.redeem(code, user_id=42, name="Bob")
    assert r.days == 25 and r.cooldown == 10
    assert r.premium_until == clock[0] + 25 * DAY
    assert store.premium(42)["cooldown"] == 10
    with pytest.raises(CodeAlreadyUsed):
        store.redeem(code, user_id=43)
    with pytest.raises(CodeNotFound):
        store.redeem("ANONYMOUS-nope", user_id=43)
    assert store.premium(43) is None


def test_codes_are_case_sensitive(store):
    code = store.create_code(1, 0, 1, "ANONYMOUS")
    with pytest.raises(CodeNotFound):
        store.redeem(code.lower() if code != code.lower() else code.upper(), 42)


def test_redeem_extends_active_premium(store, clock):
    store.redeem(store.create_code(10, 5, 1, "A"), 42)
    clock[0] += 3 * DAY
    r = store.redeem(store.create_code(20, 0, 1, "A"), 42)
    assert r.premium_until == 1_000_000.0 + 30 * DAY  # 7 left + 20 = 27 from now
    assert store.premium(42)["cooldown"] == 0  # newest code's anti-spam applies


def test_expiry(store, clock):
    store.redeem(store.create_code(1, 0, 1, "A"), 42)
    assert not store.had_premium(7)
    clock[0] += DAY + 1
    assert store.premium(42) is None
    assert store.had_premium(42)
    # redeeming after expiry starts from now, not from the old end date
    r = store.redeem(store.create_code(2, 0, 1, "A"), 42)
    assert r.premium_until == clock[0] + 2 * DAY


def test_unused_and_delete(store):
    a = store.create_code(1, 0, 1, "A")
    b = store.create_code(2, 0, 1, "A")
    store.redeem(a, 42)
    assert [r["code"] for r in store.unused_codes()] == [b]
    assert not store.delete_code(a)  # used codes can't be deleted
    assert store.delete_code(b)
    assert store.unused_codes() == []


def test_users_and_remove(store):
    store.redeem(store.create_code(5, 0, 1, "A"), 42, name="Bob")
    store.touch_user(43, "Free guy")
    assert [r["user_id"] for r in store.premium_users()] == [42]
    assert store.premium_users()[0]["name"] == "Bob"
    assert store.remove_premium(42)
    assert not store.remove_premium(42)
    assert store.premium(42) is None


def test_settings(store):
    assert store.get_setting("setresponseaft") is None
    store.set_setting("setresponseaft", "a\n<line_6>")
    store.set_setting("setresponseaft", "new text")
    assert store.get_setting("setresponseaft") == "new text"
    assert store.delete_setting("setresponseaft")
    assert not store.delete_setting("setresponseaft")
    assert store.get_setting("setresponseaft") is None


def test_persists_across_restarts(tmp_path, clock):
    path = str(tmp_path / "p.db")
    s1 = Store(path, clock=lambda: clock[0])
    code = s1.create_code(3, 0, 1, "A")
    s1.redeem(code, 42)
    s2 = Store(path, clock=lambda: clock[0])
    assert s2.premium(42) is not None
