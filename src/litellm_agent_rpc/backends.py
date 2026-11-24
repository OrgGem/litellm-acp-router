"""Factories import only the selected adapter; no intermediate HTTP host."""

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import backend_flatten, backend_flatten_map, backend_models


@dataclass
class Handle:
    provider: object
    close: object


def create_backend(name, config):
    if name == "codex":
        from litellm_codex_rpc.config import Config
        from litellm_codex_rpc.provider import CodexRPCProvider
        from serve_codex_rpc.cli import check_codex

        binary = shutil.which(config.get("executables", {}).get("codex", "codex"))
        if binary is None:
            raise ValueError("Codex executable missing")
        limits = config.get("codex_limits", {})
        if not isinstance(limits, dict):
            raise ValueError("Invalid codex_limits")
        work = tempfile.TemporaryDirectory(prefix="agent-rpc-codex-")
        cfg = Config(
            transport="stdio",
            stdio_command=(
                str(Path(binary).resolve()),
                "app-server",
                "--listen",
                "stdio://",
            ),
            models=backend_models(config, name),
            connect_timeout=limits.get("connect_timeout", 10),
            request_timeout=limits.get("request_timeout", 300),
            max_concurrent=limits.get("max_concurrent", 2),
            auto_reconnect=False,
            flatten=backend_flatten(config, name),
            flatten_by_alias=backend_flatten_map(config, name),
            stdio_cwd=work.name,
        )
        try:
            asyncio.run(check_codex(cfg))
            provider = CodexRPCProvider(cfg)
        except BaseException:
            work.cleanup()
            raise

        def close():
            try:
                provider.close()
            finally:
                work.cleanup()

        return Handle(provider, close)
    if name == "antigravity":
        from litellm_antigravity_rpc.backend import ACPBackend
        from litellm_antigravity_rpc.config import Config, Model
        from litellm_antigravity_rpc.provider import AntigravityLLM
        from litellm_antigravity_rpc.runtime import Runtime

        cfg = Config(
            models={k: Model(**v) for k, v in backend_models(config, name).items()},
            executable=config.get("executables", {}).get(name, "agy"),
            flatten=backend_flatten(config, name),
            flatten_by_alias=backend_flatten_map(config, name),
            **config.get("antigravity_limits", {}),
        )
        runtime = Runtime(ACPBackend(cfg))
        try:
            runtime.submit(runtime.backend.startup()).result()
            return Handle(AntigravityLLM(runtime), runtime.close)
        except BaseException:
            runtime.close()
            raise
    if name in ("claude", "kiro"):
        # Import by known backend name, never by an HTTP-supplied Python module.
        from importlib import import_module

        prefix = "litellm_" + name + "_rpc"
        contracts = import_module(prefix + ".contracts")
        drivers = import_module(prefix + ".driver")
        extra = (
            {
                "flatten": backend_flatten(config, name),
                "flatten_by_alias": backend_flatten_map(config, name),
            }
            if name == "claude"
            else {}
        )
        cfg = contracts.Config(
            models=backend_models(config, name),
            executable=config.get("executables", {}).get(name),
            **extra,
            **config.get(name + "_limits", {}),
        )
        # Preserve each adapter's readiness check, including in-process use.
        asyncio.run(drivers.doctor(cfg))
        pool = import_module(prefix + ".sessions").SessionPool(
            cfg,
            lambda model, system: getattr(drivers, name.title() + "Driver")(
                cfg, model, system
            ),
        )
        runtime = import_module(prefix + ".runtime").Runtime(pool)
        provider = getattr(import_module(prefix + ".provider"), name.title() + "LLM")(
            runtime
        )
        return Handle(provider, runtime.close)
    raise ValueError("Unknown backend")
