# Codex và Mini-pod

Có thể giữ Codex app-server trên Windows và chỉ chạy `codex exec-server`
trong Podman container bên trong VM QEMU. Cách này cho phép giữ đăng nhập và
gọi model trên host, trong khi shell và workspace của agent nằm ở Mini-pod.
Container chạy dưới user `node`.

## Khởi chạy trên Windows

Khởi động `sandboxd serve` với bundle đã ký theo hướng dẫn Mini-pod. Sau đó chạy
từ root của repo này, với Docker Desktop ở chế độ Linux containers:

```powershell
docker build -t minipod-codex:0.167.0 example/minipod
$miniPodCli = 'D:/Git/mini-pod/target/release/sandbox-cli.exe'
$sandbox = ./example/minipod/start.ps1 -SandboxCli $miniPodCli -Offline
$dockerEndpoint = $sandbox.DockerEndpoint
```

Nếu daemon chạy ở endpoint riêng, truyền `-Endpoint` hoặc đặt `MINIPOD_ENDPOINT`.
Docker Desktop build image, rồi script nạp image vào Podman qua endpoint Docker
riêng của VM. Thời hạn VM mặc định là hai giờ.

## Giữ agent trên Windows, thực thi trong Mini-pod

Codex 0.167.0 đọc `environments.toml` trong thư mục cấu hình Codex. Mẫu dưới
đây dành cho một phiên/profile riêng. Thay URI named pipe bằng giá trị
`$sandbox.DockerEndpoint` vừa nhận. Nếu dùng endpoint khác, cập nhật khi tạo lại VM.

```toml
default = "minipod"
include_local = false

[[environments]]
id = "minipod"
program = "docker.exe"
args = ["--host", "npipe:////./pipe/minipod-docker-56392-3", "exec", "-i", "codex", "codex", "exec-server", "--listen", "stdio://"]
```

Với app-server RPC, ứng dụng chọn môi trường tường minh trong `thread/start`:

```json
{"environments":[{"environmentId":"minipod","cwd":"/workspace"}]}
```

Host gọi `docker exec -i` qua named pipe của Mini-pod. `exec-server` phục vụ
shell và filesystem của môi trường qua stdio, không cần mở cổng TCP. `include_local = false`
bỏ môi trường thực thi local khỏi danh sách. MCP, plugin hoặc hook chạy ở đâu
phụ thuộc cấu hình riêng của chúng. Với yêu cầu tất cả tool phải ở trong Mini-pod,
chỉ bật các tool đã được xác nhận chạy qua executor đó.

Smoke thực tế dùng app-server Windows 0.167.0, mock Responses API trên host và
container trong VM `Isolated`. Tool `exec_command` trả về Linux, `/workspace`,
UID 1000. Profile read-only thử ban đầu chặn lệnh theo policy. Lượt kiểm chứng
dùng policy cho phép chạy lệnh bên trong VM cô lập và không có credential.
Chưa xác minh tool ngoài executor, TUI hoặc inference với tài khoản thật.
Dùng lease của Mini-pod để giới hạn tuổi thọ của executor và thu hồi container
khi phiên kết thúc.

### Giới hạn đọc file trên host

Chạy app-server với thư mục cấu hình Codex riêng, từ một thư mục host trống. Chỉ đưa các file
đã chọn vào workspace trong VM, rồi truyền `environmentId` và `cwd` của VM
ở `thread/start`. Giữ danh sách MCP trống nếu phiên chỉ dùng executor. Không cấp
tool `sandbox_copy_in` với đường dẫn host tùy ý cho model. Cấu hình riêng cho
phiên không được xin quyền bổ sung hoặc gọi tool ngoài executor:

```toml
approval_policy = "never"
web_search = "disabled"

[features]
hooks = false
plugins = false
apps = false
```

Trên 0.167.0, app-server với `include_local = false` từ chối `fs/readFile` với
một file kiểm thử trên Windows, trong khi môi trường `/workspace` vẫn sẵn sàng.
Bật `include_local = true` thì cùng RPC đọc được file đó. Đây là giới hạn của
API filesystem của app-server. Bản thân process Codex ở Windows vẫn có quyền
của tài khoản Windows đã chạy nó. Muốn ràng buộc quyền đọc của cả process, đặt
app-server trong VM, hoặc chạy process từ danh tính Windows bị giới hạn ở tầng OS
và chỉ cấp kết nối tới một executor qua broker tin cậy.

Nguồn cấu hình theo phiên bản:
[Codex environments.toml](https://github.com/openai/codex/blob/rust-v0.167.0/codex-rs/exec-server/src/environment_toml.rs).

## Đăng nhập và chạy toàn bộ Codex trong Mini-pod

Nếu muốn đặt cả agent trong VM, tạo VM mới bằng `start.ps1` với network mặc định.
Network này chỉ cho các đích HTTPS `auth.openai.com`, `chatgpt.com` và
`api.openai.com` qua proxy của Mini-pod.

```powershell
docker --host $dockerEndpoint exec codex codex login --device-auth
docker --host $dockerEndpoint exec codex codex login status
docker --host $dockerEndpoint exec codex codex exec --skip-git-repo-check 'Reply with READY'
```

Hoàn tất đăng nhập bằng URL và mã do CLI hiển thị. Credential và trạng thái
Codex nằm trong home của container và mất khi hủy VM. CLI bắt đầu ở `/workspace`.
Mỗi lần gọi Docker phải dùng đúng `--host $dockerEndpoint` của sandbox.

## Nối với adapter LiteLLM Codex RPC

Sau khi đăng nhập trong container, chạy từ repo đã setup `.venv`. Địa chỉ thử
là `http://127.0.0.1:14001/v1`, với model alias `codex-minipod`.

```powershell
$env:CODEX_RPC_STDIO_COMMAND = ConvertTo-Json -Compress -InputObject @(
    (Get-Command docker -CommandType Application).Source,
    '--host', $dockerEndpoint, 'exec', '-i', 'codex',
    'codex', 'app-server', '--listen', 'stdio://'
)
./.venv/Scripts/python.exe -c 'from serve_codex_rpc.cli import main; raise SystemExit(main())' `
    serve --port 14001 --model-name codex-minipod --max-concurrent 1
```

`docker exec -i` truyền JSON-RPC qua stdin/stdout, nên giữ nguyên chế độ không
TTY cho app-server. Adapter LiteLLM này cung cấp lượt trả lời API. Dùng `codex
exec` trực tiếp trong container cho tác vụ coding trên workspace.

Gia hạn hoặc thu hồi VM qua cùng daemon khi hoàn tất:

```powershell
& $miniPodCli renew $sandbox.SandboxId --ttl-secs 7200
& $miniPodCli destroy $sandbox.SandboxId
```

Dùng cùng tham số `--endpoint` ở hai lệnh trên nếu đã truyền `-Endpoint` khi khởi chạy.
