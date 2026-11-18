"""Telegram's "429 Too Many Requests" handling that never freezes the whole bot."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Union

from telegram.error import RetryAfter, TelegramError
from telegram.ext import AIORateLimiter

# Longest flood wait (seconds) the bot sits out before retrying a request.
MAX_FLOOD_WAIT = 30
# Bot-settings methods: cosmetic, and Telegram can make them wait for hours.
NO_WAIT_METHODS = frozenset(
    {"setMyCommands", "deleteMyCommands", "setMyDescription", "setMyShortDescription"}
)


class FloodWait(TelegramError):
    """A flood wait the bot chose not to sit out (see ShopRateLimiter)."""

    def __init__(self, message: str, seconds: float) -> None:
        super().__init__(message)
        self.seconds = seconds


def wait_seconds(exc: Union[RetryAfter, FloodWait]) -> float:
    """How long Telegram asked to wait, in seconds."""
    if isinstance(exc, FloodWait):
        return exc.seconds
    value: Any = getattr(exc, "_retry_after", None)  # timedelta; avoids a deprecation warning
    if isinstance(value, timedelta):
        return value.total_seconds()
    return float(value or 0)


class ShopRateLimiter(AIORateLimiter):
    """AIORateLimiter that refuses to sit out long flood waits.

    While AIORateLimiter waits out a 429 it holds back every other request, so a long
    wait would freeze the whole shop. Waits longer than MAX_FLOOD_WAIT, and any wait
    for the bot-settings methods, fail at once instead, as FloodWait (a TelegramError,
    which the handlers already deal with).
    """

    async def process_request(  # type: ignore[override]
        self, callback, args, kwargs, endpoint, data, rate_limit_args
    ):
        limit = 0 if endpoint in NO_WAIT_METHODS else MAX_FLOOD_WAIT

        async def guarded(*a: Any, **kw: Any) -> Any:
            try:
                return await callback(*a, **kw)
            except RetryAfter as exc:
                seconds = wait_seconds(exc)
                if seconds > limit:
                    raise FloodWait(f"{endpoint}: {exc}", seconds) from exc
                raise

        return await super().process_request(
            guarded, args, kwargs, endpoint, data, rate_limit_args
        )
