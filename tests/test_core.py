import re

from kdbot.core import Cooldown, is_valid_arg, parse_command, render_message


def test_parse_command():
    assert parse_command("/kd 1234") == "1234"
    assert parse_command("/kd@MyBot  abc ") == "abc"
    assert parse_command("/KD x") == "x"
    assert parse_command("/kd") == ""
    assert parse_command("/kdx 1") is None
    assert parse_command("hello") is None


def test_render_message():
    assert render_message("/kd {arg}", "42") == "/kd 42"
    assert render_message("Check {arg} please {arg}", "a") == "Check a please a"
    assert render_message("braces {} ok {arg}", "1") == "braces {} ok 1"


def test_is_valid_arg():
    pat = re.compile(r"[A-Za-z0-9_.-]{1,64}")
    assert is_valid_arg("abc-12.3", pat)
    assert not is_valid_arg("", pat)
    assert not is_valid_arg("a b", pat)
    assert not is_valid_arg("x" * 65, pat)


def test_cooldown():
    now = [100.0]
    cd = Cooldown(20, clock=lambda: now[0])
    assert cd.remaining(1) == 0
    cd.mark(1)
    now[0] += 5
    assert cd.remaining(1) == 15
    assert cd.remaining(2) == 0
    now[0] += 15
    assert cd.remaining(1) == 0
