# Claude Code Bridge cho LiteLLM

Bridge cục bộ trên Windows lắng nghe tại `127.0.0.1:14003` và phục vụ các model alias
Claude Code cho stack LiteLLM Docker (`http://127.0.0.1:14000/v1`, kết nối qua
`http://host.docker.internal:14003/v1` với `use_chat_completions_api: true`).

## Model aliases (`deploy/claude-rpc/models.json`)

```text
claude-code       claude-default
claude-opus-5-5   claude-sonnet-4-6
claude-opus-4-6   claude-haiku-4-5
```

`claude-code` và `claude-default` dùng model mặc định của Claude CLI đã đăng nhập.

## Hỗ trợ Streaming Tool Calls & Tool Results

- Nhận `tools` (`type: "function"`), `tool_choice` (`auto`, `none`, `required`, hoặc chọn hàm cụ thể), `parallel_tool_calls`, `stream: true`, và `stream_options: {"include_usage": true}`.
- Tắt toàn bộ built-in CLI tools (`tools=[]`), skills, hooks và plugins; chỉ gắn in-process SDK MCP server (`client_tools`) cho các tool mà client khai báo trong request và cấp quyền qua callback `can_use_tool`.
- Phát streaming SSE `delta.tool_calls` + `finish_reason: "tool_calls"` (hoặc JSON `message.tool_calls` khi `stream: false`), tạm dừng phiên theo `tool_call_id` (`call_<24hex>`, TTL mặc định 180 giây, nhả slot thực thi trong lúc chờ), và tiếp tục ngay khi nhận `role: "tool"`.
- Tự động fallback sang transcript stateless nếu `tool_call_id` từ bên ngoài hoặc phiên đã hết hạn TTL; cho phép lượt kết thúc rỗng (`""`) sau vòng gọi tool trả HTTP 200 (`finish_reason: "stop"`).

## Khởi chạy

```powershell
./deploy/claude-rpc/start-bridge.ps1
```

Trạng thái và khóa xác thực nằm tại `.local/claude-rpc/bridge/` (đã Git-ignore).
