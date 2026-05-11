from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

CARE_PROFILE_LABELS = {
    "auto": "Tự động thông minh",
    "warmup": "Khởi động nhẹ",
    "balanced": "Cân bằng",
    "reels_focus": "Ưu tiên Reels",
    "newsfeed_focus": "Ưu tiên Newsfeed",
    "rest": "Nghỉ / theo dõi",
    "manual": "Theo cấu hình chung",
}

CARE_PROFILE_OPTIONS = list(CARE_PROFILE_LABELS.keys())


def profile_label(profile: str | None) -> str:
    return CARE_PROFILE_LABELS.get(str(profile or "auto"), CARE_PROFILE_LABELS["auto"])


def parse_vietnamese_datetime(value: str | None, now: Optional[datetime] = None) -> Optional[datetime]:
    if not value or value in {"Chưa nuôi", "Chưa mở", "Chưa tương tác"}:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def days_since(value: str | None, now: Optional[datetime] = None) -> Optional[int]:
    now = now or datetime.now()
    parsed = parse_vietnamese_datetime(value, now)
    if not parsed:
        return None
    return max(0, (now - parsed).days)


def clamp_minutes(value: int, minimum: int = 0, maximum: int = 30) -> int:
    return max(minimum, min(maximum, int(value)))


def recommend_care_profile(account: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """Chọn kiểu nuôi an toàn dựa trên trạng thái, lịch sử và ghi chú của từng tài khoản."""
    now = now or datetime.now()
    status = account.get("status", "active")
    note = str(account.get("note", "")).lower()
    last_care_days = days_since(account.get("last_care"), now)

    if status in {"checkpoint", "cookie_error"}:
        return "rest"
    if any(keyword in note for keyword in ("nghỉ", "nghi", "rest", "tạm dừng", "tam dung", "hold")):
        return "rest"
    if last_care_days is None or any(keyword in note for keyword in ("new", "mới", "moi", "clone")):
        return "warmup"
    if last_care_days >= 7:
        return "warmup"
    if any(keyword in note for keyword in ("reels", "video", "watch")):
        return "reels_focus"
    if any(keyword in note for keyword in ("feed", "newsfeed", "bảng tin", "bang tin")):
        return "newsfeed_focus"
    return "balanced"


def build_care_plan(account: Dict[str, Any], global_settings: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Tạo kế hoạch nuôi riêng cho một account, vẫn tôn trọng giới hạn cấu hình chung."""
    selected_profile = account.get("care_profile") or "auto"
    profile = recommend_care_profile(account, now) if selected_profile == "auto" else selected_profile

    max_newsfeed = clamp_minutes(global_settings.get("newsfeed_minutes", 0))
    max_reels = clamp_minutes(global_settings.get("reels_minutes", 0))
    pause_range = global_settings.get("pause_range", "4-9")
    auto_like = bool(global_settings.get("auto_like", False))
    read_notifications = bool(global_settings.get("read_notifications", False))
    join_groups = bool(global_settings.get("join_groups", False))
    try:
        join_group_chance = float(global_settings.get("join_group_chance", 0.35))
    except (TypeError, ValueError):
        join_group_chance = 0.35
    join_group_chance = max(0.0, min(1.0, join_group_chance))
    try:
        max_join_groups = int(global_settings.get("max_join_groups", 2))
    except (TypeError, ValueError):
        max_join_groups = 2
    max_join_groups = max(0, min(2, max_join_groups))

    templates = {
        "warmup": {
            "newsfeed_minutes": min(max_newsfeed, 3) if max_newsfeed else 0,
            "reels_minutes": min(max_reels, 2) if max_reels else 0,
            "pause_range": "10-20",
            "auto_like": False,
            "read_notifications": read_notifications,
            "join_groups": False,
            "join_group_chance": 0.0,
            "max_join_groups": 0,
            "reason": "Acc mới/lâu chưa nuôi nên chỉ lướt nhẹ, có thể đọc thông báo nhưng không tự like/tham gia nhóm.",
        },
        "balanced": {
            "newsfeed_minutes": min(max_newsfeed, 8) if max_newsfeed else 0,
            "reels_minutes": min(max_reels, 5) if max_reels else 0,
            "pause_range": pause_range,
            "auto_like": auto_like,
            "read_notifications": read_notifications,
            "join_groups": join_groups,
            "join_group_chance": join_group_chance,
            "max_join_groups": max_join_groups,
            "reason": "Acc hoạt động bình thường nên chia đều Newsfeed/Reels, đọc thông báo và thỉnh thoảng tham gia nhóm nếu bật.",
        },
        "reels_focus": {
            "newsfeed_minutes": min(max_newsfeed, 3) if max_newsfeed else 0,
            "reels_minutes": min(max_reels, 10) if max_reels else 0,
            "pause_range": pause_range,
            "auto_like": auto_like,
            "read_notifications": read_notifications,
            "join_groups": join_groups,
            "join_group_chance": join_group_chance,
            "max_join_groups": max_join_groups,
            "reason": "Ghi chú/nhu cầu ưu tiên video nên tăng thời lượng Reels, vẫn có thể đọc thông báo/tham gia nhóm nhẹ.",
        },
        "newsfeed_focus": {
            "newsfeed_minutes": min(max_newsfeed, 10) if max_newsfeed else 0,
            "reels_minutes": min(max_reels, 2) if max_reels else 0,
            "pause_range": pause_range,
            "auto_like": auto_like,
            "read_notifications": read_notifications,
            "join_groups": join_groups,
            "join_group_chance": join_group_chance,
            "max_join_groups": max_join_groups,
            "reason": "Ghi chú/nhu cầu ưu tiên bảng tin nên tăng thời lượng Newsfeed, vẫn có thể đọc thông báo/tham gia nhóm nhẹ.",
        },
        "rest": {
            "newsfeed_minutes": 0,
            "reels_minutes": 0,
            "pause_range": "10-20",
            "auto_like": False,
            "read_notifications": False,
            "join_groups": False,
            "join_group_chance": 0.0,
            "max_join_groups": 0,
            "reason": "Acc checkpoint/die hoặc được đánh dấu nghỉ nên không chạy nuôi tự động.",
        },
        "manual": {
            "newsfeed_minutes": max_newsfeed,
            "reels_minutes": max_reels,
            "pause_range": pause_range,
            "auto_like": auto_like,
            "read_notifications": read_notifications,
            "join_groups": join_groups,
            "join_group_chance": join_group_chance,
            "max_join_groups": max_join_groups,
            "reason": "Dùng đúng cấu hình chung bạn đang chọn.",
        },
    }
    plan = dict(templates.get(profile, templates["balanced"]))
    plan["profile"] = profile
    plan["profile_label"] = profile_label(profile)
    return plan


def format_care_plan(plan: Dict[str, Any]) -> str:
    like_text = "bật like" if plan.get("auto_like") else "không like"
    notification_text = "đọc thông báo" if plan.get("read_notifications") else "không đọc thông báo"
    if plan.get("join_groups") and plan.get("max_join_groups", 0) > 0:
        group_text = f"thi thoảng tham gia 1-{plan.get('max_join_groups', 2)} group"
    else:
        group_text = "không tham gia group"
    return (
        f"{plan.get('profile_label', 'Không rõ')}: "
        f"Newsfeed {plan.get('newsfeed_minutes', 0)}p, "
        f"Reels {plan.get('reels_minutes', 0)}p, "
        f"nghỉ {plan.get('pause_range', '4-9')}s, {like_text}, "
        f"{notification_text}, {group_text}."
    )
