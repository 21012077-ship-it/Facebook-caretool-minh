from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional


ACCOUNT_STATUSES = {"active", "checkpoint", "cookie_error", "proxy_error"}
CARE_PROFILES = {"auto", "warmup", "balanced", "reels_focus", "newsfeed_focus", "rest", "manual"}
PROXY_ACTION_COOLDOWN_HOURS = 24
_PROXY_LOCK_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _now_label() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M")


def _format_proxy_lock_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime(_PROXY_LOCK_TIME_FORMAT)


def parse_proxy_lock_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None

    for parser in (datetime.fromisoformat,):
        try:
            return parser(text)
        except ValueError:
            pass

    for date_format in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            pass
    return None


def mark_proxy_changed(account: Dict[str, Any], changed_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Mark an account as action-locked for 24h after a proxy change."""

    changed_at = changed_at or datetime.now()
    lock_until = changed_at + timedelta(hours=PROXY_ACTION_COOLDOWN_HOURS)
    account["proxy_changed_at"] = _format_proxy_lock_time(changed_at)
    account["proxy_action_locked_until"] = _format_proxy_lock_time(lock_until)
    return account


def proxy_action_lock_until(account: Dict[str, Any]) -> Optional[datetime]:
    return parse_proxy_lock_time(account.get("proxy_action_locked_until"))


def is_proxy_action_locked(account: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    lock_until = proxy_action_lock_until(account)
    if lock_until is None:
        return False
    return (now or datetime.now()) < lock_until


def proxy_lock_remaining_label(account: Dict[str, Any], now: Optional[datetime] = None) -> str:
    lock_until = proxy_action_lock_until(account)
    if lock_until is None:
        return ""

    remaining_seconds = int((lock_until - (now or datetime.now())).total_seconds())
    if remaining_seconds <= 0:
        return "0 phút"

    hours, remainder = divmod(remaining_seconds, 3600)
    minutes = max(1, (remainder + 59) // 60)
    if hours:
        return f"{hours} giờ {minutes} phút"
    return f"{minutes} phút"


def proxy_lock_until_label(account: Dict[str, Any]) -> str:
    lock_until = proxy_action_lock_until(account)
    if lock_until is None:
        return ""
    return lock_until.strftime("%d/%m/%Y %H:%M")


@dataclass(slots=True)
class Account:
    """Schema chuẩn cho một tài khoản Facebook trong tool."""

    name: str
    uid: str = ""
    password: str = ""
    two_fa: str = ""
    status: str = "active"
    note: str = ""
    proxy: str = ""
    proxy_changed_at: str = ""
    proxy_action_locked_until: str = ""
    cookie_file: str = ""
    created_at: str = field(default_factory=_now_label)
    last_open: str = "Chưa mở"
    last_care: str = "Chưa nuôi"
    care_profile: str = "auto"
    care_plan_note: str = ""
    last_error_reason: str = ""
    views_count: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Account":
        status = str(data.get("status") or "active")
        if status not in ACCOUNT_STATUSES:
            status = "active"
        care_profile = str(data.get("care_profile") or "auto")
        if care_profile not in CARE_PROFILES:
            care_profile = "auto"
        return cls(
            name=str(data.get("name") or data.get("uid") or "Không tên"),
            uid=str(data.get("uid") or ""),
            password=str(data.get("password") or ""),
            two_fa=str(data.get("two_fa") or data.get("twofa") or ""),
            status=status,
            note=str(data.get("note") or ""),
            proxy=str(data.get("proxy") or ""),
            proxy_changed_at=str(data.get("proxy_changed_at") or ""),
            proxy_action_locked_until=str(data.get("proxy_action_locked_until") or ""),
            cookie_file=str(data.get("cookie_file") or ""),
            created_at=str(data.get("created_at") or _now_label()),
            last_open=str(data.get("last_open") or "Chưa mở"),
            last_care=str(data.get("last_care") or "Chưa nuôi"),
            care_profile=care_profile,
            care_plan_note=str(data.get("care_plan_note") or ""),
            last_error_reason=str(data.get("last_error_reason") or ""),
            views_count=str(data.get("views_count") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LogEntry:
    """Schema linh hoạt cho log thao tác tài khoản."""

    account: str
    status: str
    action: str = ""
    start_time: str = ""
    end_time: str = ""
    time: str = ""
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LogEntry":
        known = {"account", "status", "action", "start_time", "end_time", "time", "error"}
        return cls(
            account=str(data.get("account") or ""),
            status=str(data.get("status") or ""),
            action=str(data.get("action") or ""),
            start_time=str(data.get("start_time") or ""),
            end_time=str(data.get("end_time") or ""),
            time=str(data.get("time") or ""),
            error=str(data.get("error") or ""),
            metadata={k: v for k, v in data.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        base = asdict(self)
        metadata = base.pop("metadata", {})
        base.update(metadata)
        return {k: v for k, v in base.items() if v not in ("", None, {})}
