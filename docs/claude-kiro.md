# Claude / Kiro integration

Implementation locations are independent Git repositories:
`litellm-claude-rpc/` and `litellm-kiro-rpc/`. The root excludes their source from
its index and communicates with their standalone HTTP hosts. Root installation
never imports either plugin or requires an optional CLI to exist.

## Current release state

| Backend | Implementation | Windows live acceptance |
| --- | --- | --- |
| Claude | Python Agent SDK 0.2.180 / bundled CLI 2.1.321, restricted profile, owned transport | Blocked: official CLI login required; production gate also requires live safety/session acceptance |
| Kiro | ACP V3 adapter, transport, candidate profile, owned launcher | Blocked: isolated configuration + existing official login not proved; production startup refuses |

No real model request has been submitted for these backends. Protocol fixtures
and HTTP tests do not establish safety of CLI hooks or real model/session use.
Kiro has no runtime force-enable option. Review each plugin's
`docs/ACCEPTANCE.md` before release. CLI-owned transcript retention is separate
from gateway temporary files; the gateway never deletes the user's CLI store.

## Configuration and startup

`python scripts/workspace.py setup --dev` migrates old configuration atomically
after validation, writes an exclusive v1 backup, and installs enabled services
only. Existing keys, ports, executable paths and models keep their values.
Migration is idempotent. New defaults: Claude 4103, Kiro 4104, aliases
`claude-default` and `kiro-default`; a migration avoids occupied configured ports.

To install optional packages for development before enabling them:

```powershell
uv sync --frozen --extra dev --project litellm-claude-rpc
uv sync --frozen --extra dev --project litellm-kiro-rpc
& litellm-claude-rpc/.venv/Scripts/python.exe -m litellm_claude_rpc doctor
& litellm-kiro-rpc/.venv/Scripts/python.exe -m litellm_kiro_rpc doctor
```

After the required acceptance work, set `enabled.claude` or `enabled.kiro` in
`.local/config.json`. Select the actual model with `claude_models.claude-default`
or `kiro_models.kiro-default`; null uses the CLI default. Claude defaults to the
SDK-bundled binary. Kiro defaults to `kiro-cli` on PATH. Explicit executable paths
can be configured; unsupported versions are rejected.

Run the selected backend, then the router in separate terminals:

```powershell
python scripts/workspace.py serve claude
python scripts/workspace.py serve router
```

`serve` refuses disabled backends. `check` tests only enabled services. Model
listing reflects configuration, not backend readiness, quota or entitlement.
Do not use `config/router.yaml` directly to enable the new routes: the workspace
launcher generates active routes and installs strict ingress/cancellation policy.

## Text chat and session reuse

Only the new routes accept system + full user/assistant history. Send a stable
header `X-Conversation-ID: my-chat-1` with every request for that conversation.
The body remains `model`, `messages`, optional `stream` and
`stream_options.include_usage`. No header means a new session per request.
The router hashes the ID before passing it through an explicit allowlist;
arbitrary request headers, API-key/URL overrides, unsupported parameters and
non-text message fields are rejected or excluded before backend execution.

The host reuses a session only when its completed history is an exact prefix
of the new history with one additional user message. It sends just that new
message. A changed history/system/model or dirty session creates a fresh one
from a role-labeled transcript. This conversion does not preserve native OpenAI
role precedence. A single master key represents one local owner, not tenants.
Direct host access and router access have separate ID namespaces.

Defaults: 2 active turns/backend, 4 live sessions/backend, 16 pending requests,
600-second idle TTL, 300-second end-to-end plugin deadline, 350-second router
deadline, 128 KiB input, 2 MiB output and 64 queued events. Same-ID requests
serialize. Failure, timeout or disconnect evicts the session. No gateway
retry/fallback or persisted session mapping; manually resending is not exactly once.

Real LiteLLM HTTP tests verify allowlisted forwarding, strict parameter rejection,
completion/SSE, no retry/fallback, and cancellation closing upstream connections.
Plugin tests separately verify session invalidation and owned process cleanup.
End-to-end real CLI acceptance remains outstanding.

## Configurable limits and remaining compatibility gap

`claude_limits` / `kiro_limits` override the host defaults using fields shown in
`config/local.example.json`. They are passed via backend-specific RPC_LIMITS JSON
variables. `router_timeout` controls the outer deadline; keep it above the selected
host timeout plus cleanup/network margin. Router body limits follow each new
backend's input limit; the legacy body cap remains 4 MiB.

LiteLLM 1.109 can invent usage when an upstream omits it. The router currently
omits usage for both new routes (and emits null for streaming usage), including
otherwise valid upstream counters. Direct standalone hosts preserve proven
counters. This is an explicit remaining compatibility limitation, not a claim
that all requested `include_usage` behavior has been delivered.

Claude remains gated after login until tools/hooks and delayed SDK-event isolation
are demonstrated. Kiro remains gated until isolation works with the existing
CLI login. Gates are source-level release decisions, not operator bypass flags.
