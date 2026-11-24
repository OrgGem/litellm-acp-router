# Xác minh workspace

## TUI và unified plugin — 2026-11-13

Root suite: **145 passed**, Ruff check/format và build wheel/sdist thành công.
Đã kiểm thử lifecycle Windows thật, TUI headless từ wheel trong venv riêng,
Docker start/inspect/drain/recreate/stop và lưu usage qua recreate. Không gọi
inference hoặc thay đổi đăng nhập. Chi tiết và giới hạn:
[biên bản triển khai TUI](../.scratch/tui-manager/implementation.md).

## Lần xác minh kiến trúc cũ — 2026-11-12

Môi trường thực hiện: Windows, uv 0.12.17. Root dùng Python 3.12.14 và
LiteLLM 1.109.0; plugin giữ dependency trong lockfile riêng.

| Kiểm tra | Kết quả |
| --- | --- |
| Setup ba project bằng `uv sync --frozen` | Thành công |
| Router pytest: config/key, HTTP auth, routing, completion, SSE, lỗi không retry/fallback | 11 passed |
| Codex pytest trong thư mục plugin | 69 passed, 6 skipped (Windows), 3 cảnh báo dependency |
| Antigravity unittest | 27 passed, có cảnh báo Pydantic dependency |
| Ruff check và format của scripts/tests root | Passed |
| Startup Codex CLI 0.167.0 và HTTP model list có auth | Passed, alias `codex-default` |
| Startup agy 1.5.3, xác nhận model config và HTTP model list có auth | Passed, alias `antigravity` |
| Router thật nối tới hai host, `workspace.py check` | Passed cho cả ba dịch vụ |
| Credential/config local bị Git ignore | Đã xác nhận |
| Tiến trình kiểm tra | Đã dừng, không giữ listener 4000/4101/4102 |

Sửa môi trường cục bộ: venv của Codex đã được chuyển từ đường dẫn project cũ,
khiến launcher `litellm`/`pytest` không tìm được script. Đã cài lại đúng phiên bản
hai package bằng lockfile; không sửa source plugin để xử lý lỗi môi trường này.
Editable installs của hai plugin hiện trỏ tới subproject trong workspace hiện tại.

Giới hạn bằng chứng:

- Chưa gửi prompt tới CLI thật qua router; chưa xác nhận quota, entitlement,
  completion/SSE thực, hoặc cancellation xuyên toàn bộ chuỗi khi client ngắt kết nối.
- Test router dùng HTTP fixture. Test từng plugin dùng protocol/subprocess fixture.
  Startup thật không thay thế live acceptance. Codex 0.167.0 mới chỉ qua startup,
  không mở rộng tuyên bố tương thích đã ghi trong tài liệu plugin.
- Chưa chạy workflow GitHub/Linux của root. Chưa build/deploy Docker toàn stack,
  cài dịch vụ hệ điều hành hoặc công bố endpoint qua mạng.
- Chưa có fresh-clone acceptance: hai repo con còn chưa chốt commit chứa đầy đủ
  code đang dùng. Theo quyết định giữ repo riêng, root ignore hai thư mục plugin,
  không dùng submodule/gitlink. Không commit hoặc push thay người dùng.

Lệnh live acceptance và quy trình chốt nguồn nằm trong [README](../README.md).
