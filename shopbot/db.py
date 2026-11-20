"""SQLite storage for the shop.

Every money value is stored as an integer number of cents.

All methods are synchronous and never await, so a whole method (for example a
purchase) runs without any other Telegram update being processed in between.
Each method that changes several rows does so inside one transaction.
"""

from __future__ import annotations

import json
import logging
import random
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .binance import order_id_time

log = logging.getLogger("shopbot.db")

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I look-alikes
CODE_LENGTH = 8
MAX_LABEL_NAME = 40
# settings key: stock added since the last "New in the shop" message (see broadcast.py)
PENDING_STOCK = "pending_stock_alert"
# settings key: the broadcast going out, with how far it got (resumed after a restart)
BROADCAST_JOB = "broadcast_job"
# Seconds of margin when times this server noted are compared with Binance's: the clocks
# may differ by up to 10 s without Binance complaining (larger differences are measured).
DOUBT_MARGIN = 120
# Seconds a measured correction of this server's clock widens that margin: times written
# before it are off by that much.
CLOCK_JUMP_FOR = 7 * 86_400
# Seconds of margin after automatic checking started: covers the clocks' difference that
# isn't measured (a payment just before the start may have come with a receipt).
AUTO_START_MARGIN = 15
# What the notes of a request say about an Order ID Binance didn't confirm as a new
# payment to the shop (for requests written by versions that didn't store it).
UNCONFIRMED_NOTES = ("not found in your Binance Pay history", "not checked (",
                     "not a payment to the shop", "already used by someone else")
# Seconds back from a request whose Order ID Binance didn't confirm in which balance added
# by hand is listed for the owner (from when its Order ID says it was paid, if earlier).
UNVERIFIED_LOOKBACK = 7 * 86_400

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,          -- Telegram user id
    first_name      TEXT    NOT NULL DEFAULT '',
    last_name       TEXT    NOT NULL DEFAULT '',
    username        TEXT    NOT NULL DEFAULT '',
    balance         INTEGER NOT NULL DEFAULT 0,   -- cents, spendable in the shop
    total_deposits  INTEGER NOT NULL DEFAULT 0,   -- cents, all approved deposits
    earnings        INTEGER NOT NULL DEFAULT 0,   -- cents, sales income not yet withdrawn
    is_admin        INTEGER NOT NULL DEFAULT 0,
    is_seller       INTEGER NOT NULL DEFAULT 0,
    seller_locked   INTEGER NOT NULL DEFAULT 0,   -- owner removed seller: no auto-promotion
    is_banned       INTEGER NOT NULL DEFAULT 0,
    started         INTEGER NOT NULL DEFAULT 0,   -- has talked to the bot
    created_at      TEXT    NOT NULL,
    last_seen       TEXT
);

