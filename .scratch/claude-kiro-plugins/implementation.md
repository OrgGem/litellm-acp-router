# Implementation receipt — 2026-11-13

Status: development implementation integrated; **not fully complete or released**.
User-approved plan remains the acceptance contract. No new backend is enabled
in local deployment, no real model call was made, and no repository was pushed.

## Delivered

- Independent Git repos `litellm-claude-rpc` and `litellm-kiro-rpc`, each with
  package, CLI/ASGI host, LiteLLM provider, tests, lockfile, Windows/Linux CI recipe,
  operations documentation and wheel/sdist build.
- Text/system/history validation, labeled transcript bootstrap, literal slash text,
  OpenAI completions/SSE and optional conversation header. Bounded per-backend
  session registry, exact-history reuse, same-ID serialization, TTL/LRU and
  invalidate-on-error/cancel/disconnect behavior. Provider bridge has one loop
  owner; CLI SDK connections have one task owner.
- Claude SDK 0.2.180 / bundled CLI 2.1.321, restricted command options and custom
  SDK transport containing suspended children in Windows job objects before resume.
  SDK handles the Claude protocol. Real usage comes from deduplicated assistant
  step counters, not cumulative result counters.
- Kiro V3 ACP adapter and candidate profile/launcher: explicit engine/auth owner,
  capability/model selection, strict framing/correlation, permission/client-tool
  denial, terminal prompt response and cancellation/process cleanup.
- Optional root backend routes with separate keys/environments, schema-2 migration
  with validation, exclusive backup and atomic replacement. Existing key/port/
  model/executable values verified preserved; Claude/Kiro default disabled.
- Strict ingress before LiteLLM processing, hashed conversation ID allowlist,
  guards against alternate endpoint bypass, transport overrides and fabricated
  usage. SSE errors cannot acquire a later success marker. Configurable host limits
  and router timeout. Existing backend source was not modified.

## Validation evidence

| Check | Result |
| --- | --- |
| Root pytest (real LiteLLM proxy against HTTP fixtures) | 38 passed |
| Claude pytest (SDK boundary fixtures + real owned process) | 36 passed |
| Kiro pytest (ACP fixtures + real stdio process) | 33 passed |
| Existing Codex pytest | 69 passed, 6 skipped |
| Existing Antigravity unittest | 27 passed |
| Ruff check and format on changed code | passed |
| Python compileall | passed |
| Both lockfile consistency checks | passed |
| Both wheel and sdist builds | passed |
| Claude doctor | not_ready: cli_login_required |
| Kiro doctor | not_ready: kiro_profile_isolation_not_verified |

Third-party Pydantic ReadOnly warnings remain; no test failures. Antigravity uses
unittest, not pytest. CI jobs were authored but not run remotely; Linux is not
live-accepted. Root HTTP tests prove routing/cancellation, not model access or
CLI internals. Process fixtures prove ownership cleanup only for tested cases.

## Standards review

Independent reviewer found a shutdown cancellation bug, a new input limit leaking
into the legacy route, and usage-test drift. These were corrected with regression
tests. Shutdown now distinguishes a cancelled child from new caller cancellation;
the root keeps a separate legacy body cap. Malformed model values return 400.
Standalone copies of common modules are intentional to keep the repositories
installable independently; changes to these modules need matching tests in both.

## Spec review

Independent reviewer reproduced alternate-route parameter/key injection,
fabricated usage, and `[DONE]` after error with the actual pinned LiteLLM proxy.
Ingress and output guards plus actual-proxy regressions address these cases.
Slash-leading input is now preserved as literal text; all host limits are exposed.

Two requirements remain partial: verified CLI session/profile behavior and true
usage provenance across LiteLLM. Production gates enforce the former; the latter
is conservatively omitted rather than estimated. See the remaining work below.

Review totals: Standards reported four actionable categories, corrected;
Spec reported five significant categories, three mitigated and two acceptance/
compatibility gaps remain. The worst remaining Spec issue is unproved isolation
of a delayed complete SDK assistant/result pair across warm turns.

## Remaining work before enabling either backend

1. User must log into the official Claude CLI. Doctor does not read credentials.
   After login the development build still reports `claude_live_acceptance_pending`.
   Complete real Windows tools/MCP/hooks canaries and the acceptance matrix.
2. Establish a defensible Claude warm-turn event boundary. A lone stale terminal
   is rejected, but an entire delayed assistant/result pair still needs protocol
   proof or a revised session strategy. Do not remove the release gate on the
   strength of the current fake tests.
3. Prove Kiro restricted-profile isolation while keeping the existing official
   login. KIRO_HOME also affects sign-in state; no token copying or global CLI
   config edits are permitted as a workaround. Candidate launcher stays gated.
4. Preserve true upstream usage provenance through LiteLLM. For now the root
   omits new-route non-stream usage and emits null streaming usage, even when
   the backend had real counters. The in-process provider disables LiteLLM's
   estimated stream usage. Direct standalone hosts can report real counters.
5. Run the real CLI matrix (completion, SSE, cold/warm, isolation, concurrent
   turns, timeout/disconnect, stale events, cleanup and retention) independently
   per backend; record results before removing source-level release gates.

No runtime bypass flag was added. No tests are represented as live acceptance.

Relevant sources: [Claude SDK usage](https://code.claude.com/docs/en/agent-sdk/cost-tracking),
[Kiro V3 ACP](https://kiro.dev/docs/cli/v3/acp-migration/),
[Kiro environment/sign-in scope](https://kiro.dev/docs/cli/reference/cli-commands/).
