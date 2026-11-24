# LiteLLM + plugin trên localhost

Mọi lệnh chạy từ root repository. Có Python 3.11–3.13, `uv`, và Codex CLI trên
PATH. Đăng nhập bằng `codex login` nếu chưa đăng nhập. Plugin không sao chép token.

## 1. Cài plugin

Trong workspace đã có source của bốn adapter:

```powershell
uv sync --frozen
python example/init_config.py example/.local/config.json
```

Lệnh tạo config ví dụ riêng, không thay `.local/config.json` đang dùng tại root.
`enabled.codex=true`, ba backend khác false. Key được sinh ngẫu nhiên và lưu
trong config bị Git ignore. Script giữ nguyên file đã tồn tại.

Để triển khai từ wheel lên máy khác, thay bước `uv sync` bằng:

```powershell
# Tại workspace nguồn:
uv build

# Tại máy triển khai, sau khi chép wheel và thư mục example sang:
python -m venv .venv
.venv/Scripts/python.exe -m pip install dist/litellm_agent_rpc-0.2.0.dev0-py3-none-any.whl
python example/init_config.py example/.local/config.json
```

Trên Linux/macOS dùng `.venv/bin/python` và `.venv/bin/agent-rpc` thay đường dẫn
`Scripts`. Chỉ cài distribution root, không cài thêm bốn distribution con.
Nếu cài bằng pip, dùng `.venv/Scripts/python.exe` thay `uv run --no-sync python`
và `.venv/Scripts/agent-rpc.exe` thay `uv run --no-sync agent-rpc` ở các lệnh sau.

## 2. Kiểm tra và chạy

```powershell
uv run --no-sync agent-rpc doctor --config example/.local/config.json
uv run --no-sync agent-rpc serve --config example/.local/config.json
```

Doctor kiểm tra CLI/login và đóng runtime, không gửi prompt. Server mặc định bind
`127.0.0.1:4000`. Ctrl+C để dừng. Đổi cổng bằng `--port 4001` nếu cổng 4000 đã có
dịch vụ; khi gọi client, thêm `--base-url http://127.0.0.1:4001/v1`.

Ở terminal thứ hai:

```powershell
uv run --no-sync python example/client.py --config example/.local/config.json
uv run --no-sync python example/client.py --config example/.local/config.json --prompt "Chào bạn" --stream
```

Lệnh đầu chỉ liệt kê model. Lệnh có `--prompt` gửi request model thật.

## 3. Bật backend khác

Chỉnh config ví dụ rồi restart gateway:

| Backend | Việc cần làm | Alias HTTP |
| --- | --- | --- |
| Codex | `enabled.codex=true`; `executables.codex` là CLI trên PATH hoặc đường dẫn tuyệt đối; `codex_model=null` dùng mặc định CLI | `codex-default` |
| Antigravity | Cài/đăng nhập `agy` 1.5.3, bật flag, điền model/thought level tài khoản hỗ trợ trong `antigravity_models`; giá trị mẫu cần preflight xác nhận | `antigravity` |
| Claude | Cài `uv sync --frozen --extra claude`, hoàn tất gate nghiệm thu, rồi bật flag; mặc định dùng CLI đi kèm SDK | `claude-default` |
| Kiro | Cài CLI V3 phù hợp và hoàn tất gate cô lập/login, rồi bật flag | `kiro-default` |

Nếu cài wheel bằng pip và cần Claude extra:

```powershell
.venv/Scripts/python.exe -m pip install "dist/litellm_agent_rpc-0.2.0.dev0-py3-none-any.whl[claude]"
```

Hai backend mới vẫn bị gate dù có dependency/CLI. Không có cờ bypass trong ví dụ.
Chỉ CLI tương ứng với backend bật mới được khởi tạo khi có request hoặc doctor.

## Cấu hình LiteLLM để tham khảo

[provider-registration.yaml](provider-registration.yaml) minh họa map bốn alias
về cùng provider. Đây không phải file input của `agent-rpc`: launcher lấy JSON
ở `--config` và tự sinh model list chỉ cho backend bật. Giữ launcher để có ingress,
truyền conversation header, timeout/cancellation và đóng runtime khi shutdown.
