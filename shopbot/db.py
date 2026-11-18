"""SQLite storage for the shop.

Every money value is stored as an integer number of cents.

All methods are synchronous and never await, so a whole method (for example a
purchase) runs without any other Telegram update being processed in between.
Each method that changes several rows does so inside one transaction.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I look-alikes
CODE_LENGTH = 8
MAX_LABEL_NAME = 40

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
    created_at  TEXT    NOT NULL
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
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.ensure_user(owner_id)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self.conn.in_transaction:  # nested call: join the outer transaction
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

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
        """True if ``label_id`` or a label below it is a product with a price."""
        return bool(
            self._scalar(
                SUBTREE_CTE
                + """
                SELECT 1 FROM labels x JOIN sub ON x.id = sub.id
                WHERE x.deleted = 0 AND x.price IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM labels c WHERE c.parent_id = x.id AND c.deleted = 0)
                LIMIT 1
                """,
                (label_id,),
            )
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
        self, buyer_id: int, label_id: int, quantity: int, seller_discount_percent: int
    ) -> PurchaseResult:
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
            buyer = self.ensure_user(buyer_id)
            unit = label["price"]
            if buyer["is_seller"]:
                unit = seller_price(unit, seller_discount_percent)
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

    def set_balance(self, user_id: int, new_balance: int) -> tuple[int, int]:
        """Owner correction (/modifybal). Returns (old, new). Not counted as a deposit."""
        if new_balance < 0:
            raise ShopError("The balance can't be negative.")
        with self.tx():
            old = int(self.ensure_user(user_id)["balance"])
            self.conn.execute(
                "UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id)
            )
            self._log_balance(user_id, new_balance - old, "balance set by owner")
        return old, new_balance

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
