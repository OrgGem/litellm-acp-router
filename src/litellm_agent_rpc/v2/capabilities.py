"""Only host-owned, versioned evidence can enable a feature."""

from .answer_contract import CONTRACT_HASH as ANSWER_ONLY_CONTRACT_HASH
from .answer_contract import validate as validate_answer
from .contract import CONTRACT_HASH, validate
from .errors import ProtocolError


def check_manifest(manifest, *, fixture=False, answer_only=False):
    (validate_answer if answer_only else validate)("CapabilityManifest", manifest)
    expected = ANSWER_ONLY_CONTRACT_HASH if answer_only else CONTRACT_HASH
    if manifest["contract_hash"] != expected:
        raise ProtocolError("contract_mismatch", 503)

    # fixture is exclusively a constructor dependency of the conformance host,
    # never a config file, API field or environment variable.
    def supports(feature):
        capability = manifest["features"][feature]
        return capability["state"] == "verified" and (
            fixture
            or (capability["live_verified"] and capability["evidence_level"] == "live")
        )

    return supports


def check_features(manifest, payload, policy, *, fixture=False):
    supports = check_manifest(manifest, fixture=fixture)
    if not supports("text"):
        raise ProtocolError("backend_capability_unverified", 503)
    if not supports("profile_isolation"):
        raise ProtocolError("profile_isolation_unverified", 503)
    tools = payload.get("tools", [])
    if tools:
        if not supports("client_tools"):
            raise ProtocolError("client_tools_unavailable")
        choice = policy["tool_choice"]
        name = "named" if isinstance(choice, dict) else choice
        if name not in manifest["tool_choices"]:
            raise ProtocolError("tool_choice_unavailable")
        if isinstance(choice, dict) and choice["function"]["name"] not in {
            t["function"]["name"] for t in tools
        }:
            raise ProtocolError("unknown_tool_choice")
        if policy["parallel_tool_calls"] and not supports("parallel_tools"):
            raise ProtocolError("parallel_tools_unavailable")
    elif not isinstance(policy["tool_choice"], str) or policy["tool_choice"] not in {
        "auto",
        "none",
    }:
        raise ProtocolError("tools_required")
    output = payload.get("reasoning_output", "off")
    if output != "off" and (
        not supports("reasoning_output") or output not in manifest["reasoning_kinds"]
    ):
        raise ProtocolError("reasoning_output_unavailable")
    if "reasoning_effort" in payload and (
        not supports("reasoning_control")
        or payload["reasoning_effort"] not in manifest["reasoning_efforts"]
    ):
        raise ProtocolError("reasoning_effort_unavailable")
    if "max_completion_tokens" in policy and not supports("output_token_limit"):
        raise ProtocolError("output_token_limit_unavailable")
