# V2: cấu hình, control và TUI

V2 là đường chạy opt-in cho chat, client tool calling, reasoning và usage khi
driver có evidence tương ứng. **Hiện mọi backend CLI V2 vẫn bị gate**; Claude
được giữ đóng theo yêu cầu. Ví dụ HTTP kiểm thử qua LiteLLM dùng driver giả,
không chứng minh CLI thật, account, cô lập profile hay Docker đã được nghiệm thu.
Không đổi gate hoặc coi config này là bằng chứng readiness.

## Chuẩn bị trên localhost

Từ root, tạo một bản riêng; không ghi đè config đang dùng:

```powershell
New-Item -ItemType Directory -Path .local -Force | Out-Null
[System.IO.File]::Copy((Resolve-Path 'example/v2/config.example.json').Path, (Join-Path (Resolve-Path '.local').Path 'v2-example.json'), $false)
```

Thay `keys.router` bằng key ngẫu nhiên riêng được giữ bí mật. Placeholder trong
file chỉ minh họa, không dùng làm key thật. Alias `coding` trỏ tới backend Codex
và model mặc định của CLI; có thể nhập model ID cụ thể sau khi tự xác minh.
`v2.enabled` mặc định `false`: route tiếp tục dùng V1, còn profile V2 chỉ được
lưu nháp. Config mới không sửa key hay file hiện có của bạn.

```powershell
uv sync --frozen --extra tui
uv run --no-sync agent-rpc capabilities --config .local/v2-example.json
uv run --no-sync agent-rpc tui --config .local/v2-example.json
```

`capabilities` chỉ đọc manifest; không mở session, đăng nhập hay gọi model.
TUI cũng không tự gọi model hoặc đổi đăng nhập khi mở. Tại **Models & Routes**,
chọn alias, backend, model, **Route uses V2**, profile ID/revision và transport;
Stage rồi Save. **V2 enabled** là công tắc chung; sau khi bật và restart, request
đến route V2 vẫn bị từ chối nếu capability hoặc profile isolation chưa qua gate.
Không tự fallback về V1 cho route V2. Bật CLI, cài SDK hoặc login không mở gate.

`profile_id` và `profile_revision` là định danh do host quản lý, gồm chữ/số
ASCII, `.`, `_`, `:` hoặc `-`; chúng không phải token hay tên session của CLI.
Transport hiện khai báo `stdio` cho Claude/Kiro; `stdio-acp` cho Antigravity;
Codex có `stdio`, `unix` hoặc `websocket` nhưng vẫn phải qua gate của driver.
Không đặt credential, URL tùy ý hay executable trong profile V2 này.

### Answer-only

Trong TUI, chọn **Execution mode → Answer only** cho một alias V2 riêng. Khi
chỉnh JSON trực tiếp, thêm `"execution_mode": "answer_only"` vào profile
tương ứng trong `v2.routes`. Chế độ này cần `stdio` cho Codex, Claude hoặc Kiro
và `stdio-acp` cho Antigravity. Nếu thiếu trường, mode là `native`.

Capability và TUI hiển thị tình trạng mode và nguồn đề xuất tool. Gate
answer-only vẫn đóng cho đến khi profile/CLI/OS được nghiệm thu live. Khi gate
đã mở, client chạy ví dụ `uv run --no-sync python example/v2/answer_only_client.py`
với `ROUTER_API_KEY` trong environment và `ROUTER_MODEL` là alias answer-only.
Ví dụ chỉ cho phép hàm `echo` cục bộ và kiểm tra tên, đối số và số lượt trước
khi thực thi. Client thực tế cần kiểm tra quyền trên chính tài nguyên của mình.

Mỗi bước dùng session CLI mới và output được buffer trước khi gửi. Đề xuất tool
là JSON có cấu trúc được gateway kiểm tra rồi chuyển thành `tool_calls`; nội dung
tool result và lịch sử vẫn có thể chứa instruction gây sai lệch câu trả lời.
Không có bằng chứng live hay cam kết loại bỏ harness CLI từ fixture này.

`v2.limits` tùy chọn: `max_active`, `max_sessions`, `native_seconds`,
`chain_seconds`, `tool_wait_seconds`, `cleanup_seconds`, `max_handoffs`.
Các giới hạn này không bật capability. Sau khi thay config, lưu rồi drain và
restart service do TUI sở hữu để áp dụng; pending phiên cũ không khôi phục sau
restart. Lệnh chạy foreground tương ứng:

