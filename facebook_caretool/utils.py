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
    r"\b(?:meta\s+ai|bạn\s+bè|friends|công\s+cụ\s+chuyên\s+nghiệp|professional\s+dashboard)\b",
    r"\b(?:kỷ\s+niệm|memories|đã\s+lưu|saved|nhóm|groups|thước\s+phim|marketplace|bảng\s+feed|bảng|feed|netflix|netfix)\b",
    r"\b(?:menu|facebook|messenger|watch|reels|trang chủ|home|thông báo|notifications)\b",
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
    """Không còn sinh comment fallback; giữ hàm để tương thích import cũ."""
    return ""


AI_COMMENT_SYSTEM_PROMPT = """Bạn chỉ tạo đúng 1 bình luận Facebook tiếng Việt tự nhiên, bám sát ngữ cảnh bài viết. Nếu dữ liệu không đủ rõ để comment hợp lý, trả về đúng SKIP_COMMENT. Không giải thích."""

AI_COMMENT_BANNED_PATTERNS = [
    r"mình nghĩ nên",
    r"từng tình huống",
    r"mỗi người có thể",
    r"góc nhìn khác nhau",
    r"đáng suy ngẫm",
    r"vấn đề thú vị",
    r"rất đồng tình",
]


def build_ai_comment_prompt(post_text: str | None, target_comment: str | None = None) -> str:
    normalized_post = re.sub(r"\s+", " ", (post_text or "")).strip()[:3500]
    normalized_target_comment = re.sub(r"\s+", " ", (target_comment or "")).strip()[:1200]
    if normalized_target_comment:
        return f"""Bạn là một người trẻ Việt Nam thường xuyên lướt Facebook và trả lời comment rất tự nhiên.

Luồng bắt buộc:
Bước 1: Đọc kỹ nội dung bài viết và hình ảnh/thumbnail nếu có.
Bước 2: Đọc kỹ comment đang cần trả lời.
Bước 3: Viết đúng 1 câu phản hồi vừa liên quan tới bài viết, vừa ăn khớp trực tiếp với comment đó.

Nhiệm vụ:
Dựa trên cả 2 phần ngữ cảnh dưới đây, hãy viết 1 reply vào comment. Reply phải nghe như người thật đang phản hồi comment trong thread, không phải comment mới độc lập vào bài.

Phong cách mong muốn:
- Tiếng Việt tự nhiên, Gen Z vừa phải, hơi đời
- Bám sát chi tiết cụ thể của bài viết và ý của comment cần trả lời
- Có thể đồng tình, trêu nhẹ, nối ý, bắt miếng hoặc bổ sung ngắn gọn
- Không công kích cá nhân, không chửi tục nặng, không gây war
- Không viết như chatbot, không văn mẫu, không nghị luận dài

Yêu cầu bắt buộc:
- Chỉ viết 1 reply duy nhất
- Bắt buộc độ dài: Phải viết thành một câu hoàn chỉnh từ 7 đến 25 từ.
- Không lặp lại nguyên văn caption hoặc comment gốc
- Không giải thích, không thêm dấu ngoặc kép
- Không thêm tiền tố như “Reply:” hoặc “Comment:”
- Nếu thiếu nội dung bài viết hoặc không quét được comment cần trả lời, trả về đúng SKIP_COMMENT.

Dữ liệu bài viết:
- Page/account name: (đã gộp trong dữ liệu quét nếu lấy được)
- Post text: {normalized_post or '(không lấy được)'}
- Hashtags: (đã gộp trong dữ liệu quét nếu có)
- Image/video thumbnail text nếu có: (đã gộp trong dữ liệu quét nếu lấy được)

Comment cần trả lời:
- {normalized_target_comment or '(không lấy được)'}

Hãy trả về đúng 1 reply phù hợp với cả bài viết và comment cần trả lời."""

    return f"""Bạn là một người trẻ Việt Nam thường xuyên lướt Facebook và bình luận rất tự nhiên.

Nhiệm vụ:
Đọc kỹ toàn bộ ngữ cảnh của bài đăng Facebook, bao gồm:
- Tên page hoặc tài khoản đăng bài
- Nội dung caption/post text
- Hashtag
- Chữ trong ảnh hoặc thumbnail video nếu có

Sau đó viết ra đúng 1 bình luận phù hợp nhất với bài đăng.

Phong cách bình luận mong muốn:
- Kiểu Gen Z Việt Nam, tự nhiên, hơi đời
- Giống người thật lướt thấy bài rồi comment ngay
- Bám rất sát nội dung cụ thể của bài
- Có thể hùa theo, thả miếng, trêu nhẹ, cà khịa nhẹ, bắt đúng tình huống gây cười hoặc chi tiết nổi bật
- Ưu tiên những câu khiến người đọc thấy “comment này đúng bài ghê”
- Không viết như chatbot
- Không văn mẫu
- Không nghị luận dài dòng

Yêu cầu bắt buộc:
- Chỉ viết 1 comment duy nhất
- Bắt buộc độ dài: Phải viết thành một câu hoàn chỉnh từ 7 đến 25 từ. Tuyệt đối không bình luận cụt lủn 1, 2 chữ.
- Bám sát nội dung cụ thể của bài, không viết kiểu chung chung như “hay quá”, “đỉnh thật”, “xịn nha”
- Không lặp lại nguyên văn caption
- Không giải thích, không thêm dấu ngoặc kép
- Không cố nhồi trend nếu không hợp ngữ cảnh
- Không câu nào cũng phải có emoji
- Nếu dùng emoji thì chỉ 0–1 emoji là đủ
- Trả về SKIP_COMMENT nếu không có nội dung rõ ràng để bình luận.
- Không dùng kiểu giọng AI như:
  + "mình nghĩ nên nhìn theo từng tình huống thực tế"
  + "mỗi người có thể có một góc nhìn khác nhau"
  + "nội dung này rất đáng suy ngẫm"
  + "đây là một vấn đề thú vị"
  + "rất đồng tình với quan điểm này"
- Không dùng các lời khen rỗng như:
  + "hay quá"
  + "đỉnh thật"
  + "tuyệt vời"
  + "xịn nha"
  nếu không thực sự hợp ngữ cảnh
- Không lạm dụng emoji
- Nếu dùng emoji thì chỉ 0 hoặc 1 emoji
- Có thể dùng khẩu ngữ tự nhiên nếu hợp bài, nhưng hãy ghép thành câu đủ ý, ví dụ:
  + trời ơi chi tiết này nhìn là thấy có mùi rồi nha
  + pha này lộ quá rồi, ai mà chịu nổi được chứ
  + nói vậy ai tin, nhìn phản ứng là biết liền rồi
  + tình huống này không ổn nha, tới công chuyện thật rồi
  + cười kiểu này là dở rồi, chắc còn drama tiếp đây

Cách định hướng bình luận:
- Nếu bài là meme/phim/tình huống hài: phản ứng vui, bắt đúng chi tiết gây cười
- Nếu bài có tình huống yêu đương/thả thính/couple: trêu nhẹ, tinh nghịch
- Nếu bài có drama nhẹ: hóng hớt vừa phải, không công kích
- Nếu bài cảm xúc: đồng cảm ngắn gọn, tự nhiên
- Nếu bài quảng bá phim/chương trình: bình luận như một người xem đang phản ứng vào nội dung thú vị của bài, không viết kiểu quảng cáo
- Nếu bài đăng không đủ ngữ cảnh, dữ liệu quét bị rác hoặc không hiểu được nội dung, trả về chính xác chuỗi:
SKIP_COMMENT

Dữ liệu bài viết:
- Page/account name: (đã gộp trong dữ liệu quét nếu lấy được)
- Post text: {normalized_post or '(không lấy được)'}
- Hashtags: (đã gộp trong dữ liệu quét nếu có)
- Image/video thumbnail text nếu có: (đã gộp trong dữ liệu quét nếu lấy được)

Hãy trả về đúng 1 bình luận phù hợp nhất."""


