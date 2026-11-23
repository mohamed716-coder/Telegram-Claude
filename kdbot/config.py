"""Settings, read from environment variables (or a .env file)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    session_string: str
    target_bot: str
    commands: tuple[str, ...]
    admin_ids: frozenset[int]
    code_prefix: str
    reply_timeout: float
    settle_seconds: float
    data_dir: str


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required setting {name} (see .env.example)")
    return value


def load_config() -> Config:
    load_dotenv()
    admins = os.environ.get("ADMIN_IDS", "")
    return Config(
        api_id=int(_required("API_ID")),
        api_hash=_required("API_HASH"),
        bot_token=_required("BOT_TOKEN"),
        session_string=_required("SESSION_STRING"),
        target_bot=os.environ.get("TARGET_BOT", "").strip() or "@KageNoKoroshiBot",
        commands=tuple(
            c.strip().lstrip("/") for c in os.environ.get("COMMANDS", "kd,zz,ko").split(",") if c.strip()
        ),
        admin_ids=frozenset(int(x) for x in admins.replace(" ", "").split(",") if x),
        code_prefix=os.environ.get("CODE_PREFIX", "ANONYMOUS").strip(),
        reply_timeout=float(os.environ.get("REPLY_TIMEOUT", "30")),
        settle_seconds=float(os.environ.get("SETTLE_SECONDS", "3")),
        data_dir=os.environ.get("DATA_DIR", "data"),
    )
