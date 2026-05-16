# Facebook Care Tool Minh

Desktop tool Python/CustomTkinter để quản lý tài khoản Facebook, mở browser profile, chạy tác vụ chăm sóc/comment và theo dõi lịch sử thao tác.

> Lưu ý: dự án làm việc với tài khoản/cookie/profile browser nên cần giữ repo private, không commit dữ liệu thật và luôn tuân thủ điều khoản của nền tảng bạn sử dụng.

## Tính năng hiện có

- Quản lý danh sách account với trạng thái live/checkpoint/die, proxy, ghi chú và thời điểm tương tác.
- Chạy tác vụ nuôi tài khoản qua Playwright với cookie/proxy/user-agent riêng, hỗ trợ HTTP/HTTPS/SOCKS4/SOCKS5.
- Nuôi thông minh theo từng account: tự gợi ý warmup/cân bằng/ưu tiên Reels/ưu tiên Newsfeed/nghỉ dựa trên trạng thái, ghi chú và lịch sử nuôi.
- Campaign comment theo danh sách URL, nội dung spin/fallback và giới hạn comment theo account.
- Tự quét nội dung bài viết để tạo comment phù hợp ngữ cảnh; có thể để trống ô mẫu khi bật chế độ tự tạo comment, và tool sẽ ưu tiên gọi AI OpenAI-compatible nếu đã cấu hình API key.
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
