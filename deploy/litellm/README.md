# LiteLLM local stack

Stack Docker Compose độc lập: LiteLLM + PostgreSQL 16 + Redis 8.
Không dùng package Agent RPC hoặc đăng nhập CLI của repo. Model được thêm sau
qua Admin UI và lưu trong PostgreSQL (`STORE_MODEL_IN_DB=True`).

Gateway dùng stable 1.109.0 để hỗ trợ API phân quyền của LiteAdmin MCP hiện tại.
Các image được ghim digest trong `compose.yaml`.

- [Admin UI](http://127.0.0.1:14000/ui)
- OpenAI-compatible API: `http://127.0.0.1:14000/v1`
- [OpenAPI](http://127.0.0.1:14000/openapi.json)
- Username: `admin`; mật khẩu là `UI_PASSWORD` trong `.env` cạnh file này.
- Master API key: `LITELLM_MASTER_KEY` trong `.env`. Tạo virtual key riêng cho
  ứng dụng trong UI thay vì dùng master key.

## Khởi chạy và vận hành

Chạy PowerShell tại thư mục này:

```powershell
./init.ps1                       # Chỉ tạo .env nếu chưa có
docker compose config --quiet
docker compose up -d --wait --wait-timeout 300
docker compose ps
docker compose logs --tail 100 litellm
```

Chỉ cổng LiteLLM được publish trên `127.0.0.1`. PostgreSQL/Redis nằm trong
network riêng của Compose, có mật khẩu riêng và named volumes bền vững.
Các service tự chạy lại khi Docker hoạt động (`restart: unless-stopped`).

```powershell
docker compose stop             # Dừng stack, giữ container và dữ liệu
docker compose start            # Chạy lại stack
docker compose down             # Xóa container/network; vẫn giữ volumes
```

`docker compose down -v` xóa dữ liệu. Giữ `.env`, đặc biệt `LITELLM_SALT_KEY`,
cùng bản sao lưu PostgreSQL để đọc được credential đã mã hóa.
`.env` và `.env.mcp` bị Git ignore; không chia sẻ nội dung hai file này.

Đổi `LITELLM_PORT` trong `.env`, rồi chạy `docker compose up -d` để đổi cổng.
Nếu đã dùng MCP, đồng thời sửa `LITELLM_BASE_URL` trong `.env.mcp`.

## Thêm model sau

Đăng nhập UI → Models → Add Model, chọn provider và điền model ID/API key.
Với endpoint tương thích OpenAI trên Windows host, dùng
`http://host.docker.internal:<port>/v1` làm API base trong container.
Sau đó tạo virtual key trong UI và gọi API với model alias đã đặt.
Stack ban đầu không có model thật hoặc provider API key.

## Redis cache

`config.yaml` bật response cache Redis (TTL 3.600 giây), auth cache Redis
(TTL 60 giây) và cung cấp Redis cho coordination/rate limits của LiteLLM.
Đây là cache phản hồi theo request, không phải KV cache bên trong model hoặc
semantic cache cần embedding model. Có thể bỏ qua response cache bằng header
`Cache-Control: no-cache` khi cần câu trả lời mới.

Redis bật AOF `everysec`, giới hạn 512 MB và `noeviction` để tránh tự đẩy các
key coordination ra ngoài. Khi đầy bộ nhớ, tăng giới hạn hoặc giảm TTL sau khi
kiểm tra tải; container có giới hạn 768 MB. PostgreSQL có giới hạn 1 GB,
LiteLLM 3 GB; đây là cấu hình local một replica.

## LiteAdmin MCP

Sử dụng [connector chính thức](https://github.com/BerriAI/litellm-admin-mcp),
ghim commit `3d4cd40225430f32fee061f42a1fa538ae8d01b2`.
Connector chạy stdio trên Windows bằng `uvx`/Python 3.12 và gọi management API
của stack. Nó không cần container MCP hoặc license Enterprise.

`start-admin-mcp.ps1` đọc `.env.mcp` (private) với các giá trị:

```dotenv
LITELLM_BASE_URL=http://127.0.0.1:14000
LITELLM_API_KEY=<virtual key của user proxy_admin dành riêng cho MCP>
```

Sau khi gateway healthy, chạy `python setup-mcp.py` để tạo user
`local-mcp-admin` và key riêng. Script giữ key hiện có khi chạy lại và không in
credential. Cần `uvx` trong PATH; wrapper ghim Python 3.12 cho connector.

Đăng ký cho Codex bằng:

```powershell
codex mcp add litellm-admin -- pwsh -NoLogo -NoProfile -NonInteractive -File "$PWD/start-admin-mcp.ps1"
codex mcp get litellm-admin
```

Bản triển khai trên máy này dùng đường dẫn tuyệt đối tới PowerShell để Codex
Desktop khởi chạy ổn định, với `startup_timeout_sec = 60`. Đã đăng ký server
`litellm-admin` trong cấu hình Codex của user. Khởi động lại Codex để nạp server mới. Có thể yêu cầu
agent liệt kê model, thêm deployment, quản lý virtual key, team và budget.
Credential MCP có quyền `proxy_admin`; thu hồi key riêng này trong UI khi cần.
Đặt `LITELLM_ADMIN_READ_ONLY=true` trong `.env.mcp` nếu chỉ muốn tra cứu.

## Sao lưu PostgreSQL

```powershell
docker compose exec -T postgres pg_dump -U litellm -d litellm -Fc -f /tmp/litellm.dump
docker compose cp postgres:/tmp/litellm.dump ./litellm.dump
docker compose exec -T postgres rm /tmp/litellm.dump
```

Cất file dump và `.env` ở nơi riêng tư; dump chứa thông tin quản trị.
Redis không thay thế PostgreSQL và không phải bản sao lưu database.

## Nguồn

- [Docker quick start](https://docs.litellm.ai/docs/proxy/docker_quick_start)
- [Redis cache](https://docs.litellm.ai/docs/proxy/caching_redis)
- [LiteAdmin MCP](https://docs.litellm.ai/docs/proxy/liteadmin_mcp)
- [Image signatures](https://docs.litellm.ai/docs/proxy/docker_image_security)
