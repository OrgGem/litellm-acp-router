# Kết quả kiểm chứng ví dụ — 2026-11-13

Môi trường: Windows, Python 3.12, Docker Desktop với Linux containers x86_64;
Docker Engine 28.5.1, Compose v2.40.3. Plugin root `0.2.0.dev0`, LiteLLM 1.109.0.

| Kiểm tra | Kết quả |
| --- | --- |
| Toàn bộ test root | 78 passed, 10 cảnh báo Pydantic từ dependency |
| Ruff check/format, gồm `example/` | Đạt |
| Root sdist và wheel | Build thành công |
| `docker compose -f example/docker/compose.yaml config --quiet` | Đạt |
| Build image từ Dockerfile/Compose mẫu | Thành công: `litellm-agent-rpc-example:local` |
| User trong image | UID/GID 10001, không chạy root |
| `codex --version` trong image | `codex-cli 0.167.0` |
| Localhost với config ví dụ và entry point thật | `/v1/models` trả đúng alias; thiếu key bị 401; `example/client.py` chạy được |
| Container với entry point mặc định và config bind read-only | API truy cập được từ host qua port mapping; auth/models và client ví dụ đạt |
| Completion/SSE trong container | Provider Codex thật + subprocess JSONL giả lập trả `fixture hello`; SSE có `[DONE]`; client `--stream` chạy được |
| LiteLLM Python SDK example | Test đăng ký provider thật với backend giả, dispatch đúng alias và đóng provider |

Smoke test Docker dùng cổng host ngẫu nhiên bind `127.0.0.1`, tránh chiếm cổng
4000 đang có thể được sử dụng. Các container kiểm tra đã được dừng/xóa; image
được giữ lại để người dùng chạy ví dụ. Config ví dụ sinh key riêng nằm trong
đường dẫn bị Git ignore; không thay key/config workspace đang sử dụng.

Fixture completion/SSE dùng `litellm-codex-rpc/tests/fixtures/codex_stdio.py`,
được bọc thành executable tạm trong container. Nhờ đó kiểm tra được LiteLLM,
plugin dispatcher, Codex adapter, subprocess transport, API và SSE mà không
gọi nhà cung cấp. Đây không phải bằng chứng model thật hoạt động trong Docker.

Chưa thực hiện device login, chưa mount credential của host, chưa gửi prompt
tới model thật. Login vào named volume, entitlement/quota và inference Linux
cần được xác minh khi người dùng chạy các bước đăng nhập trong hướng dẫn.
CLI Antigravity/Kiro không được cài trong image mẫu; Claude SDK extra cũng chưa
được bật. Claude/Kiro giữ nguyên điều kiện nghiệm thu trước đó.

Kiểm tra code có thể lặp lại bằng:

```powershell
uv run --no-sync pytest -q
uv run --no-sync ruff check src scripts tests example
uv run --no-sync ruff format --check src scripts tests example
uv build
docker compose -f example/docker/compose.yaml config --quiet
docker compose -f example/docker/compose.yaml build
```

Chạy API/client theo [localhost](localhost/README.md) hoặc [Docker](docker/README.md).
