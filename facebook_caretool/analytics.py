from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable

DATE_FORMATS = ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def parse_log_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def log_day(log: Dict[str, Any]) -> str:
    for key in ("start_time", "end_time", "time"):
        parsed = parse_log_datetime(log.get(key))
        if parsed:
            return parsed.strftime("%d/%m/%Y")
    return "Không rõ ngày"


def summarize_accounts(accounts: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter(str(account.get("status") or "active") for account in accounts)
    return {
        "total": sum(counts.values()),
        "active": counts.get("active", 0),
        "checkpoint": counts.get("checkpoint", 0),
        "cookie_error": counts.get("cookie_error", 0),
    }


def summarize_logs(logs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    by_day: Counter[str] = Counter()
    by_account: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    latest: list[Dict[str, Any]] = []

    for log in logs:
        if not isinstance(log, dict):
            continue
        by_day[log_day(log)] += 1
        by_account[str(log.get("account") or "Không tên")] += 1
        by_status[str(log.get("status") or "unknown")] += 1
        latest.append(log)

    latest.sort(key=lambda item: parse_log_datetime(item.get("end_time") or item.get("time") or item.get("start_time")) or datetime.min, reverse=True)
    return {
        "total": sum(by_status.values()),
        "by_day": dict(by_day),
        "by_account": dict(by_account),
        "by_status": dict(by_status),
        "latest": latest[:100],
    }
