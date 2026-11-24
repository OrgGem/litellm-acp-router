# Codex Mini-pod cho LiteLLM

Các model alias ở LiteLLM `http://127.0.0.1:14000/v1` xử lý chat text qua
Codex trong Mini-pod. Mười alias dưới đây chuyển nguyên tên tới Codex:

```text
gpt-6.2-sol     gpt-6.2-astra   gpt-6.1-terra   gpt-6.1-sol
gpt-6-astra     gpt-6-sol       gpt-6-luna
gpt-5.6-sol     gpt-5.6-terra   gpt-5.6-luna
```

Alias `codex-minipod` dùng `gpt-6-luna`. Danh sách ánh xạ nằm trong
`deploy/codex-minipod/models.json`. Mỗi request chọn model theo alias và bridge
kiểm tra model Codex xác nhận khi mở thread. Alias không được hỗ trợ trả HTTP 400.
Deployment này dùng `use_chat_completions_api: true` để LiteLLM chuyển
`/v1/responses` sang chat completions cho bridge và chuyển kết quả về Responses API.
Client gửi toàn bộ lịch sử text cần dùng trong `input`.
AI Employees và các client tương thích OpenAI có thể gửi `tools` (`type: "function"`)
và `tool_choice` (`auto`, `none`, `required`, hoặc chọn tên hàm cụ thể). Bridge
đăng ký `dynamicTools` với Codex, phát `tool_calls` (cả JSON thường lẫn streaming
SSE `delta.tool_calls` kèm `finish_reason: "tool_calls"`), giữ phiên `AppServer`
chờ kết quả `role: "tool"` theo `tool_call_id` (TTL mặc định 180 giây, có fallback
stateless nếu phiên đã hết hạn), trong khi tool nội bộ của Codex vẫn chạy trong Mini-pod.
Bridge trên Windows nghe `127.0.0.1:14002` và xác thực bằng key riêng lưu trong
PostgreSQL của LiteLLM. Codex app-server trên Windows dùng tài khoản ChatGPT
trong profile riêng. Mini-pod chạy `codex exec-server` trong Podman rootless.

Mỗi request mở một thread tạm trong `minipod:/workspace`, với `include_local =
false` và `approval_policy = "never"`. Bridge kiểm tra workspace và xác nhận
app-server từ chối API đọc file host trước khi gửi prompt. Bridge từ chối mọi
yêu cầu RPC xin quyền, và profile tắt hooks, plugins, apps, web search và memories.

Podman chạy dưới các tài khoản guest tách biệt theo slot (`codexec` UID `1100`
cho slot `0`, `codexec1..codexec15` UID `1101..1115` với dải `/etc/subuid` và
`/etc/subgid` riêng khi cấu hình nhiều slot trên cùng 1 VM). Container chạy UID
1000, mạng `none`, bỏ capabilities và dùng `no-new-privileges`. Tất cả các slot
dùng chung một rootfs read-only `/workspace/minipod-codex/rootfs` qua chế độ
overlay `:O` (`--rootfs /workspace/minipod-codex/rootfs:O`, không nhân bản dung
lượng image 791 MB), trong khi mỗi slot có `$XDG_RUNTIME_DIR`, `pause.pid`,
`overlay-storage` và thư mục `/workspace` riêng (`/workspace/codexec/work` cho
slot `0`, `/workspace/slots/<k>/work` cho slot `k`). VM có policy `Isolated`.
Codex dùng VM/container làm ranh giới thực thi lệnh. Process app-server vẫn mang
quyền của tài khoản Windows đã chạy nó. Credential Codex nằm trong
`.local/minipod-codex/bridge/profile` trên Windows.

Mỗi executor của request chạy trong một cgroup v2 riêng của guest với
`pids.max` tối đa 128. Linux tính cả process và thread trong hạn mức này.
Khi hết chỗ, kernel từ chối tạo task mới. Supervisor nằm ngoài cgroup để
dọn lượt chạy và tiếp nhận request tiếp theo. Nếu không thiết lập được
quota, launcher dừng trước khi chạy Codex. Khi cgroup của lượt trước trên cùng
slot bị thu hồi làm chết tiến trình `pause` (`catatonit -P`) của slot đó,
`rootless.sh` kiểm tra trực tiếp `pause.pid` của riêng slot đó để chỉ chạy
`podman system migrate` khi cần (không ảnh hưởng các slot khác đang chạy song
song) và giúp thời gian handshake `environment/info` giữ ở mức ~0.3 giây. `stdio.rs`
cấp hạn mức `900` giây cho `exec` để các chuỗi gọi tool nhiều vòng không bị ngắt
giữa chừng ở mốc 140 giây, và bridge tự động thử lại một lần bước handshake
`environment/info` trước khi bắt đầu lượt nếu gặp lỗi tạm thời.
Sau các vòng gọi tool, nếu model kết thúc lượt với nội dung text rỗng (`""`),
bridge vẫn trả HTTP 200 (`finish_reason: "stop"`) và chỉ kích hoạt `manager.kick()`
khi gặp lỗi hạ tầng Mini-pod thực sự (`InfrastructureError` / `OSError`).

