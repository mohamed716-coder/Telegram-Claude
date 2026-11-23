"""Settings, read from environment variables (or a .env file)."""

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    session_string: str
    target_bot: str
    message_template: str
    arg_pattern: re.Pattern
    allowed_users: frozenset[int]
    cooldown_seconds: float
    reply_timeout: float
    settle_seconds: float


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required setting {name} (see .env.example)")
    return value


def load_config() -> Config:
    load_dotenv()
    allowed = os.environ.get("ALLOWED_USERS", "")
    return Config(
        api_id=int(_required("API_ID")),
        api_hash=_required("API_HASH"),
        bot_token=_required("BOT_TOKEN"),
        session_string=_required("SESSION_STRING"),
        target_bot=_required("TARGET_BOT"),
        message_template=os.environ.get("MESSAGE_TEMPLATE", "/kd {arg}"),
        arg_pattern=re.compile(os.environ.get("ARG_PATTERN", r"[A-Za-z0-9_.-]{1,64}")),
        allowed_users=frozenset(int(x) for x in allowed.replace(" ", "").split(",") if x),
        cooldown_seconds=float(os.environ.get("COOLDOWN_SECONDS", "20")),
        reply_timeout=float(os.environ.get("REPLY_TIMEOUT", "30")),
        settle_seconds=float(os.environ.get("SETTLE_SECONDS", "3")),
    )
