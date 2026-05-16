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

FACEBOOK_UI_NOISE_PATTERNS = [
    r"\b(?:thích|like|bình luận|comment|chia sẻ|share|phản hồi|reply)\b",
    r"\b(?:xem thêm|see more|ẩn bớt|view more|follow|theo dõi)\b",
    r"\b(?:giờ|phút|ngày|tuần)\s*(?:trước)?\b",
    r"\bsố\s+thông\s+báo\s+chưa\s+đọc\b",
    r"\b(?:menu|facebook|messenger|watch|reels|trang chủ|home)\b",
]

VIETNAMESE_TOPIC_STOPWORDS = {
    "anh",
    "bài",
    "bạn",
    "bằng",
    "các",
    "cách",
    "cho",
    "còn",
    "có",
    "cũng",
    "của",
    "đang",
    "để",
    "đến",
    "đi",
    "đó",
    "được",
    "em",
    "hơn",
    "khi",
    "là",
    "lại",
    "làm",
    "mà",
    "mình",
    "mọi",
    "một",
    "nào",
    "này",
    "nên",
    "nghĩ",
    "người",
    "nhé",
    "những",
    "nói",
    "nữa",
    "qua",
    "rất",
    "rồi",
    "sẽ",
    "thì",
    "thêm",
    "theo",
    "thấy",
    "trên",
    "trong",
    "và",
    "về",
    "vì",
    "với",
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


def clean_scanned_post_text(post_text: str | None) -> str:
    """Lọc bớt chữ giao diện Facebook để phần tạo comment bám vào nội dung bài."""
    text = re.sub(r"https?://\S+|www\.\S+", " ", post_text or "")
    text = re.sub(r"[#@][\wÀ-ỹ_]+", " ", text)
    for pattern in FACEBOOK_UI_NOISE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" -–—|•\n\t")


FACEBOOK_POST_SCAN_BOUNDARY_PATTERNS = [
    r"\b(?:thích|like)\s+(?:bình luận|comment)\s+(?:chia sẻ|share)\b",
    r"\b(?:viết bình luận|write a comment|most relevant|phù hợp nhất|all comments|tất cả bình luận)\b",
    r"\b(?:xem thêm bình luận|view more comments|xem các bình luận|view previous comments)\b",
]

FACEBOOK_POST_SCAN_NEGATIVE_PATTERNS = [
    r"\b(?:số thông báo chưa đọc|menu|messenger|watch|reels|trang chủ|home)\b",
    r"\b(?:viết bình luận|write a comment|phù hợp nhất|most relevant|all comments|tất cả bình luận)\b",
    r"\b(?:phản hồi|reply)\b",
    r"\b(?:đã thích|liked by|notifications?|thông báo)\b",
]


def trim_scanned_post_candidate(text: str | None) -> str:
    """Cắt phần text quét được ở ranh giới khu comment/UI để giữ nội dung bài viết."""
    candidate = re.sub(r"\s+", " ", text or "").strip()
    if not candidate:
        return ""

    boundary_indexes = []
    for pattern in FACEBOOK_POST_SCAN_BOUNDARY_PATTERNS:
        match = re.search(pattern, candidate, flags=re.IGNORECASE)
        if match:
            boundary_indexes.append(match.start())
    if boundary_indexes:
        candidate = candidate[: min(boundary_indexes)].strip()

    return candidate.strip(" -–—|•\n\t")


def score_scanned_post_candidate(text: str | None) -> int:
    """Chấm điểm ứng viên nội dung bài để tránh lấy nhầm menu/comment của Facebook."""
    candidate = trim_scanned_post_candidate(text)
    if not candidate:
        return -1000

    cleaned = clean_scanned_post_text(candidate)
    words = re.findall(r"[A-Za-zÀ-ỹ0-9]+", cleaned)
    if len(words) < 4:
        return -500

    lower_candidate = candidate.lower()
    score = min(len(cleaned), 600)
    score += min(len(words), 80) * 4

    if re.search(r"[.!?…]", cleaned):
        score += 35
    if any(keyword in lower_candidate for keyword in ("hướng dẫn", "chia sẻ", "câu chuyện", "kinh nghiệm", "thông báo", "sự kiện", "mẹo")):
        score += 30

    for pattern in FACEBOOK_POST_SCAN_NEGATIVE_PATTERNS:
        score -= 160 * len(re.findall(pattern, lower_candidate, flags=re.IGNORECASE))

    # Ứng viên quá dài thường là cả article/body gồm cả comment và thanh điều hướng.
    if len(candidate) > 1800:
        score -= 220
    elif len(candidate) > 1000:
        score -= 90

    return score


