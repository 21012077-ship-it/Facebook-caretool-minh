from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .models import Account
from .utils import load_json, save_json

SENSITIVE_FIELDS = {"password", "two_fa"}
EXPORT_VERSION = 1


def normalize_accounts(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [Account.from_dict(item).to_dict() for item in items if isinstance(item, dict)]


def build_export_payload(accounts: Iterable[Dict[str, Any]], include_sensitive: bool = False) -> Dict[str, Any]:
    clean_accounts = normalize_accounts(accounts)
    if not include_sensitive:
        for account in clean_accounts:
            for field in SENSITIVE_FIELDS:
                account[field] = ""
    return {
        "app": "facebook-caretool",
        "version": EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "include_sensitive": include_sensitive,
        "accounts": clean_accounts,
    }


def parse_import_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        payload_accounts = payload.get("accounts", [])
    else:
        payload_accounts = payload
    if not isinstance(payload_accounts, list):
        raise ValueError("File import phải là JSON list hoặc object có khóa 'accounts'.")
    accounts = normalize_accounts(payload_accounts)
    if not accounts:
        raise ValueError("File import không có tài khoản hợp lệ.")
    return accounts


def load_import_accounts(path: str | Path) -> List[Dict[str, Any]]:
    return parse_import_payload(load_json(path, {}))


def merge_accounts(
    current_accounts: Iterable[Dict[str, Any]],
    imported_accounts: Iterable[Dict[str, Any]],
    overwrite: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    merged = normalize_accounts(current_accounts)
    stats = {"added": 0, "updated": 0, "skipped": 0}
    index_by_key: Dict[str, int] = {}

    for index, account in enumerate(merged):
        key = account_identity(account)
        if key:
            index_by_key[key] = index

    for imported in normalize_accounts(imported_accounts):
        key = account_identity(imported)
        existing_index = index_by_key.get(key) if key else None
        if existing_index is None:
            merged.append(imported)
            if key:
                index_by_key[key] = len(merged) - 1
            stats["added"] += 1
        elif overwrite:
            merged[existing_index] = imported
            stats["updated"] += 1
        else:
            stats["skipped"] += 1

    return merged, stats


def account_identity(account: Dict[str, Any]) -> str:
    uid = str(account.get("uid") or "").strip()
    if uid:
        return f"uid:{uid}"
    name = str(account.get("name") or "").strip().lower()
    return f"name:{name}" if name else ""


def backup_accounts_file(accounts_path: str | Path) -> Path | None:
    source = Path(accounts_path)
    if not source.exists():
        return None
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{source.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}{source.suffix}"
    backup_path.write_bytes(source.read_bytes())
    return backup_path


def save_export_file(path: str | Path, accounts: Iterable[Dict[str, Any]], include_sensitive: bool = False) -> None:
    save_json(path, build_export_payload(accounts, include_sensitive=include_sensitive))
