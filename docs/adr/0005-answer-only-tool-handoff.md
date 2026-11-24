---
status: accepted
---

# Dùng lượt trả lời độc lập cho handoff tool answer-only

Gateway bổ sung chế độ opt-in `answer_only` theo model alias. Mỗi bước chạy
trong process, profile và session CLI mới, rồi đóng trước khi gateway trả kết
quả hoặc đề xuất tool cho API client. Client thực thi tool và gửi kết quả để
gateway tạo lượt CLI mới. Cách này tách vòng lặp hội thoại khỏi session CLI và
đổi độ trễ khởi động lấy ranh giới vòng đời dễ kiểm chứng hơn.

Khi có tools, đề xuất được phân tích từ output có cấu trúc và kiểm tra theo
schema, policy và quyền trước handoff. Đây là semantics riêng, không thay thế
native tool calling của V2. Theo ADR-0001, việc gắn nhãn lịch sử trong prompt
không bảo đảm mức ưu tiên role. Việc vô hiệu công cụ, hooks và cấu hình kế thừa
cần được chứng minh riêng cho từng phiên bản/profile trước khi mở gate.
