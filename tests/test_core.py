from kdbot.core import Cooldown, command_pattern, to_target_text

PAT = command_pattern(("kd", "zz", "ko"))


def test_passes_message_through_as_is():
    assert to_target_text("/kd hello world", PAT) == "/kd hello world"
    assert to_target_text("/zz Any Text 123 !?", PAT) == "/zz Any Text 123 !?"
    assert to_target_text("/ko line one\nline two", PAT) == "/ko line one\nline two"
    assert to_target_text("/kd   spaced  ", PAT) == "/kd   spaced  "


def test_strips_own_bot_username():
    assert to_target_text("/kd@MyRelayBot hello", PAT) == "/kd hello"


def test_bare_command():
    assert to_target_text("/zz", PAT) == "/zz"


def test_other_messages_ignored():
    assert to_target_text("/kdx hello", PAT) is None
    assert to_target_text("/start", PAT) is None
    assert to_target_text("hello /kd x", PAT) is None
    assert to_target_text("kd hello", PAT) is None


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
