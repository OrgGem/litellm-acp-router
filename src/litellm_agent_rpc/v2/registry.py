"""Lazy independent driver discovery for integration and readiness inspection."""

import importlib

from .answer_contract import check_binding as check_answer_binding
from .answer_contract import verify_live_binding
from .broker import Binding
from .capabilities import check_manifest
from .errors import ProtocolError

DRIVERS = {
    "codex": ("litellm_codex_rpc.driver_v2", "CodexDriverV2"),
    "antigravity": ("litellm_antigravity_rpc.native_v2", "AntigravityNativeDriver"),
    "claude": ("litellm_claude_rpc.native_v2", "ClaudeNativeDriver"),
    "kiro": ("litellm_kiro_rpc.native_v2", "KiroNativeDriver"),
}


def create_driver(backend, mode="native"):
    if backend not in DRIVERS:
        raise ProtocolError("unknown_backend")
    module, name = DRIVERS[backend]
    if mode == "answer_only":
        module, name = "litellm_" + backend + "_rpc.answer_only", "AnswerOnlyDriver"
    elif mode != "native":
        raise ProtocolError("unknown_execution_mode", 503)
    try:
        return getattr(importlib.import_module(module), name)()
    except (ImportError, AttributeError):
        raise ProtocolError("native_driver_unavailable", 503) from None


async def inspect_binding(backend, profile, model=None):
    mode = profile.get("execution_mode", "native")
    driver = create_driver(backend, mode)
    manifest = await driver.capabilities(profile, model)
    check_manifest(manifest, answer_only=mode == "answer_only")
    if mode == "answer_only":
        check_answer_binding(manifest, profile, model)
    return manifest


async def production_bindings(specs):
    """Validate all reachable aliases; no current backend has live acceptance."""
    result = {}
    for alias, spec in specs.items():
        backend, profile, model = spec["backend"], spec["profile"], spec.get("model")
        mode = profile.get("execution_mode", "native")
        driver = create_driver(backend, mode)
        manifest = await driver.capabilities(profile, model)
        supports = check_manifest(manifest, answer_only=mode == "answer_only")
        if mode == "answer_only":
            check_answer_binding(manifest, profile, model)
        if (
            manifest["backend"] != backend
            or manifest["profile_id"] != profile["profile_id"]
            or manifest["model"] != model
            or manifest["transport"] != profile["transport"]
        ):
            raise ProtocolError("capability_binding_mismatch", 503)
        if not supports("text") or not supports("profile_isolation"):
            raise ProtocolError("native_profile_acceptance_pending", 503)
        if mode == "answer_only":
            await verify_live_binding(driver, profile, model, manifest)
        result[alias] = Binding(driver, profile, model)
    return result
