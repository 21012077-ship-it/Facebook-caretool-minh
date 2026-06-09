"""AI Comment Generator — thay thế ChatGPT web automation.

Hỗ trợ: Gemini API, Groq API, OpenRouter API, Ollama (local).
Cách dùng:
    from .ai_comment import make_ai_generator
    gen = make_ai_generator("gemini", api_key="...", model="gemini-2.0-flash-lite")
    comment = gen(post_text, target_comment=None)  # trả về str hoặc None
"""
from __future__ import annotations

from typing import Callable, Optional

from .utils import build_ai_comment_prompt, validate_ai_comment, clean_ai_response


# ---------------------------------------------------------------------------
# Factory chính
# ---------------------------------------------------------------------------

def make_ai_generator(
    provider: str,
    api_key: str = "",
    model: str = "",
) -> Callable:
    """Trả về một callable (post_text, target_comment?, images?) → str | None.

    - post_text: nội dung bài viết đã quét
    - target_comment: comment cần trả lời (None nếu comment thẳng vào bài)
    - images: list base64 data URL (data:image/jpeg;base64,...) — ảnh bài viết

    provider: 'gemini' | 'groq' | 'openrouter' | 'ollama'
    """
    provider = (provider or "gemini").lower().strip()
    if provider == "gemini":
        return _make_gemini(api_key, model or "gemini-2.0-flash-lite")
    elif provider == "groq":
        return _make_groq(api_key, model or "llama-3.3-70b-versatile")
    elif provider == "openrouter":
        return _make_openrouter(api_key, model or "google/gemma-3-27b-it:free")
    elif provider == "ollama":
        return _make_ollama(model or "qwen2.5:7b")
    else:
        raise ValueError(
            f"AI provider không hỗ trợ: '{provider}'. "
            "Chọn: gemini | groq | openrouter | ollama"
        )


# ---------------------------------------------------------------------------
# Gemini API (Google) — đề xuất số 1
# Miễn phí 1,500 req/ngày, tốc độ 1–3s, hiểu Vietnamese rất tốt
# Lấy API key: https://aistudio.google.com/apikey
# ---------------------------------------------------------------------------

def _make_gemini(api_key: str, model: str) -> Callable:
    """Dùng google-genai SDK mới (thay thế google-generativeai đã deprecated).
    Cài đặt: python -m pip install google-genai
    Hỗ trợ multimodal: truyền images=[base64_data_url] để AI thấy ảnh bài viết.
    """
    try:
        from google import genai  # type: ignore[import]
        from google.genai import types as genai_types  # type: ignore[import]
    except ImportError as e:
        raise ImportError(
            "Thiếu thư viện Google GenAI. Chạy: python -m pip install google-genai"
        ) from e

    client = genai.Client(api_key=api_key)

    def generate(post_text: str, target_comment: Optional[str] = None, images: Optional[list] = None) -> Optional[str]:
        prompt = build_ai_comment_prompt(post_text, target_comment)
        try:
            # Build contents — text + optional images
            contents: list = []
            if images:
                import base64
                for data_url in images[:2]:
                    # Parse data:image/jpeg;base64,... → mime_type + bytes
                    try:
                        header, b64data = data_url.split(",", 1)
                        mime_type = header.split(";")[0].replace("data:", "") or "image/jpeg"
                        img_bytes = base64.b64decode(b64data)
                        contents.append(genai_types.Part.from_bytes(data=img_bytes, mime_type=mime_type))
                    except Exception:
                        pass
            contents.append(prompt)
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    temperature=1.0,
                    max_output_tokens=100,
                ),
            )
            candidate = clean_ai_response(response.text or "")
            ok, _ = validate_ai_comment(candidate)
            return candidate if ok else None
        except Exception as exc:
            print(f"[Gemini] Lỗi tạo comment: {exc}")
            return None

    return generate


# ---------------------------------------------------------------------------
# Groq API — nhanh nhất (~0.3–1s), miễn phí 14,400 req/ngày
# Lấy API key: https://console.groq.com
# ---------------------------------------------------------------------------

# Model vision của Groq (nhanh, miễn phí) — dùng khi có ảnh
_GROQ_VISION_MODEL = "llama-3.2-11b-vision-preview"
_GROQ_SYSTEM_PROMPT = (
    "Bạn là người Việt Nam Gen Z hay comment Facebook. "
    "Viết đúng 1 câu, ngắn (5-20 từ), rất tự nhiên như chat thật. "
    "Tiếng Việt CÓ DẤU đầy đủ, có thể viết tắt: ko, ma, nha, lun, :)), 😭. "
    "TUYỆT ĐỐI KHÔNG viết tiếng Việt không dấu (teen code/Viet Lat). "
    "Ví dụ SAI: 'Nhung khi mua oto' → phải viết: 'Nhưng khi mua ôtô'. "
    "KHÔNG dùng: 'Cảm ơn bạn', 'Tôi nghĩ', 'Rất đồng ý', 'Theo tôi'. "
    "KHÔNG giải thích, KHÔNG tiền tố, KHÔNG dấu ngoặc kép. "
    "Chỉ trả về đúng 1 dòng text là câu comment/reply đó thôi."
)


