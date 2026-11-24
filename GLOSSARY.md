# Agent CLI Router

Router cung cấp một giao diện chung để ứng dụng gửi yêu cầu tới các agent CLI.

## Language

**Agent CLI**:
Agent được cung cấp dưới dạng chương trình dòng lệnh, nhận yêu cầu và tạo câu trả lời.
_Avoid_: Model, API client.

**Plugin kết nối agent**:
Thành phần tích hợp một loại agent CLI với router, chuyển đổi yêu cầu và kết quả giữa hai bên.
_Avoid_: Plugin của Claude/Kiro, model.

**API client**:
Ứng dụng gửi yêu cầu tới API của router và sử dụng câu trả lời nhận được.
_Avoid_: Agent CLI, backend.

**Model alias**:
Tên model mà API client dùng để chọn cấu hình agent/model được router cung cấp.
_Avoid_: Model ID của nhà cung cấp.

**Công cụ nội bộ của CLI**:
Công cụ do agent CLI trực tiếp thực thi, như đọc/sửa file, chạy lệnh hoặc gọi dịch vụ qua MCP.
_Avoid_: OpenAI function calling, công cụ do API client thực thi.

**Lịch sử do client cung cấp**:
Các lượt hội thoại trước được API client gửi kèm yêu cầu hiện tại để cung cấp ngữ cảnh trả lời.
_Avoid_: Session của CLI, bộ nhớ tự động của router.

**Tương thích OpenAI có giới hạn**:
Mức tương thích của router với các thao tác chat được công bố, trong đó nội dung hội thoại được chuyển sang agent nhưng không cam kết giữ nguyên mức ưu tiên của từng vai trò.
_Avoid_: Tương thích OpenAI đầy đủ, giữ nguyên ngữ nghĩa role.

**Hội thoại gateway**:
Chuỗi trao đổi mà API client nhận diện bằng một mã ổn định để yêu cầu tiếp tục ngữ cảnh qua nhiều request.
_Avoid_: CLI session ID, API key.

**Session CLI**:
Ngữ cảnh hội thoại do agent CLI quản lý, có thể được sử dụng cho nhiều lượt trả lời liên tiếp.
_Avoid_: HTTP request, hội thoại gateway.

**Lượt trả lời**:
Một lần agent xử lý đầu vào và kết thúc bằng kết quả thành công, hủy hoặc lỗi.
_Avoid_: Session CLI, toàn bộ hội thoại.
