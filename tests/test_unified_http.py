import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from litellm_agent_rpc.config import ALIASES

EXTRA_ALIASES = {"codex-fast": "codex", "claude-review": "claude"}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    directory = tmp_path_factory.mktemp("unified")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    key = "sk-unified-test-12345678901234567890"
    config = {
        "enabled": dict.fromkeys(ALIASES, True),
        "keys": {"router": key},
        "ports": {"router": port},
        "routes": {
            **{
                alias: {"backend": backend, "model": "fixture-model"}
                for backend, alias in ALIASES.items()
            },
            **{
                alias: {"backend": backend, "model": "fixture-extra-model"}
                for alias, backend in EXTRA_ALIASES.items()
            },
        },
    }
    (directory / "config.json").write_text(json.dumps(config))
    with (directory / "server.log").open("wb") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("fixture_unified_server.py")),
                str(directory),
            ],
            stdout=log,
            stderr=log,
            env={
                **os.environ,
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                "AGENT_RPC_TELEMETRY_PATH": str(directory / "usage.sqlite"),
                "AGENT_RPC_CONTROL_TOKEN": "control-test-token",
                "AGENT_RPC_INSTANCE_ID": "unified-test",
            },
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": "Bearer " + key},
                timeout=10,
                trust_env=False,
            ) as client:
                for _ in range(150):
                    if child.poll() is not None:
                        pytest.fail(
                            (directory / "server.log").read_text(errors="replace")[
                                -5000:
                            ]
                        )
                    try:
                        if client.get("/v1/models").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail("Unified proxy did not start")
                yield client, directory
        finally:
            child.terminate()
            child.wait(timeout=15)


@pytest.mark.parametrize("backend", ALIASES)
def test_one_proxy_routes_all_backends_without_http_hops(server, backend):
    client, directory = server
    body = {
        "model": ALIASES[backend],
        "messages": [{"role": "user", "content": "hello"}],
    }
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == ALIASES[backend]
    response = client.post(
        "/v1/chat/completions",
        json={**body, "stream": True, "stream_options": {"include_usage": True}},
    )
    assert response.status_code == 200, response.text
    assert "[DONE]" in response.text
    resp_api = client.post(
        "/v1/responses",
        json={"model": ALIASES[backend], "input": "hello", "store": False},
    )
    assert resp_api.status_code == 200, resp_api.text
    assert resp_api.json()["status"] == "completed"
    assert (directory / (backend + ".events")).exists()


def test_single_master_key_models_and_header_dispatch(server):
    client, directory = server
    assert client.get("/v1/models", headers={"Authorization": "bad"}).status_code == 401
    assert {item["id"] for item in client.get("/v1/models").json()["data"]} == (
        set(ALIASES.values()) | set(EXTRA_ALIASES)
    )
    response = client.post(
        "/v1/chat/completions",
        headers={"X-Conversation-ID": "chat"},
        json={
            "model": "claude-default",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200, response.text
    events = [
        json.loads(line)
        for line in (directory / "claude.events").read_text().splitlines()
    ]
    assert events[-1]["cid"] == hashlib.sha256(b"chat").hexdigest()
    assert sum(event["event"] == "create" for event in events) == 1


@pytest.mark.parametrize("stream", [False, True])
def test_disconnect_cancels_direct_backend(server, stream):
    client, directory = server
    path = directory / "kiro.events"
    before = len(path.read_text().splitlines()) if path.exists() else 0
    body = {
        "model": "kiro-default",
        "messages": [{"role": "user", "content": "wait"}],
        "stream": stream,
    }
    if stream:
        with client.stream("POST", "/v1/chat/completions", json=body) as response:
            assert response.status_code == 200
            assert next(response.iter_lines()).startswith("data:")
    else:
        with pytest.raises(httpx.ReadTimeout):
            client.post("/v1/chat/completions", json=body, timeout=0.3)
    target = "stream_closed" if stream else "cancelled"
    for _ in range(50):
        events = [json.loads(line) for line in path.read_text().splitlines()[before:]]
        if any(event["event"] == target for event in events):
            break
        time.sleep(0.1)
    else:
        pytest.fail("Direct adapter was not cancelled")


@pytest.mark.parametrize("alias", ALIASES.values())
def test_transport_overrides_rejected_for_every_backend(server, alias):
    client, _ = server
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": alias,
            "messages": [{"role": "user", "content": "hello"}],
            "api_base": "http://unused",
            "api_key": "override",
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize("alias", EXTRA_ALIASES)
def test_custom_aliases_preserve_dispatch_and_ingress_contract(server, alias):
    client, _ = server
    body = {"model": alias, "messages": [{"role": "user", "content": "hello"}]}
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == alias
    assert "usage" not in response.json()
    response = client.post("/v1/chat/completions", json={**body, "stream": True})
    assert response.status_code == 200
    assert "[DONE]" in response.text
    assert (
        client.post(
            "/v1/chat/completions", json={**body, "api_base": "http://unused"}
        ).status_code
        == 400
    )
    resp = client.post(
        "/v1/responses",
        json={"model": alias, "instructions": "brief", "input": "hello"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["object"] == "response"
    assert resp.json()["status"] == "completed"
    assert resp.json().get("usage") is None
    stream_resp = client.post(
        "/v1/responses",
        json={"model": alias, "input": "hello", "stream": True},
    )
    assert stream_resp.status_code == 200, stream_resp.text
    assert "response.completed" in stream_resp.text
    assert (
        client.post(
            "/v1/responses",
            json={"model": alias, "input": "hello", "store": True},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/responses",
            json={"model": alias, "input": "hello", "api_base": "http://unused"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/embeddings", json={"model": alias, "input": "hello"}
        ).status_code
        == 400
    )


def test_managed_control_on_real_proxy_keeps_client_key_separate(server):
    client, _ = server
    url = "/_agent-rpc/control/"
    assert client.get(url + "status").status_code == 401
    control = {"Authorization": "Bearer control-test-token"}
    status = client.get(url + "status", headers=control)
    assert status.status_code == 200
    assert status.json()["instance_id"] == "unified-test"
    assert client.post(url + "stop", headers=control).status_code == 409
    try:
        assert client.post(url + "drain", headers=control).json()["state"] == "draining"
        assert (
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "codex-fast",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            ).status_code
            == 503
        )
    finally:
        assert client.post(url + "resume", headers=control).json()["state"] == "running"
