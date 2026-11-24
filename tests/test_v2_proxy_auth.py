import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS
from litellm.proxy._types import UserAPIKeyAuth

from litellm_agent_rpc.v2.auth import PrismaKeySource, ProxyAuth, ProxyAuthority
from litellm_agent_rpc.v2.errors import ProtocolError


def verified(key):
    auth = UserAPIKeyAuth(api_key=key)
    auth.via_virtual_key = True
    return auth


async def test_owner_requires_litellm_master_marker():
    authority = ProxyAuthority()
    auth = ProxyAuth(authority, {"test"})
    owner = await auth.authenticate(verified(LITELLM_PROXY_MASTER_KEY_ALIAS))
    authority.check(owner, "test")
    with pytest.raises(ProtocolError, match="verified_key_required"):
        await auth.authenticate(UserAPIKeyAuth(api_key=LITELLM_PROXY_MASTER_KEY_ALIAS))
    with pytest.raises(ProtocolError, match="owner_key_required"):
        await auth.authenticate(verified("a" * 64))
    await auth.close()


async def test_concurrent_registry_initialization_is_single_and_deadline_is_enforced():
    calls = 0

    async def source():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {}

    authority = ProxyAuthority()
    auth = ProxyAuth(authority, {"test"}, registry_source=source)
    owners = await asyncio.gather(
        *(auth.authenticate(verified(LITELLM_PROXY_MASTER_KEY_ALIAS)) for _ in range(8))
    )
    assert calls == 1 and len({id(owner) for owner in owners}) == 1
    watcher = auth.watcher
    authority.fresh_until = time.monotonic() - 1
    with pytest.raises(ProtocolError, match="auth_unavailable"):
        authority.check(owners[0], "test")
    await auth.close()
    assert watcher.done()
    with pytest.raises(ProtocolError, match="auth_unavailable"):
        await auth.authenticate(verified(LITELLM_PROXY_MASTER_KEY_ALIAS))


async def test_prisma_writer_registry_tracks_only_verified_key_and_refreshes(
    monkeypatch,
):
    from litellm.proxy import proxy_server

    digest = hashlib.sha256(b"sk-fixture-one").hexdigest()
    source = PrismaKeySource({"test"})
    authority = ProxyAuthority()
    auth = ProxyAuth(authority, {"test"}, registry_source=source)
    row = {name: None for name in source._empty}
    row.update({name: "{}" for name in source._json_fields})
    row["budget_limits"] = "null"
    row.update(
        {
            "token": digest,
            "blocked": False,
            "soft_budget_cooldown": False,
            "models": ["test"],
            "expires": datetime.now(timezone.utc) + timedelta(seconds=60),
            "created_at": datetime.now(timezone.utc),
            "settings_updated_at": None,
            "rotation_count": 0,
            "key_type": "llm_api",
            "allowed_routes": ["llm_api_routes"],
        }
    )
    reads = []

    class Table:
        async def find_many(self, *, where):
            reads.append(where)
            return [dict(row)] if row else []

    writer = SimpleNamespace(litellm_verificationtoken=Table())
    monkeypatch.setattr(
        proxy_server, "prisma_client", SimpleNamespace(writer_db=writer)
    )
    principal = await auth.authenticate(verified(digest))
    authority.check(principal, "test")
    assert reads == [{"token": {"in": [digest]}}]
    assert principal.scope_id != digest and "sk-fixture-one" not in str(reads)
    # Expiry applies immediately, even between background refreshes.
    authority.expirations[principal.scope_id] = time.time() - 1
    with pytest.raises(ProtocolError, match="key_revoked"):
        authority.check(principal, "test")
    # ACL changes invalidate the old epoch and issue new grants when still eligible.
    authority.expirations[principal.scope_id] = None
    row["rotation_count"] = 1
    await auth._refresh()
    with pytest.raises(ProtocolError, match="key_revoked"):
        authority.check(principal, "test")
    rotated = await auth.authenticate(verified(digest))
    assert rotated.epoch != principal.epoch
    row["config"] = '{"unverified_policy":true}'
    await auth._refresh()
    with pytest.raises(ProtocolError, match="key_revoked"):
        authority.check(rotated, "test")
    row["config"] = "{}"
    await auth.authenticate(verified(digest))
    # Linked user/team policy is not silently approximated.
    row["team_id"] = "team"
    await auth._refresh()
    assert digest not in source.tracked
    with pytest.raises(ProtocolError, match="key_revoked"):
        await auth.authenticate(verified(digest))
    await auth.close()


