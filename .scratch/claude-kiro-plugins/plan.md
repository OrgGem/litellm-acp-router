# Kế hoạch bổ sung plugin Claude CLI và Kiro CLI

Status: implementation in progress; live acceptance blocked
Confirmed: 2026-11-13 (Asia/Bangkok)

Người dùng đã xác nhận kế hoạch sau Q1–Q14, bao gồm các mặc định vận hành dưới đây.
Quyết định và bằng chứng khảo sát được ghi trong [design.md](design.md).
Người dùng đã yêu cầu bắt đầu implement vào `litellm-claude-rpc` và
`litellm-kiro-rpc`, rồi tích hợp router. Xem [implementation.md](implementation.md)
cho bằng chứng hiện tại và phần nghiệm thu còn thiếu. Chưa gọi model thật hoặc push repo.

## Kết quả cần đạt

Ứng dụng gọi một LiteLLM endpoint localhost trên Windows để sử dụng Codex,
Antigravity, Claude hoặc Kiro theo backend được bật. Claude/Kiro nhận system,
lịch sử user/assistant và trả completion hoặc SSE. Hai plugin mới không thực thi
công cụ nội bộ. Mỗi plugin dùng một tài khoản do người dùng đăng nhập CLI chính
thức, không đọc/sao chép token. Backend có thể phát hành độc lập sau nghiệm thu.

## Phạm vi API

- GET `/v1/models`: công bố alias đã bật; danh sách model không chứng minh quota.
- POST `/v1/chat/completions`: `model`, `messages`, `stream` và
  `stream_options.include_usage`.
- Input bản đầu: các role `system`, `user`, `assistant`; nội dung string
  hoặc mảng chỉ có text blocks. System ở đầu, ít nhất một user, lượt cuối là user.
  Giữ nguyên text, kể cả Unicode và chuỗi rỗng; kiểm tra giới hạn trước khi spawn.
- Không nhận `developer`, tool roles/calls, ảnh/audio, JSON schema, sampling/token
  limits hoặc các endpoint như Responses. Trả lỗi rõ cho input ngoài hợp đồng,
  không âm thầm bỏ tham số hay cắt lịch sử cho vừa context.
- Bootstrap session mới bằng lịch sử có nhãn role. Dùng system prompt riêng khi
  có đường cấu hình đã kiểm chứng; nếu không, đưa system vào phần chuyển đổi
  được công bố. Không hứa mức ưu tiên role giống OpenAI.
- Usage chỉ xuất số liệu backend thực cung cấp. Nếu counter là tích lũy session,
  chỉ lấy chênh lệch khi chứng minh được ý nghĩa; thiếu dữ liệu thì không tự đoán.
- Stream chỉ chứa text trả lời và usage được hỗ trợ, không đưa thought/plan ra
  ngoài. Không phát text hai lần khi SDK vừa có partial events vừa có final message.
- Lỗi trước headers dùng HTTP error; lỗi sau headers kết thúc stream bằng lỗi,
  không biến lỗi thành finish thành công hay `[DONE]`.

## Ranh giới các repo

Vị trí triển khai do người dùng chỉ định ngày 2026-11-13:

- Claude: `D:\Git\litellm-acp-router\litellm-claude-rpc`.
- Kiro: `D:\Git\litellm-acp-router\litellm-kiro-rpc`.

Source, tests, dependency/lockfile, build và tài liệu riêng của mỗi plugin nằm
trong đúng thư mục tương ứng. Repo gốc quản lý routing, cấu hình/migration,
metadata hội thoại và kiểm thử tích hợp giữa các backend.

| Repo | Trách nhiệm |
| --- | --- |
| `litellm-acp-router` | Cấu hình backend tùy chọn, định tuyến, chuyển metadata hội thoại, migration, test tích hợp |
| `litellm-claude-rpc` | LiteLLM provider và API host, official Claude Agent SDK, quản lý session/process Claude |
| `litellm-kiro-rpc` | LiteLLM provider và API host, ACP engine V3, quản lý session/process Kiro |

Giữ các repo và môi trường dependency độc lập như hai plugin hiện có. Đề xuất
Python cho API host/provider; ưu tiên official Python Agent SDK cho Claude nếu
đợt kiểm chứng đầu tiên đạt đủ lifecycle trên Windows. Nếu thiếu khả năng bắt
buộc, dừng chọn runtime và đưa phương án SDK TypeScript để duyệt, không âm thầm
thêm sidecar. Không gom hai adapter hiện hữu thành shared library trong đợt này.

## Hội thoại và session reuse

