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
    """Deprecated: Không còn sinh comment tự động trong code."""
    import warnings
    warnings.warn("build_contextual_facebook_comment() đã bị bỏ.", DeprecationWarning, stacklevel=2)
    return fallback_comment


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


def build_ai_comment_prompt(post_text: str | None, target_comment: str | None = None, image_description: str | None = None) -> str:
    """Tao prompt theo template cua user.

    Mapping:
      {{title}}             -> post_text
      {{image_description}} -> image_description (hoac "(Khong co hinh anh)")
      {{comment}}           -> target_comment (hoac huong dan comment thang vao bai)
    """
    normalized_post = re.sub(r"\s+", " ", (post_text or "")).strip()[:3500]
    normalized_comment = re.sub(r"\s+", " ", (target_comment or "")).strip()[:800]
    normalized_image = re.sub(r"\s+", " ", (image_description or "")).strip()[:500]

    title_val = normalized_post or "(Không lấy được nội dung bài)"
    image_val = normalized_image or "(Không có hình ảnh)"

    if normalized_comment:
        comment_val = normalized_comment
    else:
        comment_val = "(Không có comment cụ thể — hãy bình luận thẳng vào bài viết)"

    return (
        "Bạn là người Việt Nam 20 tuổi, thường xuyên lướt Facebook và bình luận tự nhiên, đúng kiểu Gen Z.\n"
        "\n"
        "Đọc kỹ 3 phần dữ liệu bên dưới, sau đó viết ra đúng 1 bình luận phù hợp nhất.\n"
        "\n"
        "=== QUY ĐỊNH BẮT BUỘC ===\n"
        "* Chỉ trả về đúng 1 câu comment, KHÔNG giải thích, KHÔNG tiêu đề, KHÔNG xuống dòng.\n"
        "* Tiếng Việt CÓ DẤU đầy đủ. Không viết tắt khó đọc.\n"
        "* Từ 7-20 từ, là CÂU HOÀN CHỈNH CÓ ĐỦ CHỦ VỊ, không bị cắt giữa chừng.\n"
        "* Câu KHÔNG được kết thúc bằng từ lửng: 'ai', 'gì', 'nào', 'là', 'mà', 'vì' (trừ khi có dấu ?).\n"
        "* Bám SÁT nội dung cụ thể của bài, không nói chung chung.\n"
        "* Tối đa 1 emoji, chỉ khi thật sự hợp ngữ cảnh.\n"
        "* KHÔNG bịa thêm số liệu, con số, tên người, sự kiện không có trong bài viết.\n"
        "* KHÔNG bắt đầu bằng 'Hãy...', 'Chúc...' — nghe máy móc và giả tạo.\n"
        "\n"
        "=== CẤM TUYỆT ĐỐI ===\n"
        "* CẤM bắt đầu bằng: Minh nghi, Toi nghi, Theo toi, Toi cung, Toi thay.\n"
        "* CẤM tự nói về việc comment: Khong can comment gi, Khong biet comment gi.\n"
        "* CẤM giả vờ là người trong bài: Minh cung dang thu viec, Minh cung gap tinh huong do.\n"
        "* CẤM hiểu sai chủ thể: bài về anh/ông thì KHÔNG được comment về 'cô bé', 'em bé'.\n"
        "* CẤM câu chung chung: Hay qua, Chuan luon, Dung that, Hong tiep, That tuyet voi.\n"
        "* CẤM từ văn mẫu: Cam on ban, Rat dong y, Theo quan diem cua toi.\n"
        "* CẤM quảng cáo, gắn link, rủ inbox, câu kéo like/share.\n"
        "* CẤM công kích cá nhân, gây war, phân biệt vùng miền.\n"
        "\n"
        "=== KHI NÀO TRẢ VỀ SKIP_COMMENT ===\n"
        "Trả về đúng 1 từ SKIP_COMMENT (không thêm gì khác) khi bài viết thuộc loại:\n"
        "* QUAN TRỌNG — Tôn giáo: bài có từ 'Chúa', 'Amen', 'Abba', 'cầu nguyện', 'Phật', 'tín ngưỡng',\n"
        "  'kinh thánh', 'Chúa Giêsu', 'lễ nhà thờ' → BẮT BUỘC SKIP_COMMENT, KHÔNG được comment.\n"
        "* Tiêu cực: tai nạn, tử vong, bệnh tật, đám tang, tin buồn.\n"
        "* Nhạy cảm: chính trị, pháp luật, chiến tranh, tranh cãi gay gắt.\n"
        "* Độc hại: lừa đảo, đa cấp, cờ bạc, nội dung 18+.\n"
        "* Dữ liệu bị lỗi, rác, không rõ nghĩa.\n"
        "\n"
        "=== VÍ DỤ ĐÚNG SAI ===\n"
        "Bài về ai giống người nổi tiếng: SAI=Co be nay giong bo me, DUNG=Pha giong nay chuan khong can chinh\n"
        "Bài về deadline: SAI=Minh thay that khum khi deadline ngan, DUNG=Deadline khong phai ban ma la ke thu\n"
        "Bài lên án lừa đảo: SAI=Rac ruoi dung nhu bac noi, DUNG=SKIP_COMMENT\n"
        "Bài cầu nguyện Chúa/Amen: SAI=Dung roi, con tin tuong anh se giup, DUNG=SKIP_COMMENT\n"
        "Bài xe bị hỏng lốp: SAI=Loc xoay 12 lop trong chang duong dai (bia so lieu), DUNG=Ben do nay khong hien voi lop xe ti nao luon\n"
        "Bài mẹ không nhường: SAI=Bo me no khong biet nhuong ai con cung nhuong ai (cau lung), DUNG=Gen nha minh la the roi khong ai chiu nhuong ai ca\n"
        "Bài xin gối ôm Day 135: SAI=Chuan roi ai ngo anh lai luoi (sai ngu canh), DUNG=Day 135 roi van chua duoc goi om kien tri that su\n"
        "Bài đùa về không gian: SAI=Khong can comment gi dau, DUNG=1m ma con di sat thi o nha het di\n"
        "\n"
        "Ưu tiên xưng hô: mình, ông, bà, ae, bác, tui, t. Hoặc không xưng gì cũng được.\n"
        "\n"
        "Dữ liệu đầu vào:\n"
        "\n"
        f"[NỘI DUNG BÀI VIẾT]\n{title_val}\n"
        "\n"
        f"[MÔ TẢ HÌNH ẢNH]\n{image_val}\n"
        "\n"
        f"[COMMENT CẦN PHẢN HỒI]\n{comment_val}\n"
        "\n"
        "Viết đúng 1 bình luận Facebook tự nhiên nhất:"
    )

