# LiteLLM + plugin trong Docker

Ví dụ này build một image gồm Python 3.12, LiteLLM 1.109.0, wheel plugin root,
Node 24.22.1 và Codex CLI 0.167.0. Bốn adapter Python có trong wheel; image mẫu
chỉ cài CLI Codex. Docker Desktop dùng Linux containers hoặc Docker Engine Linux.

CLI chạy **bên trong container**. Đăng nhập Codex trên Windows không tự xuất
hiện trong container; ví dụ dùng CLI login chính thức và named volume riêng,
không mount/copy credential của Windows. Nếu muốn dùng trực tiếp CLI Windows
đã đăng nhập, chọn [localhost](../localhost/README.md).

## 1. Build từ wheel

Từ root repository:

```powershell
uv build
python example/init_config.py example/docker/config.json
docker compose -f example/docker/compose.yaml build
```

Trên Docker Engine Linux, file do initializer tạo có mode `0600`. Cho group của
container (GID 10001) quyền đọc, vẫn giữ owner của bạn để client host đọc được:

```bash
sudo chgrp 10001 example/docker/config.json
chmod 640 example/docker/config.json
```

Docker Desktop Windows dùng quyền bind mount do Desktop quản lý; không chạy
các lệnh `chgrp`/`chmod` trên PowerShell.

Build cần source bốn adapter để tạo wheel. Docker chỉ nhận wheel trong build
context nhờ `Dockerfile.dockerignore`; config/credential không được COPY vào
image. File runtime `config.json` được bind mount read-only, không tạo thư mục
thay thế nếu quên khởi tạo file. Key không được bake vào image.

Nếu phiên bản package thay đổi, sửa `PLUGIN_WHEEL` trong compose cho đúng tên
wheel mới. `CODEX_VERSION` được pin trong compose và Dockerfile; chỉ nâng sau
khi kiểm tra lại protocol/cleanup. Build cần mạng tải image và dependency.

## 2. Đăng nhập CLI trong volume

```powershell
docker compose -f example/docker/compose.yaml run --rm --entrypoint codex litellm login --device-auth
docker compose -f example/docker/compose.yaml run --rm --entrypoint codex litellm login status
docker compose -f example/docker/compose.yaml run --rm litellm doctor --config /app/config.json
```

Mở URL và nhập mã do CLI chính thức hiển thị để hoàn tất device login. Nếu tài
khoản chưa cho phép device login, thực hiện yêu cầu của CLI/tài khoản trước.
Các container chạy dưới UID 10001, dùng cùng volume `codex-home` ở
`/home/agent/.codex`. Volume lưu trạng thái CLI qua lần tạo lại container.
Doctor không gọi model; nó chưa xác minh quota hoặc quyền sử dụng model.

## 3. Chạy gateway

```powershell
docker compose -f example/docker/compose.yaml up -d
docker compose -f example/docker/compose.yaml logs --tail 50 litellm
```

LiteLLM listen `0.0.0.0:4000` **trong container** để Docker có thể chuyển tiếp
kết nối. Compose chỉ publish `127.0.0.1:4000:4000` trên host, nên client dùng
`http://127.0.0.1:4000/v1`. Đây vẫn là một LiteLLM/provider; không có server
backend phụ. Không dùng `network_mode: host` hoặc mount Docker socket.

Từ host, dùng venv đã có package/OpenAI client:

```powershell
uv run --no-sync python example/client.py --config example/docker/config.json
uv run --no-sync python example/client.py --config example/docker/config.json --prompt "Chào bạn"
uv run --no-sync python example/client.py --config example/docker/config.json --prompt "Trả lời ngắn" --stream
```

Chỉ các lệnh có `--prompt` gọi model thật. Nếu host chưa có venv, chạy
`uv sync --frozen` tại root hoặc dùng cách cài wheel trong hướng dẫn localhost.
Client trong container khác cùng Compose network có thể dùng
`http://litellm:4000/v1`; không dùng `localhost` của container client.

Để tránh trùng cổng, sửa mapping thành `127.0.0.1:4001:4000` và gọi client với
`--base-url http://127.0.0.1:4001/v1`. Không cần sửa cổng bên trong container.

## Bật backend khác và giới hạn

- Antigravity/Kiro: cần bổ sung **CLI Linux tương ứng** vào image, đúng phiên
  bản adapter hỗ trợ, và kiểm chứng login/config isolation trong container.
  Không trỏ `executables` tới `C:\...` hoặc chỉ mount file `.exe` từ Windows.
- Claude: đặt build arg `PLUGIN_EXTRAS: "[claude]"`, rebuild để cài SDK;
  vẫn phải hoàn tất gate nghiệm thu trước khi bật `enabled.claude`.
- Cấu hình model/flag trong `config.json`; CLI path là đường dẫn container.
  Restart service sau khi sửa. Model list không chứng minh backend sẵn sàng.
- Ví dụ chưa nghiệm thu inference Linux thực tế cho các backend. Các kiểm tra
  fixture/HTTP và build được ghi riêng tại [VALIDATION](../VALIDATION.md).

## Dừng

```powershell
docker compose -f example/docker/compose.yaml down
```

Lệnh này giữ named volume đăng nhập. Không thêm `--volumes` nếu muốn giữ trạng
thái CLI. Config/key vẫn nằm trong `example/docker/config.json` trên host.
Không ghi prompt/token vào build args, image hoặc file được commit.

Tham khảo: [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/),
[Compose services và mounts](https://docs.docker.com/reference/compose-file/services/).
