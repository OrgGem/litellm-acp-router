import asyncio
import copy
import io
import json
import os
import sys
import time
import urllib.error
from types import SimpleNamespace

import httpx
import pytest

from litellm_agent_rpc.config import v2_config, v2_route_specs, validate_config
from litellm_agent_rpc.control import ManagedControl
from litellm_agent_rpc.management import (
    ManagementError,
    ServiceManager,
    _ControlNotDrained,
)

TOKEN = "test-management-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


def base_config():
    return {
        "enabled": {"codex": True, "claude": False},
        "keys": {"router": "sk-fixture-key-123456789012345"},
        "routes": {
            "coding": {"backend": "codex", "model": "model-a"},
            "claude": {"backend": "claude", "model": "model-b", "enabled": False},
        },
    }


def profile(transport="stdio"):
    return {
        "profile_id": "local-profile",
        "profile_revision": "rev-1",
        "transport": transport,
    }


def test_v2_configuration_is_opt_in_and_preserves_legacy():
    config = base_config()
    original = copy.deepcopy(config)
    validate_config(config)
    assert v2_route_specs(config) == {} and config == original
    config["v2"] = {"enabled": False, "routes": {"claude": profile()}}
    validate_config(config)
    assert v2_route_specs(config) == {}
    config["v2"] = {
        "enabled": True,
        "routes": {"coding": profile()},
        "limits": {"max_active": 2, "tool_wait_seconds": 5},
    }
    validate_config(config)
    assert v2_route_specs(config) == {"coding": profile()}
    assert v2_config(config)["limits"]["max_active"] == 2
    assert config["keys"] == original["keys"]


@pytest.mark.parametrize(
    "v2",
    [
        {"enabled": True, "routes": {}},
        {"enabled": True, "routes": {"unknown": profile()}},
        {"enabled": True, "routes": {"claude": profile()}},
        {"enabled": True, "routes": {"coding": {**profile(), "live_verified": True}}},
        {"enabled": True, "routes": {"coding": profile("fixture")}},
        {
            "enabled": True,
            "routes": {"coding": {**profile(), "profile_id": "two words"}},
        },
        {"enabled": True, "routes": {"coding": {**profile(), "profile_id": "組"}}},
        {"enabled": True, "routes": {"coding": profile()}, "fixture": True},
        {
            "enabled": True,
            "routes": {"coding": profile()},
            "limits": {"max_active": True},
        },
        {
            "enabled": True,
            "routes": {"coding": profile()},
            "limits": {"cleanup_seconds": float("nan")},
        },
        {
            "enabled": True,
            "routes": {"coding": profile()},
            "limits": {"max_handoffs": 129},
        },
    ],
)
def test_invalid_v2_configuration_fails_closed(v2):
    config = base_config()
    config["v2"] = v2
    with pytest.raises(ValueError):
        validate_config(config)


def test_virtual_keys_require_every_active_alias_to_be_v2():
    config = base_config()
    config["v2"] = {
        "enabled": True,
        "auth_mode": "virtual_keys",
        "routes": {"coding": profile()},
    }
    validate_config(config)
    config["routes"]["other"] = {"backend": "codex", "model": "model-b"}
    with pytest.raises(ValueError, match="all active routes"):
        validate_config(config)
    config["v2"]["enabled"] = False
    with pytest.raises(ValueError, match="all active routes"):
        validate_config(config)


class Host:
    def __init__(self):
        self.draining = False
        self.stopping = False
        self.pending = True
        self.try_entered = asyncio.Event()
        self.try_release = asyncio.Event()

    async def status(self):
        return {
            "counts": {
                "native_running": 0,
                "prepared": 0,
                "awaiting_client": int(self.pending),
                "cleaning": 0,
                "generation_permits": int(self.pending),
                "quarantined": 0,
            },
            "pending": [
                {
                    "id": "chain_abc",
                    "model": "coding",
                    "state": "DELIVERED",
                    "expires_in_seconds": 12.0,
                }
            ]
            if self.pending
            else [],
            "capabilities": {},
            "usage": {"available": True, "totals": {"unknown": 1}},
            "draining": self.draining,
            "stopping": self.stopping,
        }

    async def drain(self):
        self.draining = True

    async def resume(self):
        self.draining = False

    async def try_stop(self, active_http):
        self.try_entered.set()
        await self.try_release.wait()
        if self.pending or active_http():
            return False
        self.stopping = True
        return True

    async def cancel_pending(self, pending_id):
        if pending_id != "chain_abc" or not self.pending:
            return False
        self.pending = False
        return True