def select_relevant_scanned_post_text(candidates: list[str] | tuple[str, ...] | None) -> str:
    """Chọn đoạn giống nội dung bài viết nhất từ các đoạn DOM quét được.

    Facebook thường trả về cả thanh nút, bộ lọc bình luận hoặc comment bên dưới bài.
    Hàm này ưu tiên đoạn nội dung chính và cắt bỏ phần sau cụm nút Thích/Bình luận/Chia sẻ.
    """
    best_text = ""
    best_score = -1000
    seen: set[str] = set()

    for raw_candidate in candidates or []:
        candidate = trim_scanned_post_candidate(raw_candidate)
        if not candidate:
            continue
        compact_key = re.sub(r"\W+", "", candidate.lower())[:180]
        if compact_key in seen:
            continue
        seen.add(compact_key)

        score = score_scanned_post_candidate(candidate)
        if score > best_score:
            best_score = score
            best_text = candidate

    return best_text[:1800].strip() if best_score > 0 else ""

def extract_post_focus(post_text: str | None, max_words: int = 10) -> str:
    """Rút một cụm ý nổi bật từ bài viết để comment không còn là câu mẫu cố định."""
    cleaned = clean_scanned_post_text(post_text)
    if not cleaned:
        return ""

    sentence_candidates = [
        sentence.strip(" -–—:;,.!?\n\t")
        for sentence in re.split(r"(?<=[.!?…])\s+|\n+", cleaned)
        if sentence.strip(" -–—:;,.!?\n\t")
    ]
    sentence_candidates = [
        sentence
        for sentence in sentence_candidates
        if len(sentence) >= 18 and not re.fullmatch(r"[\d\W_]+", sentence)
    ]
    chosen_sentence = sentence_candidates[0] if sentence_candidates else cleaned

    words = re.findall(r"[A-Za-zÀ-ỹ0-9]+", chosen_sentence)
    topic_words: List[str] = []
    for word in words:
        lowered = word.lower()
        if len(lowered) <= 1 or lowered in VIETNAMESE_TOPIC_STOPWORDS:
            continue
        topic_words.append(word)
        if len(topic_words) >= max_words:
            break

    if len(topic_words) >= 3:
        return " ".join(topic_words).strip()

    # Nếu sau khi lọc chỉ còn 1-2 từ (thường là tên tài khoản/page hoặc chữ giao diện),
    # không dùng làm trọng tâm để tránh sinh comment bám vào UI như "Số thông báo chưa đọc Menu Facebook...".
    return ""


def build_contextual_facebook_comment(
    post_text: str | None,
    fallback_comment: str = "",
    *,
    chooser=random.choice,
) -> str:
    """Tạo comment ngắn dựa trực tiếp vào nội dung quét được, không chọn từ bộ câu mẫu cố định.

    Khi không có API AI, hàm vẫn rút một cụm ý nổi bật trong bài rồi viết lại thành
    một phản hồi tự nhiên. Nếu không đọc được bài, fallback của người dùng chỉ được
    dùng khi vượt kiểm tra an toàn cơ bản.
    """
    normalized_post = clean_scanned_post_text(post_text)
    fallback = normalize_comment_text(fallback_comment)
    if not normalized_post and is_facebook_standard_comment(fallback):
        return fallback

    focus = extract_post_focus(normalized_post)
    if focus:
        if "?" in normalized_post:
            comment = (
                f"Với ý về {focus}, mình nghĩ nên nhìn theo từng tình huống thực tế "
                "vì mỗi người có thể gặp một bối cảnh khác nhau."
            )
        else:
            comment = (
                f"Mình thấy phần {focus} khá đáng chú ý, vì nó gợi ra một góc nhìn "
                "rất dễ liên hệ với thực tế."
            )
        comment = normalize_comment_text(comment)
        if is_facebook_standard_comment(comment):
            return comment

    if is_facebook_standard_comment(fallback):
        return fallback
    return "Mình thấy nội dung này có vài điểm đáng suy nghĩ và khá dễ liên hệ với thực tế."


AI_COMMENT_SYSTEM_PROMPT = """Bạn viết bình luận Facebook tự nhiên, lịch sự và liên quan trực tiếp đến bài viết.
Hãy đọc bài, tự suy nghĩ một ý phù hợp với nội dung cụ thể rồi viết như người thật đang phản hồi.
Không dùng câu mẫu chung chung như "bài viết hay", "cảm ơn đã chia sẻ", "đáng bàn thêm" nếu không nêu rõ ý trong bài.
Yêu cầu: chỉ trả về đúng 1 bình luận tiếng Việt, 1 câu ngắn 50-180 ký tự; không quảng cáo,
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
                    "Hãy quét và hiểu nội dung bài viết dưới đây, sau đó nghĩ ra một bình luận mới "
                    "phù hợp riêng với bài này như người thật đang đọc rồi phản hồi. "
                    "Bình luận phải nhắc hoặc bám vào một ý cụ thể trong bài, không dùng câu mẫu chung chung. "
                    "Không sao chép nguyên văn bài viết. "
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
