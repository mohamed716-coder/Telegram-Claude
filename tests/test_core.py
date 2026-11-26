import pytest

from kdbot.core import (
    Cooldown,
    command_pattern,
    format_duration,
    parse_code_args,
    parse_command,
    to_target_text,
)

PAT = command_pattern(("kd", "zz", "ko"))


def test_passes_message_through_as_is():
    assert to_target_text("/kd hello world", PAT) == "/kd hello world"
    assert to_target_text("/zz Any Text 123 !?", PAT) == "/zz Any Text 123 !?"
    assert to_target_text("/ko line one\nline two", PAT) == "/ko line one\nline two"
    assert to_target_text("/kd   spaced  ", PAT) == "/kd   spaced  "


def test_strips_own_bot_username():
    assert to_target_text("/kd@MyRelayBot hello", PAT) == "/kd hello"


def test_other_messages_ignored():
    assert to_target_text("/kdx hello", PAT) is None
    assert to_target_text("/start", PAT) is None
    assert to_target_text("hello /kd x", PAT) is None


def test_parse_command():
    assert parse_command("/Code 25 0") == ("code", None, "25 0")
    assert parse_command("/redeem@MyBot  ABC-1 ") == ("redeem", "MyBot", "ABC-1")
    assert parse_command("/status") == ("status", None, "")
    assert parse_command("hello") is None


def test_parse_code_args():
    assert parse_code_args("7 20 5000") == (7, 20, 5000, 1)
    assert parse_code_args("7 20 5000 10") == (7, 20, 5000, 10)
    assert parse_code_args("5000") == (0, 0, 5000, 1)  # credits-only
    for bad in ["", "a b c", "25 0", "25 -1 0", "0 0 0", "7 20 5000 0", "7 20 5000 51",
                "1 2 3 4 5", "99999 0 100"]:
        with pytest.raises(ValueError):
            parse_code_args(bad)


def test_format_duration():
    assert format_duration(25 * 86400) == "25 days"
    assert format_duration(86400 + 5 * 3600 + 7) == "1 day, 5 hours"
    assert format_duration(3 * 3600 + 60) == "3 hours, 1 minute"
    assert format_duration(600) == "10 minutes"
    assert format_duration(5) == "less than a minute"


def test_cooldown_per_user_seconds():
    now = [100.0]
    cd = Cooldown(clock=lambda: now[0])
    assert cd.remaining(1, 20) == 0
    cd.mark(1)
    now[0] += 5
    assert cd.remaining(1, 20) == 15
    assert cd.remaining(1, 0) == 0  # anti-spam 0 means no wait
    assert cd.remaining(2, 20) == 0
