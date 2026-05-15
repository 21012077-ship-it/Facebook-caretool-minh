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
