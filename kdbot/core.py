"""Pure helpers with no Telegram dependency, so they are easy to test."""

import re
import time

COMMAND_RE = re.compile(r"^/kd(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)


def parse_command(text: str) -> str | None:
    """Return the argument of a `/kd xxxx` message ('' if none), or None if not /kd."""
    match = COMMAND_RE.match(text.strip())
    if not match:
        return None
    return (match.group(1) or "").strip()


def render_message(template: str, arg: str) -> str:
    """Insert the user's value into the template wherever `{arg}` appears."""
    return template.replace("{arg}", arg)


def is_valid_arg(arg: str, pattern: re.Pattern) -> bool:
    return bool(arg) and pattern.fullmatch(arg) is not None


class Cooldown:
    """Per-user rate limit: one request every `seconds` seconds."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._last: dict[int, float] = {}

    def remaining(self, user_id: int) -> float:
        last = self._last.get(user_id)
        if last is None:
            return 0.0
        return max(0.0, self.seconds - (self._clock() - last))

    def mark(self, user_id: int) -> None:
        self._last[user_id] = self._clock()
