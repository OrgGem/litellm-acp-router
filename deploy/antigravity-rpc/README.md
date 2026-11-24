# Antigravity ACP Bridge cho LiteLLM

Bridge cục bộ trên Windows lắng nghe tại `127.0.0.1:14004` và phục vụ các model alias
Antigravity (`agy acp`) cho stack LiteLLM Docker (`http://127.0.0.1:14000/v1`, kết nối qua
`http://host.docker.internal:14004/v1` với `use_chat_completions_api: true`).

## Model aliases (`deploy/antigravity-rpc/models.json`)

```text
antigravity             gemini-4-neon           gemini-4-carbon
gemini-4-argon          gemini-4-hydrogen       gemini-3.8-flash
agy-claude-sonnet-4-6   agy-claude-opus-4-6     gpt-oss-120b-medium
```

## Hỗ trợ Streaming Tool Calls & Tool Results

- Nhận `tools` (`type: "function"`), `tool_choice` (`auto`, `none`, `required`, hoặc chọn hàm cụ thể), `parallel_tool_calls`, `stream: true`, và `stream_options: {"include_usage": true}`.
- Mỗi phiên chạy trong workspace cô lập (`commandExecutionPolicy: off`, tắt skills/plugins/built-in tools); khi có `tools`, bridge gắn stdio MCP server cục bộ (`mcp_stdio_bridge.py`) kết nối ngược về callback socket `127.0.0.1` kèm token ngẫu nhiên theo phiên, và chỉ duyệt `session/request_permission` cho các MCP client tools hợp lệ.
- Tự động lọc bỏ trường `toolSummary` mà Antigravity chèn vào đối số MCP nếu schema gốc của client không khai báo `toolSummary` trong `properties`.
- Phát streaming SSE `delta.tool_calls` + `finish_reason: "tool_calls"` (hoặc JSON `message.tool_calls`), giữ phiên chờ `role: "tool"` theo `tool_call_id` (TTL 180 giây), hỗ trợ fallback stateless khi phiên hết hạn và cho phép lượt hoàn tất rỗng (`""`) sau vòng gọi tool.

## Khởi chạy

```powershell
./deploy/antigravity-rpc/start-bridge.ps1
```

Trạng thái và khóa xác thực nằm tại `.local/antigravity-rpc/bridge/` (đã Git-ignore).