## Gọi thử

Gửi `POST /v1/chat/completions` với một key LiteLLM có quyền gọi alias:

```json
{"model":"codex-minipod","messages":[{"role":"user","content":"Chạy uname -s và trả lại kết quả."}]}
```

Mỗi request thực thi lại và không dùng Redis cache. Bridge không tự chạy lại
lượt Codex đã bắt đầu. Với
`stream: true`, bridge gửi từng đoạn text ngay khi Codex phát sự kiện và kết
thúc SSE khi lượt hoàn tất. Thời gian chờ đoạn đầu gồm khởi tạo Codex, kiểm
tra Mini-pod và thời gian model xử lý trước khi có text. Tin nhắn nhiều lượt
được gửi tới Codex dưới dạng transcript text. Mặc định bridge chạy `1` slot
(`MINIPOD_SLOTS=1`) nhưng xếp hàng chờ FIFO tối đa `60` giây
(`MINIPOD_QUEUE_TIMEOUT_SECONDS=60`) và nhả slot trước khi gửi gói kết thúc
HTTP `200`, chỉ trả HTTP `429` kèm `Retry-After: 5` khi hết thời gian chờ. Khi
đặt `MINIPOD_SLOTS=2..16`, bridge phân phối các lượt chạy song song sang các user
và workspace độc lập ngay trong cùng 1 VM (ưu tiên slot chưa có phiên `tool_calls`
tạm dừng, và ghim phiên nối tiếp `role: "tool"` về đúng slot gốc).

Khi Codex cung cấp thống kê token đầy đủ, phản hồi Chat Completions trả số token
input, output và tổng. Chi tiết cache đọc, cache ghi và reasoning được chuyển
tiếp nếu Codex cung cấp các số hợp lệ. Các lượt stream nhận thêm chunk `usage`
khi gửi `stream_options: {"include_usage": true}`. Giá trị usage là `null` khi
Codex không cung cấp đủ số liệu. Số cache này phản ánh cache token của Codex,
còn Redis response cache của LiteLLM được tắt cho các request này.

## Vận hành

Bridge theo dõi lease của Mini-pod khi khởi động và trong quá trình chạy. Khi
lease còn dưới 12 giờ, bridge gia hạn thêm 24 giờ. Nếu daemon xác nhận sandbox
đã mất, bridge tạo một VM `Isolated`, nạp image `minipod-codex:0.167.0`, kiểm
tra Podman rootless và chạy thử Codex trong container trước khi chuyển sang VM
mới. Quy trình chuẩn bị được thực hiện một lần tại một thời điểm, và tiếp tục
với cùng candidate nếu gặp lỗi giữa chừng. Khi không kết nối được daemon,
bridge thử lại sau khi kết nối được khôi phục.

`GET /healthz` trả 200 khi executor đã sẵn sàng và 503 trong lúc chuẩn bị hoặc
khi daemon gặp lỗi. Request chat chờ tối đa 5 giây để kiểm tra trạng thái. Nếu
executor vẫn chưa sẵn sàng, bridge trả 503 và `Retry-After: 10` trong khi quá
trình chuẩn bị tiếp tục chạy nền. Bridge không phát lại lượt Codex đã bắt đầu.
Workspace nằm trong VM và sẽ bị xóa cùng lease khi VM bị thu hồi.

Trạng thái local nằm trong `.local/minipod-codex/bridge/`, bị Git ignore và giới
hạn cho tài khoản Windows hiện tại. Tài khoản chạy bridge cần truy cập được
daemon qua endpoint trong profile, image Docker cục bộ, Cargo và mini-pod checkout
đã dùng để tạo launcher. Lệnh chuẩn bị có thể mất vài phút.

