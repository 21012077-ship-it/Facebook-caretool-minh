# Roadmap cải thiện Facebook Care Tool

Tài liệu này ghi lại các điểm nên ưu tiên sau khi rà soát nhanh codebase hiện tại.

## Nhận xét nhanh

- Ứng dụng đã được tách module khá rõ: `ui`, `automation`, `storage`, `account_io`, `analytics`, `models`, `utils`.
- Đã có test cho các phần lõi như parse proxy/delay, spin content, JSON/SQLite storage, import/export và thống kê log.
- UI đã có các màn hình chính: nuôi tài khoản, comment, mở browser, lịch sử và cài đặt.
- Điểm rủi ro lớn nhất là dữ liệu runtime nhạy cảm như account, cookie và Chrome profile đang nằm trong workspace; các file này cần được loại khỏi Git history trước khi public repo.

## Ưu tiên 1 - Bảo mật dữ liệu

1. Xoá khỏi Git các file runtime nhạy cảm đã bị track: `accounts.json`, `account.json`, `cookies.json`, thư mục `cookies/`, `logs.json`, `fb_profiles/`.
2. Rotate toàn bộ cookie/password/2FA/proxy nếu repo từng được đẩy lên remote công khai hoặc chia sẻ cho người khác.
3. Thêm luồng tạo file mẫu `accounts.example.json` thay vì commit dữ liệu thật.
4. Cân nhắc mã hoá trường `password`, `two_fa`, cookie file path bằng key local hoặc OS keyring.

## Ưu tiên 2 - Đóng gói và chạy dự án

1. Chuẩn hoá hướng dẫn cài đặt Python, dependency và Playwright browser.
2. Thêm script CLI hoặc Makefile cho các lệnh thường dùng: test, lint, run app.
3. Tách cấu hình runtime sang `.env` hoặc `settings.json` local và có file mẫu.
4. Thêm kiểm tra khởi động: thiếu Chrome/Playwright/customtkinter thì báo lỗi thân thiện thay vì crash.

## Ưu tiên 3 - Chất lượng code và test

1. Thêm formatter/linter như `ruff` và `black` để thống nhất style.
2. Bổ sung test cho import payload lỗi, cookie normalization, SQLite metadata lỗi/không hợp lệ, backup file.
3. Tách logic dài trong `ui.py` thành các component/service nhỏ hơn để dễ test.
4. Thêm type checking nhẹ bằng `mypy` hoặc `pyright` cho các module không phụ thuộc UI.

## Ưu tiên 4 - Tính năng sản phẩm

1. Quản lý nhiều profile theo account, có trạng thái profile tồn tại/hỏng/dung lượng.
2. Hàng đợi tác vụ có pause/resume/cancel, giới hạn concurrency, retry có backoff.
3. Màn hình thống kê nâng cao: tỉ lệ thành công theo ngày, theo proxy, theo account.
4. Import/export có preview trước khi merge và báo chi tiết trùng UID/tên.
5. Cảnh báo health check: proxy lỗi, cookie hết hạn, checkpoint tăng bất thường.

## Ưu tiên 5 - Vận hành an toàn

1. Ghi log dạng JSONL hoặc SQLite thay vì append UI-only để dễ audit.
2. Backup tự động trước khi import/overwrite account.
3. Cơ chế dry-run cho campaign comment để kiểm tra danh sách URL/account trước khi chạy thật.
4. Tài liệu hoá giới hạn sử dụng và yêu cầu tuân thủ điều khoản nền tảng.
