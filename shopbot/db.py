"""SQLite storage for the shop.

Every money value is stored as an integer number of cents.

All methods are synchronous and never await, so a whole method (for example a
purchase) runs without any other Telegram update being processed in between.
Each method that changes several rows does so inside one transaction.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

log = logging.getLogger("shopbot.db")

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I look-alikes
CODE_LENGTH = 8
MAX_LABEL_NAME = 40
# settings key: stock added since the last "New in the shop" message (see broadcast.py)
PENDING_STOCK = "pending_stock_alert"
# settings key: the broadcast going out, with how far it got (resumed after a restart)
BROADCAST_JOB = "broadcast_job"

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
    status           TEXT    NOT NULL,            -- pending | approved | rejected
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


class PaymentUsed(ShopError):
    """This Binance payment (Order ID) was already used."""

    def __init__(self, row: sqlite3.Row) -> None:
        super().__init__("This payment was already used.")
        self.row = row


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


def clean_label_name(raw: str) -> str:
    return " ".join((raw or "").split())


def seller_price(price: int, discount_percent: int) -> int:
    """Price after the seller discount, rounded half-up to the cent."""
    return (price * (100 - discount_percent) + 50) // 100


class Database:
    def __init__(self, path: str, owner_id: int) -> None:
        self.path = path
        self.owner_id = owner_id
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._tx_depth = 0
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
        """Buy ``quantity`` items of a product, oldest stock first.

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
            rows = self._all(
                "SELECT id, owner_id FROM items WHERE label_id = ? AND status = 'available'"
                " ORDER BY id LIMIT ?",
                (label_id, quantity),
            )
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
            if approve and order_id:  # the receipt names a Binance payment: use it only once
                used = self.binance_payment(order_id)
                if used is not None and used["status"] == "credited":
                    raise ShopError("This payment (Order ID) was already added to a balance. "
                                    "Reject this request.")
                if used is None:
                    self.conn.execute(
                        "INSERT INTO binance_payments (order_id, user_id, amount, raw_amount,"
                        " currency, status, deposit_id, created_at)"
                        " VALUES (?, ?, ?, '', '', 'credited', ?, ?)",
                        (order_id, dep["user_id"], dep["amount"], dep["id"], now()),
                    )
                else:  # kept for this request when it came to the owner: now it's added
                    self.conn.execute(
                        "UPDATE binance_payments SET status = 'credited', amount = ?,"
                        " deposit_id = ? WHERE order_id = ?",
                        (dep["amount"], dep["id"], used["order_id"]),
                    )
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
            dep = self.get_deposit(deposit_id)
            user = self.get_user(dep["user_id"]) or self.ensure_user(dep["user_id"])  # type: ignore[index]
        return DepositDecision(dep, user, became_seller)  # type: ignore[arg-type]

    def manual_deposit(
        self, user_id: int, amount: int, seller_threshold: int
    ) -> tuple[sqlite3.Row, bool]:
        """Owner-added balance (/addbal). Counts as a deposit."""
        if amount <= 0:
            raise ShopError("The amount must be more than 0.")
        with self.tx():
            self.ensure_user(user_id)
            cur = self.conn.execute(
                "INSERT INTO deposits (user_id, amount, method, status, created_at, decided_at)"
                " VALUES (?, ?, 'manual', 'approved', ?, ?)",
                (user_id, amount, now(), now()),
            )
            self.conn.execute(
                "UPDATE users SET balance = balance + ?, total_deposits = total_deposits + ?"
                " WHERE id = ?",
                (amount, amount, user_id),
            )
            self._log_balance(user_id, amount, "manual deposit", f"deposit#{cur.lastrowid}")
            became_seller = self._maybe_auto_seller(user_id, seller_threshold)
        return self.get_user(user_id), became_seller  # type: ignore[return-value]

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

    def binance_auto_since(self) -> int:
        """When automatic checking started (ms since 1970): it never credits an older
        payment (it may have been added by hand before)."""
        value = self.get_setting("binance_auto_since")
        if value is None:
            value = str(int(datetime.now(timezone.utc).timestamp() * 1000))
            self.set_setting("binance_auto_since", value)
        return int(value)

    def _record_payment(self, user_id: int, payment: Any, cents: int, status: str,
                        deposit_id: Optional[int]) -> None:
        paid = datetime.fromtimestamp(payment.time_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
        transaction ID) can never be used again, not even by two people at the same time."""
        if cents <= 0:
            raise ShopError("The amount is too small to add.")
        with self.tx():
            used = self.binance_payment(payment.order_id, payment.transaction_id)
            if used is not None:
                raise PaymentUsed(used)
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
            deposit = self.get_deposit(deposit_id)
            user = self.get_user(user_id)
        return DepositDecision(deposit, user, became_seller)  # type: ignore[arg-type]

    def create_review_request(
        self, user_id: int, amount: int, order_id: Optional[str], note: str, payments: list[Any]
    ) -> sqlite3.Row:
        """A deposit the bot couldn't add (its checks failed): a request the owner approves
        or rejects. The received ``payments`` Binance found meanwhile are kept for it, so
        nobody can claim them while the owner decides; ``order_id`` is the one an approval
        marks as added (left out if it's used already)."""
        if amount <= 0:
            raise ShopError("The amount must be more than 0.")
        with self.tx():
            if order_id and self.binance_payment(order_id) is not None:
                order_id = None
            self.ensure_user(user_id)
            cur = self.conn.execute(
                "INSERT INTO deposits (user_id, amount, method, status, created_at, order_id, note)"
                " VALUES (?, ?, 'binance', 'pending', ?, ?, ?)",
                (user_id, amount, now(), order_id, note or None),
            )
            deposit_id = int(cur.lastrowid)
            for payment in payments:
                if self.binance_payment(payment.order_id, payment.transaction_id) is None:
                    self._record_payment(user_id, payment, 0, "owner", deposit_id)
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