async def authorized_proxy(scope, receive, send):
    # Synthetic ingress: authentication precedes continuation admission.
    headers = dict(scope.get("headers", []))
    if headers.get(b"authorization") != b"Bearer client-key":
        return await ManagedControl.reply(send, 401, {"error": "unauthorized"})
    if scope["app_host"].draining and headers.get(b"x-owned") != b"yes":
        return await ManagedControl.reply(send, 503, {"error": "new_work_draining"})
    return await ManagedControl.reply(send, 200, {"ok": True})


async def test_v2_drain_authenticates_before_continuation_and_stop_is_atomic():
    host = Host()

    async def proxy(scope, receive, send):
        scope["app_host"] = host
        return await authorized_proxy(scope, receive, send)

    stopped = []
    app = ManagedControl(
        proxy, {}, TOKEN, "instance", lambda: stopped.append(True), v2_host=host
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/_agent-rpc/control/"
        assert (await client.post(path + "drain", headers=AUTH)).json()["v2"]["counts"][
            "awaiting_client"
        ] == 1
        assert (
            await client.post("/v1/chat/completions", headers={"X-Owned": "yes"})
        ).status_code == 401
        assert (
            await client.post(
                "/v1/chat/completions", headers={"Authorization": "Bearer client-key"}
            )
        ).status_code == 503
        assert (
            await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer client-key", "X-Owned": "yes"},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer client-key", "X-Owned": "yes"},
            )
        ).status_code == 503
        stop = asyncio.create_task(client.post(path + "stop", headers=AUTH))
        await host.try_entered.wait()
        assert (
            await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer client-key", "X-Owned": "yes"},
            )
        ).status_code == 503
        host.try_release.set()
        assert (await stop).status_code == 409
        assert not app.stopping and not stopped
        assert (
            await client.post(path + "cancel", json={"id": "chain_abc"})
        ).status_code == 401
        assert (
            await client.post(path + "cancel", headers=AUTH, json={"id": "chain_abc"})
        ).status_code == 200
        assert (await client.post(path + "stop", headers=AUTH)).status_code == 200
        await asyncio.sleep(0)
        assert stopped == [True]


async def test_failed_v2_drain_never_allows_new_http_requests():
    host = Host()

    async def failed():
        raise RuntimeError("secret")

    host.drain = failed
    app = ManagedControl(authorized_proxy, {}, TOKEN, "instance", v2_host=host)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/_agent-rpc/control/drain", headers=AUTH)
        assert response.status_code == 503 and "secret" not in response.text
        assert (await client.post("/v1/chat/completions")).status_code == 503


@pytest.mark.parametrize(
    "field",
    [
        "native_running",
        "prepared",
        "awaiting_client",
        "cleaning",
        "generation_permits",
        "quarantined",
    ],
)
def test_stop_guard_includes_every_v2_count(field):
    counts = {
        name: 0
        for name in (
            "native_running",
            "prepared",
            "awaiting_client",
            "cleaning",
            "generation_permits",
            "quarantined",
        )
    }
    state = {
        "state": "draining",
        "active_requests": 0,
        "v2": {"available": True, "counts": counts},
    }
    assert ServiceManager._quiescent(state)
    counts[field] = 1
    assert not ServiceManager._quiescent(state)
    counts[field] = False
    assert not ServiceManager._quiescent(state)
    assert not ServiceManager._quiescent({**state, "v2": {"available": False}})


