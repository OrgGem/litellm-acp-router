# Domain docs

## Layout

Repo router dùng single-context:
- `GLOSSARY.md`: thuật ngữ chung.
- `docs/adr/`: các quyết định kiến trúc.

Hai plugin backend là repo độc lập; khi làm việc trong từng plugin,
đọc thêm hướng dẫn và tài liệu thuộc repo đó.

## Consumer rules

- Trước khi khám phá code, đọc GLOSSARY.md nếu có và các ADR liên quan.
- Nếu tài liệu chưa tồn tại, tiếp tục mà không yêu cầu tạo trước.
- Domain-modeling tạo tài liệu khi thuật ngữ hoặc quyết định được chốt.
- Dùng thuật ngữ đã định nghĩa trong glossary, tránh tự đổi tên đồng nghĩa.
- Nếu đề xuất trái với ADR hiện có, nêu rõ ADR và lý do cần xem xét lại.