1. Xác thực HTTP trước khi tra session. Header tùy chọn `X-Conversation-ID` là
   khóa client đặt, không phải quyền truy cập session tùy ý.
2. Scope khóa theo backend và danh tính client đã xác thực. Bản local một master
   key có một scope chủ sở hữu; không tuyên bố cô lập nhiều người cùng chia sẻ key.
   Không lấy danh tính từ header tự khai của client.
3. Không có header: session dùng một request, giải phóng sau kết quả hoặc lỗi.
4. Có header: serialize các request cùng hội thoại bằng lock; kiểm tra lại lịch sử
   sau khi lấy lock. Không đưa hai HTTP request đang chạy vào cùng input queue CLI.
5. Chỉ reuse session còn khỏe, idle, đã có terminal result thành công, cùng model
   thực/cấu hình và cùng fingerprint system/history. Body đầy đủ của request luôn
   là nguồn đối chiếu, không tự dùng ký ức ngoài lịch sử client đã gửi.
6. Fast path: messages khớp toàn bộ transcript đã hoàn tất và thêm đúng
   một user message mới; chỉ gửi user message mới. Trường hợp khác bootstrap
   session mới từ toàn bộ messages, không phát lại từng lượt lịch sử để gọi model.
7. Đổi system, sửa/xóa lịch sử, đổi model hoặc config: dùng session mới. Khi đổi
   backend, session thuộc namespace backend mới; session cũ được thu hồi khi idle
   hết hạn. Không nối hoặc chuyển session giữa hai agent khác nhau.
8. Chỉ cập nhật lịch sử đã hoàn tất sau terminal result hợp lệ và kết thúc HTTP/SSE
   thành công ở mức gateway quan sát được. Không dùng text stream dở làm lịch sử chuẩn.
9. Sau timeout/cancel/disconnect/lỗi hoặc mất transport: đánh dấu session
   không còn đủ tin cậy để reuse, hủy lượt đang chạy, drain terminal nếu có rồi
   đóng process bằng deadline cleanup; lần sau bootstrap lại từ messages của client.
10. Không tự replay, retry hoặc chuyển backend khi chưa rõ lượt cũ đã xử lý tới đâu.
    Client cũng nên cấu hình `max_retries=0`. Request do client chủ động gửi lại là
    request mới; bản đầu không cung cấp idempotency/exactly-once cho việc gọi model.

Session registry chỉ ở RAM trong bản đầu; restart host làm mất mapping. Request
kế tiếp với cùng header vẫn chạy bằng bootstrap từ messages. Transcript do CLI
lưu không được tự quét/import để dựng lại mapping.

### Mặc định vận hành đã chốt

| Thiết lập | Giá trị ban đầu |
| --- | --- |
| Alias | `claude-default`, `kiro-default`; map tới model CLI được cấu hình |
| Cổng backend | Claude 4103, Kiro 4104; có thể đổi trong config |
| Lượt đang chạy | 2/backend, tối đa 1/hội thoại |
| Session giữ ấm | Tối đa 4/backend; thu hồi idle theo LRU khi cần |
| Idle TTL | 10 phút; không thu hồi session đang chạy |
| Request đang xử lý hoặc đợi | Tối đa 16/backend; vượt giới hạn trả 429 |
| Deadline backend | 300 giây, gồm chờ lock/queue, startup và model |
| Deadline router | 350 giây; cleanup riêng tối đa 10 giây |
| Giới hạn input/output | HTTP body 128 KiB, output text 2 MiB; không âm thầm truncate |
| Queue stream | 64 chunk; consumer quá chậm thì lỗi và hủy, không bỏ text |

Pool hết chỗ và không có session idle để thu hồi: đợi trong ngân sách request hoặc
trả 429, không vượt giới hạn process. Các giá trị phải có cấu hình và được đo lại
trong nghiệm thu; không cam kết mức giảm độ trễ trước khi đo warm/cold thật.

## Claude adapter

- Dùng official Agent SDK với kết nối sống cho session dùng lại; Python
  `ClaudeSDKClient` là ứng viên, không dùng top-level `query()` mới cho mọi lượt
  rồi tuyên bố đã reuse. Chỉ định executable đã resolve, khóa SDK/CLI sau probe.
- Lấy model/system/tool profile trước khi mở session. Giữ đường đăng nhập CLI
  chính thức; gateway key và credential nhà cung cấp là hai loại khác nhau.
- Disable built-in tools, MCP thừa hưởng và hooks/settings không thuộc profile.
  Kiểm chứng tác động của managed policies; không xem callback deny là đủ.
