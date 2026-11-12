---
status: accepted
---

# Chấp nhận chuyển đổi lịch sử có giới hạn khi gọi agent CLI

Claude/Kiro cần nhận hội thoại do API client cung cấp, nhưng các giao diện đã
khảo sát chưa chứng minh hỗ trợ nhập lịch sử role tùy ý như OpenAI Chat
Completions. Theo quyết định Q7, plugin chuyển đầy đủ lịch sử thành văn bản có
nhãn role, dùng system prompt riêng khi CLI hỗ trợ và công bố rõ giới hạn ngữ
nghĩa, để giữ mục tiêu sử dụng agent CLI mà không bắt client quản lý session CLI.

Không mô tả việc giữ nội dung/thứ tự như bằng chứng giữ nguyên mức ưu tiên role.
Không sửa transcript nội bộ của CLI hay phát lại từng lượt lịch sử thành các
lần gọi model để giả lập khả năng nhập lịch sử. Nếu sau này cần role semantics
nguyên vẹn, phải xem xét lại hợp đồng tích hợp và ADR này.

Xem [nghiên cứu và quyết định](../../.scratch/claude-kiro-plugins/design.md).
