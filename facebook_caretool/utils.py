from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import random
import re
import struct
import tempfile
import time as time_module
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Dict, Iterable, List, Optional, Tuple


def load_json(path: str | os.PathLike[str], default: Any) -> Any:
    json_path = Path(path)
    if not json_path.exists():
        return default
    try:
        with json_path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def dumps_json(data: Any) -> str:
    return json.dumps(data, indent=4, ensure_ascii=False)


def save_json(path: str | os.PathLike[str], data: Any) -> None:
    json_path = Path(path)
    if json_path.parent and str(json_path.parent) != ".":
        json_path.parent.mkdir(parents=True, exist_ok=True)

    payload = dumps_json(data)
    try:
        if json_path.exists() and json_path.read_text(encoding="utf-8").rstrip("\n") == payload:
            return
    except OSError:
        pass

    target_dir = json_path.parent if str(json_path.parent) != "" else Path(".")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target_dir, delete=False) as file:
        temp_path = Path(file.name)
        file.write(payload)
        file.write("\n")
    try:
        os.replace(temp_path, json_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


def generate_totp_code(
    secret: str | None,
    *,
    for_time: float | None = None,
    period: int = 30,
    digits: int = 6,
) -> str | None:
    """Generate a TOTP 2FA code from a Base32 secret using RFC 6238.

    The helper accepts secrets copied from Facebook with spaces, lower-case
    characters, or missing Base32 padding. It returns ``None`` for invalid
    secrets so the UI can show a friendly validation message instead of
    crashing during login automation.
    """
    normalized_secret = (secret or "").replace(" ", "").upper()
    if not normalized_secret or period <= 0 or digits <= 0:
        return None

    padding = len(normalized_secret) % 8
    if padding:
        normalized_secret += "=" * (8 - padding)

    try:
        key = base64.b32decode(normalized_secret, casefold=True)
    except (binascii.Error, ValueError):
        return None

    timestamp = time_module.time() if for_time is None else for_time
    counter = int(timestamp // period)
    counter_bytes = struct.pack(">Q", counter)
    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary_code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary_code % (10**digits)).zfill(digits)


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5"}


def _build_proxy_config(scheme: str, host: str, port: str, username: str = "", password: str = "") -> Dict[str, str]:
    if scheme not in SUPPORTED_PROXY_SCHEMES or not host or not port:
        raise ValueError(
            "Proxy không đúng định dạng. Dùng host:port, host:port:user:pass "
            "hoặc socks5://host:port."
        )

    config = {"server": f"{scheme}://{host}:{port}"}
    if username or password:
        if not username or not password:
            raise ValueError("Proxy có user/pass phải nhập đủ username và password.")
        config["username"] = username
        config["password"] = password
    return config


def parse_proxy(proxy_text: str | None) -> Optional[Dict[str, str]]:
    proxy_text = (proxy_text or "").strip()
    if not proxy_text:
        return None

    if "://" in proxy_text:
        parsed = urlsplit(proxy_text)
        scheme = parsed.scheme.lower()
        username = parsed.username or ""
        password = parsed.password or ""
        return _build_proxy_config(scheme, parsed.hostname or "", str(parsed.port or ""), username, password)

    parts = proxy_text.split(":")
    scheme = "http"
    if parts and parts[0].lower() in SUPPORTED_PROXY_SCHEMES:
        scheme = parts.pop(0).lower()
    elif parts and parts[-1].lower() in SUPPORTED_PROXY_SCHEMES:
        scheme = parts.pop(-1).lower()

    if len(parts) == 2 and all(parts):
        return _build_proxy_config(scheme, parts[0], parts[1])
    if len(parts) >= 4 and all(parts[:3]) and parts[3]:
        return _build_proxy_config(scheme, parts[0], parts[1], parts[2], ":".join(parts[3:]))
    raise ValueError(
        "Proxy không đúng định dạng. Dùng host:port, host:port:user:pass "
        "hoặc socks5://host:port."
    )


def parse_delay(delay_text: str | None, default: Tuple[float, float] = (4.0, 9.0)) -> Tuple[float, float]:
    text = (delay_text or "").strip()
    if not text:
        return default
    if "-" in text:
        left, right = text.split("-", 1)
        minimum = float(left.strip())
        maximum = float(right.strip())
    else:
        minimum = maximum = float(text)
    if minimum < 0 or maximum < 0 or minimum > maximum:
        raise ValueError("Delay phải là số dương hoặc khoảng min-max hợp lệ.")
    return minimum, maximum


def random_delay(delay_text: str | None, default: Tuple[float, float] = (4.0, 9.0)) -> float:
    minimum, maximum = parse_delay(delay_text, default)
    return random.uniform(minimum, maximum)



FACEBOOK_COMMENT_BLOCKLIST_PATTERNS = [
    r"https?://",
    r"www\.",
    r"\b(?:zalo|telegram|whatsapp|inbox|ib|dm|pm)\b",
    r"\b(?:mua ngay|giảm giá|khuyến mãi|chốt đơn|đặt hàng|kiếm tiền|tuyển sỉ|tuyển ctv)\b",
    r"\b\d{9,11}\b",
    r"[#@]{2,}",
]

COMMENT_CATEGORY_TEMPLATES: Dict[str, List[str]] = {
    "question": [
        "Câu hỏi này hay, mình cũng muốn xem thêm chia sẻ từ mọi người.",
        "Nội dung này đáng để thảo luận thêm, cảm ơn bạn đã nêu vấn đề.",
        "Mình thấy chủ đề này khá hữu ích, theo dõi thêm ý kiến của mọi người.",
    ],
    "congratulation": [
        "Chúc mừng bạn, thông tin rất tích cực và đáng vui.",
        "Tin vui quá, chúc mọi việc tiếp tục thuận lợi nhé.",
        "Chúc mừng thành quả này, cảm ơn bạn đã chia sẻ.",
    ],
    "support": [
        "Mong mọi việc sớm ổn hơn, cảm ơn bạn đã chia sẻ thông tin.",
        "Chúc bạn và mọi người thật nhiều sức khỏe, hy vọng mọi chuyện sẽ tốt hơn.",
        "Đọc nội dung thấy rất cần sự cảm thông, mong mọi việc sớm ổn định.",
    ],
    "learning": [
        "Bài viết hữu ích, mình lưu lại để đọc kỹ hơn.",
        "Cảm ơn bạn đã chia sẻ thông tin rõ ràng và thiết thực.",
        "Nội dung này có nhiều ý đáng tham khảo, cảm ơn bạn.",
    ],
    "event": [
        "Sự kiện này đáng chú ý, cảm ơn bạn đã cập nhật thông tin.",
        "Thông tin rất kịp thời, mình sẽ theo dõi thêm.",
        "Cập nhật hữu ích, cảm ơn bạn đã chia sẻ.",
    ],
    "default": [
        "Bài viết hay và đáng quan tâm, cảm ơn bạn đã chia sẻ.",
        "Nội dung khá hữu ích, mình sẽ theo dõi thêm.",
        "Cảm ơn bạn đã chia sẻ, thông tin này rất đáng tham khảo.",
    ],
}


def normalize_comment_text(text: str | None) -> str:
    """Chuẩn hóa comment để giảm dấu hiệu spam/lặp ký tự quá mức."""
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    normalized = re.sub(r"([!?.,])\1{1,}", r"\1", normalized)
    normalized = re.sub(r"([😀-🙏🚀-🛿])\1{1,}", r"\1", normalized)
    return normalized[:220].strip()


def is_facebook_standard_comment(text: str | None) -> bool:
    """Kiểm tra nhanh comment có tự nhiên, ngắn gọn và ít dấu hiệu quảng cáo/spam."""
    normalized = normalize_comment_text(text)
    if len(normalized) < 12 or len(normalized) > 220:
        return False
    if normalized.isupper() and len(normalized) > 20:
        return False
    lower_text = normalized.lower()
    return not any(re.search(pattern, lower_text, re.IGNORECASE) for pattern in FACEBOOK_COMMENT_BLOCKLIST_PATTERNS)


def detect_facebook_post_category(post_text: str | None) -> str:
    """Phân loại nhẹ nội dung bài viết để chọn comment phù hợp ngữ cảnh."""
    text = (post_text or "").lower()
    if not text:
        return "default"
    if "?" in text or any(word in text for word in ("hỏi", "xin ý kiến", "theo bạn", "mọi người nghĩ", "nên chọn")):
        return "question"
    if any(word in text for word in ("chúc mừng", "khai trương", "thành công", "đạt được", "vinh danh", "tốt nghiệp")):
        return "congratulation"
    if any(word in text for word in ("chia buồn", "tai nạn", "khó khăn", "bệnh", "mất", "qua đời", "cầu mong", "ủng hộ")):
        return "support"
    if any(word in text for word in ("hướng dẫn", "cách", "kinh nghiệm", "mẹo", "lưu ý", "kiến thức", "tutorial")):
        return "learning"
    if any(word in text for word in ("sự kiện", "thông báo", "cập nhật", "ra mắt", "livestream", "hôm nay", "ngày mai")):
        return "event"
    return "default"


def build_contextual_facebook_comment(
    post_text: str | None,
    fallback_comment: str = "",
    *,
    chooser=random.choice,
) -> str:
    """Tạo comment ngắn, liên quan nội dung và tránh dấu hiệu spam thường gặp.

    Hàm ưu tiên comment sinh theo ngữ cảnh bài viết. Nếu không đọc được bài,
    comment fallback của người dùng chỉ được dùng khi vượt kiểm tra an toàn cơ bản.
    """
    normalized_post = re.sub(r"\s+", " ", (post_text or "")).strip()
    fallback = normalize_comment_text(fallback_comment)
    if not normalized_post and is_facebook_standard_comment(fallback):
        return fallback

    category = detect_facebook_post_category(normalized_post)
    templates = COMMENT_CATEGORY_TEMPLATES.get(category, COMMENT_CATEGORY_TEMPLATES["default"])
    comment = normalize_comment_text(chooser(templates))
    if is_facebook_standard_comment(comment):
        return comment
    if is_facebook_standard_comment(fallback):
        return fallback
    return COMMENT_CATEGORY_TEMPLATES["default"][0]


AI_COMMENT_SYSTEM_PROMPT = """Bạn viết bình luận Facebook tự nhiên, lịch sự và liên quan trực tiếp đến bài viết.
Yêu cầu: chỉ trả về đúng 1 bình luận tiếng Việt, 1 câu ngắn 40-160 ký tự; không quảng cáo,
không kêu gọi inbox/mua hàng, không link, không hashtag, không tag, không số điện thoại,
không spam emoji/dấu câu, không cam kết Facebook sẽ hiển thị bình luận."""


def extract_ai_comment_text(response_payload: Dict[str, Any]) -> str:
    """Lấy text bình luận từ payload Chat Completions/OpenAI-compatible."""
    try:
        content = response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return normalize_comment_text(str(content).strip().strip('"“”'))


def generate_ai_facebook_comment(
    post_text: str | None,
    fallback_comment: str = "",
    *,
    api_key: str = "",
    model: str = "gpt-4o-mini",
    base_url: str = "https://api.openai.com/v1/chat/completions",
    temperature: float = 0.9,
    timeout: float = 25,
    requester: Any = None,
) -> str | None:
    """Gọi AI để tạo comment ngẫu nhiên theo nội dung bài viết.

    Trả về ``None`` khi thiếu cấu hình hoặc API lỗi để UI dùng fallback an toàn.
    Comment AI vẫn được kiểm tra qua bộ lọc spam cơ bản trước khi sử dụng.
    """
    normalized_post = re.sub(r"\s+", " ", (post_text or "")).strip()[:1800]
    if not normalized_post or not api_key.strip() or not model.strip() or not base_url.strip():
        return None

    safe_fallback = normalize_comment_text(fallback_comment)
    variant_id = random.randint(1000, 9999)
    payload = {
        "model": model.strip(),
        "temperature": max(0.0, min(float(temperature), 1.5)),
        "max_tokens": 90,
        "messages": [
            {"role": "system", "content": AI_COMMENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Hãy đọc nội dung bài viết dưới đây và tạo một bình luận mới, ngẫu nhiên, "
                    "phù hợp ngữ cảnh nhất. Không sao chép nguyên văn bài viết. "
                    f"Mã biến thể để tránh lặp: {variant_id}.\n\n"
                    f"Nội dung bài viết:\n{normalized_post}\n\n"
                    f"Mẫu dự phòng tham khảo nếu phù hợp: {safe_fallback}"
                ),
            },
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url.strip(),
        data=data,
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        response = requester(request, timeout=timeout) if requester else urllib.request.urlopen(request, timeout=timeout)
        try:
            raw_response = response.read().decode("utf-8")
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        candidate = extract_ai_comment_text(json.loads(raw_response))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError, TypeError):
        return None

    if is_facebook_standard_comment(candidate):
        return candidate
    return None

def build_comment_payloads(raw_content: str, media_paths: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Ghép nội dung comment với ảnh/video thành từng gói không tách rời.

    Khi có nhiều ảnh/video, mỗi dòng comment nhận một file theo thứ tự/cycle.
    Nhờ vậy lúc random chỉ random cả gói text + media thay vì random riêng
    comment và ảnh, tránh gửi nhầm ảnh không đi cùng nội dung đã chuẩn bị.
    """
    comments = [line.strip() for line in raw_content.split("\n") if line.strip()]
    media = [path for path in (media_paths or []) if path]
    payloads: List[Dict[str, str]] = []
    for index, comment in enumerate(comments):
        payload = {"text": comment, "media_path": ""}
        if media:
            payload["media_path"] = media[index % len(media)]
        payloads.append(payload)
    return payloads


def spin_content(text: str, chooser=random.choice) -> str:
    def spin(match: re.Match[str]) -> str:
        options = [option.strip() for option in match.group(1).split("|")]
        options = [option for option in options if option]
        if not options:
            return ""
        return chooser(options)

    previous = None
    current = text
    while previous != current:
        previous = current
        current = re.sub(r"\{([^{}]*)\}", spin, current)
    return current
