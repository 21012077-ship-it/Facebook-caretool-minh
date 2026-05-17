# Facebook Care Tool Minh

Desktop tool Python/CustomTkinter để quản lý tài khoản Facebook, mở browser profile, chạy tác vụ chăm sóc/comment và theo dõi lịch sử thao tác.

> Lưu ý: dự án làm việc với tài khoản/cookie/profile browser nên cần giữ repo private, không commit dữ liệu thật và luôn tuân thủ điều khoản của nền tảng bạn sử dụng.

## Tính năng hiện có

- Quản lý danh sách account với trạng thái live/checkpoint/die, proxy, ghi chú và thời điểm tương tác.
- Chạy tác vụ nuôi tài khoản qua Playwright với cookie/proxy/user-agent riêng, hỗ trợ HTTP/HTTPS/SOCKS4/SOCKS5.
- Nuôi thông minh theo từng account: tự gợi ý warmup/cân bằng/ưu tiên Reels/ưu tiên Newsfeed/nghỉ dựa trên trạng thái, ghi chú và lịch sử nuôi.
- Campaign comment theo danh sách URL, có thể để trống nội dung để tool quét đúng bài post chính, quét comment cần trả lời, mở ChatGPT trên máy bằng cookie/profile trình duyệt, paste prompt đã lưu và lấy reply liên quan cả bài lẫn comment; không gọi API ngoài và không sinh fallback bịa.

- Mở browser thủ công cho từng account.
- Lịch sử thao tác và thống kê theo ngày/account/trạng thái.
- Import/export account an toàn, mặc định ẩn password và 2FA.
- Hỗ trợ lưu dữ liệu bằng JSON và SQLite.

## Cấu trúc chính

```text
main.py                         # Entry point chạy app
facebook_caretool/ui.py          # UI CustomTkinter và điều phối thao tác
facebook_caretool/automation.py  # Playwright/browser/cookie/proxy
facebook_caretool/care_planner.py  # Gợi ý kế hoạch nuôi riêng từng account
facebook_caretool/storage.py     # JSONStorage và SQLiteStorage
facebook_caretool/account_io.py  # Import/export/merge/backup account
facebook_caretool/analytics.py   # Tổng hợp dashboard/lịch sử
facebook_caretool/models.py      # Schema account/log
facebook_caretool/utils.py       # Helper JSON, proxy, delay, spin content
tests/test_utils_storage.py      # Unit test phần lõi
ROADMAP.md                       # Đánh giá và hướng phát triển tiếp theo
```

## Cài đặt nhanh

Yêu cầu: Python 3.10+ và Chrome/Chromium phù hợp với Playwright.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```


## Định dạng proxy

Ô proxy hỗ trợ các định dạng sau:

- `host:port` (mặc định dùng HTTP)
- `host:port:user:pass` (mặc định dùng HTTP, password có thể chứa dấu `:`)
- `socks5://host:port` hoặc `socks5://user:pass@host:port`
- `socks4://host:port`, `http://host:port`, `https://host:port`
- `socks5:host:port:user:pass` hoặc `host:port:user:pass:socks5`



## ChatGPT thủ công tạo comment trong app desktop

Trong tab **Comment**, nếu muốn tool tự nghĩ comment theo từng bài mà không gọi API ngoài:

1. Đăng nhập `https://chatgpt.com` trong browser/profile mà tool đang mở để cookie được lưu sẵn.
2. Vào **Cài đặt → ChatGPT thủ công tạo comment**, bật **Bật ChatGPT thủ công tự nghĩ comment theo bài viết**.
3. Ở màn **Comment**, bật **Quét bài rồi tự nghĩ comment phù hợp** và nên để trống ô **Nội dung Comment / Fallback**.
4. Khi campaign chạy, tool quét nội dung bài Facebook, ghi rõ phần chữ trong ảnh nếu chưa OCR, quét comment cần trả lời, mở `chatgpt.com`, paste prompt + dữ liệu bài viết + comment đã quét, chờ ChatGPT trả đúng 1 reply rồi dán vào ô phản hồi của comment đó trên Facebook.

## CLI tự động tạo bình luận Facebook bằng ChatGPT thủ công

CLI Node.js hỗ trợ nhập link bài viết Facebook, mở Chromium bằng profile cố định để giữ đăng nhập Facebook và ChatGPT, quét caption/media text nếu có, quét comment cần trả lời, gửi prompt qua `https://chatgpt.com` trên web rồi mặc định chỉ preview. Luồng này không dùng API key.

Cài dependency Node:

```bash
npm install
```

Chuẩn bị đăng nhập:

```bash
# Chạy một lần để mở profile, đăng nhập Facebook và chatgpt.com trong cửa sổ Chromium, sau đó chạy lại link thật.
node index.js "https://www.facebook.com/..."
```

Chạy preview (mặc định, không tự đăng):

```bash
node index.js "https://www.facebook.com/..."
```

Tự đăng reply vào comment đã quét sau khi ChatGPT trả nội dung:

```bash
node index.js "https://www.facebook.com/..." --post
```

Ghi chú vận hành:

- Chromium dùng profile cố định `fb_comment_profile/` (hoặc đặt `FB_PROFILE_DIR=/duong/dan/profile`) để giữ trạng thái đăng nhập Facebook và ChatGPT.
- Nếu profile chưa đăng nhập Facebook hoặc ChatGPT, tool sẽ dừng và báo đăng nhập trước.
- Tool chụp ảnh/thumbnail có kích thước phù hợp để đính kèm vào ChatGPT; nếu không lấy được chữ trong ảnh thì prompt/log ghi rõ chưa OCR. Có thể tắt bước chụp/gửi ảnh bằng `FB_COMMENT_ENABLE_VISION=0`.
- Không có flag `--post` thì tool chỉ in reply ở chế độ preview, không dán và không tự bấm đăng.
- Luồng tự tạo hiện chạy theo thứ tự: quét nội dung/ảnh bài viết → quét comment cần trả lời → nhờ ChatGPT tạo reply liên quan cả bài viết và comment → đăng vào ô phản hồi của comment.

## Chạy ứng dụng

```bash
python main.py
```

## Chạy test

```bash
python -m unittest discover -s tests -v
python -m compileall main.py facebook_caretool tests
python -m mypy
```

## Dữ liệu local không nên commit

Các file/thư mục sau là dữ liệu runtime hoặc có thể chứa thông tin nhạy cảm và đã được thêm vào `.gitignore`:

- `accounts.json`, `account.json`, `logs.json`
- `cookies.json`, `cookies/`
- `fb_profiles/`
- `caretool.db`, `*.sqlite`, `*.sqlite3`
- `backups/`, `exports/`

Nếu những file này đã từng được commit, hãy xoá khỏi index bằng `git rm --cached ...`, rotate secret/cookie liên quan và cân nhắc làm sạch Git history trước khi public repo.

## Hướng cải thiện đề xuất

Xem chi tiết trong [ROADMAP.md](ROADMAP.md). Ưu tiên cao nhất hiện tại là bảo mật dữ liệu local, chuẩn hoá packaging/runbook, duy trì type-check bằng `mypy` và tách nhỏ `ui.py` để test dễ hơn.
