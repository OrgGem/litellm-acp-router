import asyncio

import httpx

from litellm_agent_rpc.control import ManagedControl

TOKEN = "managed-test-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


async def basic(scope, receive, send):
    await ManagedControl.reply(send, 200, {"ok": True})


async def test_control_requires_own_token_not_gateway_key():
    stopped = []
    app = ManagedControl(basic, {}, TOKEN, "instance", lambda: stopped.append(True))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = "/_agent-rpc/control/"
        assert (await client.get(url + "status")).status_code == 401
        assert (
            await client.post(
                url + "drain", headers={"Authorization": "Bearer gateway-key"}
            )
        ).status_code == 401
        assert (await client.post(url + "stop", headers=AUTH)).status_code == 409
        response = await client.get(url + "status", headers=AUTH)
        assert response.json()["instance_id"] == "instance"
        assert TOKEN not in response.text
        assert (await client.post(url + "drain", headers=AUTH)).json()[
            "state"
        ] == "draining"
        assert (await client.post(url + "stop", headers=AUTH)).status_code == 200
        await asyncio.sleep(0)
        assert stopped == [True]


async def test_unmanaged_service_has_no_management_endpoint():
    app = ManagedControl(basic, {})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.get("/_agent-rpc/control/status", headers=AUTH)
        ).status_code == 404
        assert (await client.post("/v1/chat/completions", json={})).status_code == 200


async def test_drain_tracks_inflight_requests_cancellation_and_resume():
    entered = asyncio.Event()

    async def blocking(scope, receive, send):
        entered.set()
        await asyncio.Event().wait()

    app = ManagedControl(blocking, {}, TOKEN, "instance")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        active = asyncio.create_task(client.post("/v1/chat/completions", json={}))
        await entered.wait()
        response = await client.post("/_agent-rpc/control/drain", headers=AUTH)
        assert response.json()["active_requests"] == 1
        assert (await client.post("/v1/chat/completions", json={})).status_code == 503
        assert (
            await client.post("/_agent-rpc/control/stop", headers=AUTH)
        ).status_code == 409
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
        assert app.active == 0
        response = await client.post("/_agent-rpc/control/resume", headers=AUTH)
        assert response.json()["state"] == "running"


async def test_usage_failure_does_not_break_control():
    def unavailable():
        raise OSError("storage unavailable")

    app = ManagedControl(basic, {}, TOKEN, "instance", usage=unavailable)
    assert app.status()["usage"] == {"available": False, "degraded": True}


async def test_accepted_stop_cannot_be_resumed_while_reply_is_pending():
    entered, release = asyncio.Event(), asyncio.Event()
    stopped = []
    app = ManagedControl(
        basic, {}, TOKEN, "instance", lambda: stopped.append(app.active)
    )
    app.draining = True

    async def send(event):
        entered.set()
        await release.wait()

    async def receive():
        return {"type": "http.request", "body": b""}

    stop = asyncio.create_task(
        app(
            {
                "type": "http",
                "method": "POST",
                "path": "/_agent-rpc/control/stop",
                "headers": [(b"authorization", ("Bearer " + TOKEN).encode())],
            },
            receive,
            send,
        )
    )
    await entered.wait()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post("/_agent-rpc/control/resume", headers=AUTH)
        ).status_code == 409
        assert (await client.post("/v1/chat/completions", json={})).status_code == 503
    release.set()
    await stop
    await asyncio.sleep(0)
    assert stopped == [0]


def test_server_keeps_control_token_out_of_child_environment(monkeypatch):
    import os
    import subprocess
    import sys
    from unittest.mock import patch

    import uvicorn

    from litellm_agent_rpc.provider import plugin
    from litellm_agent_rpc.server import run

    monkeypatch.setenv("AGENT_RPC_CONTROL_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_RPC_INSTANCE_ID", "instance")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setattr(plugin, "configure", lambda *args: None)
    monkeypatch.setattr(plugin, "close", lambda: None)

    class Server:
        def __init__(self, config):
            self.app = config.app

        def run(self):
            assert "AGENT_RPC_CONTROL_TOKEN" not in os.environ
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os; assert 'AGENT_RPC_CONTROL_TOKEN' not in os.environ",
                ],
                check=False,
                capture_output=True,
            )
            assert child.returncode == 0

            async def probe():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app), base_url="http://test"
                ) as client:
                    assert (
                        await client.get("/_agent-rpc/control/status", headers=AUTH)
                    ).status_code == 200

            asyncio.run(probe())

    monkeypatch.setattr(uvicorn, "Server", Server)
    with patch.dict(os.environ):
        run(
            {
                "enabled": {"codex": True},
                "keys": {"router": "sk-fixture-key-1234567890123456"},
            }
        )
