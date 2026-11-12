---
status: accepted
---

# Chọn giao diện điều khiển riêng cho Claude và Kiro

Theo quyết định Q8, plugin Claude dùng Agent SDK chính thức điều khiển binary
Claude CLI, còn plugin Kiro dùng ACP engine V3 được chọn rõ. Orca là nguồn tham
khảo cho cách Claude dùng SDK và hạ tầng ACP chung, nhưng không cung cấp Kiro ACP
adapter đã kiểm chứng tại commit khảo sát; không ép cả hai CLI đi qua cùng một
ACP adapter chỉ để đồng nhất giao thức.

Đổi lại hai plugin có cơ chế transport và dependency khác nhau. Mỗi plugin được
khóa phiên bản riêng; bản đầu không hỗ trợ Kiro V2 và không suy luận engine từ
ACP protocol version. Trước khi bật backend phải kiểm chứng đăng nhập CLI,
tắt công cụ/hooks và hủy request trên tổ hợp phiên bản đã chọn.

Xem [nghiên cứu Orca](../../.scratch/claude-kiro-plugins/design.md#kết-quả-tham-khảo-orca).