def clean_ai_response(text: str | None) -> str:
    """Strip cac tien to/hau to ma model co the tu them vao truoc khi validate.

    Vi du:
        "Reply: abc xyz"              -> "abc xyz"
        "Output: xyz"                 -> "xyz"
        "Comment: X\\nReply:\\nabc"   -> "abc"
        '"abc xyz"'                   -> "abc xyz"
        "Tro lai: abc"                -> "abc"
    """
    if not text:
        return ""
    s = text.strip()

    # Xoa pattern "Comment/Input: ...\\nReply/Output:\\n<reply>"
    multi_match = re.search(
        r"(?:comment|nhan xet|input)[^\n]*\n(?:reply|tro lai|output)[:\s]*\n?(.+)",
        s,
        re.IGNORECASE | re.DOTALL,
    )
    if multi_match:
        s = multi_match.group(1).strip()

    # Xoa tien to don: "Reply:", "Output:", "Comment:", "Tro lai:", ...
    s = re.sub(
        r"^(?:reply|output|comment|tro lai|nhan xet|result|cau reply|cau comment)[:\s]+",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()

    # Xoa dau ngoac kep bao quanh neu co
    if len(s) >= 2 and s[0] in ('"', "'", "\u201c", "\u201d") and s[-1] in ('"', "'", "\u201c", "\u201d"):
        s = s[1:-1].strip()

    # Giu dong dau tien neu tra ve nhieu dong
    first_line = s.split("\n")[0].strip()
    if first_line:
        return first_line
    return s.strip()


def _has_vietnamese_diacritics(text: str) -> bool:
    """Trả về True nếu text có ít nhất 1 ký tự tiếng Việt có dấu.
    
    Dùng để phát hiện AI trả về tiếng Việt không dấu (e.g. 'Nhung khi mua oto').
    """
    viet_chars = (
        "àáâãäåæèéêëìíîïòóôõöùúûüýÿ"
        "ÀÁÂÃÄÅÆÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜÝ"
        # Đặc trưng tiếng Việt
        "ăắặằẳẵÂấậầẩẫđĐơớợờởỡưứựừửữ"
        "ÃẠẢÔỒỔỖỘỚỢỜỞỠưÚỨỰỪỬỮÝỴỶỸ"
        "ạảãàáâấầẩẫậăắằẳẵặđèéêếềểễệ"
        "ìíịỉĩòóôốồổỗộơớờởỡợùúưứừửữựỳỷỹỵ"
    )
    return any(c in viet_chars for c in text)

def _looks_like_unaccented_vietnamese(text: str) -> bool:
    """Phát hiện tiếng Việt không dấu: nhiều từ Latin nhưng KHÔNG CÓ dấu nào."""
    import re as _re
    # Chỉ kiểm tra khi text đủ dài (>= 4 từ)
    words = text.split()
    if len(words) < 4:
        return False
    # Đếm từ thuần Latin (chỉ a-z, không dấu)
    latin_words = [w for w in words if _re.fullmatch(r"[a-zA-Z']+", w)]
    if len(latin_words) < 3:
        return False   # Ít từ Latin → bình thường (emoji, số, tiếng Anh thật)
    # Nếu > 60% là từ Latin thuần và không có dấu tiếng Việt → reject
    ratio = len(latin_words) / len(words)
    if ratio > 0.60 and not _has_vietnamese_diacritics(text):
        return True
    return False

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
    # Reject tiếng Việt không dấu — trông rất fake trên Facebook
    if _looks_like_unaccented_vietnamese(normalized):
        return False, "no_diacritics"
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
    """Deprecated: không còn gọi API AI. Dùng build_ai_comment_prompt() + ChatGPT web."""
    import warnings
    warnings.warn(
        "generate_ai_facebook_comment() đã bị tắt. "
        "Dùng build_ai_comment_prompt() + ChatGPT web trình duyệt.",
        DeprecationWarning,
        stacklevel=2,
    )
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
    max_iterations = 50  # Guard against infinite loops with malformed input
    iterations = 0
    while previous != current and iterations < max_iterations:
        previous = current
        current = re.sub(r"\{([^{}]*)\}", spin, current)
        iterations += 1
    return current
