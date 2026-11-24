"""Real LiteLLM Proxy against two HTTP fixtures; no CLI or paid model calls."""

import hashlib
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from test_workspace import workspace


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(body)
        self.server.headers_seen.append(dict(self.headers))
        if self.headers.get("Authorization") != "Bearer " + self.server.key:
            self.send_response(401)
            self.end_headers()
            return
        assert self.path == "/v1/chat/completions"
        if body["messages"][0]["content"] == "wait-for-disconnect":
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(
                    b'data: {"id":"chatcmpl-wait","object":"chat.completion.chunk","created":1,"model":"claude-default","choices":[{"index":0,"delta":{"content":"waiting"},"finish_reason":null}]}\n\n'
                )
                self.wfile.flush()
            self.connection.settimeout(6)
            try:
                if self.connection.recv(1) == b"":
                    self.server.disconnected.set()
            except OSError:
                pass
            return
        if body["messages"][0]["content"] == "stream-error":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'data: {"id":"chatcmpl-error","object":"chat.completion.chunk","created":1,"model":"claude-default","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            )
            self.wfile.write(
                b'data: {"error":{"message":"backend_turn_failed","type":"gateway_error"}}\n\n'
            )
            self.wfile.flush()
            return
        if body["messages"][0]["content"] == "fail":
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"error":{"message":"fixture unavailable","type":"server_error"}}'
            )
            return
        base = {"id": "chatcmpl-fixture", "created": 1, "model": body["model"]}
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for content, finish in [(self.server.alias, None), (None, "stop")]:
                chunk = {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content},
                            "finish_reason": finish,
                        }
                    ],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        **base,
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": self.server.alias,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 3,
                            "total_tokens": 5,
                        },
                    }
                ).encode()
            )


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    temp = tmp_path_factory.mktemp("router")
    servers = {}
    config = json.loads((workspace.ROOT / "config/local.example.json").read_text())
    config["keys"] = {
        name: "sk-fixture-only-secret-" + name for name in workspace.PROJECTS
    }
    config["enabled"] = {s: True for s in workspace.ALIASES}
    child = None
    try:
        for service, alias in [
            ("codex", "codex-default"),
            ("antigravity", "antigravity"),
            ("claude", "claude-default"),
            ("kiro", "kiro-default"),
        ]:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
            server.alias, server.key, server.calls = alias, config["keys"][service], []
            server.headers_seen = []
            server.disconnected = threading.Event()
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers[service] = server
            config["ports"][service] = server.server_port
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            config["ports"]["router"] = sock.getsockname()[1]
        with (temp / "router.log").open("wb") as log:
            child = subprocess.Popen(
                [
                    str(workspace.python_for("router")),
                    str(workspace.ROOT / "scripts/serve_router.py"),
                    "--port",
                    str(config["ports"]["router"]),
                ],
                cwd=workspace.ROOT,
                env=workspace.environment(config, "router"),
                stdout=log,
                stderr=log,
            )
            with httpx.Client(
                base_url=f"http://127.0.0.1:{config['ports']['router']}",
                headers={"Authorization": "Bearer " + config["keys"]["router"]},
                trust_env=False,
                timeout=15,
            ) as client:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        pytest.fail(
                            (temp / "router.log").read_text(errors="replace")[-5000:]
                        )
                    try:
                        if client.get("/v1/models").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.2)
                else:
                    pytest.fail(
                        "Router startup timed out: "
                        + (temp / "router.log").read_text(errors="replace")[-5000:]
                    )
                yield client, servers
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        for server in servers.values():
            server.shutdown()
            server.server_close()


def test_auth_and_models(stack):
    client, servers = stack
    response = client.get("/v1/models")
    assert {item["id"] for item in response.json()["data"]} == {
        "codex-default",
        "antigravity",
        "claude-default",
        "kiro-default",
    }
    assert client.get("/v1/models", headers={"Authorization": ""}).status_code == 401
    assert not any(server.calls for server in servers.values())


@pytest.mark.parametrize("service,alias", list(workspace.ALIASES.items()))
def test_completion_stream_and_upstream_contract(stack, service, alias):
    client, servers = stack
    body = {"model": alias, "messages": [{"role": "user", "content": "hello"}]}
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == alias
    if service in ("codex", "antigravity"):
        assert response.json()["usage"]["total_tokens"] == 5
    else:
        assert "usage" not in response.json()
    with client.stream(
        "POST", "/v1/chat/completions", json={**body, "stream": True}
    ) as response:
        assert response.status_code == 200
        lines = list(response.iter_lines())
        assert any(alias in line for line in lines)
        assert "data: [DONE]" in lines
    # Antigravity rejects unknown top-level fields; verify the proxy adds none.
    for request in servers[service].calls:
        assert set(request) <= {"model", "messages", "stream", "stream_options"}
        assert request["model"] == alias


