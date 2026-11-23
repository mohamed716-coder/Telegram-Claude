"""Pure helpers with no Telegram dependency, so they are easy to test."""

import re
import time


def command_pattern(commands: tuple[str, ...]) -> re.Pattern:
    names = "|".join(re.escape(c) for c in commands)
    # "/kd", "/kd text", "/kd@YourBot text" (the @YourBot part is added by Telegram in groups)
    return re.compile(rf"^/({names})(@\w+)?(?=\s|$)", re.IGNORECASE)


def to_target_text(text: str, pattern: re.Pattern) -> str | None:
    """Return the message to send to the target bot, or None if it isn't one of our commands.

    The message is passed through exactly as the user typed it; only a
    "@YourBot" suffix on the command is removed, since the target bot
    would not recognise it.
    """
    match = pattern.match(text)
    if not match:
        return None
    return "/" + match.group(1) + text[match.end():]


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
