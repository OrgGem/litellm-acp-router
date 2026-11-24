# Ví dụ dùng LiteLLM Agent RPC

Hai cách triển khai cùng một plugin `agent_rpc`:

| Chế độ | LiteLLM/plugin chạy ở đâu? | CLI và đăng nhập ở đâu? | Hướng dẫn |
| --- | --- | --- | --- |
| Localhost | Python venv trên máy | CLI chính thức trên cùng máy, dùng đăng nhập của bạn | [Localhost](localhost/README.md) |
| Docker | Cùng một container Linux | CLI Linux trong container, đăng nhập vào volume riêng | [Docker](docker/README.md) |

Cả hai cung cấp `http://127.0.0.1:4000/v1`. Chỉ chạy một chế độ trên cổng này
tại một thời điểm. Container không gọi CLI Windows qua `host.docker.internal`:
backend hiện dùng subprocess/stdio trong môi trường của LiteLLM.

Ví dụ mặc định chỉ bật Codex. Bốn adapter đều có trong wheel, nhưng CLI tương
ứng phải có trong môi trường chạy. Claude/Kiro vẫn giữ gate nghiệm thu hiện tại;
không phải cứ bật route là dùng được. Xem [trạng thái backend](../docs/claude-kiro.md).

## Các file

- `config.example.json`: cấu hình tối thiểu cho plugin, chưa có key.
- `init_config.py`: tạo bản config riêng với key ngẫu nhiên; không ghi đè file cũ.
- `client.py`: client OpenAI, dùng chung cho localhost và Docker; mặc định chỉ liệt kê model.
- `litellm_sdk.py`: đăng ký plugin trực tiếp trong Python và gọi `litellm.completion()`.
- `localhost/provider-registration.yaml`: minh họa một provider cho bốn alias.
- `docker/Dockerfile`, `docker/compose.yaml`: cài wheel và Codex CLI trong container.

Các lệnh trong hướng dẫn đều chạy **từ root repository**. `uv build` cần đủ
source bốn adapter trong các thư mục con; chúng vẫn là repo độc lập. Sau khi có
wheel, máy triển khai không cần checkout các repo này.

## Plugin được đăng ký như thế nào?

```yaml
model_list:
  - model_name: codex-default
    litellm_params:
      model: agent_rpc/codex-default
      max_retries: 0
litellm_settings:
  custom_provider_map:
    - provider: agent_rpc
      custom_handler: litellm_agent_rpc.provider.plugin
```

`agent-rpc serve` là launcher cho **LiteLLM Proxy thật**, không phải một HTTP
proxy khác đứng trước LiteLLM. Nó sinh cấu hình này từ JSON, nạp plugin, gắn
ingress policy và cleanup/cancellation. Dùng launcher trong hai ví dụ triển khai
để có đầy đủ các bước đó. Chạy riêng `litellm --config` với đoạn YAML trên chỉ
đăng ký provider, không tự cài ingress hoặc lifecycle của launcher.

Client HTTP gửi alias như `codex-default`; code gọi LiteLLM SDK trực tiếp dùng
prefix như `agent_rpc/codex-default`.

## Gọi API chung

Sau khi server đã chạy, dùng Python trong root venv (hoặc venv cài wheel):

```powershell
# Chỉ liệt kê model, không gọi agent/model:
uv run --no-sync python example/client.py --config example/.local/config.json

# Mỗi lệnh dưới gửi một request thật, có thể dùng quota:
uv run --no-sync python example/client.py --config example/.local/config.json --prompt "Chào bạn"
uv run --no-sync python example/client.py --config example/.local/config.json --prompt "Trả lời một câu ngắn" --stream
```

Với Docker, thay `--config` bằng `example/docker/config.json`. Dùng `--base-url`
nếu đổi host port, và `--model antigravity` khi backend đó được bật và sẵn sàng.
`/v1/models` phản ánh cấu hình, không chứng minh CLI đã đăng nhập hay còn quota.

Codex/Antigravity hiện chỉ nhận một user text message. Claude/Kiro nhận system
message và lịch sử sau khi đạt gate phát hành. Với reuse, gửi `X-Conversation-ID` ổn định
**và toàn bộ lịch sử đã hoàn tất** ở mỗi request; không chỉ gửi câu mới. Tùy chọn
`--conversation-id` trong client minh họa header; script một lượt không lưu lịch
sử. Không dùng header này với Codex/Antigravity.

Không hỗ trợ tools/function calling, ảnh/audio, `/v1/responses`, sampling/token
limit hay tự retry/fallback. SSE trả text delta; usage có thể bị bỏ/null.

## Nhúng trực tiếp vào Python với LiteLLM

Không cần chạy API server nếu ứng dụng Python gọi plugin trực tiếp:

```powershell
uv run --no-sync python example/litellm_sdk.py --config example/.local/config.json --model codex-default --prompt "Chào bạn"
```

Ví dụ gọi một model thật, đóng provider trong `finally`, và chỉ in nội dung
assistant. Dùng backend fake nếu kiểm tra mà không muốn gọi model. Cách nhúng
này không có middleware HTTP của gateway; ứng dụng gọi SDK chịu trách nhiệm
vòng đời provider và không diễn giải usage ước tính của LiteLLM thành usage CLI.
Trong ứng dụng sống lâu, giữ một provider cho vòng đời ứng dụng, không đăng ký
trùng `agent_rpc` cho mỗi request.

## Kiểm chứng và nguồn tham khảo

Kết quả thực hiện các ví dụ được ghi tại [VALIDATION.md](VALIDATION.md).
Semantics cấu hình container tham chiếu [Docker Compose services](https://docs.docker.com/reference/compose-file/services/)
và [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/).
Plugin được đối chiếu với LiteLLM `1.109.0` đã cài trong workspace.
