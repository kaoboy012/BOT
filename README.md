# AutoBot — bỏ O8, tạm tắt QQ88; chỉ dùng tab có sẵn

Bản này tiếp nối bản audit trước, giữ các bản sửa SQLite/queue/OCR và thay hành vi browser theo yêu cầu: bot không mở Edge, không tạo context/tab mới và không tự đóng các tab đang mở.

## Bản sửa độ trễ theo log

Đọc LATENCY_DIAGNOSIS_VI.md. Bản này sửa học nhịp FloodWait dài, chia sẻ attach CDP giữa worker, cooldown và watchdog warm reconnect. Thêm cấu hình trong báo cáo vào .env thật. Marker startup: `[Build] 2026-10-07-latency-v1`. Log 31 kênh/7 site còn O8/QQ88 là cấu hình phiên cũ.

## Site đang hoạt động

HI88, XX88, RR88, MM88 và GG88. O8 đã xóa khỏi cấu hình/profile; QQ88 tạm tắt mặc định. Thêm `QQ88_ENABLED=false` vào `.env`; đổi true và restart để bật lại QQ88 (nếu ACTIVE_DOMAINS có trong env, cũng cần cho phép QQ88). Khi đang tắt, site không tham gia poll, worker, preload hoặc recovery queue.


## Cài bản sửa

1. Dừng bot; sao lưu mã, session và thư mục dữ liệu hiện tại.
2. Giải nén vào thư mục riêng và copy `.env`, session, thư mục `data` của bạn sang. File `.env` thật không nằm trong ZIP. `.env.example` có cấu hình tham khảo, không chứa API_HASH/token.
3. Dùng Python 3.11+ và môi trường dependencies hiện có, hoặc cài `python -m pip install -r requirements.txt`.
4. Bạn phải tự mở Edge với CDP tại `127.0.0.1:9222` trước khi chạy bot. Edge mở theo cách thông thường mà chưa bật CDP sẽ không kết nối được; bot không đổi hoặc mở lại cửa sổ đó.
5. Trong Edge có CDP, tự mở tab của từng site và đăng nhập đúng tài khoản. Nếu cấu hình nhiều account song song trên một site, chuẩn bị đủ tab tương ứng. Các tab ngoài site được cấu hình không bị lấy để nhập mã.
6. Chạy `python main_script.py`.

Bot dùng tab theo hostname đúng, có thể điều hướng tab cùng site tới trang nhập mã. Nếu thiếu tab, tab bị đóng hoặc CDP mất kết nối, bot trả lỗi hạ tầng và retry theo giới hạn hiện có. Bạn tự mở lại tab đúng site/tài khoản để bot nhận ở lần thử tiếp theo. Code quá tuổi vẫn bị bỏ qua theo `CODE_MAX_AGE_SECONDS`.

`AUTO_OPEN_MISSING_TABS=false` được giữ làm thuộc tính tương thích. Đường tạo tab/browser đã bị loại khỏi engine; đặt true trong env cũ cũng không bật lại tính năng mở tự động. GC chỉ xóa tham chiếu tab đã đóng; shutdown dừng kết nối Playwright, giữ Edge hiện có.

Đã chạy 44 tests offline, compileall và quét AST không còn lời gọi tạo/đóng browser/tab trong browser_engine. Chưa thử Telegram/Edge thật trên Windows.

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

`SITE_PRIORITY_CHANGES.md` ghi thay đổi site lần này. `EXISTING_BROWSER_CHANGES.md` ghi thay đổi browser trước đó. `existing_browser.patch` áp dụng lên bản AutoBot_Audited trước. `autobot_audit.patch` là patch tổng từ mã gốc tải lên tới bản hiện tại; khi dùng GNU patch với nguồn có line ending hỗn hợp, dùng `patch --binary -p1`.

README gốc nằm ở README_ORIGINAL.md; setup.bat/run.bat không nằm trong upload và không nằm trong gói này. Nếu launcher riêng trên máy bạn có lệnh tự mở Edge, bạn cần bỏ lệnh đó hoặc chạy trực tiếp Python như trên.
