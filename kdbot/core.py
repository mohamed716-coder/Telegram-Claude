"""Pure helpers with no Telegram dependency, so they are easy to test."""

import re
import time

COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@(\w+))?(?=\s|$)")

MAX_DAYS = 3650
MAX_COOLDOWN = 86400
MAX_CODES_AT_ONCE = 50
MAX_CREDITS = 1_000_000


def parse_command(text: str) -> tuple[str, str | None, str] | None:
    """Split "/name@Bot rest" into ("name", "Bot", "rest"). None if not a command."""
    match = COMMAND_RE.match(text)
    if not match:
        return None
    return match.group(1).lower(), match.group(2), text[match.end():].strip()


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


def parse_code_args(args: str) -> tuple[int, int, int, int, int]:
    """Parse "/code <days> <antispam> <credits> [count]" or "/code <credits>".

    Returns (days, cooldown, credits, count).  days=0 means credits-only.
    """
    parts = args.split()
    usage = (
        "Usage:\n"
        "/code <days> <antispam_seconds> <credits> [count]\n"
        "/code <credits>  (adds credits to an existing plan)\n"
        "Example: /code 7 20 5000"
    )
    if not parts or not all(p.isdigit() for p in parts):
        raise ValueError(usage)
    if len(parts) == 1:
        credits = int(parts[0])
        if credits < 1 or credits > MAX_CREDITS:
            raise ValueError(f"Credits must be between 1 and {MAX_CREDITS}.")
        return 0, 0, credits, 1
    if len(parts) not in (3, 4):
        raise ValueError(usage)
    days, cooldown, credits = int(parts[0]), int(parts[1]), int(parts[2])
    count = int(parts[3]) if len(parts) == 4 else 1
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"Days must be between 1 and {MAX_DAYS}.")
    if cooldown > MAX_COOLDOWN:
        raise ValueError(f"Anti-spam must be between 0 and {MAX_COOLDOWN} seconds.")
    if credits < 0 or credits > MAX_CREDITS:
        raise ValueError(f"Credits must be between 0 and {MAX_CREDITS}.")
    if not 1 <= count <= MAX_CODES_AT_ONCE:
        raise ValueError(f"You can generate 1 to {MAX_CODES_AT_ONCE} codes at once.")
    return days, cooldown, credits, count


def format_duration(seconds: float) -> str:
    """Human-friendly time left, e.g. "24 days, 5 hours" or "12 minutes"."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    def unit(n, word):
        return f"{n} {word}{'' if n == 1 else 's'}"

    if days:
        return unit(days, "day") + (f", {unit(hours, 'hour')}" if hours else "")
    if hours:
        return unit(hours, "hour") + (f", {unit(minutes, 'minute')}" if minutes else "")
    if minutes:
        return unit(minutes, "minute")
    return "less than a minute"


class Cooldown:
    """Per-user rate limit; each user can have their own wait time."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last: dict[int, float] = {}

    def remaining(self, user_id: int, seconds: float) -> float:
        last = self._last.get(user_id)
        if last is None or seconds <= 0:
            return 0.0
        return max(0.0, seconds - (self._clock() - last))

    def mark(self, user_id: int) -> None:
        self._last[user_id] = self._clock()
