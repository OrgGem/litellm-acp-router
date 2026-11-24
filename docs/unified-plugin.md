# Unified plugin verification — 2026-11-13

Root now builds `litellm-agent-rpc==0.2.0.dev0`. One LiteLLM 1.109.0 process
registers `agent_rpc`, dispatching four model aliases directly to local adapter
objects. Backend HTTP listeners and separate backend venvs are unnecessary.
Default configuration still enables Codex/Antigravity and disables Claude/Kiro;
no existing local key, model or executable was changed.

## Evidence from this workspace

| Check | Result |
| --- | --- |
| Root pytest | 75 passed |
| Codex suite using root Python / LiteLLM 1.109.0 | 71 passed, 6 skipped |
| Antigravity unittest suite using root Python | 27 passed |
| Claude suite using root Python | 36 passed |
| Kiro suite using root Python | 33 passed |
| Ruff check and format for root and changed Codex files | Passed |
| Root lockfile check | Passed |
| Root sdist → wheel build | Passed |
| Wheel installation in a separate venv without Claude extra | Passed; all adapter package imports resolved inside that venv |
| Actual Codex + Antigravity `agent-rpc doctor` | Passed without prompts; runtime cleanup completed, exit 0 |
| Installed-wheel doctor, isolated Python from outside the workspace | Both enabled backends passed and closed cleanly; Claude SDK was not installed in this venv |

Total: **242 passed, 6 skipped**. Third-party Pydantic ReadOnly warnings remain.
These are local Windows checks; the authored CI workflow has not been run remotely.

The 11 new HTTP tests launch an actual LiteLLM proxy with a controlled adapter
factory, without backend HTTP servers. They exercise all four aliases for
completion/SSE, authentication, model listing, hashed conversation dispatch,
rejected transport overrides, and stream/nonstream disconnect cancellation.
Provider tests cover all four CustomLLM interfaces, lazy initialization,
concurrent initialization, disabled backends and failure independence.
Older HTTP topology tests remain as regression coverage, not evidence of the
new deployment topology.

## Codex lifecycle correction

The standalone Codex host previously changed its process-wide cwd to an empty
temporary directory. A shared gateway cannot do that per backend. Codex Config
now accepts an absolute `stdio_cwd`; root owns a temporary backend directory
and supplies it to subprocess creation while keeping the gateway cwd unchanged.

Real no-prompt preflight exposed an existing Windows launcher-descendant cleanup
gap: terminating only the launcher could leave inherited pipes and the temporary
directory open. Codex stdio now uses an owned Windows Job Object / POSIX process
group, contains the Windows process before resume, kills remaining descendants,
drains pipes, and waits before removing its directory. Regression tests cover
child cwd and a launcher that exits while its descendant retains stdout.
The containment helper is derived from the existing local adapter implementation;
Codex remains independently packaged and does not import another adapter.

## Limits of this evidence

- No real model prompt was sent in this unification step. Doctor confirms
  protocol/login/configuration preflight, not entitlement, quota or text quality.
- Claude and Kiro keep their existing source-level release gates. They are
  integrated routes/adapters, not live-ready backends. Claude requires live
  safety/session acceptance; Kiro requires verified isolation with CLI login.
- SDK session reuse and tools/hooks behavior still require the previously agreed
  live acceptance in each new adapter's `docs/ACCEPTANCE.md`.
- All four adapter packages are bundled from local source. Existing uncommitted
  adapter changes remain; the wheel is not yet reproducible from published
  remote revisions alone. The folders are independent repos, not registered
  Git submodules. A fresh root checkout needs all four source trees to build.
- Root CI intentionally uses `uv sync --no-install-project` and `PYTHONPATH=src`
  to validate dispatcher/policy fixtures without unavailable adapter checkouts.
  Full wheel and adapter compatibility checks were performed locally.
- API usage is omitted/null for all aliases because LiteLLM may synthesize it.

Run the new topology with `python scripts/workspace.py serve router`; see
[README](../README.md) for setup, configuration and optional Claude dependencies.