async def test_idle_keys_evict_but_active_keys_keep_their_epoch(monkeypatch):
    from litellm.proxy import proxy_server

    digest = "d" * 64
    source = PrismaKeySource({"test"})
    row = {name: None for name in source._empty}
    row.update(
        {
            "token": digest,
            "blocked": False,
            "soft_budget_cooldown": False,
            "models": ["test"],
            "expires": None,
            "created_at": None,
            "settings_updated_at": None,
            "rotation_count": 0,
            "key_type": "llm_api",
            "allowed_routes": ["llm_api_routes"],
        }
    )

    class Table:
        async def find_many(self, *, where):
            return [dict(row)] if digest in where["token"]["in"] else []

    monkeypatch.setattr(
        proxy_server,
        "prisma_client",
        SimpleNamespace(writer_db=SimpleNamespace(litellm_verificationtoken=Table())),
    )
    active = True
    authority = ProxyAuthority()
    auth = ProxyAuth(
        authority,
        {"test"},
        registry_source=source,
        has_active_scope=lambda scope: active,
    )
    principal = await auth.authenticate(verified(digest))
    source.tracked[digest] -= 121
    await auth._refresh()
    authority.check(principal, "test")
    active = False
    await auth._refresh()
    assert digest not in source.tracked
    with pytest.raises(ProtocolError, match="key_revoked"):
        authority.check(principal, "test")
    await auth.close()


async def test_registry_outage_fails_closed_without_fabricating_revocation():
    failing = False
    digest = "c" * 64

    async def source():
        if failing:
            raise OSError("private db details")
        return {
            digest: {
                "scope_id": "fixture",
                "revision": "1",
                "grants": ["test"],
                "max_sessions": 1,
            }
        }

    authority = ProxyAuthority()
    auth = ProxyAuth(authority, {"test"}, registry_source=source)
    principal = await auth.authenticate(verified(digest))
    failing = True
    with pytest.raises(ProtocolError, match="auth_unavailable"):
        await auth._refresh()
    with pytest.raises(ProtocolError, match="auth_unavailable"):
        authority.check(principal, "test")
    with pytest.raises(ProtocolError, match="auth_unavailable"):
        await auth.authenticate(verified("b" * 64))
    failing = False
    await auth._refresh()
    authority.check(principal, "test")
    await auth.close()


async def test_empty_prisma_registry_probes_writer_and_fails_closed(monkeypatch):
    from litellm.proxy import proxy_server

    queries = []

    class Writer:
        async def query_raw(self, sql):
            queries.append(sql)
            raise OSError("fixture writer unavailable")

    monkeypatch.setattr(
        proxy_server, "prisma_client", SimpleNamespace(writer_db=Writer())
    )
    authority = ProxyAuthority()
    auth = ProxyAuth(authority, {"test"}, registry_source=PrismaKeySource({"test"}))
    try:
        with pytest.raises(ProtocolError, match="auth_unavailable"):
            await auth.authenticate(verified(LITELLM_PROXY_MASTER_KEY_ALIAS))
        assert queries == ["SELECT 1"]
    finally:
        await auth.close()