```powershell
uv run --no-sync agent-rpc serve --config .local/v2-example.json
```

## Auth và request

`auth_mode: "owner"` là mặc định: client dùng `keys.router`; key này tách biệt
với tài khoản đã đăng nhập trong CLI. TUI chỉ chạy login/logout khi người vận
hành chọn rõ và điều kiện dừng service cho phép.

`auth_mode: "virtual_keys"` là tùy chọn nâng cao. Tất cả alias đang bật phải
thuộc `v2.routes` và mọi profile phải được chứng minh cô lập trước khi dùng
nhiều key. Cần LiteLLM Prisma đang hoạt động với nguồn writer và virtual keys đã
được cấp model grants tường minh. Các key có policy phụ thuộc team/user/org,
budget/rate hoặc đối tượng ngoài phạm vi xác minh hiện bị từ chối. Ví dụ này
không cài database, migrate hay tạo key. Nếu registry không khả dụng, gateway
fail closed; không chuyển sang owner hoặc tài khoản CLI khác.

Khi các gate thật sự đã được mở bằng nghiệm thu phiên bản tương ứng, request
text-only có dạng OpenAI-compatible. Đặt key trong biến môi trường của terminal,
không đưa key vào ví dụ được commit:

```powershell
$headers = @{
  Authorization = "Bearer $env:ROUTER_API_KEY"
  "X-Conversation-ID" = "example-conversation-1"
}
$body = @{
  model = "coding"
  messages = @(@{ role = "user"; content = "Xin chào" })
  stream = $false
} | ConvertTo-Json -Depth 20
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:4000/v1/chat/completions -Headers $headers -ContentType "application/json" -Body $body
```

Với tools, client gửi schema function và `X-Conversation-ID` ổn định; nếu
driver đã hỗ trợ, client tự chạy tool rồi gửi đủ lịch sử và `tool_call_id` để
tiếp tục. Gateway không tự chạy function client. Chỉ các trường có capability
đã xác minh mới được nhận; reasoning/usage chưa biết không biến thành token 0.
Ở trạng thái hiện tại, request V2 có thể trả lỗi capability/profile chưa sẵn sàng.

## Theo dõi và dừng

Tab **V2 Sessions** hiển thị capability/evidence, số lượt native, prepared,
đang chờ client, cleanup và quarantine; chọn một pending session rồi **Cancel
selected** để xác nhận hủy. Nếu mạng control timeout, kết quả hủy có thể chưa
rõ: Refresh trạng thái trước khi thử lại. **Usage** hiển thị nguồn, scope key,
completeness và token/cache/reasoning đã quan sát; các tổng có thể chỉ là một
phần và không phải chi phí hay quota.

Stop/Restart đóng admission mới và chờ HTTP, native, pending, cleanup và
quarantine; continuation đã xác thực và thuộc phiên đang chờ có thể hoàn tất
trong lúc drain. Hết hạn chờ thì service vẫn drain để người vận hành xem, hủy
hoặc Resume. TUI chỉ dừng service do chính phiên đó sở hữu.

Ledger V2 là `usage-v2.sqlite`, tách khỏi telemetry V1. Với service do TUI quản
lý, file nằm cạnh telemetry của target; chạy `serve --config` trực tiếp sẽ dùng
thư mục config khi chưa đặt `AGENT_RPC_TELEMETRY_PATH`. Nếu không lưu được, trạng
thái báo degraded; dữ liệu và key CLI không được tự xóa khi dừng service.

## Docker

[Ví dụ Docker hiện có](../docker/README.md) giải thích build/mount và CLI login
trong container. TUI có thể chọn target Docker với Compose project và service
riêng; profile/login container tách với Windows host. Image mẫu chỉ cài Codex
CLI. Muốn dùng config V2 trong container, chuẩn bị bản config được mount theo
hướng dẫn Docker, giữ key ngoài image và cấp nơi ghi metadata. Virtual-key mode
cần Prisma riêng và all-alias isolation đã nghiệm thu. **Chưa có nghiệm thu
V2 với CLI thật trên Docker/Linux**; HTTP fixture không thay thế kiểm thử đó.
