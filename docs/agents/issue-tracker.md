# Issue tracker: Local Markdown

Issues và specs của repo router lưu trong `.scratch/`.
Hai plugin backend vẫn là repo độc lập.

## Conventions

- Mỗi feature có thư mục `.scratch/<feature-slug>/`.
- Spec: `.scratch/<feature-slug>/spec.md`.
- Mỗi ticket là một file `issues/<NN>-<slug>.md`, đánh số từ 01.
- Trạng thái triage ghi bằng dòng `Status:` gần đầu ticket,
  dùng tên trong `triage-labels.md`.
- Bình luận được nối vào cuối file dưới mục `## Comments`.
- “Publish to the issue tracker” nghĩa là tạo file Markdown cục bộ.
- “Fetch the relevant ticket” nghĩa là đọc file ticket được chỉ định.
- Nếu số ticket không đủ xác định feature, hỏi lại đường dẫn.

## Wayfinding operations

- Map: `.scratch/<effort>/map.md`, gồm Notes, Decisions-so-far và Fog.
- Ticket con: `.scratch/<effort>/issues/NN-<slug>.md`.
- `Type:` dùng research, prototype, grilling hoặc task.
- Wayfinder dùng `Status: open`, `claimed`, `resolved`
  cho vòng đời ticket riêng của workflow này.
- `Blocked by: NN, NN` liệt kê các ticket chặn trong cùng effort.
- Ticket được mở chặn khi mọi ticket chặn đều resolved.
- Frontier: ticket open, không bị chặn, chưa được nhận; chọn theo số.
- Claim: lưu `Status: claimed` trước khi bắt đầu làm.
- Resolve: thêm `## Answer`, đặt `Status: resolved`,
  rồi cập nhật Decisions-so-far trong map với liên kết tới ticket.
