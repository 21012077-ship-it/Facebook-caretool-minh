from __future__ import annotations

import heapq
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

DATE_FORMATS = ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def parse_log_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    # Try ISO format with timezone first (e.g. 2024-01-15T10:30:00+07:00)
    try:
        dt = datetime.fromisoformat(text)
        # Convert to naive local for consistency
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def log_day(log: Dict[str, Any]) -> str | None:
    for key in ("start_time", "end_time", "time"):
        parsed = parse_log_datetime(log.get(key))
        if parsed:
            return parsed.strftime("%d/%m/%Y")
    return None  # Return None instead of a string to avoid polluting day counters


def summarize_accounts(accounts: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter(str(account.get("status") or "active") for account in accounts)
    result: Dict[str, int] = {
        "total": sum(counts.values()),
        "active": counts.get("active", 0),
        "checkpoint": counts.get("checkpoint", 0),
        "cookie_error": counts.get("cookie_error", 0),
    }
    # Include any additional statuses dynamically
    for status, count in counts.items():
        if status not in result:
            result[status] = count
    return result


def summarize_logs(logs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    by_day: Counter[str] = Counter()
    by_account: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    all_logs: list[Dict[str, Any]] = []

    for log in logs:
        if not isinstance(log, dict):
            continue
        day = log_day(log)
        if day:  # Only count logs with valid dates
            by_day[day] += 1
        by_account[str(log.get("account") or "Không tên")] += 1
        by_status[str(log.get("status") or "unknown")] += 1
        all_logs.append(log)

    def _sort_key(item: Dict[str, Any]) -> datetime:
        return (
            parse_log_datetime(item.get("end_time") or item.get("time") or item.get("start_time"))
            or datetime.min
        )

    # Use heapq.nlargest for O(n log k) instead of sorting entire list O(n log n)
    latest = heapq.nlargest(100, all_logs, key=_sort_key)

    return {
        "total": sum(by_status.values()),
        "by_day": dict(by_day),
        "by_account": dict(by_account),
        "by_status": dict(by_status),
        "latest": latest,
    }
