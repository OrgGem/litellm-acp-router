# TUI quản lý Agent RPC

TUI chạy trên máy người dùng và quản lý một gateway local hoặc một service
Docker Compose được chọn. Cài extra `tui` cùng môi trường của package:

```powershell
uv sync --extra tui
uv run --no-sync agent-rpc tui --config .local/config.json
```

File cấu hình phải tồn tại trước. Trong source checkout, có thể tạo file mẫu
với `python example/init_config.py .local/config.json`; lệnh này sinh gateway
key mới. Không dùng nó để ghi đè cấu hình đang dùng. TUI giữ gateway key và các
trường khác trong file khi lưu thay đổi.

Ví dụ chọn Docker ngay khi mở TUI:

```powershell
uv run --no-sync agent-rpc tui --config example/docker/config.json --mode docker --compose example/docker/compose.yaml --project agent-rpc-manager --service litellm
```

Chọn đúng file cấu hình host được container sử dụng. Compose file phải tồn tại;
project phải được chỉ định rõ. Có thể đổi target trong **Overview**; TUI giữ
controller của các target đã mở trong cùng phiên. Gateway chỉ được quản lý khi
do chính phiên TUI này khởi chạy. Process/container do nơi khác khởi chạy không
được tự nhận quyền điều khiển, kể cả khi dùng cùng cổng hoặc Compose project.
Service Compose phải chỉ publish cổng container `4000/tcp` trên `127.0.0.1`,
không dùng host networking, `container_name` cố định hoặc chính sách tự restart.

## Service và cấu hình

- **Overview** có Start, Stop, Restart, Resume, Refresh và Doctor. Trạng thái
  cổng, request đang chạy và phiên bản cấu hình được hiển thị khi có nguồn xác
  thực; `N/A` hoặc `Unknown` có nghĩa là chưa biết.
- Khi Stop/Restart, controller ngừng nhận request mới và chờ request đang chạy
  kết thúc trước khi dừng. Nếu chờ hết hạn, service vẫn ở trạng thái drain;
  dùng Resume hoặc chờ rồi thử lại. Không tự ép dừng tiến trình không sở hữu.
- Quit hoặc Ctrl+Q drain và dừng **mọi** service local/Docker mà phiên TUI này
  đã khởi chạy, kể cả target đã chuyển khỏi; nếu không dừng được thì TUI vẫn mở.
  Service không do TUI sở hữu không bị dừng. Khi process TUI bị kết thúc cưỡng
  bức, không thể bảo đảm quy trình thoát bình thường; phiên TUI mới không tự
  tiếp quản một container còn chạy.
- Doctor phải được bấm rõ ràng và không gửi prompt. Doctor local kiểm tra file
  đã lưu; Doctor Docker kiểm tra snapshot cấu hình trong container đang chạy.
  Kết quả preflight không chứng minh entitlement, quota hay quyền dùng model.
- Tại **Backends** hoặc **Models & Routes**, sửa form rồi bấm **Stage**. Bấm
  **Save config** để ghi nguyên file một cách atomic; nếu file đã bị sửa ngoài
  TUI, lưu bị từ chối để tránh ghi đè. **Reload config** bỏ thay đổi chưa lưu.
  Cấu hình lưu và cấu hình đang chạy có thể khác nhau; Restart áp dụng bản đã
  lưu sau khi drain. Với Docker, controller tạo lại container để nhận file mới.
  Bản nháp có thể tắt mọi route; gateway không khởi động nếu không có route bật.

## Backend, tài khoản và routes

Chọn backend trong bảng để sửa trạng thái bật và đường dẫn executable; để trống
để dùng default của adapter. **Inspect** kiểm tra executable, phiên bản CLI và
dependency của backend trong cấu hình local đã lưu hoặc container Docker đang
chạy; nó không đăng nhập, gửi prompt hay chứng minh backend sẵn sàng. Nếu cần,
**Install extra** cài phiên bản Claude SDK đã pin vào Python local sau khi xác
nhận và khi mọi service local do TUI quản lý đã dừng. Việc cài có thể tải package
và thay đổi môi trường Python hiện tại; chạy Inspect/Doctor lại sau khi cài.
Docker không được cài trực tiếp vào container đang chạy: build lại image với extra
phù hợp, ví dụ `PLUGIN_EXTRAS: "[claude]"` trong Compose mẫu.

Nút Login, Logout và Check chỉ hoạt động nếu
controller hỗ trợ thao tác đó và service của target đã dừng. TUI nhường terminal
cho CLI chính thức, không thu hay lưu token. Logout yêu cầu xác nhận và tác động
tài khoản CLI thật trong môi trường đã chọn. Docker example chỉ cài CLI Codex;
đăng nhập trong container dùng credential riêng, không sao chép từ host. Các
backend hoặc action không hỗ trợ được vô hiệu hóa. Claude và Kiro vẫn bị gate
theo trạng thái adapter; cài CLI hoặc đăng nhập không tự vượt gate.
Login và logout chỉ được phép khi mọi service do phiên TUI quản lý cùng profile
đã dừng (local, hoặc các service Docker trong cùng Compose project). Với service
khởi chạy từ bên ngoài và volume/profile được chia sẻ tùy chỉnh, người vận hành
cần tự phối hợp dừng service trước khi thay đổi đăng nhập.

Trong **Models & Routes**, chọn một dòng để sửa hoặc bấm New để thêm alias,
điền backend và model ID, rồi Stage. Có thể tắt hoặc xóa route, và chọn một
route đang hoạt động làm default. Default hiện là metadata của UI; API client
vẫn phải gửi trường `model`. Một backend có thể có nhiều alias. Model ID nhập
thủ công là **chưa xác minh**; ô trống dùng default của CLI khi adapter hỗ trợ,
không khẳng định model đã được resolve. Antigravity cần model ID khi route và
backend cùng được bật; chỉ Antigravity hiện hỗ trợ `thought_level`. TUI không
cung cấp thiết lập effort cho adapter chưa hỗ trợ.

## Usage và workflows

**Usage** lấy metadata của service được chọn, không cộng dữ liệu local với
Docker. Dashboard hiển thị request, trạng thái, latency, TTFT và token do adapter
báo khi có nguồn xác thực; token thiếu là `N/A`, không phải zero. Các số token
chỉ bao gồm request đã có nguồn tương ứng; chúng không đại diện chi phí, quota
hay credit. Dữ liệu hiện chỉ truy vấn qua service đang được quản lý. Metadata
local nằm cạnh file config; Docker dùng volume quản lý riêng theo Compose project
và service, được giữ lại qua lần tạo lại container và không bị TUI xóa.
Docker image tùy chỉnh cần cho service user quyền ghi vào
`/home/agent/.local/state/agent-rpc`.

**Workflows** luôn tắt trong bản này: chưa có engine, scheduler, agent delegation
hay chạy task. Có thể chủ động lưu bản nháp JSON tại file bên cạnh config, tên
`<config-stem>.workflow-draft.json`. File chứa envelope `enabled: false` và
`draft`; TUI chỉ kiểm tra JSON object, chưa xác nhận schema nghiệp vụ. Draft
không được router đọc hoặc chạy. Không có file được tạo chỉ vì mở màn hình.
Nội dung draft có thể là dữ liệu riêng tư; chỉ lưu khi có chủ ý.
