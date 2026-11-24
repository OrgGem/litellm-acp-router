---
status: accepted
---

# Một provider LiteLLM sở hữu nhiều backend CLI

Ngày 2026-11-13, người dùng yêu cầu gom plugin RPC tại root để chỉ chạy một
LiteLLM kết nối tất cả agent CLI trong các subproject.

Root xuất distribution `litellm-agent-rpc` và đăng ký một CustomLLM provider
`agent_rpc`. Alias chọn adapter. Factory khởi tạo lười backend trong process;
adapter tiếp tục sở hữu transport, session và process CLI. Root đóng toàn bộ
handle khi shutdown, truyền cancellation và giữ các gate phát hành hiện có.

Wheel gom source adapter từ các repo độc lập. Không cài các distribution con
có ràng buộc LiteLLM xung đột. Root khóa LiteLLM 1.109.0 và chịu trách nhiệm
kiểm tra tương thích tất cả adapter khi nâng dependency hoặc thay source.
Claude SDK là extra; backend tắt không cần import dependency riêng.

Topology này thay thế các HTTP host trung gian. Cấu hình/key cũ được bảo toàn;
chỉ port/key router còn được sử dụng. Thư mục con vẫn có vòng đời Git riêng,
chưa chuyển thành gitlink. Phát hành tái tạo được cần chốt revision nguồn và
cách lấy đủ bốn repo; CI root hiện chỉ kiểm tra ranh giới provider với fixture.

Một tiến trình làm giảm số môi trường và HTTP hop nhưng lỗi nghiêm trọng trong
adapter có thể ảnh hưởng toàn gateway. Mỗi adapter vẫn sở hữu runtime/giới hạn
riêng; lỗi khởi tạo backend được báo 503, không làm vô hiệu các backend khác.
Không thay đổi khả năng chat/session hoặc điều kiện nghiệm thu CLI thực tế.

Codex được bổ sung cwd cho subprocess và thu hồi cây process để thay phần
working directory trước đây thuộc standalone host. Mọi thay đổi source có sẵn
trong các repo adapter vẫn được giữ nguyên.
