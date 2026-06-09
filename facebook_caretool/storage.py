from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .models import Account, LogEntry
from .utils import dumps_json, load_json, save_json


class JsonStorage:
    """Lưu account/log bằng JSON file, giữ tương thích dữ liệu cũ."""

    def __init__(self, accounts_path: str = "accounts.json", logs_path: str = "logs.json") -> None:
        self.accounts_path = accounts_path
        self.logs_path = logs_path
        self._last_accounts_payload: str | None = None
        self._last_logs_payload: str | None = None

    def load_accounts(self) -> List[Dict[str, Any]]:
        accounts = [Account.from_dict(item).to_dict() for item in load_json(self.accounts_path, []) if isinstance(item, dict)]
        self._last_accounts_payload = dumps_json(accounts)
        return accounts

    def save_accounts(self, accounts: Iterable[Dict[str, Any]]) -> None:
        clean_accounts = [Account.from_dict(item).to_dict() for item in accounts]
        payload = dumps_json(clean_accounts)
        if payload == self._last_accounts_payload:
            return
        save_json(self.accounts_path, clean_accounts)
        self._last_accounts_payload = payload

    def load_logs(self) -> List[Dict[str, Any]]:
        logs = [LogEntry.from_dict(item).to_dict() for item in load_json(self.logs_path, []) if isinstance(item, dict)]
        self._last_logs_payload = dumps_json(logs)
        return logs

    def save_logs(self, logs: Iterable[Dict[str, Any]]) -> None:
        clean_logs = [LogEntry.from_dict(item).to_dict() for item in logs]
        payload = dumps_json(clean_logs)
        if payload == self._last_logs_payload:
            return
        save_json(self.logs_path, clean_logs)
        self._last_logs_payload = payload


class SQLiteStorage:
    """Storage SQLite tùy chọn cho account/log khi dữ liệu lớn hơn JSON."""

    def __init__(self, db_path: str = "caretool.db") -> None:
        self.db_path = db_path
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            # WAL mode: better concurrent reads, less locking
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    uid TEXT,
                    password TEXT,
                    two_fa TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    note TEXT,
                    proxy TEXT,
                    proxy_changed_at TEXT,
                    proxy_action_locked_until TEXT,
                    cookie_file TEXT,
                    created_at TEXT,
                    last_open TEXT,
                    last_care TEXT,
                    care_profile TEXT,
                    care_plan_note TEXT,
                    UNIQUE(uid, name)
                )
                """
            )
            existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
            for column, definition in {
                "proxy_changed_at": "TEXT",
                "proxy_action_locked_until": "TEXT",
                "care_profile": "TEXT",
                "care_plan_note": "TEXT",
            }.items():
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT,
                    status TEXT,
                    action TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    time TEXT,
                    error TEXT,
                    metadata TEXT
                )
                """
            )
            # Index for common queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_account ON logs(account)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_status ON logs(status)")

    def load_accounts(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts").fetchall()
        return [Account.from_dict(dict(row)).to_dict() for row in rows]

    def save_accounts(self, accounts: Iterable[Dict[str, Any]]) -> None:
        clean_accounts = [Account.from_dict(item).to_dict() for item in accounts]
        with self.connect() as conn:
            # Use UPSERT to avoid data loss from DELETE+INSERT pattern
            conn.executemany(
                """
                INSERT OR REPLACE INTO accounts
                  (name, uid, password, two_fa, status, note, proxy,
                   proxy_changed_at, proxy_action_locked_until,
                   cookie_file, created_at, last_open, last_care,
                   care_profile, care_plan_note)
                VALUES
                  (:name, :uid, :password, :two_fa, :status, :note, :proxy,
                   :proxy_changed_at, :proxy_action_locked_until,
                   :cookie_file, :created_at, :last_open, :last_care,
                   :care_profile, :care_plan_note)
                """,
                clean_accounts,
            )
            # Remove rows whose (uid, name) no longer exist in the new list
            valid_identities = [
                (a.get("uid") or "", a.get("name") or "") for a in clean_accounts
            ]
            if not valid_identities:
                conn.execute("DELETE FROM accounts")
            else:
                # Bulk-delete accounts not in the current list
                conn.executemany(
                    "DELETE FROM accounts WHERE (uid IS NULL OR uid = ?) AND (name IS NULL OR name != ?)",
                    valid_identities,
                )

    def load_logs(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM logs ORDER BY id DESC").fetchall()
        logs = []
        for row in rows:
            item = dict(row)
            item.pop("id", None)  # Remove auto-generated id
            metadata = item.pop("metadata", "")
            if metadata:
                try:
                    item.update(json.loads(metadata))
                except (json.JSONDecodeError, TypeError):
                    pass  # Corrupt metadata — skip gracefully
            logs.append(LogEntry.from_dict(item).to_dict())
        return logs

    def save_logs(self, logs: Iterable[Dict[str, Any]]) -> None:
        clean_logs = [LogEntry.from_dict(item).to_dict() for item in logs]
        rows = []
        for item in clean_logs:
            known = {"account", "status", "action", "start_time", "end_time", "time", "error"}
            row = {key: item.get(key, "") for key in known}
            row["metadata"] = json.dumps({k: v for k, v in item.items() if k not in known}, ensure_ascii=False)
            rows.append(row)
        with self.connect() as conn:
            conn.execute("DELETE FROM logs")
            conn.executemany(
                """
                INSERT INTO logs (account, status, action, start_time, end_time, time, error, metadata)
                VALUES (:account, :status, :action, :start_time, :end_time, :time, :error, :metadata)
                """,
                rows,
            )
