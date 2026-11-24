# Adapter sources in the unified distribution

The root wheel includes the Python adapter packages from the four local source
repositories. They are compiled into a single distribution, not installed as four
distributions with conflicting LiteLLM pins. Their separate repositories remain
the source of truth; rebuilding the root wheel is required after adapter updates.

The Codex adapter carries the MIT license (copyright 2026 GeM), included under
`litellm_agent_rpc/licenses/codex-MIT.txt`. Other adapter code was authored in this
workspace. The root package does not bundle external CLI executables. The optional
Claude Agent SDK dependency supplies its own official CLI under its own terms.

Building from a Git checkout requires the four source directories to be present.
The resulting wheel and sdist contain their adapter code and do not require the
source repositories at runtime. Credentials, local config, tests from subprojects,
virtual environments and Git directories are excluded.