def test_no_retry_or_cross_agent_fallback(stack):
    client, servers = stack
    before = {name: len(server.calls) for name, server in servers.items()}
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "codex-default",
            "messages": [{"role": "user", "content": "fail"}],
        },
    )
    assert response.status_code == 503, response.text
    assert len(servers["codex"].calls) == before["codex"] + 1
    assert len(servers["antigravity"].calls) == before["antigravity"]


def test_unknown_alias_not_forwarded(stack):
    client, servers = stack
    before = sum(len(server.calls) for server in servers.values())
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "unconfigured",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code in {400, 404}
    assert sum(len(server.calls) for server in servers.values()) == before


@pytest.mark.parametrize("service", ["claude", "kiro"])
def test_agent_conversation_header_allowlist_and_exact_history(stack, service):
    client, servers = stack
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]
    response = client.post(
        "/v1/chat/completions",
        json={"model": workspace.ALIASES[service], "messages": messages},
        headers={
            "X-Conversation-ID": "chat-123",
            "X-Api-Key": "must-not-forward",
            "X-Custom": "secret",
        },
    )
    assert response.status_code == 200, response.text
    headers = {k.lower(): v for k, v in servers[service].headers_seen[-1].items()}
    assert headers.get("x-conversation-id") == hashlib.sha256(b"chat-123").hexdigest()
    assert "x-custom" not in headers and "x-api-key" not in headers
    assert servers[service].calls[-1]["messages"] == messages


@pytest.mark.parametrize(
    "parameter",
    [
        "headers",
        "extra_headers",
        "api_key",
        "api_base",
        "temperature",
        "tools",
        "metadata",
    ],
)
def test_agent_transport_overrides_rejected_before_forwarding(stack, parameter):
    client, servers = stack
    before = sum(len(s.calls) for s in servers.values())
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-default",
            "messages": [{"role": "user", "content": "hello"}],
            parameter: {},
        },
    )
    assert response.status_code == 400
    assert sum(len(s.calls) for s in servers.values()) == before


@pytest.mark.parametrize("stream", [False, True])
def test_router_disconnect_closes_upstream_without_retry(stack, stream):
    client, servers = stack
    server = servers["claude"]
    server.disconnected.clear()
    before = len(server.calls)
    body = {
        "model": "claude-default",
        "messages": [{"role": "user", "content": "wait-for-disconnect"}],
        "stream": stream,
    }
    if stream:
        with client.stream("POST", "/v1/chat/completions", json=body) as response:
            assert response.status_code == 200
            assert next(response.iter_lines()).startswith("data:")
    else:
        with pytest.raises(httpx.ReadTimeout):
            client.post("/v1/chat/completions", json=body, timeout=0.3)
    assert server.disconnected.wait(5), "LiteLLM left upstream connected"
    assert len(server.calls) == before + 1


@pytest.mark.parametrize(
    "path",
    [
        "/engines/claude-default/chat/completions",
        "/openai/deployments/claude-default/chat/completions",
        "/v1/responses",
        "/queue/chat/completions",
    ],
)
def test_alternate_agent_endpoints_cannot_bypass_validation(stack, path):
    client, servers = stack
    before = sum(len(s.calls) for s in servers.values())
    response = client.post(
        path,
        json={
            "model": "claude-default",
            "messages": [{"role": "user", "content": "hello"}],
            "api_key": "override",
            "extra_headers": {"X-Override": "bad"},
        },
    )
    assert response.status_code == 400
    assert sum(len(s.calls) for s in servers.values()) == before


def test_new_input_limit_does_not_shrink_codex_contract(stack):
    client, _ = stack
    messages = [{"role": "user", "content": "x" * (129 * 1024)}]
    assert (
        client.post(
            "/v1/chat/completions",
            json={"model": "codex-default", "messages": messages},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/chat/completions",
            json={"model": "claude-default", "messages": messages},
        ).status_code
        == 400
    )


def test_router_never_invents_usage_for_agent_stream(stack):
    client, _ = stack
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-default",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )
    assert response.status_code == 200
    chunks = [
        json.loads(line[5:])
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    assert all(chunk.get("usage") is None for chunk in chunks)


def test_stream_error_never_gets_done_or_stop_after_error(stack):
    client, _ = stack
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-default",
            "messages": [{"role": "user", "content": "stream-error"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert '"error"' in response.text
    assert "[DONE]" not in response.text
    after_error = response.text[response.text.index('"error"') :]
    assert '"finish_reason": "stop"' not in after_error


@pytest.mark.parametrize("model", [[], {}, 123, None])
def test_malformed_model_is_client_error(stack, model):
    client, _ = stack
    assert (
        client.post(
            "/v1/chat/completions", json={"model": model, "messages": []}
        ).status_code
        == 400
    )
