# Native tool calling, reasoning and usage — development status

The v2 proxy/auth/TUI integration is implemented and opt-in. All native CLI
capability gates remain closed; Claude is explicitly kept closed this wave.
Existing `agent_rpc` configurations and standalone text-only behavior remain.

Inspect manifests without spawning an agent or changing login:

```powershell
uv run agent-rpc capabilities --config .local/config.json
```

Claude inspection requires the optional SDK: `uv sync --frozen --extra claude`.
Without that extra the other backends remain independently inspectable and Claude
reports `native_driver_unavailable`.

`state`, `evidence_level` and `live_verified` describe distinct things. A fixture
test does not establish CLI/profile isolation or live tool result continuation.
No config flag can turn an unverified native capability into live evidence.

Root owns public tool IDs, authenticated conversation scope, leases, delivery
acknowledgement and usage projection. Independent drivers own native protocol
tasks and callback Futures. Tools run in the API client, never in the router.
Client tool support needs a proven native result channel and a profile that
disables other agent tools, hooks, memory/config leakage and side effects.

Actual LiteLLM 1.109.0/Uvicorn tests cover authenticated tools and continuations,
SSE, opt-in reasoning, native usage projection, cache bypass, disconnect cleanup,
key isolation/revocation and managed drain/stop. Their native drivers and Prisma
writer are synthetic. A live PostgreSQL deployment has not been validated.

The current internal SDK and proxy buffer a bounded native round before output.
Native incremental streaming, completed-tool-history reuse and full live CLI
acceptance remain pending. Usage without proven counters or HTTP-round
attribution is null; subscription cost is not estimated. Observed partial usage
survives failed/cancelled invocations in a separate bounded metadata SQLite
ledger, with source/completeness visible in TUI. No legacy database migration runs.

The [V2 example](../example/v2/README.md) covers config, owner/virtual-key auth,
TUI pending-session control, localhost and Docker limitations. Multi-key mode
requires live text/profile-isolation evidence for every enabled alias.

Windows native preflight found Codex 0.167.0 with unresolved transport/profile
gates, Antigravity 1.5.9 versus the reviewed 1.5.3 pin, and Kiro 3.1.0 with a
successful ACP initialize but unproven hook/login isolation. Claude SDK 0.2.180's
bundled CLI 2.1.321 reported not logged in before the user chose to keep it closed.
No model inference or gate opening is claimed by that metadata-only preflight.

See the [integration receipt](../.scratch/cli-capabilities-v2/integration.md),
[earlier foundation receipt](../.scratch/cli-capabilities-v2/implementation.md) and
[canonical plan](../.scratch/cli-capabilities-v2/plan.md) for completed work,
remaining gates and per-repo review evidence.