- Đọc đến terminal result của mỗi lượt; kiểm tra cả error/terminal state, không
  chỉ tên event. Interrupt phải có xử lý drain/close; không để event lượt cũ lẫn
  vào lượt sau.
- Tắt persistence nếu tổ hợp CLI/SDK hỗ trợ và không phá reuse sống. Không log
  prompt/response hay credential. Canary tests xác nhận tools/hooks không chạy.

## Kiro adapter

- Launch rõ engine V3; không fallback V2 và không nhận diện engine bằng ACP version.
- Initialize/capability negotiation, profile và model config được kiểm chứng
  trước prompt. Đăng ký session updates và agent-to-client handlers từ đầu.
- Session ID chỉ dùng nội bộ, các prompt tiếp theo dùng session đó khi idle.
  Hoàn tất dựa trên response của `session/prompt`, không dựa vào notification gần giống.
- `session/cancel` là yêu cầu hủy, không đồng nghĩa đã hủy/đóng/xóa session.
  Chờ terminal trong giới hạn cleanup; mất trạng thái thì không reuse.
- Chứng minh profile không có built-in/MCP tools hoặc hooks chạy tự động và vẫn
  dùng được login CLI mà không sao chép token. Nếu chưa đạt, backend chưa đủ điều
  kiện bật/phát hành; không hạ yêu cầu thành chỉ deny permission.

## Thay đổi router

- Thay danh sách services cố định bằng mô tả backend có `enabled` và cấu hình
  riêng; chỉ setup/check/serve backend được chọn. Tắt backend thì không đăng ký alias.
- Migration config cũ hai backend: validate, tạo backup local, thêm schema version
  và backend tùy chọn ở trạng thái disabled; giữ nguyên port/key/model/executable
  cũ. Ghi atomic; config lỗi không bị ghi đè. Chạy migration lại không đổi key.
- Truyền riêng `X-Conversation-ID` qua lớp proxy tới đúng backend. LiteLLM
  1.109.0 không bật forwarding mặc định; cờ `forward_client_headers_to_llm_api`
  chuyển nhiều `x-*` headers nên không dùng cờ đó để giả lập allowlist một header.
  Đề xuất custom `async_pre_call_hook` đọc header từ request context đã xác thực,
  chuẩn hóa/giới hạn giá trị và gán upstream header; xác nhận bằng spike thật trước.
  Không forward toàn bộ headers hoặc Authorization
  từ client; upstream dùng key riêng của backend. Test chứng minh header thật sự
  tới host, không bị bỏ/đổi tên hay chảy sang request khác.
- Bật `general_settings.cancel_on_disconnect` cho đường non-stream sau khi test
  regression; bản config hiện tại chưa bật và mặc định thư viện là false. SSE có
  cơ chế đóng upstream iterator, nhưng vẫn phải test cancellation đến tận CLI ở
  cả thời điểm trước chunk đầu và giữa stream.
- Profile adapter phải không cho API client ghi đè api_base, upstream key,
  executable, process args, CLI session ID hoặc cấu hình tools bằng payload.
  Với alias mới, kiểm tra allowlist payload ở đầu vào router trước khi LiteLLM
  tiêu thụ transport fields; không chỉ dựa vào backend để phát hiện trường đã bị
  proxy xử lý. Từ chối `headers`, `extra_headers`, `provider_specific_header`,
  `api_key`, `api_base` do client gửi; test không làm đổi hợp đồng hai alias cũ.
- Mỗi backend sở hữu pool riêng. Không cần Redis/database/session service chung
  cho bản localhost một process. Scale nhiều worker cần thiết kế khác, ngoài scope.
- Bổ sung `.gitignore` cho hai repo mới; chuẩn bị pin commit/tag ở bản phát hành,
  không tự tạo remote hay push trong bước lập kế hoạch.

## Thứ tự thực hiện và điều kiện chuyển bước

### 1. Kiểm chứng giao thức và profile

Ghi phiên bản CLI/SDK, các command/config hỗ trợ, khả năng model/system, giữ session
và tắt tools/hooks. Dùng canary workspace để phát hiện đọc/ghi/chạy lệnh ngoài ý
muốn. Kiểm chứng việc giữ đăng nhập khi dùng profile riêng và đường cancel/drain.
Test giả và probe không prompt chạy trước; acceptance gọi model được chạy riêng,
công bố sử dụng quota, không được gắn vào setup hoặc readiness nền.

Đầu ra: compatibility matrix và fixture đã loại dữ liệu nhạy cảm. Chưa đạt điều
kiện tool-less/auth thì chưa coi backend sẵn sàng dù đã nhận được câu trả lời.