def test_manager_does_not_stop_while_a_v2_handoff_is_pending(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base_config()), encoding="utf-8")
    manager = ServiceManager(path)
    counts = {
        "native_running": 0,
        "prepared": 0,
        "awaiting_client": 1,
        "cleaning": 0,
        "generation_permits": 1,
        "quarantined": 0,
    }
    state = {
        "state": "draining",
        "owned": True,
        "active_requests": 0,
        "v2": {"available": True, "counts": counts},
    }
    calls = []
    monkeypatch.setattr(manager, "status", lambda: state)
    monkeypatch.setattr(
        manager, "_request", lambda action, **kwargs: calls.append(action)
    )
    with pytest.raises(ManagementError, match="remains draining"):
        manager.stop(timeout=0.01)
    assert calls == ["drain"]


def test_manager_retries_an_atomic_stop_rejection(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base_config()), encoding="utf-8")
    manager = ServiceManager(path)
    state = {"exited": False, "stops": 0}

    class Child:
        def poll(self):
            return 0 if state["exited"] else None

    manager._child = Child()

    def status():
        return {
            "state": "stopped" if state["exited"] else "draining",
            "owned": not state["exited"],
            "active_requests": 0,
        }

    def request(action, **kwargs):
        if action == "stop":
            state["stops"] += 1
            if state["stops"] == 1:
                raise _ControlNotDrained("synthetic race")
            state["exited"] = True

    monkeypatch.setattr(manager, "status", status)
    monkeypatch.setattr(manager, "_request", request)
    assert manager.stop(timeout=2)["state"] == "stopped"
    assert state["stops"] == 2


def test_only_matching_owned_stop_conflict_is_retryable(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base_config()), encoding="utf-8")
    manager = ServiceManager(path)
    manager._port, manager._token, manager._instance = 12345, "token", "instance"

    class Opener:
        def open(self, request, timeout):
            payload = json.dumps(
                {"error": "service_not_drained", "instance_id": "instance"}
            ).encode()
            raise urllib.error.HTTPError(
                request.full_url, 409, "conflict", {}, io.BytesIO(payload)
            )

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    with pytest.raises(_ControlNotDrained):
        manager._request("stop", post=True)


def test_cancel_timeout_covers_cleanup_longer_than_two_seconds(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base_config()), encoding="utf-8")
    manager = ServiceManager(path)
    manager._port, manager._token, manager._instance = 12345, "token", "instance"
    manager._child = SimpleNamespace(pid=123)
    monkeypatch.setattr(manager, "status", lambda: {"state": "draining", "owned": True})
    seen = []
    payload = json.dumps(
        {
            "state": "draining",
            "active_requests": 0,
            "pid": 123,
            "instance_id": "instance",
        }
    ).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _limit):
            return payload

    class Opener:
        def open(self, request, timeout):
            seen.append((request.full_url.rsplit("/", 1)[-1], timeout))
            if seen[-1][0] == "cancel":
                time.sleep(2.1)
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    assert manager._request("status")["state"] == "draining"
    assert manager.cancel_pending("chain_abc")["state"] == "draining"
    assert seen == [("status", 2), ("cancel", 125)]


def test_cancel_transport_timeout_reports_unknown_outcome(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base_config()), encoding="utf-8")
    manager = ServiceManager(path)
    manager._port, manager._token, manager._instance = 12345, "token", "instance"

    class Opener:
        def open(self, request, timeout):
            raise TimeoutError("private transport details")

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    with pytest.raises(ManagementError, match="outcome unknown") as error:
        manager._request("cancel", post=True, body={"id": "chain_abc"})
    assert "private" not in str(error.value)