async def test_multikey_waits_for_every_reachable_alias():
    from test_v2_host import PROFILE, Driver, manifest

    from litellm_agent_rpc.v2.broker import Binding
    from litellm_agent_rpc.v2.host import ProxyV2Host

    class DriverForGate(Driver):
        def __init__(self, blocked):
            super().__init__()
            self.blocked = blocked

        async def capabilities(self, profile, model):
            result = manifest()
            result["transport"] = "stdio"
            if self.blocked:
                result["features"]["profile_isolation"]["state"] = "unverified"
            return result

    async def source():
        return {}

    profile = {**PROFILE, "transport": "stdio"}
    enabled, blocked = DriverForGate(False), DriverForGate(True)
    config = {
        "enabled": {"codex": True},
        "keys": {"router": "sk-fixture-12345678901234567890"},
        "routes": {
            name: {"backend": "codex", "model": "fake"} for name in ("test", "other")
        },
        "v2": {
            "enabled": True,
            "auth_mode": "virtual_keys",
            "routes": {
                name: {
                    "profile_id": "fixture",
                    "profile_revision": "1",
                    "transport": "stdio",
                }
                for name in ("test", "other")
            },
        },
    }
    host = ProxyV2Host(
        config,
        _fixture=True,
        _registry_source=source,
        _bindings={
            "test": Binding(enabled, profile, "fake"),
            "other": Binding(blocked, profile, "fake"),
        },
    )
    try:
        with pytest.raises(ProtocolError, match="native_profile_acceptance_pending"):
            await host.authenticate(verified(LITELLM_PROXY_MASTER_KEY_ALIAS), "test")
        assert enabled.opens == blocked.opens == 0
        blocked.blocked = False
        assert await host.authenticate(verified(LITELLM_PROXY_MASTER_KEY_ALIAS), "test")
    finally:
        await host.close()


async def test_bare_customllm_cannot_deliver_or_create_v2_handoff():
    from litellm.llms.custom_llm import CustomLLMError

    from litellm_agent_rpc.provider import AgentRPC

    config = {
        "enabled": {"codex": True},
        "keys": {"router": "sk-fixture-12345678901234567890"},
        "routes": {"test": {"backend": "codex", "model": "fake"}},
        "v2": {
            "enabled": True,
            "routes": {
                "test": {
                    "profile_id": "fixture",
                    "profile_revision": "1",
                    "transport": "stdio",
                }
            },
        },
    }
    provider = AgentRPC(config)
    with pytest.raises(CustomLLMError, match="trusted_proxy_context_required"):
        await provider.acompletion("test", [{"role": "user", "content": "hello"}])
    with pytest.raises(CustomLLMError, match="trusted_proxy_context_required"):
        await anext(provider.astreaming("test", [{"role": "user", "content": "hello"}]))


async def test_production_host_persists_separate_usage_and_closes(
    monkeypatch, tmp_path
):
    import sqlite3

    import litellm_agent_rpc.v2.host as host_module

    config = {
        "enabled": {"codex": True},
        "keys": {"router": "sk-fixture-12345678901234567890"},
        "routes": {"test": {"backend": "codex", "model": "fake"}},
        "v2": {
            "enabled": True,
            "routes": {
                "test": {
                    "profile_id": "fixture",
                    "profile_revision": "1",
                    "transport": "stdio",
                }
            },
        },
    }
    monkeypatch.setattr(
        host_module,
        "create_driver",
        lambda backend, execution_mode="native": object(),
    )
    monkeypatch.setenv("AGENT_RPC_TELEMETRY_PATH", str(tmp_path / "usage.sqlite"))
    host = host_module.ProxyV2Host(config)
    assert host.broker.ledger.db is not None
    host.broker.ledger.begin(
        "inv-fixture",
        key_scope="scope-fixture",
        model="test",
        backend="codex",
        request_id="req-fixture",
    )
    host.broker.ledger.finish("inv-fixture", "succeeded")
    connection = host.broker.ledger.db
    await host.close()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    assert (tmp_path / "usage-v2.sqlite").is_file()
    assert not (tmp_path / "usage.sqlite").exists()
    restored = host_module.ProxyV2Host(config)
    try:
        records = restored.broker.ledger.status_snapshot()["invocations"]
        assert len(records) == 1 and records[0]["invocation_id"] == "inv-fixture"
        assert records[0]["usage_completeness"] == "unknown"
    finally:
        await restored.close()


async def test_production_host_degrades_if_ledger_directory_cannot_be_created(
    monkeypatch, tmp_path
):
    import litellm_agent_rpc.v2.host as host_module

    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    monkeypatch.setenv("AGENT_RPC_TELEMETRY_PATH", str(blocked / "usage.sqlite"))
    ledger = host_module._production_ledger()
    try:
        assert ledger.degraded and ledger.db is None
    finally:
        ledger.close()
