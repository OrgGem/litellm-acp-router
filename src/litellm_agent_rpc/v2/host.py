"""Proxy-owned V2 runtime; the production constructor cannot enable fixtures."""

import os
from pathlib import Path

from ..config import route_specs, routes, v2_config, v2_route_specs
from .answer_contract import check_binding as check_answer_binding
from .answer_contract import verify_live_binding
from .auth import PrismaKeySource, ProxyAuth, ProxyAuthority
from .broker import Binding, Broker
from .capabilities import check_manifest
from .errors import ProtocolError
from .registry import create_driver
from .usage import Ledger


def _production_ledger():
    """Separate metadata-only store beside the existing managed usage store."""
    legacy = os.environ.get("AGENT_RPC_TELEMETRY_PATH")
    if legacy:
        directory = Path(legacy).expanduser().parent
    else:
        directory = (
            Path(os.environ.get("AGENT_RPC_CONFIG", ".local/config.json"))
            .expanduser()
            .parent
        )
    path = directory / "usage-v2.sqlite"
    if legacy and path.resolve() == Path(legacy).expanduser().resolve():
        path = directory / "usage-v2-ledger.sqlite"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError("invalid ledger path")
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        return Ledger(path)
    except OSError:
        ledger = Ledger()
        ledger.degraded = True
        return ledger


class _Unavailable:
    async def capabilities(self, profile, model):
        raise ProtocolError("native_driver_unavailable", 503)


class ProxyV2Host:
    def __init__(
        self, config, *, _bindings=None, _fixture=False, _registry_source=None
    ):
        profiles = v2_route_specs(config)
        options = v2_config(config)
        if not profiles:
            raise ValueError("V2 host requires active routes")
        if options["auth_mode"] == "virtual_keys" and _registry_source is None:
            _registry_source = PrismaKeySource(profiles)
        if _registry_source is not None and set(profiles) != set(routes(config)):
            raise ValueError("Multi-key isolation requires all routes to use V2")
        specs = route_specs(config)
        if _bindings is None:
            bindings = {}
            for alias, profile in profiles.items():
                spec = specs[alias]
                try:
                    driver = create_driver(
                        spec["backend"], profile.get("execution_mode", "native")
                    )
                except ProtocolError:
                    driver = _Unavailable()
                answer_config = {}
                if profile.get("execution_mode") == "answer_only":
                    answer_config["executable"] = config.get("executables", {}).get(
                        spec["backend"]
                    )
                bindings[alias] = Binding(
                    driver,
                    {**profile, **answer_config, "models": {alias: spec["model"]}},
                    spec["model"],
                )
        else:
            # Test-only constructor dependency, never loaded from env/config.
            if not _fixture or set(_bindings) != set(profiles):
                raise ValueError("Invalid test bindings")
            bindings = dict(_bindings)
        self.backends = {alias: specs[alias]["backend"] for alias in profiles}
        self.authority = ProxyAuthority()
        self.broker = Broker(
            self.authority,
            bindings,
            ledger=None if _fixture else _production_ledger(),
            _fixture=_fixture,
            **options["limits"],
        )
        self.auth = ProxyAuth(
            self.authority,
            profiles,
            registry_source=_registry_source,
            has_active_scope=lambda scope: any(
                record.principal.scope_id == scope
                for record in self.broker.records.values()
            ),
        )

    async def authenticate(self, user, alias):
        principal = await self.auth.authenticate(user)
        self.authority.check(principal, alias)
        if self.auth.source is not None:
            # Multi-key isolation is enabled only if EVERY reachable alias is
            # covered by exact live text+profile evidence, not just this alias.
            for name, binding in self.broker.bindings.items():
                try:
                    manifest = await binding.driver.capabilities(
                        binding.config, binding.model
                    )
                    supports = check_manifest(
                        manifest,
                        fixture=self.broker._fixture,
                        answer_only=binding.config.get("execution_mode")
                        == "answer_only",
                    )
                    if binding.config.get("execution_mode") == "answer_only":
                        check_answer_binding(manifest, binding.config, binding.model)
                    if (
                        manifest["backend"] != self.backends[name]
                        or manifest["profile_id"] != binding.config["profile_id"]
                        or manifest["transport"] != binding.config["transport"]
                        or manifest["model"] != binding.model
                        or not supports("text")
                        or not supports("profile_isolation")
                    ):
                        raise ProtocolError("native_profile_acceptance_pending", 503)
                    if binding.config.get("execution_mode") == "answer_only":
                        await verify_live_binding(
                            binding.driver,
                            binding.config,
                            binding.model,
                            manifest,
                            fixture=self.broker._fixture,
                        )
                except Exception:
                    raise ProtocolError(
                        "native_profile_acceptance_pending", 503
                    ) from None
            # Refresh/expiry can change while waiting for manifests.
            self.authority.check(principal, alias)
        return principal

    def guard_legacy(self):
        if self.broker.draining or self.broker.closed:
            raise ProtocolError("service_draining", 503)

    async def status(self):
        snapshot = self.broker.status_snapshot()
        capabilities = {}
        for alias, binding in self.broker.bindings.items():
            try:
                manifest = await binding.driver.capabilities(
                    binding.config, binding.model
                )
                supports = check_manifest(
                    manifest,
                    fixture=self.broker._fixture,
                    answer_only=binding.config.get("execution_mode") == "answer_only",
                )
                if binding.config.get("execution_mode") == "answer_only":
                    check_answer_binding(manifest, binding.config, binding.model)
                if (
                    manifest["backend"] != self.backends[alias]
                    or manifest["profile_id"] != binding.config["profile_id"]
                    or manifest["transport"] != binding.config["transport"]
                    or manifest["model"] != binding.model
                ):
                    raise ProtocolError("capability_binding_mismatch", 503)
                if (
                    binding.config.get("execution_mode") == "answer_only"
                    and supports("text")
                    and supports("profile_isolation")
                ):
                    await verify_live_binding(
                        binding.driver,
                        binding.config,
                        binding.model,
                        manifest,
                        fixture=self.broker._fixture,
                    )
                capabilities[alias] = {
                    "ready": supports("text") and supports("profile_isolation"),
                    "manifest": manifest,
                    "execution_mode": binding.config.get("execution_mode", "native"),
                    "tool_proposal_source": "structured_output"
                    if binding.config.get("execution_mode") == "answer_only"
                    else "native",
                }
            except Exception:
                capabilities[alias] = {
                    "ready": False,
                    "error": "capability_unavailable",
                }
        return {
            "counts": {
                name: snapshot[name]
                for name in (
                    "native_running",
                    "prepared",
                    "awaiting_client",
                    "cleaning",
                    "generation_permits",
                    "quarantined",
                )
            },
            "pending": [
                {
                    "id": item["chain_id"],
                    "model": item["model"],
                    "state": item["state"],
                    "expires_in_seconds": item["expires_in_seconds"],
                }
                for item in snapshot["sessions"]
                if item["state"] in {"PREPARED", "DELIVERED"}
            ],
            "capabilities": capabilities,
            "usage": self.broker.ledger.status_snapshot(),
            "draining": self.broker.draining,
            "stopping": self.broker.closed,
        }

    async def drain(self):
        await self.broker.set_draining(True)

    async def resume(self):
        await self.broker.set_draining(False)

    async def try_stop(self, active_http):
        return await self.broker.try_stop(active_http)

    async def cancel_pending(self, pending_id):
        return await self.broker.cancel_pending(pending_id)

    async def close(self):
        try:
            await self.broker.close()
        finally:
            await self.auth.close()
        self.broker.ledger.close()
