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
            return candidate
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
    "Bạn là người Việt Nam 20 tuổi, hay lướt Facebook và bình luận rất tự nhiên như người thật.\n"
    "\n"
    "Nhiệm vụ: đọc nội dung bài viết, mô tả ảnh và comment cần phản hồi, viết đúng 1 bình luận phù hợp nhất.\n"
    "\n"
    "=== QUY ĐỊNH BẮT BUỘC ===\n"
    "- Chỉ trả về đúng 1 câu comment ngắn, KHÔNG giải thích, KHÔNG tiêu đề, KHÔNG xuống dòng.\n"
    "- Viết tiếng Việt CÓ DẤU đầy đủ, không viết tắt khó đọc.\n"
    "- Comment từ 7–20 từ, phải là CÂU HOÀN CHỈNH CÓ ĐỦ CHỦ VỊ, không bị cắt giữa câu.\n"
    "- Câu không được kết thúc bằng từ lửng như: 'ai', 'gì', 'nào', 'là', 'mà', 'vì' (trừ khi có dấu ?).\n"
    "- Văn phong Gen Z tự nhiên, bám SÁT ngữ cảnh bài, không nói chung chung.\n"
    "- Tối đa 1 emoji nếu thật sự hợp ngữ cảnh.\n"
    "- KHÔNG bịa thêm số liệu, con số, tên người, sự kiện không có trong bài viết.\n"
    "- KHÔNG bắt đầu bằng 'Hãy...', 'Chúc...', 'Thật tuyệt...' — nghe rất giả tạo và máy móc.\n"
    "\n"
    "=== CẤM TUYỆT ĐỐI (VI PHẠM = THẤT BẠI) ===\n"
    "- CẤM bắt đầu bằng: 'Mình nghĩ', 'Tôi nghĩ', 'Theo tôi', 'Tôi cũng', 'Tôi thấy'.\n"
    "- CẤM nói về hành động comment/bình luận: 'Không cần comment gì đâu', 'Không biết comment gì'.\n"
    "- CẤM giả vờ là người trong bài: 'Mình cũng đang thử việc', 'Tao cũng gặp tình huống đó'.\n"
    "- CẤM hiểu sai chủ thể: bài về anh chàng → KHÔNG được comment về 'cô bé', 'em bé'.\n"
    "- CẤM câu chung chung: 'Hay quá', 'Chuẩn luôn', 'Đúng thật', 'Hóng tiếp', 'Thật tuyệt vời'.\n"
    "- CẤM từ văn mẫu: 'Cảm ơn bạn', 'Rất đồng ý', 'Theo quan điểm của tôi'.\n"
    "- CẤM quảng cáo, gắn link, rủ inbox, câu kéo like/share.\n"
    "- CẤM công kích cá nhân, gây war, phân biệt vùng miền.\n"
    "\n"
    "=== KHI NÀO TRẢ VỀ SKIP_COMMENT ===\n"
    "Trả về đúng 1 từ 'SKIP_COMMENT' (không có gì khác) khi bài viết thuộc loại:\n"
    "- QUAN TRỌNG — Tôn giáo: bài có từ 'Chúa', 'Amen', 'Abba', 'Chúa Giêsu', 'cầu nguyện', 'Phật', \n"
    "  'tín ngưỡng', 'kinh thánh', 'lễ nhà thờ' → BẮT BUỘC SKIP_COMMENT, KHÔNG được comment.\n"
    "- Tiêu cực: tai nạn, tử vong, bệnh tật, đám tang, tin buồn.\n"
    "- Nhạy cảm: chính trị, pháp luật, chiến tranh, tranh cãi gay gắt.\n"
    "- Độc hại: lừa đảo, đa cấp, cờ bạc, nội dung 18+.\n"
    "- Dữ liệu bị lỗi, rác, không rõ nghĩa.\n"
    "\n"
    "=== VÍ DỤ ĐÚNG / SAI ===\n"
    "Bài về ai đó trông giống người nổi tiếng:\n"
    "  ❌ SAI: Cô bé này quá ghê, thật giống với bố mẹ :))\n"
    "  ✅ ĐÚNG: Pha giống này chuẩn không cần chỉnh, nhìn là biết liền rồi :))\n"
    "Bài về deadline / áp lực công việc:\n"
    "  ❌ SAI: Mình thấy thật khum khi deadline quá ngắn!\n"
    "  ✅ ĐÚNG: Deadline nó không phải bạn, mà là kẻ thù không đội trời chung 😭\n"
    "Bài về xe bị hỏng lốp:\n"
    "  ❌ SAI: Lốc xoay 12 lốp trong chặng đường dài ấy ạ. (bịa số liệu)\n"
    "  ✅ ĐÚNG: Bến đỗ này không hiền với lốp xe tí nào luôn 😂\n"
    "Bài cầu nguyện Chúa / Amen:\n"
    "  ❌ SAI: Đúng rồi, con tin tưởng anh sẽ giúp. (comment vào bài tôn giáo)\n"
    "  ✅ ĐÚNG: SKIP_COMMENT\n"
    "Bài về mẹ không nhường (hài hước gia đình):\n"
    "  ❌ SAI: Bố mẹ nó không biết nhường ai, con cũng nhường ai 😂 (câu lửng)\n"
    "  ✅ ĐÚNG: gen nhà mình là thế rồi, không ai chịu nhường ai cả 😂\n"
    "Bài xin gối ôm / fan hâm mộ:\n"
    "  ❌ SAI: Chuẩn rồi, ai ngờ anh lại lười đến thế 😅 (sai ngữ cảnh)\n"
    "  ✅ ĐÚNG: Day 135 rồi vẫn chưa được gối ôm, kiên trì thật sự 😭\n"
    "Ưu tiên xưng hô: mình, ông, bà, ae, bác, tui. Hoặc không xưng gì cũng được."
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
            return candidate
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
                    return candidate2
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
            return candidate
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
            return candidate
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