def validate_ai_comment(candidate: str | None, *, min_words: int = 1) -> tuple[bool, str]:
    normalized = normalize_comment_text(candidate)
    if not normalized:
        return False, "empty"
    if normalized == "SKIP_COMMENT":
        return False, "skip"
    if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in AI_COMMENT_BANNED_PATTERNS):
        return False, "generic"
    word_count = len(normalized.split())
    if word_count < min_words:
        return False, "too_short"
    if len(normalized) > 220 or word_count > 35:
        return False, "too_long"
    if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in FACEBOOK_COMMENT_BLOCKLIST_PATTERNS):
        return False, "spam_filter"
    return True, ""

def generate_ai_facebook_comment(
    post_text: str | None,
    fallback_comment: str = "",
    *,
    api_key: str = "",
    model: str = "gpt-4o-mini",
    base_url: str = "",
    temperature: float = 0.9,
    timeout: float = 25,
    requester: Any = None,
) -> str | None:
    """Không còn gọi API AI; giữ hàm cũ để báo migration rõ ràng.

    Luồng comment mới phải dùng :func:`build_ai_comment_prompt` rồi paste prompt
    vào https://chatgpt.com trong browser đã đăng nhập cookie. Các tham số API
    được giữ lại chỉ để không phá import cũ, nhưng tuyệt đối không tạo request
    OpenAI/Gemini tại đây.
    """
    raise ValueError(
        "Đã tắt hoàn toàn gọi API OpenAI/Gemini. Hãy dùng luồng ChatGPT thủ công "
        "trên chatgpt.com bằng cookie trình duyệt."
    )

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