Bridge lưu UUID của thao tác trước khi gọi `spawn --operation-id`. Nếu kết nối
bị mất, bridge thử lại cùng UUID. Daemon keyed trả lại cùng lease, kiểm tra spec
và từ chối dùng UUID với spec khác. Khi lease đã hết hạn, bridge tạo UUID mới.
Nếu daemon khởi động lại, việc tạo lease mới chờ hoàn tất dọn journal cũ. Log
`bridge.err.log` cho biết giai đoạn đang gặp lỗi. Daemon lưu spec và kết quả
của mỗi UUID trong `spawn-operations` sau khi lease kết thúc để nhận diện các
lần gửi lại, kể cả sau khi khởi động lại.

`settings.json` có thể đặt `spawn_endpoint` tới daemon hỗ trợ `SpawnKeyed` và
`sandbox_cli` tới CLI phiên bản tương ứng. Bridge tiếp tục dùng endpoint hiện
tại cho lease đang hoạt động, rồi chuyển sang `spawn_endpoint` khi cần tạo VM
mới. Với lease thuộc daemon cũ, `legacy_sandbox` phải ghi endpoint, ID, đường
dẫn journal, PID và start token đã xác thực của lease đó. Bridge chờ journal
của lease kết thúc trước khi tạo VM ở daemon mới. Daemon cũ từ chối yêu cầu
keyed và bridge giữ UUID để thử lại. Một marker
từ phiên bản cũ chưa có cả ID sandbox lẫn UUID không thể xác định quyền sở hữu
an toàn và cần được kiểm tra trước khi di chuyển sang daemon keyed.

Nếu bridge dừng hoặc cần đổi cấu hình slot/hàng đợi, chạy từ root repo:

```powershell
# Mặc định: MINIPOD_SLOTS=1, MINIPOD_QUEUE_TIMEOUT_SECONDS=60, MINIPOD_DISK_MB=2048
./deploy/codex-minipod/start-bridge.ps1

# Hoặc thiết lập số user/slot độc lập trên 1 VM, thời gian chờ và dung lượng đĩa VM rồi khởi động lại:
$env:MINIPOD_SLOTS = '10'
$env:MINIPOD_QUEUE_TIMEOUT_SECONDS = '60'
$env:MINIPOD_DISK_MB = '2048'
./deploy/codex-minipod/start-bridge.ps1 -Restart
```

Daemon keyed trên máy hiện tại dùng binary trong
`.local/minipod-keyed-target/release/`. Sau khi source Mini-pod hoặc binary
thay đổi, build vào target riêng khi daemon và bridge tương ứng đã dừng. Dừng
daemon thu hồi các VM thuộc daemon đó:

```powershell
cargo build --release -p sandboxd -p sandbox-cli `
  --manifest-path D:/Git/mini-pod/Cargo.toml `
  --target-dir D:/Git/litellm-acp-router/.local/minipod-keyed-target
```

Để khởi chạy daemon keyed trên máy hiện tại khi endpoint chưa có daemon, dùng
bundle đã ký và state directory riêng:

```powershell
$root = 'D:/Git/litellm-acp-router/.local'
$state = "$root/minipod-codex/daemon-keyed"
New-Item -ItemType Directory -Force -Path $state | Out-Null
$daemonArgs = @('serve',
  '--bundle-dir', 'D:/Git/mini-pod/.cache/bundles-src/x86_64',
  '--pub-key-file', 'D:/Git/mini-pod/.cache/keys/dev-bundle.pub',
  '--state-dir', $state, '--endpoint', 'npipe:////./pipe/minipod-codex-keyed',
  '--max-sandboxes', '1')
Start-Process -FilePath "$root/minipod-keyed-target/release/sandboxd.exe" `
  -ArgumentList $daemonArgs -WindowStyle Hidden
```

Các đường dẫn bundle, khóa công khai, binary và endpoint cần khớp với cài đặt
và `settings.json` trên máy chạy bridge. Tiến trình daemon cần được khởi động
trước khi bridge tạo sandbox mới. Route trong PostgreSQL giữ nguyên vì địa chỉ
bridge và khóa xác thực vẫn dùng lại.

## Kết quả kiểm tra

Podman báo `rootless=true`. Hai chat completion giống nhau qua LiteLLM đều
chạy `uname`, `pwd`, `id -u` và ghi vào file kiểm thử trong VM. Kết quả là
`Linux`, `/workspace`, `1000`, với file tăng từ 1 lên 2 byte. File thử đã xóa.
Bridge và sandboxd phải hoạt động, và lease phải còn hạn để route phục vụ.