### 2. Chốt contract và test chung

Định nghĩa validation messages, bootstrap/delta input, normalized text/usage/error
events, session state machine, locking/TTL và contract HTTP. Dùng fixture chứng
minh duplicate finals, stale events, failure/disconnect và cả pool overflow.

### 3. Xây plugin Claude

Tạo package/provider/API host, SDK session adapter và các lệnh doctor/serve.
Kiểm thử direct provider sync/async, streaming/non-streaming, auth, restricted
profile và warm session. Build wheel/runtime lockfile trong repo riêng.

### 4. Xây plugin Kiro

Tạo package/provider/API host, ACP V3 transport và restricted profile. Tận dụng
kiến thức từ Antigravity/Orca, không giả định cùng dialect hoặc sao chép toàn bộ
backend. Kiểm thử riêng capabilities, permission, model selection, cancel và cleanup.

### 5. Tích hợp router và migration

Bổ sung backend tùy chọn, forwarding metadata và cấu hình default. Test bằng
HTTP fixtures cho cả bốn route trước; xác minh config cũ còn dùng được, không cần
Claude/Kiro cài sẵn. Sau đó chạy qua hai API host thật với protocol fixtures.

### 6. Nghiệm thu CLI thật trên Windows

- Auth/preflight riêng với quota/model access; không nhầm `/v1/models` với completion.
- Non-stream và SSE qua endpoint router; không lặp text, usage không đoán.
- Hai lượt cùng header tạo một session; lượt thứ hai chỉ gửi phần mới và đo warm/cold.
- Hai header khác nhau không dùng chung session; header thiếu tạo session mới.
- Đổi system/model/history tạo session mới, không trộn ngữ cảnh hoặc gửi lịch sử hai lần.
- Hai request cùng header được serialize và đối chiếu lại sau lock.
- Timeout, cancel, stream disconnect, child crash và queue overflow không tạo thành công giả,
  không replay tự động, không để process con hoặc slot pool bị rò rỉ.
- Canary tools/MCP/hooks không thực thi, key không xuất hiện trong log/error.
- TTL, LRU, restart và migration giữ đúng giới hạn; không xóa dữ liệu/credential CLI.

Đầu ra: báo cáo bằng chứng/version/platform, giới hạn retention, hướng dẫn vận hành
và gói phát hành cho từng repo. Claude có thể đạt trước; Kiro chỉ bật sau khi đạt
cùng yêu cầu. Linux/Docker/multi-user nằm ngoài nghiệm thu bản đầu.

## Lưu trữ và vận hành

Gateway chỉ giữ thông tin cần đối chiếu hội thoại trong RAM, không ghi prompt hay
response vào log. Dọn workspace/config tạm do gateway tạo khi session bị thu hồi.
Transcript backend có thể tồn tại theo CLI; mô tả vị trí/phạm vi khi đã kiểm chứng,
không cam kết xóa dữ liệu phía nhà cung cấp. Không tự xóa kho CLI của người dùng.

Metadata chẩn đoán chỉ gồm request ID, backend, trạng thái, thời lượng và mã lỗi;
không xuất raw stderr/protocol errors chứa prompt, path nhạy cảm hoặc credential.
Không ghi nguyên `X-Conversation-ID` vào log/metadata của LiteLLM; nếu cần đối
chiếu chẩn đoán thì dùng giá trị băm có scope thay cho ID do client cung cấp.

## Các điểm cần bằng chứng, không được coi là đã hoàn thành

- Claude đã có binary bundled 2.1.321 từ SDK 0.2.180; doctor báo cần login,
  sau login vẫn phải vượt chốt nghiệm thu isolation/session trước khi ready.
- Kiro V3 phải chứng minh đồng thời tool-less và dùng login hiện hữu mà không copy token.
- Root LiteLLM đã có bằng chứng HTTP E2E với fixtures cho header/cancellation;
  chưa có nghiệm thu xuyên suốt CLI thật. Usage qua router tạm được bỏ vì LiteLLM
  tự sinh số liệu khi thiếu upstream counters; còn thiếu provenance để giữ số thật.
- Chuyển đổi system/history vẫn có giới hạn theo ADR-0001, cả khi reuse session.
- Bộ test và startup hai backend cũ không phải bằng chứng cho plugin mới.

Xem [nguồn và khảo sát Orca](design.md),
[ADR-0001](../../docs/adr/0001-chat-history-through-cli.md),
[ADR-0002](../../docs/adr/0002-claude-sdk-and-kiro-acp-v3.md),
[ADR-0003](../../docs/adr/0003-explicit-conversation-session-reuse.md).