@pytest.mark.parametrize("telemetry_set", [False, True])
def test_serve_uses_selected_config_for_v2_ledger_without_overriding_telemetry(
    tmp_path, monkeypatch, telemetry_set
):
    from litellm_agent_rpc import server
    from litellm_agent_rpc.cli import main

    path = tmp_path / "config.json"
    path.write_text(json.dumps(base_config()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["agent-rpc", "serve", "--config", str(path)])
    monkeypatch.setenv("AGENT_RPC_CONFIG", "original-config")
    if telemetry_set:
        monkeypatch.setenv(
            "AGENT_RPC_TELEMETRY_PATH", str(tmp_path / "telemetry.sqlite")
        )
    else:
        monkeypatch.delenv("AGENT_RPC_TELEMETRY_PATH", raising=False)
    observed = {}

    def run(config, port, host):
        observed["config"] = os.environ.get("AGENT_RPC_CONFIG")
        observed["telemetry"] = os.environ.get("AGENT_RPC_TELEMETRY_PATH")

    monkeypatch.setattr(server, "run", run)
    assert main() == 0
    assert observed["config"] == (
        "original-config" if telemetry_set else str(path.resolve())
    )
    assert observed["telemetry"] == (
        str(tmp_path / "telemetry.sqlite") if telemetry_set else None
    )
    assert os.environ["AGENT_RPC_CONFIG"] == "original-config"


def test_real_proxy_drain_allows_only_owned_continuation(tmp_path):
    from test_v2_host import TOOL
    from test_v2_proxy_http import CONTROL, body, start_proxy

    with start_proxy(tmp_path) as (client, _):
        initial = body(tools=[TOOL])
        headers = {"X-Conversation-ID": "managed-continuation"}
        first = client.post("/v1/chat/completions", json=initial, headers=headers)
        assert first.status_code == 200, first.text
        assistant = first.json()["choices"][0]["message"]
        follow = {
            **initial,
            "messages": initial["messages"]
            + [
                assistant,
                {
                    "role": "tool",
                    "tool_call_id": assistant["tool_calls"][0]["id"],
                    "content": "result",
                },
            ],
        }
        before = client.get("/_agent-rpc/control/status", headers=CONTROL).json()["v2"]
        assert before["counts"]["awaiting_client"] == 1
        assert before["pending"][0]["expires_in_seconds"] > 0
        assert before["capabilities"]["test"]["ready"] is True
        assert (
            client.post("/_agent-rpc/control/drain", headers=CONTROL).status_code == 200
        )
        rejected = client.post(
            "/v1/chat/completions",
            json=follow,
            headers={**headers, "Authorization": "Bearer bad"},
        )
        assert rejected.status_code in {400, 401, 403}, rejected.text
        new_work = client.post(
            "/v1/chat/completions", json=body(), headers={"X-Conversation-ID": "new"}
        )
        assert new_work.status_code == 503, new_work.text
        assert (
            client.post("/_agent-rpc/control/stop", headers=CONTROL).status_code == 409
        )
        continued = client.post("/v1/chat/completions", json=follow, headers=headers)
        assert continued.status_code == 200, continued.text
        after = client.get("/_agent-rpc/control/status", headers=CONTROL).json()["v2"]
        assert after["counts"]["awaiting_client"] == 0
        assert (
            client.post("/_agent-rpc/control/stop", headers=CONTROL).status_code == 200
        )


def test_real_proxy_cancel_pending_and_usage_metadata(tmp_path):
    from test_v2_host import TOOL
    from test_v2_proxy_http import CONTROL, body, start_proxy

    with start_proxy(tmp_path) as (client, directory):
        initial = body(
            tools=[TOOL],
            messages=[{"role": "user", "content": "private-prompt-sentinel"}],
        )
        response = client.post(
            "/v1/chat/completions",
            json=initial,
            headers={"X-Conversation-ID": "cancel-chain"},
        )
        assert response.status_code == 200, response.text
        snapshot = client.get("/_agent-rpc/control/status", headers=CONTROL).json()[
            "v2"
        ]
        assert "private-prompt-sentinel" not in json.dumps(snapshot)
        assert snapshot["usage"]["available"] is True
        pending_id = snapshot["pending"][0]["id"]
        assert (
            client.post(
                "/_agent-rpc/control/cancel", headers=CONTROL, json={"id": pending_id}
            ).status_code
            == 200
        )
        snapshot = client.get("/_agent-rpc/control/status", headers=CONTROL).json()[
            "v2"
        ]
        assert snapshot["counts"]["awaiting_client"] == 0
        events = (directory / "driver.events").read_text(encoding="utf-8")
        assert '"event": "cancel"' in events and '"event": "close"' in events
        assert (
            client.post("/_agent-rpc/control/drain", headers=CONTROL).status_code == 200
        )
        assert (
            client.post("/_agent-rpc/control/stop", headers=CONTROL).status_code == 200
        )
