from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    suspended_reason TEXT,
    max_bps INTEGER NOT NULL,
    allocation_quota INTEGER NOT NULL,
    traffic_quota_bytes INTEGER NOT NULL,
    credential_ttl INTEGER NOT NULL,
    api_key_hash TEXT NOT NULL UNIQUE,
    auth_secret TEXT NOT NULL,
    realm TEXT NOT NULL UNIQUE,
    slot INTEGER NOT NULL UNIQUE,
    turn_port INTEGER NOT NULL UNIQUE,
    tls_port INTEGER UNIQUE,
    relay_min_port INTEGER NOT NULL,
    relay_max_port INTEGER NOT NULL,
    metrics_port INTEGER NOT NULL UNIQUE,
    cli_port INTEGER NOT NULL UNIQUE,
    billed_rx_bytes INTEGER NOT NULL DEFAULT 0,
    billed_tx_bytes INTEGER NOT NULL DEFAULT 0,
    last_usage_at INTEGER
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create_account(self, values: dict[str, Any]) -> dict[str, Any]:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.connection() as conn:
            conn.execute(
                f"INSERT INTO accounts ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (values["id"],)).fetchone()
        return dict(row)

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None

    def get_account_by_key_hash(self, key_hash: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE api_key_hash = ?", (key_hash,)).fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def used_slots(self) -> set[int]:
        with self.connection() as conn:
            rows = conn.execute("SELECT slot FROM accounts").fetchall()
        return {int(row["slot"]) for row in rows}

    def update_account(self, account_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        if not values:
            return self.get_account(account_id)
        values = {**values, "updated_at": int(time.time())}
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connection() as conn:
            conn.execute(
                f"UPDATE accounts SET {assignments} WHERE id = ?",
                (*values.values(), account_id),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None

    def update_usage(self, account_id: str, rx_bytes: int, tx_bytes: int) -> None:
        now = int(time.time())
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET billed_rx_bytes = ?, billed_tx_bytes = ?, last_usage_at = ?
                WHERE id = ?
                """,
                (max(0, rx_bytes), max(0, tx_bytes), now, account_id),
            )

    def delete_account(self, account_id: str) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
