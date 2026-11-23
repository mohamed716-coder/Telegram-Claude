"""Premium codes and users, kept in a small SQLite database file."""

import secrets
import sqlite3
import string
import time
from dataclasses import dataclass

DAY = 86400
CODE_ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 11

SCHEMA = """
CREATE TABLE IF NOT EXISTS codes (
    code        TEXT PRIMARY KEY,
    days        INTEGER NOT NULL,
    cooldown    INTEGER NOT NULL,
    created_by  INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    redeemed_by INTEGER,
    redeemed_at REAL
);
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    premium_until REAL NOT NULL DEFAULT 0,
    cooldown      INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Redeemed:
    days: int
    cooldown: int
    premium_until: float


class CodeNotFound(Exception):
    pass


class CodeAlreadyUsed(Exception):
    pass


class Store:
    def __init__(self, path: str, clock=time.time):
        self._clock = clock
        self.db = sqlite3.connect(path, isolation_level=None)  # we manage transactions
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- codes -------------------------------------------------------------

    def create_code(self, days: int, cooldown: int, created_by: int, prefix: str) -> str:
        while True:
            body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            code = f"{prefix}-{body}" if prefix else body
            try:
                self.db.execute(
                    "INSERT INTO codes (code, days, cooldown, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (code, days, cooldown, created_by, self._clock()),
                )
                return code
            except sqlite3.IntegrityError:  # astronomically unlikely duplicate
                continue

    def redeem(self, code: str, user_id: int, name: str = "") -> Redeemed:
        """Use a code. Adds its days on top of any Premium time the user still has."""
        now = self._clock()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
            if row is None:
                raise CodeNotFound(code)
            if row["redeemed_by"] is not None:
                raise CodeAlreadyUsed(code)
            self.db.execute(
                "UPDATE codes SET redeemed_by = ?, redeemed_at = ? WHERE code = ?",
                (user_id, now, code),
            )
            self._ensure_user(user_id, name)
            current = self.db.execute(
                "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()["premium_until"]
            until = max(now, current) + row["days"] * DAY
            self.db.execute(
                "UPDATE users SET premium_until = ?, cooldown = ? WHERE user_id = ?",
                (until, row["cooldown"], user_id),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return Redeemed(days=row["days"], cooldown=row["cooldown"], premium_until=until)

    def unused_codes(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM codes WHERE redeemed_by IS NULL ORDER BY created_at"
        ).fetchall()

    def delete_code(self, code: str) -> bool:
        """Delete a code that hasn't been used yet."""
        cur = self.db.execute(
            "DELETE FROM codes WHERE code = ? AND redeemed_by IS NULL", (code,)
        )
        return cur.rowcount > 0

    # ---- users -------------------------------------------------------------

    def _ensure_user(self, user_id: int, name: str) -> None:
        self.db.execute(
            "INSERT INTO users (user_id, name) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET name = CASE WHEN excluded.name != ''"
            " THEN excluded.name ELSE users.name END",
            (user_id, name),
        )

    def touch_user(self, user_id: int, name: str) -> None:
        self._ensure_user(user_id, name)

    def premium(self, user_id: int) -> sqlite3.Row | None:
        """The user's row if their Premium is active right now, else None."""
        return self.db.execute(
            "SELECT * FROM users WHERE user_id = ? AND premium_until > ?",
            (user_id, self._clock()),
        ).fetchone()

    def had_premium(self, user_id: int) -> bool:
        row = self.db.execute(
            "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row and row["premium_until"])

    def premium_users(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM users WHERE premium_until > ? ORDER BY premium_until",
            (self._clock(),),
        ).fetchall()

    def remove_premium(self, user_id: int) -> bool:
        cur = self.db.execute(
            "UPDATE users SET premium_until = 0 WHERE user_id = ? AND premium_until > ?",
            (user_id, self._clock()),
        )
        return cur.rowcount > 0