def _make_groq(api_key: str, model: str) -> Callable:
    """Groq generator với vision support.
    Khi images được truyền vào, tự động chuyển sang vision model.
    """
    try:
        from groq import Groq  # type: ignore[import]
    except ImportError as e:
        raise ImportError(
            "Thiếu thư viện Groq. Chạy: pip install groq"
        ) from e

    client = Groq(api_key=api_key)

    def generate(post_text: str, target_comment: Optional[str] = None, images: Optional[list] = None) -> Optional[str]:
        prompt = build_ai_comment_prompt(post_text, target_comment)
        # Chọn model: vision nếu có ảnh, text nếu không
        active_model = _GROQ_VISION_MODEL if images else model
        try:
            # Build user message content
            if images:
                # Multimodal: ảnh + text
                user_content: list = []
                for data_url in images[:2]:  # Groq vision: tối đa 2 ảnh
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    })
                user_content.append({"type": "text", "text": prompt})
            else:
                user_content = prompt  # Text only

            response = client.chat.completions.create(
                model=active_model,
                messages=[
                    {"role": "system", "content": _GROQ_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=1.0,
                max_tokens=100,
            )
            candidate = clean_ai_response(response.choices[0].message.content or "")
            ok, _ = validate_ai_comment(candidate)
            return candidate if ok else None
        except Exception as exc:
            err = str(exc)
            print(f"[Groq] Lỗi tạo comment (model={active_model}): {err}")
            # Fallback: nếu vision model lỗi, thử lại với text-only model
            if images and active_model != model:
                try:
                    response2 = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": _GROQ_SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=1.0,
                        max_tokens=100,
                    )
                    candidate2 = clean_ai_response(response2.choices[0].message.content or "")
                    ok2, _ = validate_ai_comment(candidate2)
                    return candidate2 if ok2 else None
                except Exception:
                    pass
            return None

    return generate


# ---------------------------------------------------------------------------
# OpenRouter API — linh hoạt, truy cập 200+ model, nhiều model miễn phí
# Lấy API key: https://openrouter.ai
# ---------------------------------------------------------------------------

def _make_openrouter(api_key: str, model: str) -> Callable:
    try:
        from openai import OpenAI  # type: ignore[import]
    except ImportError as e:
        raise ImportError(
            "Thiếu thư viện OpenAI. Chạy: pip install openai"
        ) from e

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    def generate(post_text: str, target_comment: Optional[str] = None, images: Optional[list] = None) -> Optional[str]:
        prompt = build_ai_comment_prompt(post_text, target_comment)
        try:
            if images:
                user_content: list = []
                for data_url in images[:2]:
                    user_content.append({"type": "image_url", "image_url": {"url": data_url}})
                user_content.append({"type": "text", "text": prompt})
            else:
                user_content = prompt
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": user_content}],
                temperature=1.0,
                max_tokens=100,
            )
            candidate = clean_ai_response(response.choices[0].message.content or "")
            ok, _ = validate_ai_comment(candidate)
            return candidate if ok else None
        except Exception as exc:
            print(f"[OpenRouter] Lỗi tạo comment: {exc}")
            return None

    return generate


# ---------------------------------------------------------------------------
# Ollama — chạy local, hoàn toàn offline, không giới hạn, privacy tuyệt đối
# Cài Ollama: https://ollama.com/download
# Tải model: ollama pull qwen2.5:7b
# ---------------------------------------------------------------------------

def _make_ollama(model: str, base_url: str = "http://localhost:11434") -> Callable:
    try:
        import httpx  # type: ignore[import]
    except ImportError as e:
        raise ImportError(
            "Thiếu thư viện httpx. Chạy: pip install httpx"
        ) from e

    def generate(post_text: str, target_comment: Optional[str] = None, images: Optional[list] = None) -> Optional[str]:
        prompt = build_ai_comment_prompt(post_text, target_comment)
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 1.0, "num_predict": 100},
        }
        # Ollama vision: truyền images dưới dạng base64 (bỏ data:image/...;base64, prefix)
        if images:
            raw_b64 = []
            for du in images[:2]:
                try:
                    raw_b64.append(du.split(",", 1)[1])
                except Exception:
                    pass
            if raw_b64:
                payload["images"] = raw_b64
        try:
            response = httpx.post(
                f"{base_url}/api/generate",
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            candidate = clean_ai_response(response.json().get("response") or "")
            ok, _ = validate_ai_comment(candidate)
            return candidate if ok else None
        except Exception as exc:
            print(f"[Ollama] Lỗi tạo comment: {exc}")
            return None

    return generate


# ---------------------------------------------------------------------------
# Stub tương thích ngược (nếu code cũ gọi trực tiếp)
# ---------------------------------------------------------------------------

def generate_comment(
    post_text: str,
    target_comment: Optional[str] = None,
    *,
    provider: str = "gemini",
    api_key: str = "",
    model: str = "",
) -> Optional[str]:
    """Convenience wrapper — gọi đúng provider theo params."""
    gen = make_ai_generator(provider=provider, api_key=api_key, model=model)
    return gen(post_text, target_comment)
