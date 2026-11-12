---
status: accepted
---

# Tái sử dụng session theo hội thoại gateway và lịch sử đã hoàn tất

Người dùng ưu tiên giảm độ trễ bằng cách tái sử dụng session CLI. Theo Q13/Q14,
client có thể gửi `X-Conversation-ID` để nhận diện hội thoại; đây là phần mở rộng
của gateway, không phải session ID của CLI hay trường chuẩn của OpenAI Chat
Completions. Không có header thì tạo session mới. Session chỉ được tiếp tục khi
lịch sử khớp hội thoại đã hoàn tất và cấu hình vẫn phù hợp; đổi lịch sử, system
hoặc model thì tạo session mới từ đầy đủ messages.

Đổi lại gateway phải quản lý vòng đời, đồng bộ request và xác minh lịch sử,
thay vì giả định mọi request độc lập. Việc dùng lại session không biến lịch sử
được nhập dạng văn bản ban đầu thành role semantics nguyên vẹn; ADR-0001 vẫn áp dụng.
Header không thay thế xác thực và không cho phép client truy cập một CLI session
tùy ý. Kế hoạch đã được xác nhận ngày 2026-11-13: tối đa 2 lượt đồng thời và
4 session giữ ấm/backend, idle TTL 10 phút, deadline backend 300 giây. Session
gặp lỗi/hủy/disconnect bị loại khỏi pool, không tự retry; mapping chỉ giữ trong
RAM và không được phục hồi sau restart host.

Xem [kế hoạch](../../.scratch/claude-kiro-plugins/plan.md).