CREATE TABLE IF NOT EXISTS labels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER REFERENCES labels(id),    -- NULL = top level
    name        TEXT    NOT NULL,
    price       INTEGER,                          -- cents; used by the last label (the product)
    deleted     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    disabled    INTEGER NOT NULL DEFAULT 0        -- 1 = customers don't see it or anything inside
);
CREATE INDEX IF NOT EXISTS idx_labels_parent ON labels(parent_id, deleted);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,
    buyer_id      INTEGER NOT NULL,
    label_id      INTEGER,
    product_name  TEXT    NOT NULL,               -- label path at the time of sale
    quantity      INTEGER NOT NULL,
    unit_price    INTEGER NOT NULL,
    total         INTEGER NOT NULL,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,          -- the public product ID
    label_id    INTEGER NOT NULL REFERENCES labels(id),
    owner_id    INTEGER NOT NULL,                 -- owner/admin who added it
    content     TEXT    NOT NULL,                 -- JSON: the message delivered to the buyer
    status      TEXT    NOT NULL DEFAULT 'available',  -- available | sold | removed | pulled
    created_at  TEXT    NOT NULL,
    order_id    INTEGER REFERENCES orders(id),
    buyer_id    INTEGER,                          -- buyer, or who pulled it
    sold_price  INTEGER,                          -- cents actually paid for this item
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_label ON items(label_id, status);
CREATE INDEX IF NOT EXISTS idx_items_owner ON items(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_items_order ON items(order_id);

CREATE TABLE IF NOT EXISTS deposits (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    amount           INTEGER NOT NULL,
    method           TEXT    NOT NULL,            -- binance | manual
    status           TEXT    NOT NULL,            -- pending | approved | rejected | closed
    receipt_file_id  TEXT,
    receipt_kind     TEXT,                        -- photo | document
    created_at       TEXT    NOT NULL,
    decided_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(user_id, status);

CREATE TABLE IF NOT EXISTS withdrawals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    binance_id  TEXT    NOT NULL,
    status      TEXT    NOT NULL,                 -- pending | approved | rejected
    created_at  TEXT    NOT NULL,
    decided_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id, status);

CREATE TABLE IF NOT EXISTS ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    delta          INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    reason         TEXT    NOT NULL,
    ref            TEXT,
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS binance_payments (     -- Binance Pay transfers already used
    order_id        TEXT    NOT NULL UNIQUE,      -- the Order ID the payer sees
    transaction_id  TEXT    UNIQUE,               -- Binance's other id for the same transfer
    user_id         INTEGER NOT NULL,             -- who claimed it
    amount          INTEGER NOT NULL,             -- cents credited (0: left to the owner)
    raw_amount      TEXT    NOT NULL,             -- as Binance sent it, e.g. 25.00000000
    currency        TEXT    NOT NULL,
    paid_at         TEXT,
    status          TEXT    NOT NULL,             -- credited | owner (left to the owner)
    deposit_id      INTEGER REFERENCES deposits(id),
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# Recursive query: a label and every (non-deleted) label below it.
SUBTREE_CTE = """
WITH RECURSIVE sub(id) AS (
    SELECT ?
    UNION ALL
    SELECT l.id FROM labels l JOIN sub s ON l.parent_id = s.id WHERE l.deleted = 0
)
"""

# The same, but what customers can see: it stops at a disabled label.
VISIBLE_SUBTREE_CTE = """
WITH RECURSIVE sub(id) AS (
    SELECT id FROM labels WHERE id = ? AND deleted = 0 AND disabled = 0
    UNION ALL
    SELECT l.id FROM labels l JOIN sub s ON l.parent_id = s.id
    WHERE l.deleted = 0 AND l.disabled = 0
)
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class ShopError(Exception):
    """An expected problem whose message can be shown to the user as-is."""


class InsufficientBalance(ShopError):
    def __init__(self, needed: int, balance: int, total: int) -> None:
        super().__init__("Not enough balance.")
        self.needed = needed
        self.balance = balance
        self.total = total


class PriceChanged(ShopError):
    """The price is no longer the one the buyer confirmed. Nothing was bought."""

    def __init__(self, unit: int) -> None:
        super().__init__("The price changed. Please check the new total and confirm again.")
        self.unit = unit


class PaymentWaiting(ShopError):
    """This Binance payment is kept for a request still waiting for the owner."""

    def __init__(self, row: sqlite3.Row, request: sqlite3.Row) -> None:
        super().__init__("This payment is waiting with a deposit request.")
        self.row = row
        self.request = request


class PaymentUsed(ShopError):
    """This Binance payment (Order ID) was already used."""

    def __init__(self, row: sqlite3.Row) -> None:
        super().__init__("This payment was already used.")
        self.row = row


class PaymentInDoubt(ShopError):
    """This payment may be one the owner already decided on by hand: ``requests`` may be
    about it (see payment_doubts), or ``early``: it was paid before automatic checking
    started (receipts may have added it). The owner decides."""

    def __init__(self, requests: list[sqlite3.Row], early: bool = False) -> None:
        super().__init__("This payment may already have been decided on by the owner.")
        self.requests = requests
        self.early = early


class BalanceChanged(ShopError):
    """The balance changed after it was shown. Nothing was changed."""

    def __init__(self, balance: int) -> None:
        super().__init__("The balance changed in the meantime.")
        self.balance = balance


@dataclass
class PurchaseResult:
    order: sqlite3.Row
    items: list[sqlite3.Row]
    new_balance: int
    credits: dict[int, int]  # seller id -> cents earned from this order


@dataclass
class DepositDecision:
    deposit: sqlite3.Row
    user: sqlite3.Row
    became_seller: bool
    closed: tuple[int, ...] = ()  # requests closed because this added their payment


@dataclass
class Registered:
    """A Binance payment the owner recorded as added by hand (/regbin, /addbal with an
    Order ID)."""
    payment: sqlite3.Row
    closed: tuple[int, ...] = ()  # this customer's requests waiting with it, now closed


@dataclass
class ManualDeposit:
    user: sqlite3.Row
    became_seller: bool
    registered: Optional[Registered] = None  # with an Order ID


def clean_label_name(raw: str) -> str:
    return " ".join((raw or "").split())


def seller_price(price: int, discount_percent: int) -> int:
    """Price after the seller discount, rounded half-up to the cent."""
    return (price * (100 - discount_percent) + 50) // 100


def _closed_by_itself(request: sqlite3.Row) -> bool:
    """A request the bot closed when its payment was added automatically or with another
    request (its note's last line says so), not one the owner closed by hand."""
    last = (request["note"] or "").splitlines()[-1:]
    return bool(last) and last[0].startswith(("closed: added automatically", "closed: added with request #"))


def _no_zeros(value: str) -> str:
    """An Order ID (digits) without leading zeros; anything else as it is."""
    return (value.lstrip("0") or value) if value.isdigit() else value


class Database:
    def __init__(self, path: str, owner_id: int) -> None:
        self.path = path
        self.owner_id = owner_id
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._tx_depth = 0
        # Who gets a sale among sellers of one product, and which products are pulled;
        # tests may put a seeded random.Random here.
        self.rng: random.Random = random.SystemRandom()
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            mode = self.conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                # Happens on some network drives. The bot still works, but a program that
                # reads the file at the same time (e.g. a DB browser) can make writes fail.
                log.warning("SQLite WAL mode is not available here (journal mode: %s).", mode)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.ensure_user(owner_id)

    def _migrate(self) -> None:
        """Add what newer versions need to a database made by an older version."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(labels)")}
        if "disabled" not in columns:
            self.conn.execute("ALTER TABLE labels ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(deposits)")}
        if "order_id" not in columns:  # the Binance Order ID of the payment, if known
            self.conn.execute("ALTER TABLE deposits ADD COLUMN order_id TEXT")
        if "note" not in columns:  # why it wasn't added automatically, one line per check
            self.conn.execute("ALTER TABLE deposits ADD COLUMN note TEXT")
        with self.tx():  # the columns and the marks below: all of it, or none
            if "unverified" not in columns:  # 1: a request with an Order ID Binance didn't confirm
                self.conn.execute("ALTER TABLE deposits ADD COLUMN unverified INTEGER NOT NULL DEFAULT 0")
            if "unconfirmed" not in columns:  # those Order IDs ("": none; NULL: an older version's)
                self.conn.execute("ALTER TABLE deposits ADD COLUMN unconfirmed TEXT")
            for name, on in (("idx_deposits_doubt", "deposits(unverified, status)"),
                             ("idx_deposits_method", "deposits(method, user_id, created_at)"),
                             ("idx_deposits_decided", "deposits(status, decided_at)"),
                             ("idx_binance_payments_deposit", "binance_payments(deposit_id)")):
                self.conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {on}")
            # Requests written by an older version (also one gone back to after an update)
            # say in their notes which Order IDs Binance didn't confirm; those that are a
            # payment kept for the request were confirmed after all. At every start.
            for row in self.conn.execute(
                    "SELECT id, note, unverified FROM deposits WHERE unconfirmed IS NULL"
                    " AND method = 'binance' AND receipt_file_id IS NULL").fetchall():
                sent = []
                for line in (row["note"] or "").splitlines():
                    value, _, why = line.partition(": ")
                    if any(text in why for text in UNCONFIRMED_NOTES):
                        sent.append(value.strip())
                if not sent and row["unverified"]:
                    continue  # marked for a reason its notes don't give: leave it marked
                held = {value for kept in self.conn.execute(  # as Binance has them (an approval
                    "SELECT order_id, transaction_id FROM binance_payments"  # records one it didn't)
                    " WHERE deposit_id = ? AND currency != ''", (row["id"],)) for value in kept if value}
                unconfirmed = [value for value in dict.fromkeys(sent) if value not in held]
                self.conn.execute("UPDATE deposits SET unverified = ?, unconfirmed = ? WHERE id = ?",
                                  (int(bool(unconfirmed)), " ".join(unconfirmed), row["id"]))
            # The version before this one counted an Order ID recorded by approving its request
            # as confirmed: such an approval was for a payment Binance never confirmed, so it
            # holds back the payments made before it again (see payment_doubts).
            for row in self._all(
                    "SELECT id, note FROM deposits WHERE unconfirmed = '' AND status = 'approved'"
                    " AND method = 'binance' AND receipt_file_id IS NULL AND note IS NOT NULL"):
                sent = [line.partition(": ")[0].strip() for line in row["note"].splitlines()
                        if any(text in line.partition(": ")[2] for text in UNCONFIRMED_NOTES)]
                never = [value for value in dict.fromkeys(sent) if value and self._one(
                    "SELECT 1 FROM binance_payments WHERE (order_id = ? OR transaction_id = ?)"
                    " AND deposit_id = ? AND currency = ''", (value, value, row["id"]))]
                if never:
                    self.conn.execute("UPDATE deposits SET unverified = 1, unconfirmed = ? WHERE id = ?",
                                      (" ".join(never), row["id"]))
            held = {row["name"] for row in self.conn.execute("PRAGMA table_info(binance_payments)")}
            if "held_before" not in held:  # the decided request that held it before this one
                self.conn.execute("ALTER TABLE binance_payments ADD COLUMN held_before INTEGER")
            if "approved_in" not in held:  # an approved request that held it (kept for good)
                self.conn.execute("ALTER TABLE binance_payments ADD COLUMN approved_in INTEGER")
            self.conn.execute(
                "UPDATE binance_payments SET approved_in = deposit_id WHERE status = 'owner' AND"
                " approved_in IS NULL AND deposit_id IN (SELECT id FROM deposits WHERE status = 'approved')")
            # One spelling for each ID: Binance's transaction IDs in capitals, as typed ones
            # are, so a payment is never missed for the case of its letters.
            for row in self._all("SELECT order_id, transaction_id FROM binance_payments WHERE"
                                 " order_id != UPPER(order_id) OR transaction_id != UPPER(transaction_id)"):
                try:
                    self.conn.execute(
                        "UPDATE binance_payments SET order_id = UPPER(order_id),"
                        " transaction_id = UPPER(transaction_id) WHERE order_id = ?", (row["order_id"],))
                except sqlite3.IntegrityError:  # the same payment twice: leave it to be seen
                    log.warning("Binance payment %s is recorded twice", row["order_id"])
            self.conn.execute("UPDATE deposits SET order_id = UPPER(order_id) WHERE order_id != UPPER(order_id)")
            # Older versions kept the leading zeros of a typed Order ID: "0466..." is the payment
            # "466..." (as clean_order_id has it), so it's found whichever way it's typed now.
            for row in self._all("SELECT order_id FROM binance_payments"
                                 " WHERE order_id GLOB '0*' AND order_id NOT GLOB '*[^0-9]*'"):
                try:
                    self.conn.execute("UPDATE binance_payments SET order_id = ? WHERE order_id = ?",
                                      (_no_zeros(row["order_id"]), row["order_id"]))
                except sqlite3.IntegrityError:  # the same payment twice: leave it to be seen
                    log.warning("Binance payment %s is recorded twice", _no_zeros(row["order_id"]))
            for row in self._all("SELECT id, order_id, unconfirmed FROM deposits"
                                 " WHERE order_id GLOB '0*' OR (' ' || unconfirmed) GLOB '* 0*'"):
                self.conn.execute(
                    "UPDATE deposits SET order_id = ?, unconfirmed = ? WHERE id = ?",
                    (_no_zeros(row["order_id"]) if row["order_id"] else row["order_id"],
                     " ".join(dict.fromkeys(_no_zeros(value) for value in row["unconfirmed"].split()))
                     if row["unconfirmed"] else row["unconfirmed"], row["id"]))

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One transaction. A nested call joins the outer one.

        If COMMIT fails (disk full, database locked by another program), the transaction
        is rolled back and the error raised, so later writes don't silently end up in a
        transaction that never commits.
        """
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield self.conn
            finally:
                self._tx_depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield self.conn
        except BaseException:
            self._tx_depth = 0
            self._rollback()
            raise
        self._tx_depth = 0
        try:
            self.conn.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def _new_code(self) -> str:
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            taken = self._scalar(
                "SELECT 1 FROM items WHERE code = ? UNION ALL SELECT 1 FROM orders WHERE code = ?",
                (code, code),
            )
            if not taken:
                return code

    def _log_balance(self, user_id: int, delta: int, reason: str, ref: str = "") -> None:
        balance = self._scalar("SELECT balance FROM users WHERE id = ?", (user_id,))
        self.conn.execute(
            "INSERT INTO ledger (user_id, delta, balance_after, reason, ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, delta, balance or 0, reason, ref, now()),
        )

    # -------------------------------------------------------------------- users
    def touch_user(
        self, user_id: int, first_name: str, last_name: str | None, username: str | None
    ) -> sqlite3.Row:
        ts = now()
        self.conn.execute(
            """
            INSERT INTO users (id, first_name, last_name, username, started, created_at, last_seen)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                username   = excluded.username,
                started    = 1,
                last_seen  = excluded.last_seen
            """,
            (user_id, first_name or "", last_name or "", username or "", ts, ts),
        )
        return self.get_user(user_id)  # type: ignore[return-value]

    def ensure_user(self, user_id: int) -> sqlite3.Row:
        self.conn.execute(
            "INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)", (user_id, now())
        )
        return self.get_user(user_id)  # type: ignore[return-value]

    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def find_user_by_username(self, username: str) -> Optional[sqlite3.Row]:
        """Someone who has used the bot with this @username (most recently seen first:
        a username can pass from one person to another)."""
        name = username.strip().lstrip("@")
        if not name:
            return None
        return self._one(
            "SELECT * FROM users WHERE lower(username) = lower(?)"
            " ORDER BY last_seen IS NULL, last_seen DESC, id DESC LIMIT 1",
            (name,),
        )

    def list_users(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM users ORDER BY total_deposits DESC, created_at, id")

    def is_admin(self, user_id: int) -> bool:
        return bool(self._scalar("SELECT is_admin FROM users WHERE id = ?", (user_id,)))

    def is_banned(self, user_id: int) -> bool:
        if user_id == self.owner_id:
            return False
        return bool(self._scalar("SELECT is_banned FROM users WHERE id = ?", (user_id,)))

    def list_admins(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM users WHERE is_admin = 1 ORDER BY id")

    def set_admin(self, user_id: int, flag: bool) -> sqlite3.Row:
        with self.tx():
            self.ensure_user(user_id)
            self.conn.execute(
                "UPDATE users SET is_admin = ? WHERE id = ?", (1 if flag else 0, user_id)
            )
        return self.get_user(user_id)  # type: ignore[return-value]

    def set_seller(self, user_id: int, flag: bool) -> sqlite3.Row:
        with self.tx():
            self.ensure_user(user_id)
            if flag:
                self.conn.execute(
                    "UPDATE users SET is_seller = 1, seller_locked = 0 WHERE id = ?", (user_id,)
                )
            else:
                self.conn.execute(
                    "UPDATE users SET is_seller = 0, seller_locked = 1 WHERE id = ?", (user_id,)
                )
        return self.get_user(user_id)  # type: ignore[return-value]

    def set_banned(self, user_id: int, flag: bool) -> sqlite3.Row:
        if flag and user_id == self.owner_id:
            raise ShopError("The owner can't be banned.")
        with self.tx():
            self.ensure_user(user_id)
            self.conn.execute(
                "UPDATE users SET is_banned = ? WHERE id = ?", (1 if flag else 0, user_id)
            )
        return self.get_user(user_id)  # type: ignore[return-value]

    def _maybe_auto_seller(self, user_id: int, threshold_cents: int) -> bool:
        user = self.get_user(user_id)
        if (
            user is not None
            and not user["is_seller"]
            and not user["seller_locked"]
            and user["total_deposits"] >= threshold_cents
        ):
            self.conn.execute("UPDATE users SET is_seller = 1 WHERE id = ?", (user_id,))
            return True
        return False

    # ----------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        value = self._scalar("SELECT value FROM settings WHERE key = ?", (key,))
        return default if value is None else value

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def shop_is_open(self) -> bool:
        return self.get_setting("shop_open", "1") == "1"

    def set_shop_open(self, is_open: bool) -> None:
        self.set_setting("shop_open", "1" if is_open else "0")

    # ------------------------------------------------------------------- labels
    def get_label(self, label_id: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM labels WHERE id = ? AND deleted = 0", (label_id,))

    def children(self, parent_id: Optional[int]) -> list[sqlite3.Row]:
        if parent_id is None:
            return self._all(
                "SELECT * FROM labels WHERE parent_id IS NULL AND deleted = 0 ORDER BY id"
            )
        return self._all(
            "SELECT * FROM labels WHERE parent_id = ? AND deleted = 0 ORDER BY id", (parent_id,)
        )

    def has_children(self, label_id: int) -> bool:
        return bool(
            self._scalar(
                "SELECT 1 FROM labels WHERE parent_id = ? AND deleted = 0 LIMIT 1", (label_id,)
            )
        )

    def label_path(self, label_id: int) -> list[sqlite3.Row]:
        """Labels from the top level down to ``label_id`` (deleted ones included)."""
        path: list[sqlite3.Row] = []
        seen: set[int] = set()
        current: Optional[int] = label_id
        while current is not None and current not in seen:
            seen.add(current)
            row = self._one("SELECT * FROM labels WHERE id = ?", (current,))
            if row is None:
                break
            path.append(row)
            current = row["parent_id"]
        path.reverse()
        return path

    def path_text(self, label_id: Optional[int], sep: str = " › ") -> str:
        if not label_id:
            return ""
        return sep.join(row["name"] for row in self.label_path(label_id)) or "(deleted)"

    def subtree_ids(self, label_id: int) -> list[int]:
        return [r[0] for r in self._all(SUBTREE_CTE + "SELECT id FROM sub", (label_id,))]

    def has_priced_product(self, label_id: int) -> bool:
        """True if ``label_id`` or a label below it is a product with a price that
        customers can see (not inside a disabled label)."""
        return bool(
            self._scalar(
                VISIBLE_SUBTREE_CTE
                + """
                SELECT 1 FROM labels x JOIN sub ON x.id = sub.id
                WHERE x.price IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM labels c WHERE c.parent_id = x.id AND c.deleted = 0)
                LIMIT 1
                """,
                (label_id,),
            )
        )

    def disabled_on_path(self, label_id: int) -> list[sqlite3.Row]:
        """The disabled labels from the top level down to ``label_id`` (itself included).
        Empty if customers can see it."""
        return [row for row in self.label_path(label_id) if row["disabled"]]

    def set_label_disabled(self, label_id: int, disabled: bool) -> sqlite3.Row:
        """Disable a label (customers no longer see it or anything inside it) or
        enable it again. Labels inside keep their own setting."""
        with self.tx():
            if self.get_label(label_id) is None:
                raise ShopError("That label no longer exists.")
            self.conn.execute(
                "UPDATE labels SET disabled = ? WHERE id = ?", (1 if disabled else 0, label_id)
            )
        return self.get_label(label_id)  # type: ignore[return-value]

    def disabled_inside(self, label_id: int) -> list[sqlite3.Row]:
        """Labels below ``label_id`` (not itself) that are disabled on their own."""
        return self._all(
            SUBTREE_CTE
            + "SELECT * FROM labels WHERE id IN (SELECT id FROM sub)"
            " AND id != ? AND disabled = 1 ORDER BY id",
            (label_id, label_id),
        )

    def add_labels(
        self, parent_id: Optional[int], names: Sequence[str]
    ) -> tuple[list[sqlite3.Row], list[str]]:
        """Create labels under ``parent_id``. Returns (created, skipped duplicates)."""
        cleaned: list[str] = []
        for raw in names:
            name = clean_label_name(raw)
            if not name:
                continue
            if len(name) > MAX_LABEL_NAME:
                raise ShopError(
                    f"“{name[:25]}…” is too long. Use at most {MAX_LABEL_NAME} characters."
                )
            cleaned.append(name)
        if not cleaned:
            raise ShopError("Send at least one label name.")
        created_ids: list[int] = []
        skipped: list[str] = []
        with self.tx():
            if parent_id is not None:
                parent = self.get_label(parent_id)
                if parent is None:
                    raise ShopError("That label no longer exists.")
                stock = self.count_items(parent_id)
                if stock:
                    raise ShopError(
                        f"“{parent['name']}” already has {stock} product(s) in stock, so it "
                        "can't contain other labels. Remove or pull them first."
                    )
            taken = {row["name"].casefold() for row in self.children(parent_id)}
            ts = now()
            for name in cleaned:
                key = name.casefold()
                if key in taken:
                    skipped.append(name)
                    continue
                taken.add(key)
                cur = self.conn.execute(
                    "INSERT INTO labels (parent_id, name, created_at) VALUES (?, ?, ?)",
                    (parent_id, name, ts),
                )
                created_ids.append(int(cur.lastrowid))
        return [self.get_label(i) for i in created_ids], skipped  # type: ignore[misc]

    def rename_label(self, label_id: int, new_name: str) -> sqlite3.Row:
        name = clean_label_name(new_name)
        if not name:
            raise ShopError("Send a name for the label.")
        if len(name) > MAX_LABEL_NAME:
            raise ShopError(f"That name is too long. Use at most {MAX_LABEL_NAME} characters.")
        with self.tx():
            label = self.get_label(label_id)
            if label is None:
                raise ShopError("That label no longer exists.")
            for sibling in self.children(label["parent_id"]):
                if sibling["id"] != label_id and sibling["name"].casefold() == name.casefold():
                    raise ShopError(f"A label named “{name}” already exists there.")
            self.conn.execute("UPDATE labels SET name = ? WHERE id = ?", (name, label_id))
        return self.get_label(label_id)  # type: ignore[return-value]

    def set_label_price(self, label_id: int, price: int) -> sqlite3.Row:
        if price <= 0:
            raise ShopError("The price must be more than 0.")
        with self.tx():
            if self.get_label(label_id) is None:
                raise ShopError("That label no longer exists.")
            self.conn.execute("UPDATE labels SET price = ? WHERE id = ?", (price, label_id))
        return self.get_label(label_id)  # type: ignore[return-value]

    def delete_label(self, label_id: int) -> int:
        """Delete a label and everything below it. Refuses while stock is left."""
        with self.tx():
            if self.get_label(label_id) is None:
                raise ShopError("That label no longer exists.")
            stock = self.count_subtree_items(label_id)
            if stock:
                raise ShopError(
                    f"There are still {stock} product(s) in stock inside this label. "
                    "Remove or pull them from My Products first."
                )
            ids = self.subtree_ids(label_id)
            self.conn.execute(
                SUBTREE_CTE + "UPDATE labels SET deleted = 1 WHERE id IN (SELECT id FROM sub)",
                (label_id,),
            )
        return len(ids)

    # -------------------------------------------------------------------- items
    def count_items(
        self, label_id: int, owner_id: Optional[int] = None, status: str = "available"
    ) -> int:
        if owner_id is None:
            return int(
                self._scalar(
                    "SELECT COUNT(*) FROM items WHERE label_id = ? AND status = ?",
                    (label_id, status),
                )
            )
        return int(
            self._scalar(
                "SELECT COUNT(*) FROM items WHERE label_id = ? AND status = ? AND owner_id = ?",
                (label_id, status, owner_id),
            )
        )

    def count_subtree_items(self, label_id: int, owner_id: Optional[int] = None) -> int:
        sql = (
            SUBTREE_CTE
            + "SELECT COUNT(*) FROM items WHERE status = 'available'"
            " AND label_id IN (SELECT id FROM sub)"
        )
        params: list[Any] = [label_id]
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params.append(owner_id)
        return int(self._scalar(sql, params))

    def count_all_items(self, owner_id: Optional[int] = None) -> int:
        """Available items under non-deleted labels."""
        sql = (
            "SELECT COUNT(*) FROM items i JOIN labels l ON l.id = i.label_id"
            " WHERE i.status = 'available' AND l.deleted = 0"
        )
        if owner_id is None:
            return int(self._scalar(sql))
        return int(self._scalar(sql + " AND i.owner_id = ?", (owner_id,)))

    def add_items(self, label_id: int, owner_id: int, contents: Sequence[dict]) -> list[str]:
        if not contents:
            raise ShopError("There are no products to save.")
        codes: list[str] = []
        with self.tx():
            label = self.get_label(label_id)
            if label is None:
                raise ShopError("This product no longer exists, so nothing was saved.")
            if self.has_children(label_id):
                raise ShopError(
                    "This label now contains other labels, so products can't be added to it."
                )
            ts = now()
            for content in contents:
                code = self._new_code()
                self.conn.execute(
                    "INSERT INTO items (code, label_id, owner_id, content, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (code, label_id, owner_id, json.dumps(content, ensure_ascii=False), ts),
                )
                codes.append(code)
        return codes

    def get_item(self, item_id: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM items WHERE id = ?", (item_id,))

    def get_item_by_code(self, code: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM items WHERE code = ?", (code,))

    def list_items(
        self, label_id: int, owner_id: Optional[int] = None, offset: int = 0, limit: int = 30
    ) -> list[sqlite3.Row]:
        if owner_id is None:
            return self._all(
                "SELECT * FROM items WHERE label_id = ? AND status = 'available'"
                " ORDER BY id LIMIT ? OFFSET ?",
                (label_id, limit, offset),
            )
        return self._all(
            "SELECT * FROM items WHERE label_id = ? AND status = 'available' AND owner_id = ?"
            " ORDER BY id LIMIT ? OFFSET ?",
            (label_id, owner_id, limit, offset),
        )

    def close_item(self, item_id: int, status: str, by_user: int) -> Optional[sqlite3.Row]:
        """Mark an available item as ``removed`` or ``pulled``. None if not available."""
        if status not in ("removed", "pulled"):
            raise ValueError(status)
        with self.tx():
            row = self._one(
                "SELECT * FROM items WHERE id = ? AND status = 'available'", (item_id,)
            )
            if row is None:
                return None
            self.conn.execute(
                "UPDATE items SET status = ?, closed_at = ?, buyer_id = ? WHERE id = ?",
                (status, now(), by_user if status == "pulled" else None, item_id),
            )
        return row

    # ---------------------------------------------------------------- purchases
    def purchase(
        self,
        buyer_id: int,
        label_id: int,
        quantity: int,
        seller_discount_percent: int,
        expected_unit: Optional[int] = None,
    ) -> PurchaseResult:
        """Buy ``quantity`` items of a product, picked fairly between sellers (_pick_fairly).

        ``expected_unit`` is the price each that the buyer was shown and confirmed; if the
        price is different now (the owner changed it, or the seller discount ended),
        PriceChanged is raised and nothing is bought.
        """
        if quantity < 1:
            raise ShopError("The quantity must be at least 1.")
        with self.tx():
            label = self.get_label(label_id)
            if label is None:
                raise ShopError("This product no longer exists.")
            if self.has_children(label_id):
                raise ShopError("This is a category, not a product.")
            if label["price"] is None:
                raise ShopError("This product isn't for sale yet.")
            if self.disabled_on_path(label_id):
                raise ShopError("This product isn't available right now.")
            buyer = self.ensure_user(buyer_id)
            unit = label["price"]
            if buyer["is_seller"]:
                unit = seller_price(unit, seller_discount_percent)
            if expected_unit is not None and unit != expected_unit:
                raise PriceChanged(unit)
            total = unit * quantity
            rows = self._pick_fairly(label_id, quantity)
            if len(rows) < quantity:
                if not rows:
                    raise ShopError("Sorry, this product is sold out.")
                raise ShopError(f"Sorry, only {len(rows)} left in stock.")
            if buyer["balance"] < total:
                raise InsufficientBalance(total - buyer["balance"], buyer["balance"], total)
            code = self._new_code()
            ts = now()
            cur = self.conn.execute(
                "INSERT INTO orders (code, buyer_id, label_id, product_name, quantity,"
                " unit_price, total, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (code, buyer_id, label_id, self.path_text(label_id), quantity, unit, total, ts),
            )
            order_id = int(cur.lastrowid)
            ids = [r["id"] for r in rows]
            self.conn.executemany(
                "UPDATE items SET status = 'sold', order_id = ?, buyer_id = ?, sold_price = ?,"
                " closed_at = ? WHERE id = ? AND status = 'available'",
                [(order_id, buyer_id, unit, ts, item_id) for item_id in ids],
            )
            self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE id = ?", (total, buyer_id)
            )
            self._log_balance(buyer_id, -total, "purchase", code)
            credits: dict[int, int] = {}
            for row in rows:
                credits[row["owner_id"]] = credits.get(row["owner_id"], 0) + unit
            for seller_id, amount in credits.items():
                self.ensure_user(seller_id)
                self.conn.execute(
                    "UPDATE users SET earnings = earnings + ? WHERE id = ?", (amount, seller_id)
                )
            order = self._one("SELECT * FROM orders WHERE id = ?", (order_id,))
            items = self._all("SELECT * FROM items WHERE order_id = ? ORDER BY id", (order_id,))
            new_balance = int(
                self._scalar("SELECT balance FROM users WHERE id = ?", (buyer_id,))
            )
        return PurchaseResult(order, items, new_balance, credits)  # type: ignore[arg-type]

    def _pick_fairly(self, label_id: int, quantity: int) -> list[sqlite3.Row]:
        """``quantity`` available items of a product (fewer if there are fewer), unit by unit:
        each unit comes from a seller chosen at random among those who still have one, all
        with the same chance however many they have (100, 5 or 1: a third each); from that
        seller, the oldest. A seller whose stock runs out drops out, and the others share
        the chance."""
        left = {row["owner_id"]: row["n"] for row in self._all(
            "SELECT owner_id, COUNT(*) AS n FROM items WHERE label_id = ? AND status = 'available'"
            " GROUP BY owner_id", (label_id,))}
        taken: dict[int, int] = {}
        for _ in range(min(quantity, sum(left.values()))):
            seller = self.rng.choice(sorted(owner for owner, n in left.items() if n > 0))
            left[seller] -= 1
            taken[seller] = taken.get(seller, 0) + 1
        rows: list[sqlite3.Row] = []
        for seller, count in taken.items():
            rows += self._all(
                "SELECT id, owner_id FROM items WHERE label_id = ? AND status = 'available'"
                " AND owner_id = ? ORDER BY id LIMIT ?", (label_id, seller, count))
        return sorted(rows, key=lambda row: row["id"])

    # -------------------------------------------------------------------- pulls
    def seller_products(self, owner_id: int) -> list[sqlite3.Row]:
        """The products (labels) where this seller has stock: label id and count."""
        return self._all(
            "SELECT i.label_id AS label_id, COUNT(*) AS n FROM items i JOIN labels l ON l.id = i.label_id"
            " WHERE i.owner_id = ? AND i.status = 'available' AND l.deleted = 0"
            " GROUP BY i.label_id ORDER BY i.label_id", (owner_id,))

    def pull_random(self, label_id: int, owner_id: int, count: int, by_user: int) -> list[sqlite3.Row]:
        """Take up to ``count`` of this seller's available items of a product, chosen at
        random, out of the shop for ``by_user`` (status 'pulled'). The items taken."""
        if count < 1:
            raise ShopError("The number must be at least 1.")
        with self.tx():
            ids = [row["id"] for row in self._all(
                "SELECT id FROM items WHERE label_id = ? AND owner_id = ? AND status = 'available'"
                " ORDER BY id", (label_id, owner_id))]
            chosen = sorted(self.rng.sample(ids, min(count, len(ids))))
            if not chosen:
                return []
            ts = now()
            self.conn.executemany(
                "UPDATE items SET status = 'pulled', closed_at = ?, buyer_id = ? WHERE id = ?"
                " AND status = 'available'", [(ts, by_user, item_id) for item_id in chosen])
            return self._all(
                f"SELECT * FROM items WHERE id IN ({', '.join('?' for _ in chosen)}) ORDER BY id", chosen)

    def restore_pulled(self, item_ids: Sequence[int]) -> list[int]:
        """Put pulled items that couldn't be sent back in stock, if their product is still
        one (not deleted, no labels inside). The ids put back."""
        if not item_ids:
            return []
        with self.tx():
            back = [row["id"] for row in self._all(
                "SELECT i.id FROM items i JOIN labels l ON l.id = i.label_id"
                f" WHERE i.status = 'pulled' AND i.id IN ({', '.join('?' for _ in item_ids)})"
                " AND l.deleted = 0 AND NOT EXISTS (SELECT 1 FROM labels c WHERE c.parent_id = l.id"
                " AND c.deleted = 0) ORDER BY i.id", list(item_ids))]
            if back:
                self.conn.execute(
                    "UPDATE items SET status = 'available', closed_at = NULL, buyer_id = NULL"
                    f" WHERE id IN ({', '.join('?' for _ in back)})", back)
            return back

    def get_order(self, order_id: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM orders WHERE id = ?", (order_id,))

    def order_items(self, order_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM items WHERE order_id = ? ORDER BY id", (order_id,))

    def user_orders(self, user_id: int, offset: int = 0, limit: int = 10) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM orders WHERE buyer_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )

    def count_user_orders(self, user_id: int) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM orders WHERE buyer_id = ?", (user_id,)))

    # ----------------------------------------------------------------- deposits
    def create_deposit(
        self, user_id: int, amount: int, receipt_file_id: str, receipt_kind: str
    ) -> sqlite3.Row:
        if amount <= 0:
            raise ShopError("The amount must be more than 0.")
        with self.tx():
            self.ensure_user(user_id)
            cur = self.conn.execute(
                "INSERT INTO deposits (user_id, amount, method, status, receipt_file_id,"
                " receipt_kind, created_at) VALUES (?, ?, 'binance', 'pending', ?, ?, ?)",
                (user_id, amount, receipt_file_id, receipt_kind, now()),
            )
        return self._one("SELECT * FROM deposits WHERE id = ?", (cur.lastrowid,))  # type: ignore[return-value]

    def get_deposit(self, deposit_id: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM deposits WHERE id = ?", (deposit_id,))

    def decide_deposit(
        self, deposit_id: int, approve: bool, seller_threshold: int
    ) -> DepositDecision:
        with self.tx():
            dep = self.get_deposit(deposit_id)
            if dep is None:
                raise ShopError("This request doesn't exist.")
            if dep["status"] != "pending":
                raise ShopError(f"This request was already {dep['status']}.")
            became_seller = False
            order_id = dep["order_id"] if "order_id" in dep.keys() else None
            if approve and dep["method"] == "binance" and not dep["receipt_file_id"]:
                block = self.approval_block(deposit_id)
                if block:  # it would add a payment that was added already (or may be)
                    raise ShopError(block)
                taken = self.payment_records(order_id)
                if not taken:  # named by the customer, unknown to the bot: used from now on
                    self.conn.execute(
                        "INSERT INTO binance_payments (order_id, user_id, amount, raw_amount,"
                        " currency, status, deposit_id, created_at)"
                        " VALUES (?, ?, ?, '', '', 'credited', ?, ?)",
                        (order_id, dep["user_id"], dep["amount"], dep["id"], now()),
                    )
                for row in taken:  # kept for it, or for a request decided without adding it
                    self.conn.execute(
                        "UPDATE binance_payments SET status = 'credited', amount = ?, user_id = ?,"
                        " held_before = CASE WHEN deposit_id IS NULL OR deposit_id = ? THEN held_before"
                        " ELSE deposit_id END, deposit_id = ? WHERE order_id = ?",
                        (dep["amount"], dep["user_id"], deposit_id, deposit_id, row["order_id"]),
                    )
                # Every payment it shows is added with it: none can be added again later.
                self.conn.execute("UPDATE binance_payments SET status = 'credited', approved_in = ?"
                                  " WHERE deposit_id = ? AND status = 'owner'", (deposit_id, deposit_id))
            if approve:
                self.ensure_user(dep["user_id"])
                self.conn.execute(
                    "UPDATE users SET balance = balance + ?, total_deposits = total_deposits + ?"
                    " WHERE id = ?",
                    (dep["amount"], dep["amount"], dep["user_id"]),
                )
                self._log_balance(dep["user_id"], dep["amount"], "deposit", f"deposit#{dep['id']}")
                became_seller = self._maybe_auto_seller(dep["user_id"], seller_threshold)
            self.conn.execute(
                "UPDATE deposits SET status = ?, decided_at = ? WHERE id = ?",
                ("approved" if approve else "rejected", now(), deposit_id),
            )
            closed: tuple[int, ...] = ()
            note = f"closed: added with request #{deposit_id}"
            credited = self.binance_payment(order_id) if approve and order_id else None
            if credited is not None:  # like an automatic credit, it settles waiting requests
                closed = self._close_settled(dep["user_id"], (credited["order_id"], credited["transaction_id"]), note)
            if approve:  # and so does every other payment it showed, added with it
                for row in self.deposit_payments(deposit_id):
                    if row["status"] == "credited" and (credited is None or row["order_id"] != credited["order_id"]):
                        closed += self._close_settled(dep["user_id"], (row["order_id"], row["transaction_id"]), note)
            dep = self.get_deposit(deposit_id)
            user = self.get_user(dep["user_id"]) or self.ensure_user(dep["user_id"])  # type: ignore[index]
        return DepositDecision(dep, user, became_seller, closed)  # type: ignore[arg-type]

    def _close_settled(self, user_id: int, ids: Sequence[Optional[str]], note: str) -> tuple[int, ...]:
        """Close this customer's requests waiting with exactly this payment, which was just
        added: they can't be approved any more (Binance listed it late, or couldn't be
        asked). One that still holds another payment stays, naming that one. Ids closed."""
        ids = [value for value in ids if value]
        if not ids:
            return ()
        closed = []
        for request in self._all(
                "SELECT id FROM deposits WHERE status = 'pending' AND user_id = ? AND order_id IN"
                f" ({', '.join('?' for _ in ids)})", (user_id, *ids)):
            others = [row for row in self.deposit_payments(request["id"]) if row["status"] == "owner"]
            if others:  # it still holds another payment for the owner: it stays, naming that
                self.conn.execute(
                    "UPDATE deposits SET order_id = ?, note = COALESCE(note || char(10), '') || ?"
                    " WHERE id = ?", (others[-1]["order_id"], note.replace("closed: ", ""), request["id"]))
                continue
            self.conn.execute(
                "UPDATE deposits SET status = 'closed', decided_at = ?,"
                " note = COALESCE(note || char(10), '') || ? WHERE id = ?", (now(), note, request["id"]))
            closed.append(request["id"])
        return tuple(closed)

    def manual_deposit(
        self, user_id: int, amount: int, seller_threshold: int, order_id: Optional[str] = None,
        payment: Any = None,
    ) -> ManualDeposit:
        """Owner-added balance (/addbal). Counts as a deposit. With ``order_id``, that
        Binance payment is registered as added with it (see register_payment; ``payment``:
        what Binance has for it): if it can't be (PaymentUsed, PaymentWaiting), nothing is
        added."""
        if amount <= 0:
            raise ShopError("The amount must be more than 0.")
        registered = None
        with self.tx():  # _register refusing rolls the deposit back too
            self.ensure_user(user_id)
            cur = self.conn.execute(
                "INSERT INTO deposits (user_id, amount, method, status, created_at, decided_at, order_id)"
                " VALUES (?, ?, 'manual', 'approved', ?, ?, ?)",
                (user_id, amount, now(), now(), order_id),
            )
            if order_id:
                registered = self._register(order_id, user_id, amount, int(cur.lastrowid), payment)
            self.conn.execute(
                "UPDATE users SET balance = balance + ?, total_deposits = total_deposits + ?"
                " WHERE id = ?",
                (amount, amount, user_id),
            )
            self._log_balance(user_id, amount, "manual deposit", f"deposit#{cur.lastrowid}")
            became_seller = self._maybe_auto_seller(user_id, seller_threshold)
            user = self.get_user(user_id)
        return ManualDeposit(user, became_seller, registered)  # type: ignore[arg-type]

    def set_balance(
        self, user_id: int, new_balance: int, expected_old: Optional[int] = None
    ) -> tuple[int, int]:
        """Owner correction (/modifybal). Returns (old, new). Not counted as a deposit.

        With ``expected_old`` (the balance the owner was shown), BalanceChanged is raised
        instead if the balance changed since, e.g. because a deposit was approved.
        """
        if new_balance < 0:
            raise ShopError("The balance can't be negative.")
        with self.tx():
            old = int(self.ensure_user(user_id)["balance"])
            if expected_old is not None and old != expected_old:
                raise BalanceChanged(old)
            self.conn.execute(
                "UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id)
            )
            self._log_balance(user_id, new_balance - old, "balance set by owner")
        return old, new_balance

    # --------------------------------------------------------- binance payments
    @staticmethod
    def used(row: sqlite3.Row) -> bool:
        """A recorded payment that was added: credited, or kept for a request the owner
        approved (it was part of that approval)."""
        return row["status"] == "credited" or row["approved_in"] is not None

    def payment_problem(self, ids: Sequence[str], user_id: int) -> Optional[ShopError]:
        """Why this payment (known by ``ids``) can't be registered as added by hand for
        ``user_id``: PaymentUsed or PaymentWaiting (see _check_registrable); None if it can."""
        try:
            self._check_registrable(ids, user_id)
        except (PaymentUsed, PaymentWaiting) as exc:
            return exc
        return None

    def hand_doubts(self, payment: Any, user_id: int) -> tuple[list[sqlite3.Row], bool]:
        """Why this payment may have been added already, as the automatic check would hold it
        back (for balance added by hand for it to ``user_id``): payment_doubts, leaving out
        the waiting request that holds it (adding it closes that), the decided requests that
        held it (also the one it's still kept for), and whether it was paid before automatic
        checking started."""
        ids = [value for value in (payment.order_id, payment.transaction_id) if value]
        records = self.payment_records(*ids)
        waiting = {row["deposit_id"] for row in records if row["status"] == "owner" and row["deposit_id"]
                   and (self.get_deposit(row["deposit_id"]) or {"status": ""})["status"] == "pending"}
        doubts = self.payment_doubts(ids, self._paid_at(payment), user_id,
                                     exclude=next(iter(waiting)) if len(waiting) == 1 else None)
        seen = {row["id"] for row in doubts}
        for earlier in self.held_by(records):  # decided while holding it
            if earlier not in waiting and earlier not in seen:
                before = self.get_deposit(earlier)
                if before is not None:
                    doubts.append(before)
                    seen.add(earlier)
        return self.most_telling(doubts, user_id), self.paid_before_auto(payment.time_ms)

    @staticmethod
    def held_by(records: Sequence[sqlite3.Row]) -> list[int]:
        """The requests these records of a payment were kept for: the one it's kept for now,
        the one approved with it, and the one decided without adding it before."""
        found = [row["deposit_id"] if row["status"] == "owner" else None for row in records]
        found += [value for row in records for value in (row["approved_in"], row["held_before"])]
        return list(dict.fromkeys(value for value in found if value))

    def _check_registrable(self, ids: Sequence[str], user_id: int) -> list[sqlite3.Row]:
        """The records of this payment (by any of ``ids``) that can be marked as added by
        hand: kept for a request decided without adding it, or for this customer's waiting
        request that holds only it (it's closed with this). PaymentUsed if it was added
        (also as part of an approved request), PaymentWaiting if a request still waits
        with it otherwise."""
        rows = self.payment_records(*ids)
        for row in rows:
            if self.used(row):
                raise PaymentUsed(row)
        for row in rows:
            request = self.get_deposit(row["deposit_id"]) if row["deposit_id"] else None
            if request is not None and request["status"] == "pending" and not (
                    request["user_id"] == user_id
                    and [p["order_id"] for p in self.deposit_payments(request["id"]) if p["status"] == "owner"]
                    == [row["order_id"]]):
                raise PaymentWaiting(row, request)
        return rows

    def _register(self, order_id: str, user_id: int, amount: int, deposit_id: Optional[int],
                  payment: Any = None) -> Registered:
        """Record this payment as added by hand. ``payment``: what Binance has for it (both
        its IDs); without it only an Order ID (digits) is taken, so that one payment can't
        be recorded twice under its two IDs."""
        if payment is None and not order_id.isdigit():
            raise ShopError("That's Binance's transaction ID, which can't be checked right now: "
                            "send the payment's Order ID (the digits in its details) instead.")
        ids = [order_id, *((payment.order_id, payment.transaction_id) if payment is not None else ())]
        rows = self._check_registrable([value for value in ids if value], user_id)
        closed: list[int] = []
        unlinked = self.unlinked_payments(order_id) if payment is None and not rows else []
        if unlinked:
            raise ShopError(
                f"Binance can't be asked about Order ID {order_id}, and an older version recorded "
                "payments by their transaction ID only ("
                + ", ".join(f"{row['order_id']} on {row['created_at']} UTC" for row in unlinked[:3])
                + (f" and {len(unlinked) - 3} more" if len(unlinked) > 3 else "")
                + "): it may be one of them. Check in your Binance app: if it isn't, add the balance "
                  "without the Order ID; if it is, it was added already.")
        if not rows:
            if payment is not None:
                self._record_payment(user_id, payment, amount, "credited", deposit_id)
            else:
                self.conn.execute(
                    "INSERT INTO binance_payments (order_id, user_id, amount, raw_amount, currency,"
                    " status, deposit_id, created_at) VALUES (?, ?, ?, '', '', 'credited', ?, ?)",
                    (order_id, user_id, amount, deposit_id, now()))
        for row in rows:  # kept for a request: the owner settled it by hand
            request = self.get_deposit(row["deposit_id"]) if row["deposit_id"] else None
            self.conn.execute(
                "UPDATE binance_payments SET status = 'credited', user_id = ?, amount = ?,"
                " held_before = deposit_id, deposit_id = ? WHERE order_id = ?",
                (user_id, amount, deposit_id, row["order_id"]))
            if request is not None and request["status"] == "pending":  # this customer's, holding only it
                self.conn.execute(
                    "UPDATE deposits SET status = 'closed', decided_at = ?,"
                    " note = COALESCE(note || char(10), '') || ? WHERE id = ?",
                    (now(), "closed: added by hand", request["id"]))
                closed.append(request["id"])
            if payment is not None:  # what Binance has: its other ID too, so either finds it
                linked = self.link_payment(row, payment)
                self.conn.execute(
                    "UPDATE binance_payments SET raw_amount = ?, currency = ?, paid_at = COALESCE(paid_at, ?)"
                    " WHERE order_id = ? AND currency = ''",
                    (format(payment.amount.normalize(), "f"), payment.currency, self._paid_at(payment),
                     linked["order_id"]))
        row = self.binance_payment(*[value for value in ids if value])
        assert row is not None
        ids = {order_id, row["order_id"], row["transaction_id"]} - {None}
        closed += self._close_settled(user_id, tuple(ids), "closed: added by hand")
        # This customer's requests that name no payment and hold none, whose Order IDs are
        # all added for them now (this one among them): settled too.
        for request in self._all(
                "SELECT * FROM deposits WHERE status = 'pending' AND user_id = ? AND method = 'binance'"
                " AND receipt_file_id IS NULL AND order_id IS NULL AND unconfirmed IS NOT NULL",
                (user_id,)):
            if (ids & set(request["unconfirmed"].split()) and not self.deposit_payments(request["id"])
                    and self.still_unconfirmed(request) == []):
                self.conn.execute(
                    "UPDATE deposits SET status = 'closed', decided_at = ?,"
                    " note = COALESCE(note || char(10), '') || ? WHERE id = ?",
                    (now(), "closed: added by hand", request["id"]))
                closed.append(request["id"])
        return Registered(row, tuple(closed))

    def register_payment(self, order_id: str, user_id: Optional[int] = None,
                         payment: Any = None) -> Registered:
        """The owner added this Binance payment by hand (/regbin): it's recorded as added,
        so it can never be added again, automatically or by a request. ``user_id``: whose
        it was (the owner's if not said); ``payment``: what Binance has for it. PaymentUsed
        if it was added already, PaymentWaiting if a request still waits with it (decide
        that instead)."""
        user_id = user_id or self.owner_id
        with self.tx():
            self.ensure_user(user_id)
            return self._register(order_id, user_id, 0, None, payment)

    def payment_records(self, *ids: str) -> list[sqlite3.Row]:
        """Every record that has one of these IDs as its Order ID or its other ID."""
        values = [value for value in ids if value]
        if not values:
            return []
        marks = ", ".join("?" for _ in values)
        return self._all(f"SELECT * FROM binance_payments WHERE order_id IN ({marks})"
                         f" OR transaction_id IN ({marks}) ORDER BY rowid", (*values, *values))

    def request_used(self, deposit_id: int) -> list[sqlite3.Row]:
        """Payments a waiting Binance request names (its Order ID, or one Binance didn't
        confirm) that something else added already: approving it may add them again."""
        dep = self.get_deposit(deposit_id)
        if dep is None or dep["method"] != "binance" or dep["receipt_file_id"]:
            return []
        used: dict[str, sqlite3.Row] = {}
        for value in dict.fromkeys([dep["order_id"], *(dep["unconfirmed"] or "").split()]):
            for row in self.payment_records(value) if value else []:
                if self.used(row) and row["deposit_id"] != deposit_id:
                    used.setdefault(row["order_id"], row)
        return list(used.values())

    def unlinked_payments(self, order_id: str) -> list[sqlite3.Row]:
        """Payments an older version recorded by Binance's transaction ID alone (its /regbin,
        or an approved request naming one, when Binance didn't list the payment), recorded
        after this Order ID was paid (its own time says when): any of them may be this
        payment, and without Binance that can't be told. Newer versions never record one."""
        if not order_id.isdigit():
            return []
        paid = order_id_time(order_id)
        since = "" if paid is None else self._before(
            datetime.fromtimestamp(paid / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), binance_clock=True)
        return self._all("SELECT * FROM binance_payments WHERE transaction_id IS NULL"
                         " AND order_id GLOB '*[^0-9]*' AND created_at >= ? ORDER BY rowid", (since,))

    def request_unlinked(self, deposit_id: int) -> list[tuple[str, list[sqlite3.Row]]]:
        """For each Order ID a waiting Binance request names or was sent with that the bot
        doesn't know: the payments an older version recorded by the transaction ID alone
        that may be it (unlinked_payments). A payment Binance confirmed has both IDs."""
        dep = self.get_deposit(deposit_id)
        if dep is None or dep["method"] != "binance" or dep["receipt_file_id"] or dep["status"] != "pending":
            return []
        found = []
        for value in dict.fromkeys([dep["order_id"] or "", *(dep["unconfirmed"] or "").split()]):
            if value and not self.payment_records(value):
                rows = self.unlinked_payments(value)
                if rows:
                    found.append((value, rows))
        return found

    def binance_payment(self, *ids: str) -> Optional[sqlite3.Row]:
        """The already used payment with one of these Order IDs or transaction IDs."""
        for value in ids:
            if value:
                row = self._one(
                    "SELECT * FROM binance_payments WHERE order_id = ? OR transaction_id = ?",
                    (value, value),
                )
                if row is not None:
                    return row
        return None

    def start_binance_auto(self, on: bool) -> None:
        """At start-up. With a Binance key, automatic checking counts from when it was first
        turned on; without one the count is dropped, so turning it on again starts afresh
        (payments meanwhile were added by hand from receipts, without their Order IDs)."""
        if on:
            self.binance_auto_since()
        else:
            self.conn.execute("DELETE FROM settings WHERE key = 'binance_auto_since'")

    def binance_auto_since(self) -> int:
        """When automatic checking started (ms since 1970): it never credits an older
        payment (it may have been added by hand before)."""
        value = self.get_setting("binance_auto_since")
        if value is None:
            value = str(int(datetime.now(timezone.utc).timestamp() * 1000))
            self.set_setting("binance_auto_since", value)
        return int(value)

    def paid_before_auto(self, time_ms: int) -> bool:
        """Was a payment (Binance's time) made before automatic checking started, on this
        server's clock? Binance searches by its own clock, so one made just before can be
        found; within the margin it counts as before."""
        started = self.get_setting("binance_auto_since")
        if started is None:
            return False
        margin_ms = (AUTO_START_MARGIN + self._clock_jump()) * 1000
        return time_ms - self.binance_clock_offset() < int(started) + margin_ms

    @staticmethod
    def _paid_at(payment: Any) -> str:
        return datetime.fromtimestamp(payment.time_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def binance_clock_offset(self) -> int:
        """Binance's clock minus this server's (ms), as the Binance client last measured it."""
        return int(self.get_setting("binance_clock_offset_ms") or 0)

    def note_binance_clock(self, offset_ms: int) -> None:
        """The offset the Binance client measured. A change bigger than DOUBT_MARGIN means
        this server's clock was set right (or went wrong), or was off before it was first
        measured: times written until then compare off by that much, so the margin widens
        by it for CLOCK_JUMP_FOR."""
        offset_ms, old = int(offset_ms), self.binance_clock_offset()
        if offset_ms == old:
            return
        self.set_setting("binance_clock_offset_ms", str(offset_ms))
        jump = abs(offset_ms - old) / 1000
        if jump > DOUBT_MARGIN:
            self.set_setting("binance_clock_jump", json.dumps(
                {"seconds": max(jump, self._clock_jump()),
                 "at": datetime.now(timezone.utc).timestamp()}))

    def _clock_jump(self) -> float:
        """Seconds of a clock correction measured in the last CLOCK_JUMP_FOR, else 0."""
        try:
            data = json.loads(self.get_setting("binance_clock_jump") or "{}")
            if datetime.now(timezone.utc).timestamp() - float(data["at"]) < CLOCK_JUMP_FOR:
                return float(data["seconds"])
        except (KeyError, TypeError, ValueError):
            pass
        return 0.0

    def _before(self, when: str, binance_clock: bool = False) -> str:
        """``when`` (as now() writes it) minus the margin, on this server's clock; with
        ``binance_clock``, ``when`` is a time from Binance (a payment's)."""
        moment = datetime.strptime(when, "%Y-%m-%d %H:%M:%S") - timedelta(
            seconds=DOUBT_MARGIN + self._clock_jump())
        if binance_clock:
            moment -= timedelta(milliseconds=self.binance_clock_offset())
        return moment.strftime("%Y-%m-%d %H:%M:%S")

    def payment_doubts(self, ids: Sequence[str], paid_at: str, user_id: int,
                       exclude: Optional[int] = None) -> list[sqlite3.Row]:
        """Deposits that may be about this payment (paid at ``paid_at``, known by ``ids``,
        claimed by ``user_id``) without naming it, so it's never added without the owner:
        - requests with an Order ID Binance didn't confirm ("unverified", anyone's) that
          the owner decided after it was paid: he may have approved this payment through
          one, or rejected it and refunded it or added the money by hand;
        - such requests still waiting: this customer's (he may approve one for it), and
          anyone's made after it was paid (another customer's naming exactly this payment
          too: who paid it is for the owner to say). This customer's naming exactly this
          payment doesn't count: it's closed once the payment is added; nor does one whose
          unconfirmed Order IDs have all been added for its customer since. A decided one
          always counts: it may have been decided for any payment;
        - balance the owner added by hand after it was paid, to anyone (/addbal with or
          without an Order ID, ⏭ Skip, a /modifybal raise): that money may have been this
          payment, whoever claims it now;
        - receipts, like requests Binance didn't confirm: decided after it was paid, or
          waiting (this customer's, or anyone's sent after it was paid).
        ``exclude``: the request asking. paid_at is on Binance's clock, converted to this
        server's, with a margin for what the conversion can't know."""
        since = self._before(paid_at, binance_clock=True)
        rows = self._all(
            "SELECT * FROM deposits WHERE id != ? AND ("
            " (unverified = 1 AND status IN ('approved', 'rejected', 'closed') AND decided_at >= ?)"
            " OR (unverified = 1 AND status = 'pending' AND (created_at >= ? OR user_id = ?))"
            " OR (method = 'manual' AND created_at >= ?)"
            " OR (receipt_file_id IS NOT NULL AND status IN ('approved', 'rejected') AND decided_at >= ?)"
            " OR (receipt_file_id IS NOT NULL AND status = 'pending' AND (created_at >= ? OR user_id = ?)))"
            " ORDER BY id",
            (exclude or 0, since, since, user_id, since, since, since, user_id),
        )
        named = {value for value in ids if value}
        return ([r for r in rows if not (
            (r["status"] == "pending" and (
                (r["user_id"] == user_id and r["order_id"] in named)  # closed once it's added
                or (r["method"] == "binance" and self.still_unconfirmed(r) == [])))
            # closed because the payments it named were added, as Binance has them, or by the
            # bot itself when one was added automatically or with another request: it added
            # nothing (that other request is a doubt by itself if Binance didn't confirm it)
            or (r["status"] == "closed" and (self._settled_by_known_payments(r) or _closed_by_itself(r))))]
            + self._raises_since(since))

    def _settled_by_known_payments(self, request: sqlite3.Row) -> bool:
        """Every Order ID this request was sent with is a payment the bot added, with what
        Binance has for it (so the request was about those, nothing else)."""
        values = (request["unconfirmed"] or "").split()
        rows = [self.binance_payment(value) for value in values]
        return bool(values) and all(row is not None and self.used(row) and row["currency"] for row in rows)

    def _raises_since(self, since: str) -> list[sqlite3.Row]:
        """Balance the owner raised with /modifybal since then (anyone's), shaped like a
        deposit: method 'raise', and the ledger line's id made negative."""
        return self._all(
            "SELECT -id AS id, user_id, delta AS amount, 'raise' AS method, 'approved' AS status,"
            " created_at, created_at AS decided_at, 0 AS unverified, NULL AS order_id,"
            " NULL AS unconfirmed, NULL AS receipt_file_id, NULL AS receipt_kind, NULL AS note"
            " FROM ledger WHERE reason = 'balance set by owner' AND delta > 0 AND created_at >= ?"
            " ORDER BY ledger.id DESC", (since,))

    def added_around(self, dep: sqlite3.Row) -> list[sqlite3.Row]:
        """Balance added since unverified_start that may be the payment of a request Binance
        didn't confirm, or of a receipt (the owner picks the payment himself), most telling
        first: this customer's (raises too); anyone's with a payment Binance has of the same
        amount; and for a request, also anyone's of the same amount, by hand, with a
        request Binance didn't confirm or with a receipt, and raises (a receipt shows who
        paid, so those are left out for it: without a key every deposit is a receipt)."""
        start = self.unverified_start(dep)
        receipt = bool(dep["receipt_file_id"])
        rows = self._all(
            "SELECT d.* FROM deposits d WHERE d.id != ? AND d.status = 'approved' AND d.decided_at >= ?"
            " AND (d.user_id = ? OR EXISTS (SELECT 1 FROM binance_payments p WHERE p.deposit_id = d.id"
            " AND p.status = 'credited' AND p.raw_amount != '' AND ROUND(CAST(p.raw_amount AS REAL) * 100) = ?)"
            + ("" if receipt else " OR d.amount = ? OR d.method = 'manual' OR d.unverified = 1"
               " OR d.receipt_file_id IS NOT NULL") + ")",
            (dep["id"], start, dep["user_id"], dep["amount"], *(() if receipt else (dep["amount"],))))
        raises = [row for row in self._raises_since(start) if not receipt or row["user_id"] == dep["user_id"]]
        return self.most_telling(rows + raises, dep["user_id"], decisions_first=False)

    @staticmethod
    def most_telling(rows: list[sqlite3.Row], user_id: int, decisions_first: bool = True) -> list[sqlite3.Row]:
        """Deposits (and raises) that may be about a payment, as they're listed (cut short):
        the customer's own first, then (``decisions_first``) approved, rejected, the rest,
        each newest first, raises among them by when they were made."""
        rank = {"approved": 0, "rejected": 1}
        rows = sorted(rows, key=lambda r: (r["decided_at"] or r["created_at"], r["id"]), reverse=True)
        return sorted(rows, key=lambda r: (r["user_id"] != user_id,
                                           rank.get(r["status"], 2) if decisions_first else 0))

    def unverified_start(self, dep: sqlite3.Row) -> str:
        """Since when balance may have been added for the payment of a request Binance didn't
        confirm: a week before the request, or from when the earliest of the Order IDs it was
        sent with says it was paid, if that's earlier (any of them may be the payment)."""
        start = (datetime.strptime(dep["created_at"], "%Y-%m-%d %H:%M:%S")
                 - timedelta(seconds=UNVERIFIED_LOOKBACK)).strftime("%Y-%m-%d %H:%M:%S")
        sent = [dep["order_id"] or "", *(dep["unconfirmed"] or "").split()]
        times = [paid for paid in (order_id_time(value) for value in sent if value) if paid is not None]
        if times:
            when = datetime.fromtimestamp(min(times) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            start = min(start, self._before(when, binance_clock=True))
        return start

    def still_unconfirmed(self, request: sqlite3.Row) -> Optional[list[str]]:
        """The Order IDs of this request Binance didn't confirm that still aren't a payment
        added for its customer by another deposit (Binance may list one later, and then it
        can't be about another payment). None: an older version's, not known."""
        if request["unconfirmed"] is None:
            return None
        left = []
        for value in request["unconfirmed"].split():
            row = self.binance_payment(value)
            if (row is None or row["status"] != "credited" or row["user_id"] != request["user_id"]
                    or row["deposit_id"] == request["id"]):
                left.append(value)
        return left

    def request_warnings(self, deposit_id: int) -> tuple[list[tuple[sqlite3.Row, list[sqlite3.Row], bool]],
                                                         list[sqlite3.Row], list[tuple[str, sqlite3.Row]]]:
        """What the owner should know before approving this Binance request, most telling
        first (lists are shown cut short):
        - for each payment it holds, what may be about it already (payment_doubts, and a
          request that was decided while holding it): decisions first, then requests still
          waiting; and whether it was paid before automatic checking started;
        - for a request with an Order ID Binance didn't confirm (the owner picks the payment
          himself), the deposits added since it was made that may be that payment: this
          customer's first, then anyone's of the same amount (also when Binance's payment
          had it but another amount was chosen); newest first;
        - an Order ID of it that Binance has as a payment kept for another waiting request
          (approving this one would take that payment away from it)."""
        dep = self.get_deposit(deposit_id)
        if dep is None or dep["method"] != "binance":
            return [], [], []
        if dep["receipt_file_id"]:  # a receipt names no payment: what was added around then
            return [], self.added_around(dep), []
        held = []
        rows = self.deposit_payments(deposit_id)
        for row in self.payment_records(dep["order_id"]) if dep["order_id"] else []:
            # The payment it names, kept for a request decided without adding it: approving
            # this one adds it, so it's shown as one this request holds.
            other = self.get_deposit(row["deposit_id"]) if row["deposit_id"] not in (None, deposit_id) else None
            if row["status"] == "owner" and other is not None and other["status"] != "pending":
                rows.append(row)
        for row in rows:
            if row["status"] == "owner" and row["currency"] and row["paid_at"]:
                doubts = self.payment_doubts((row["order_id"], row["transaction_id"] or ""),
                                             row["paid_at"], dep["user_id"], exclude=deposit_id)
                # decided while holding it (also the one it's still kept for, if not this one)
                for earlier in (row["approved_in"], row["held_before"], row["deposit_id"]):
                    before = self.get_deposit(earlier) if earlier and earlier != deposit_id else None
                    if before is not None and before["id"] not in {r["id"] for r in doubts}:
                        doubts.append(before)
                paid_ms = int(datetime.strptime(row["paid_at"], "%Y-%m-%d %H:%M:%S")
                              .replace(tzinfo=timezone.utc).timestamp() * 1000)
                early = self.paid_before_auto(paid_ms)
                if doubts or early:
                    held.append((row, self.most_telling(doubts, dep["user_id"]), early))
        since = self.added_around(dep) if dep["unverified"] else []
        elsewhere = []  # its payment, as Binance has it, waits with another request
        for value in dict.fromkeys([dep["order_id"], *(dep["unconfirmed"] or "").split()]):
            row = self.binance_payment(value) if value else None
            other = (self.get_deposit(row["deposit_id"])
                     if row is not None and row["status"] == "owner" and row["deposit_id"] not in (None, deposit_id)
                     else None)
            if other is not None and other["status"] == "pending":
                elsewhere.append((row["order_id"], other))
        return held, since, elsewhere

    def approval_block(self, deposit_id: int) -> Optional[str]:
        """Why this waiting Binance request can't be approved, or None: approving adds the
        payment it names and every payment it holds, so none of them may have been added
        already, be kept for another waiting request, or be known by only an ID that
        another record of the same payment may use."""
        dep = self.get_deposit(deposit_id)
        if dep is None or dep["status"] != "pending" or dep["method"] != "binance" or dep["receipt_file_id"]:
            return None
        order_id = dep["order_id"]
        if not order_id:
            return ("This request names no payment that can still be added: each Order ID it was sent "
                    "with was added already or is kept for another request. Reject it. If the customer "
                    "paid with another payment, add that one with /addbal ID AMOUNT ORDER_ID.")
        records = self.payment_records(order_id)
        for row in records + self.deposit_payments(deposit_id):
            if row["deposit_id"] == deposit_id and row["status"] == "owner" and row["approved_in"] is None:
                continue  # kept for this request: added with it
            if self.used(row):
                where = (f" in request #{row['approved_in']}, which you approved"
                         if row["status"] != "credited" else "")
                return f"Order ID {row['order_id']} was added already{where}. Reject this request."
            holder = self.get_deposit(row["deposit_id"]) if row["deposit_id"] else None
            if holder is not None and holder["status"] == "pending" and holder["id"] != deposit_id:
                return (f"Order ID {row['order_id']} is kept for request #{holder['id']}, which is "
                        "waiting: decide that one first.")
        if not records and not order_id.isdigit():
            return ("This request names Binance's transaction ID, which Binance didn't confirm, so the "
                    "bot can't make sure that payment isn't added again under its Order ID. Reject it "
                    "and ask the customer for the payment's Order ID (the digits in its details).")
        return None

    def approval_warnings(self, deposit_id: int) -> set[tuple[str, Any]]:
        """Why approving this request may add a payment twice: what request_warnings and
        request_used give, each as (what kind of warning, which deposit or payment), also
        those the request only counts ("and N more"), so a new one is never confirmed unseen.
        The owner has to confirm exactly these. Empty when approving is refused anyway
        (approval_block)."""
        dep = self.get_deposit(deposit_id)
        if dep is None or dep["status"] != "pending" or self.approval_block(deposit_id):
            return set()
        held, since, elsewhere = self.request_warnings(deposit_id)
        found: set[tuple[str, Any]] = {("doubt", row["id"]) for _, doubts, _ in held for row in doubts}
        found |= {("since", row["id"]) for row in since}
        found |= {("elsewhere", other["id"]) for _, other in elsewhere}
        if any(early for _, _, early in held):  # paid before automatic checking started
            found.add(("early", deposit_id))
        found |= {("used", row["order_id"]) for row in self.request_used(deposit_id)}
        found |= {("unlinked", (value, row["order_id"])) for value, rows in self.request_unlinked(deposit_id)
                  for row in rows}
        return found

    def link_payment(self, row: sqlite3.Row, payment: Any) -> sqlite3.Row:
        """Binance shows a payment the bot knows by only one of its IDs (an approval of the
        ID a customer typed): remember the other one too, so either finds it."""
        order_id, transaction_id = payment.order_id, payment.transaction_id
        try:
            with self.tx():
                if (row["transaction_id"] is None and transaction_id and row["order_id"] != transaction_id
                        and self.binance_payment(transaction_id) is None):
                    self.conn.execute("UPDATE binance_payments SET transaction_id = ? WHERE order_id = ?",
                                      (transaction_id, row["order_id"]))
                elif (row["order_id"] == transaction_id and order_id
                      and self.binance_payment(order_id) is None):
                    self.conn.execute("UPDATE binance_payments SET order_id = ?, transaction_id = ?"
                                      " WHERE order_id = ?", (order_id, transaction_id, transaction_id))
        except sqlite3.IntegrityError:  # already noted under another record: nothing to add
            log.warning("Binance payment %s is recorded twice", order_id or transaction_id)
        return self.binance_payment(order_id, transaction_id) or row

    def held_after_decision(self, row: sqlite3.Row) -> Optional[sqlite3.Row]:
        """The request a payment is kept for, if the owner decided it without adding it
        (rejected it, e.g. for its amount, or it was closed): nobody got that payment, so
        whoever claims it next, the owner decides on it again. None otherwise."""
        if row["status"] != "owner" or row["deposit_id"] is None or row["approved_in"] is not None:
            return None  # (in an approved request: added)
        request = self.get_deposit(row["deposit_id"])
        return request if request is not None and request["status"] not in ("pending", "approved") else None

    def _record_payment(self, user_id: int, payment: Any, cents: int, status: str,
                        deposit_id: Optional[int]) -> None:
        paid = self._paid_at(payment)
        self.conn.execute(
            "INSERT INTO binance_payments (order_id, transaction_id, user_id, amount, raw_amount,"
            " currency, paid_at, status, deposit_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (payment.order_id or payment.transaction_id, payment.transaction_id or None,
             user_id, cents, format(payment.amount.normalize(), "f"), payment.currency, paid, status,
             deposit_id, now()),
        )

    def credit_binance_payment(
        self, user_id: int, payment: Any, cents: int, seller_threshold: int
    ) -> DepositDecision:
        """Add a received Binance Pay transfer to the balance, once: its Order ID (and
        transaction ID) can never be used again, not even by two people at the same time.
        PaymentInDoubt: the owner may have decided on it by hand already (payment_doubts)."""
        if cents <= 0:
            raise ShopError("The amount is too small to add.")
        with self.tx():
            used = self.binance_payment(payment.order_id, payment.transaction_id)
            if used is not None:
                raise PaymentUsed(used)
            doubt = self.payment_doubts((payment.order_id, payment.transaction_id),
                                        self._paid_at(payment), user_id)
            early = self.paid_before_auto(payment.time_ms)
            if doubt or early:
                raise PaymentInDoubt(doubt, early)
            self.ensure_user(user_id)
            ts = now()
            cur = self.conn.execute(
                "INSERT INTO deposits (user_id, amount, method, status, created_at, decided_at,"
                " order_id) VALUES (?, ?, 'binance_auto', 'approved', ?, ?, ?)",
                (user_id, cents, ts, ts, payment.order_id or payment.transaction_id),
            )
            deposit_id = int(cur.lastrowid)
            self._record_payment(user_id, payment, cents, "credited", deposit_id)
            self.conn.execute(
                "UPDATE users SET balance = balance + ?, total_deposits = total_deposits + ?"
                " WHERE id = ?",
                (cents, cents, user_id),
            )
            self._log_balance(user_id, cents, "binance deposit", f"deposit#{deposit_id}")
            became_seller = self._maybe_auto_seller(user_id, seller_threshold)
            closed = self._close_settled(user_id, (payment.order_id, payment.transaction_id),
                                         f"closed: added automatically with deposit #{deposit_id}")
            deposit = self.get_deposit(deposit_id)
            user = self.get_user(user_id)
        return DepositDecision(deposit, user, became_seller, closed)  # type: ignore[arg-type]

    def create_review_request(
        self, user_id: int, amount: int, order_id: Optional[str], note: str, payments: list[Any],
        tried: Sequence[str] = (),
    ) -> sqlite3.Row:
        """A deposit the bot couldn't add (its checks failed): a request the owner approves
        or rejects. The received ``payments`` Binance found are kept for it, so nobody can
        claim them while the owner decides, and so is a payment kept for an earlier request
        the owner already decided (anyone's: nobody got it). ``order_id`` is the Order ID checked
        last, ``tried`` all the ones the customer sent.

        The request names the payment an approval marks as added: the one checked last if
        it's kept, else the latest one kept, else the latest one sent that nobody used (an
        Order ID rather than a transaction ID). The Order
        IDs sent that aren't a payment kept for it are "unconfirmed": the owner may approve
        it for a payment the bot can't name (see payment_doubts)."""
        if amount <= 0:
            raise ShopError("The amount must be more than 0.")
        with self.tx():
            self.ensure_user(user_id)
            cur = self.conn.execute(
                "INSERT INTO deposits (user_id, amount, method, status, created_at, note)"
                " VALUES (?, ?, 'binance', 'pending', ?, ?)",
                (user_id, amount, now(), note or None),
            )
            deposit_id = int(cur.lastrowid)
            kept: list[str] = []  # the payments kept for it, in the order they were sent

            def keep(row: Optional[sqlite3.Row], payment: Any = None) -> None:
                if row is None and payment is not None:  # found at Binance, used by nobody
                    self._record_payment(user_id, payment, 0, "owner", deposit_id)
                    kept.append(payment.order_id or payment.transaction_id)
                elif row is not None and self.held_after_decision(row) is not None:  # anyone's
                    self.conn.execute("UPDATE binance_payments SET held_before = deposit_id,"
                                      " deposit_id = ?, user_id = ? WHERE order_id = ?",
                                      (deposit_id, user_id, row["order_id"]))
                    kept.append(row["order_id"])

            for payment in payments:
                keep(self.binance_payment(payment.order_id, payment.transaction_id), payment)
            if order_id:
                keep(self.binance_payment(order_id))
            sent = list(dict.fromkeys([*tried, *([order_id] if order_id else [])]))
            last = self.binance_payment(order_id) if order_id else None
            if last is not None and last["deposit_id"] == deposit_id and last["status"] == "owner":
                named: Optional[str] = last["order_id"]  # the payment being claimed now
            elif kept:
                named = kept[-1]
            else:  # the latest one sent that nobody used (one used already can't be added),
                # an Order ID before a transaction ID (which can't be approved unconfirmed)
                free = [value for value in sent if self.binance_payment(value) is None]
                named = next((value for value in reversed(free) if value.isdigit()), free[-1] if free else None)
            held = {value for row in self.deposit_payments(deposit_id)
                    for value in (row["order_id"], row["transaction_id"]) if value}
            unconfirmed = [value for value in sent if value not in held]
            self.conn.execute(
                "UPDATE deposits SET order_id = ?, unverified = ?, unconfirmed = ? WHERE id = ?",
                (named, int(bool(unconfirmed)), " ".join(unconfirmed), deposit_id),
            )
            return self.get_deposit(deposit_id)  # type: ignore[return-value]

    def deposit_payments(self, deposit_id: int) -> list[sqlite3.Row]:
        """The Binance payments a deposit request was checked against."""
        return self._all("SELECT * FROM binance_payments WHERE deposit_id = ? ORDER BY rowid",
                         (deposit_id,))

    # -------------------------------------------------------------- withdrawals
    def create_withdrawal(
        self, user_id: int, amount: int, binance_id: str, auto_approve: bool
    ) -> sqlite3.Row:
        if amount <= 0:
            raise ShopError("The amount must be more than 0.")
        with self.tx():
            user = self.ensure_user(user_id)
            if user["earnings"] < amount:
                raise ShopError(
                    f"You only have {user['earnings'] / 100:.2f}$ available to withdraw."
                )
            self.conn.execute(
                "UPDATE users SET earnings = earnings - ? WHERE id = ?", (amount, user_id)
            )
            ts = now()
            cur = self.conn.execute(
                "INSERT INTO withdrawals (user_id, amount, binance_id, status, created_at,"
                " decided_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    amount,
                    binance_id,
                    "approved" if auto_approve else "pending",
                    ts,
                    ts if auto_approve else None,
                ),
            )
        return self._one("SELECT * FROM withdrawals WHERE id = ?", (cur.lastrowid,))  # type: ignore[return-value]

    def get_withdrawal(self, withdrawal_id: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))

    # ------------------------------------------------------- pending requests
    def pending_deposits(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM deposits WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)
        )

    def pending_withdrawals(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)
        )

    def count_pending_requests(self) -> int:
        return int(
            self._scalar(
                "SELECT (SELECT COUNT(*) FROM deposits WHERE status = 'pending')"
                " + (SELECT COUNT(*) FROM withdrawals WHERE status = 'pending')"
            )
        )

    def decide_withdrawal(self, withdrawal_id: int, approve: bool) -> sqlite3.Row:
        with self.tx():
            wd = self.get_withdrawal(withdrawal_id)
            if wd is None:
                raise ShopError("This request doesn't exist.")
            if wd["status"] != "pending":
                raise ShopError(f"This request was already {wd['status']}.")
            if not approve:  # give the money back to the seller's earnings
                self.ensure_user(wd["user_id"])
                self.conn.execute(
                    "UPDATE users SET earnings = earnings + ? WHERE id = ?",
                    (wd["amount"], wd["user_id"]),
                )
            self.conn.execute(
                "UPDATE withdrawals SET status = ?, decided_at = ? WHERE id = ?",
                ("approved" if approve else "rejected", now(), withdrawal_id),
            )
        return self.get_withdrawal(withdrawal_id)  # type: ignore[return-value]

    # --------------------------------------------------------------- broadcasts
    _RECIPIENTS = "started = 1 AND is_banned = 0 AND id != ?"

    def count_broadcast_recipients(self) -> int:
        """Everyone who has started the bot and isn't banned, without the owner."""
        return int(self._scalar(f"SELECT COUNT(*) FROM users WHERE {self._RECIPIENTS}", (self.owner_id,)))

    def broadcast_page(self, after_id: int, limit: int) -> list[sqlite3.Row]:
        """The next recipients after user id ``after_id``, in id order (see above)."""
        return self._all(
            f"SELECT * FROM users WHERE {self._RECIPIENTS} AND id > ? ORDER BY id LIMIT ?",
            (self.owner_id, after_id, limit),
        )

    def broadcast_recipients(self, exclude: Iterable[int] = ()) -> list[sqlite3.Row]:
        skip = set(exclude)
        return [row for row in self.broadcast_page(-(2**63), 2**31) if row["id"] not in skip]

    def _load_json_setting(self, key: str) -> Any:
        raw = self.get_setting(key)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            log.warning("Ignoring a damaged %s setting", key)
            return None

    def _pending_stock(self) -> dict[str, dict[str, Any]]:
        """{label id (text): {"count": n, "adders": [user ids]}}. Reads the older format
        ({"labels": {id: n}, "adders": [...]}) too."""
        data = self._load_json_setting(PENDING_STOCK)
        if not isinstance(data, dict) or not isinstance(data.get("labels"), dict):
            return {}
        shared = [a for a in data.get("adders", []) if isinstance(a, int)]
        out: dict[str, dict[str, Any]] = {}
        for key, value in data["labels"].items():
            if isinstance(value, dict):
                count, adders = value.get("count", 0), value.get("adders", [])
            else:
                count, adders = value, shared
            try:
                out[str(int(key))] = {"count": int(count),
                                      "adders": [a for a in adders if isinstance(a, int)]}
            except (TypeError, ValueError):
                continue
        return out

    def add_pending_stock(self, label_id: int, count: int, adder_id: int) -> None:
        """Note stock that was just added, for the next "New in the shop" message. It
        is kept in the database, so a restart in the meantime doesn't lose it."""
        with self.tx():
            labels = self._pending_stock()
            entry = labels.setdefault(str(label_id), {"count": 0, "adders": []})
            entry["count"] += count
            if adder_id not in entry["adders"]:
                entry["adders"].append(adder_id)
            self.set_setting(PENDING_STOCK, json.dumps({"labels": labels}))

    def has_pending_stock(self) -> bool:
        return self.get_setting(PENDING_STOCK) is not None

    def take_pending_stock(self) -> Optional[dict[int, dict[str, Any]]]:
        """The noted stock ({label id: {"count", "adders"}}), removed from the
        database; None if there is none."""
        with self.tx():
            if self.get_setting(PENDING_STOCK) is None:
                return None
            labels = self._pending_stock()
            self.conn.execute("DELETE FROM settings WHERE key = ?", (PENDING_STOCK,))
        return {int(key): value for key, value in labels.items()}

    def get_broadcast_job(self) -> Optional[dict[str, Any]]:
        """The broadcast that is going out (or was, when the bot stopped), if any."""
        job = self._load_json_setting(BROADCAST_JOB)
        return job if isinstance(job, dict) and job.get("kind") in ("announce", "stock") else None

    def save_broadcast_job(self, job: dict[str, Any]) -> None:
        self.set_setting(BROADCAST_JOB, json.dumps(job, ensure_ascii=False))

    def clear_broadcast_job(self) -> None:
        self.conn.execute("DELETE FROM settings WHERE key = ?", (BROADCAST_JOB,))

    # -------------------------------------------------------------------- stats
    def seller_stats(self, user_id: int) -> dict[str, Any]:
        user = self.get_user(user_id) or self.ensure_user(user_id)
        return {
            "user": user,
            "earned": int(
                self._scalar(
                    "SELECT COALESCE(SUM(sold_price), 0) FROM items"
                    " WHERE owner_id = ? AND status = 'sold'",
                    (user_id,),
                )
            ),
            "sold": int(
                self._scalar(
                    "SELECT COUNT(*) FROM items WHERE owner_id = ? AND status = 'sold'",
                    (user_id,),
                )
            ),
            "withdrawn": int(
                self._scalar(
                    "SELECT COALESCE(SUM(amount), 0) FROM withdrawals"
                    " WHERE user_id = ? AND status = 'approved'",
                    (user_id,),
                )
            ),
            "pending": int(
                self._scalar(
                    "SELECT COALESCE(SUM(amount), 0) FROM withdrawals"
                    " WHERE user_id = ? AND status = 'pending'",
                    (user_id,),
                )
            ),
            "available": int(user["earnings"]),
            "stock": self.count_all_items(user_id),
        }

    def all_seller_stats(self) -> list[dict[str, Any]]:
        """Owner first, then every admin or past seller, by amount earned."""
        ids = {r["id"] for r in self.list_admins()}
        ids |= {r[0] for r in self._all("SELECT DISTINCT owner_id FROM items")}
        ids |= {r[0] for r in self._all("SELECT DISTINCT user_id FROM withdrawals")}
        ids.discard(self.owner_id)
        others = sorted(
            (self.seller_stats(uid) for uid in ids), key=lambda s: (-s["earned"], s["user"]["id"])
        )
        return [self.seller_stats(self.owner_id), *others]

    def totals(self) -> dict[str, int]:
        return {
            "orders": int(self._scalar("SELECT COUNT(*) FROM orders")),
            "items_sold": int(self._scalar("SELECT COUNT(*) FROM items WHERE status = 'sold'")),
            "revenue": int(self._scalar("SELECT COALESCE(SUM(total), 0) FROM orders")),
            "deposits": int(
                self._scalar(
                    "SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE status = 'approved'"
                )
            ),
            "users": int(self._scalar("SELECT COUNT(*) FROM users WHERE started = 1")),
            "balances": int(self._scalar("SELECT COALESCE(SUM(balance), 0) FROM users")),
            "stock": self.count_all_items(),
        }

    def sales_analysis(self, owner_id: Optional[int] = None) -> list[dict[str, Any]]:
        sql = """
            SELECT label_id, owner_id,
                   SUM(CASE WHEN status = 'sold' THEN 1 ELSE 0 END)              AS sold,
                   SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END)         AS left_count,
                   SUM(CASE WHEN status = 'sold' THEN sold_price ELSE 0 END)     AS revenue,
                   SUM(CASE WHEN status = 'removed' THEN 1 ELSE 0 END)           AS removed,
                   SUM(CASE WHEN status = 'pulled' THEN 1 ELSE 0 END)            AS pulled
            FROM items
        """
        params: tuple[Any, ...] = ()
        if owner_id is not None:
            sql += " WHERE owner_id = ?"
            params = (owner_id,)
        sql += " GROUP BY label_id, owner_id"
        rows = []
        for r in self._all(sql, params):
            label = self._one("SELECT * FROM labels WHERE id = ?", (r["label_id"],))
            path = self.path_text(r["label_id"], sep=" > ")
            if label is not None and label["deleted"]:
                path += " (deleted)"
            elif label is not None and self.disabled_on_path(label["id"]):
                path += " (disabled)"
            rows.append(
                {
                    "label_id": r["label_id"],
                    "product": path,
                    "price": label["price"] if label is not None else None,
                    "owner_id": r["owner_id"],
                    "owner": self.get_user(r["owner_id"]),
                    "sold": int(r["sold"]),
                    "left": int(r["left_count"]),
                    "revenue": int(r["revenue"]),
                    "removed": int(r["removed"]),
                    "pulled": int(r["pulled"]),
                }
            )
        rows.sort(key=lambda x: (x["product"].casefold(), x["owner_id"] != self.owner_id, x["owner_id"]))
        return rows
