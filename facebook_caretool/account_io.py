from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .models import Account, LogEntry
from .utils import load_json, save_json

SENSITIVE_FIELDS = {"password", "two_fa"}
EXPORT_VERSION = 1
FULL_BACKUP_VERSION = 1
BACKUP_FILE_TYPE = "full_backup"


def normalize_accounts(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [Account.from_dict(item).to_dict() for item in items if isinstance(item, dict)]


def normalize_logs(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [LogEntry.from_dict(item).to_dict() for item in items if isinstance(item, dict)]


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


def _safe_relative_path(path: str | Path, base_dir: str | Path = ".") -> str:
    source = Path(path).expanduser()
    if not source.is_absolute():
        return source.as_posix()
    try:
        return source.resolve().relative_to(Path(base_dir).resolve()).as_posix()
    except ValueError:
        return (Path("imported_files") / source.name).as_posix()


def _collect_backup_file(path: str | Path, base_dir: str | Path = ".") -> Dict[str, Any] | None:
    source = Path(path).expanduser()
    if not source.is_absolute():
        source = Path(base_dir) / source
    if not source.is_file():
        return None
    content = base64.b64encode(source.read_bytes()).decode("ascii")
    return {
        "path": _safe_relative_path(path, base_dir=base_dir),
        "size": source.stat().st_size,
        "content_b64": content,
    }


def _unique_cookie_paths(accounts: Iterable[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    seen: set[str] = set()
    for account in accounts:
        path = str(account.get("cookie_file") or "").strip()
        if path and path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


def build_full_backup_payload(
    accounts: Iterable[Dict[str, Any]],
    logs: Iterable[Dict[str, Any]],
    settings: Dict[str, Any] | None = None,
    *,
    include_cookie_files: bool = True,
    base_dir: str | Path = ".",
) -> Dict[str, Any]:
    """Build a single-file backup containing accounts, logs, settings and cookie files."""

    clean_accounts = normalize_accounts(accounts)
    files: List[Dict[str, Any]] = []
    if include_cookie_files:
        for cookie_path in _unique_cookie_paths(clean_accounts):
            entry = _collect_backup_file(cookie_path, base_dir=base_dir)
            if entry:
                files.append(entry)
        chatgpt_cookie = _collect_backup_file("chatgpt_cookies.json", base_dir=base_dir)
        if chatgpt_cookie:
            files.append(chatgpt_cookie)

    return {
        "app": "facebook-caretool",
        "type": BACKUP_FILE_TYPE,
        "version": FULL_BACKUP_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "accounts": clean_accounts,
        "logs": normalize_logs(logs),
        "settings": settings or {},
        "files": files,
    }


def save_full_backup_file(
    path: str | Path,
    accounts: Iterable[Dict[str, Any]],
    logs: Iterable[Dict[str, Any]],
    settings: Dict[str, Any] | None = None,
    *,
    include_cookie_files: bool = True,
    base_dir: str | Path = ".",
) -> None:
    save_json(
        path,
        build_full_backup_payload(
            accounts,
            logs,
            settings,
            include_cookie_files=include_cookie_files,
            base_dir=base_dir,
        ),
    )


def parse_full_backup_payload(payload: Any) -> Dict[str, Any]:
    is_full_backup = isinstance(payload, dict) and (
        payload.get("type") == BACKUP_FILE_TYPE or {"logs", "settings", "files"}.issubset(payload.keys())
    )
    if not is_full_backup:
        raise ValueError("File backup không đúng định dạng full backup của Facebook Care Tool.")
    accounts = normalize_accounts(payload.get("accounts", []))
    if not accounts:
        raise ValueError("File backup không có tài khoản hợp lệ.")
    logs_payload = payload.get("logs", [])
    files_payload = payload.get("files", [])
    settings_payload = payload.get("settings", {})
    return {
        "accounts": accounts,
        "logs": normalize_logs(logs_payload if isinstance(logs_payload, list) else []),
        "settings": settings_payload if isinstance(settings_payload, dict) else {},
        "files": files_payload if isinstance(files_payload, list) else [],
    }


def load_full_backup_file(path: str | Path) -> Dict[str, Any]:
    return parse_full_backup_payload(load_json(path, {}))


def restore_backup_files(file_entries: Iterable[Dict[str, Any]], base_dir: str | Path = ".") -> Dict[str, int]:
    stats = {"restored": 0, "skipped": 0}
    root = Path(base_dir).resolve()
    for entry in file_entries:
        if not isinstance(entry, dict):
            stats["skipped"] += 1
            continue
        relative_path = str(entry.get("path") or "").strip()
        content_b64 = str(entry.get("content_b64") or "")
        if not relative_path or not content_b64:
            stats["skipped"] += 1
            continue
        target = (root / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            stats["skipped"] += 1
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(content_b64.encode("ascii"), validate=True))
            stats["restored"] += 1
        except Exception:
            stats["skipped"] += 1
    return stats


def restore_full_backup(
    payload: Dict[str, Any],
    current_accounts: Iterable[Dict[str, Any]],
    current_logs: Iterable[Dict[str, Any]],
    current_settings: Dict[str, Any] | None = None,
    *,
    overwrite_accounts: bool = False,
    restore_logs: bool = True,
    restore_settings: bool = True,
    restore_files: bool = True,
    base_dir: str | Path = ".",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, int]]:
    backup = parse_full_backup_payload(payload)
    merged_accounts, account_stats = merge_accounts(current_accounts, backup["accounts"], overwrite=overwrite_accounts)
    merged_logs = normalize_logs(current_logs)
    imported_logs = backup["logs"] if restore_logs else []
    if restore_logs:
        merged_logs.extend(imported_logs)

    merged_settings = dict(current_settings or {})
    imported_settings = backup["settings"] if restore_settings else {}
    if restore_settings:
        merged_settings.update(imported_settings)

    file_stats = restore_backup_files(backup["files"], base_dir=base_dir) if restore_files else {"restored": 0, "skipped": 0}
    stats = {
        **account_stats,
        "logs_added": len(imported_logs),
        "files_restored": file_stats["restored"],
        "files_skipped": file_stats["skipped"],
        "settings_restored": 1 if imported_settings else 0,
    }
    return merged_accounts, merged_logs, merged_settings, stats
