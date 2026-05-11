from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict


ACCOUNT_STATUSES = {"active", "checkpoint", "cookie_error"}
CARE_PROFILES = {"auto", "warmup", "balanced", "reels_focus", "newsfeed_focus", "rest", "manual"}


def _now_label() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M")


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
    cookie_file: str = ""
    created_at: str = field(default_factory=_now_label)
    last_open: str = "Chưa mở"
    last_care: str = "Chưa nuôi"
    care_profile: str = "auto"
    care_plan_note: str = ""

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
            cookie_file=str(data.get("cookie_file") or ""),
            created_at=str(data.get("created_at") or _now_label()),
            last_open=str(data.get("last_open") or "Chưa mở"),
            last_care=str(data.get("last_care") or "Chưa nuôi"),
            care_profile=care_profile,
            care_plan_note=str(data.get("care_plan_note") or ""),
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
