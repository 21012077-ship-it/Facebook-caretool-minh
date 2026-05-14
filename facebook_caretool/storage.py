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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    name TEXT NOT NULL,
                    uid TEXT,
                    password TEXT,
                    two_fa TEXT,
                    status TEXT NOT NULL,
                    note TEXT,
                    proxy TEXT,
                    cookie_file TEXT,
                    created_at TEXT,
                    last_open TEXT,
                    last_care TEXT,
                    care_profile TEXT,
                    care_plan_note TEXT
                )
                """
            )
            existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
            for column, definition in {
                "care_profile": "TEXT",
                "care_plan_note": "TEXT",
            }.items():
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
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

    def load_accounts(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts").fetchall()
        return [Account.from_dict(dict(row)).to_dict() for row in rows]

    def save_accounts(self, accounts: Iterable[Dict[str, Any]]) -> None:
        clean_accounts = [Account.from_dict(item).to_dict() for item in accounts]
        with self.connect() as conn:
            conn.execute("DELETE FROM accounts")
            conn.executemany(
                """
                INSERT INTO accounts (name, uid, password, two_fa, status, note, proxy, cookie_file, created_at, last_open, last_care, care_profile, care_plan_note)
                VALUES (:name, :uid, :password, :two_fa, :status, :note, :proxy, :cookie_file, :created_at, :last_open, :last_care, :care_profile, :care_plan_note)
                """,
                clean_accounts,
            )

    def load_logs(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM logs").fetchall()
        logs = []
        for row in rows:
            item = dict(row)
            metadata = item.pop("metadata", "")
            if metadata:
                item.update(json.loads(metadata))
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
