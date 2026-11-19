"""Bot settings, read from environment variables or a local ``.env`` file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

class ConfigError(Exception):
    """A setting in .env or the environment is invalid. The message says which."""


DEFAULT_OWNER_ID = 6883493877
DEFAULT_BINANCE_PAY_ID = "850940840"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Load ``KEY=VALUE`` lines from a .env file. Real environment variables win."""
    env_file = Path(path)
    if not env_file.is_file():
        return
    values: dict[str, str] = {}  # a later line wins, like in a shell
    for raw in env_file.read_text(encoding="utf-8-sig").splitlines():  # -sig: Windows BOM
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)


def _default_db_path() -> str:
    """data/shop.db, or on Railway the attached volume, wherever it is mounted."""
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    return f"{volume.rstrip('/')}/shop.db" if volume else "data/shop.db"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str = ""
    owner_id: int = DEFAULT_OWNER_ID
    binance_pay_id: str = DEFAULT_BINANCE_PAY_ID
    db_path: str = "data/shop.db"
    # Amounts (in $) offered on the "Add Balance" screen, besides "Custom".
    deposit_presets: tuple[int, ...] = (10, 20, 30, 50, 100)
    # Largest whole-dollar amount accepted for a custom deposit.
    max_custom_deposit: int = 100_000
    # Sellers get this discount on every product.
    seller_discount_percent: int = 10
    # Users become sellers automatically once their total deposits reach this ($).
    seller_auto_threshold: int = 1000
    # Shown to customers who need help ("@username" or a link). Empty: the owner's
    # @username. "none": don't show any contact.
    support_contact: str = ""
    # A read-only Binance API key: deposits are then checked automatically by the Order
    # ID the customer sends (shopbot/binance.py). Empty: the owner checks receipts by hand.
    binance_api_key: str = ""
    binance_api_secret: str = ""
    # Coins credited 1:1 in $ when received with Binance Pay (others go to the owner).
    binance_coins: tuple[str, ...] = ("USDT",)
    # Payments older than this (hours) aren't credited automatically.
    binance_max_age_hours: int = 48

    @property
    def binance_auto(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @classmethod
    def from_env(cls) -> "Settings":
        # .env next to bot.py works even when the bot is started from another folder.
        load_dotenv(Path.cwd() / ".env")
        load_dotenv(PROJECT_ROOT / ".env")
        token = (
            os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
        ).strip()
        db_path = Path(os.environ.get("DB_PATH", "").strip() or _default_db_path())
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        discount = _env_int("SELLER_DISCOUNT_PERCENT", 10)
        if not 0 <= discount <= 100:
            raise ConfigError("SELLER_DISCOUNT_PERCENT must be between 0 and 100")
        key = os.environ.get("BINANCE_API_KEY", "").strip()
        secret = os.environ.get("BINANCE_API_SECRET", "").strip()
        for name, value in (("BINANCE_API_KEY", key), ("BINANCE_API_SECRET", secret)):
            if value and (len(value) < 16 or not value.isalnum()):
                raise ConfigError(f"{name} doesn't look like a Binance API key or secret")
        if bool(key) != bool(secret):
            raise ConfigError("Set both BINANCE_API_KEY and BINANCE_API_SECRET, or neither")
        coins = tuple(c.strip().upper() for c in os.environ.get("BINANCE_COINS", "").split(",")
                      if c.strip()) or ("USDT",)
        return cls(
            bot_token=token,
            owner_id=_env_int("OWNER_ID", DEFAULT_OWNER_ID),
            binance_pay_id=os.environ.get("BINANCE_PAY_ID", DEFAULT_BINANCE_PAY_ID).strip()
            or DEFAULT_BINANCE_PAY_ID,
            db_path=str(db_path),
            seller_discount_percent=discount,
            seller_auto_threshold=_env_int("SELLER_AUTO_THRESHOLD", 1000),
            support_contact=os.environ.get("SUPPORT_CONTACT", "").strip(),
            binance_api_key=key,
            binance_api_secret=secret,
            binance_coins=coins,
            binance_max_age_hours=_env_int("BINANCE_MAX_AGE_HOURS", 48),
        )
