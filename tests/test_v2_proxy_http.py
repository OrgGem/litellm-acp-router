"""Wire V2 through a real LiteLLM Proxy with synthetic, in-process drivers."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from test_v2_host import TOOL

KEY = "sk-v2-fixture-owner-1234567890123456"
CONTROL = {"Authorization": "Bearer v2-control-token"}


@contextmanager
def start_proxy(directory, virtual=False, execution_mode="native"):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = {
        "enabled": {"codex": True},
        "keys": {"router": KEY},
        "ports": {"router": port},
        "routes": {"test": {"backend": "codex", "model": "fake"}},
        "v2": {
            "enabled": True,
            "routes": {
                "test": {
                    "profile_id": "fixture",
                    "profile_revision": "1",
                    "transport": "stdio",
                    **(
                        {"execution_mode": "answer_only"}
                        if execution_mode == "answer_only"
                        else {}
                    ),
                }
            },
            "auth_mode": "virtual_keys" if virtual else "owner",
        },
    }
    (directory / "config.json").write_text(json.dumps(config))
    with (directory / "server.log").open("wb") as log:
        # A proxy lifespan may connect/migrate DATABASE_URL. Never inherit the
        # operator's environment or project dotenv into this fixture process.
        child_env = {
            name: os.environ[name]
            for name in (
                "SystemRoot",
                "WINDIR",
                "PATH",
                "PATHEXT",
                "TEMP",
                "TMP",
                "USERPROFILE",
            )
            if name in os.environ
        }
        child_env.update(
            {
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                "LITELLM_TELEMETRY": "False",
                "AGENT_RPC_CONTROL_TOKEN": "v2-control-token",
                "AGENT_RPC_INSTANCE_ID": "v2-fixture",
            }
        )
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("fixture_v2_proxy_server.py")),
                str(directory),
            ],
            stdout=log,
            stderr=log,
            cwd=directory,
            env=child_env,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": "Bearer " + KEY},
                timeout=15,
                trust_env=False,
            ) as client:
                for _ in range(200):
                    if child.poll() is not None:
                        pytest.fail(
                            (directory / "server.log").read_text(errors="replace")[
                                -10000:
                            ]
                        )
                    try:
                        if client.get("/v1/models").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail("V2 fixture proxy did not start")
                yield client, directory
        finally:
            child.terminate()
            child.wait(timeout=15)


@pytest.fixture(scope="module")
def proxy(tmp_path_factory):
    with start_proxy(tmp_path_factory.mktemp("v2-proxy")) as server:
        yield server


@pytest.fixture(scope="module")
def virtual_proxy(tmp_path_factory):
    with start_proxy(
        tmp_path_factory.mktemp("v2-virtual-proxy"), virtual=True
    ) as server:
        yield server


@pytest.fixture(scope="module")
def answer_only_proxy(tmp_path_factory):
    with start_proxy(
        tmp_path_factory.mktemp("v2-answer-only"), execution_mode="answer_only"
    ) as server:
        yield server


def body(**values):
    return {
        "model": "test",
        "messages": [{"role": "user", "content": "hello"}],
        **values,
    }


def test_text_and_usage(proxy):
    client, _ = proxy
    result = client.post("/v1/chat/completions", json=body())
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["choices"][0]["message"]["content"] == "answer"
    assert data["usage"]["prompt_tokens"] == 10
    assert data["usage"]["completion_tokens"] == 3
    assert client.post(
        "/v1/chat/completions", json=body(), headers={"Authorization": "Bearer bad"}
    ).status_code in {400, 401, 403}


def test_tool_handoff_and_stream(proxy):
    client, _ = proxy
    headers = {"X-Conversation-ID": "tool-chat"}
    initial = body(tools=[TOOL])
    first = client.post("/v1/chat/completions", json=initial, headers=headers)
    assert first.status_code == 200, first.text
    assistant = first.json()["choices"][0]["message"]
    assert first.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert "n1" not in json.dumps(assistant)
    status = client.get("/_agent-rpc/control/status", headers=CONTROL).json()["v2"]
    assert status["counts"]["awaiting_client"] == 1
    follow = {
        **initial,
        "messages": initial["messages"]
        + [
            assistant,
            {
                "role": "tool",
                "tool_call_id": assistant["tool_calls"][0]["id"],
                "content": "client result",
            },
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    result = client.post("/v1/chat/completions", json=follow, headers=headers)
    assert result.status_code == 200, result.text
    assert "answer" in result.text and "data: [DONE]" in result.text
    frames = [
        json.loads(line[5:].strip())
        for line in result.text.splitlines()
        if line.startswith("data:") and line[5:].strip() != "[DONE]"
    ]
    # Native usage covered the whole handoff, not the second HTTP round.
    assert all(frame.get("usage") is None for frame in frames)


def test_untrusted_parameters_rejected(proxy):
    client, _ = proxy
    for field in ("api_key", "api_base", "metadata", "user", "max_retries"):
        result = client.post("/v1/chat/completions", json=body(**{field: "untrusted"}))
        assert result.status_code == 400, result.text


def test_stream_text_and_truthful_usage(proxy):
    client, _ = proxy
    result = client.post(
        "/v1/chat/completions",
        json=body(stream=True, stream_options={"include_usage": True}),
    )
    assert result.status_code == 200, result.text
    assert "answer" in result.text and "data: [DONE]" in result.text
    frames = [
        json.loads(line[5:].strip())
        for line in result.text.splitlines()
        if line.startswith("data:") and line[5:].strip() != "[DONE]"
    ]
    usage = [frame["usage"] for frame in frames if frame.get("usage")]
    assert usage and usage[-1]["prompt_tokens"] == 10
    assert usage[-1]["completion_tokens"] == 3


def test_reasoning_is_explicit_and_no_cache_reuse(proxy):
    client, directory = proxy
    before = (directory / "driver.events").read_text().count('"event": "open"')
    for _ in range(2):
        result = client.post(
            "/v1/chat/completions", json=body(reasoning_output="summary")
        )
        assert result.status_code == 200, result.text
        assert result.json()["choices"][0]["message"]["reasoning_content"] == "public"
    assert (directory / "driver.events").read_text().count(
        '"event": "open"'
    ) == before + 2
    time.sleep(0.2)
    assert '"event": "cache"' not in (directory / "driver.events").read_text()


def test_real_disconnect_cancels_native_owner(proxy):
    client, directory = proxy
    path = directory / "driver.events"
    before = len(path.read_text().splitlines())
    with pytest.raises(httpx.ReadTimeout):
        client.post(
            "/v1/chat/completions",
            json={"model": "test", "messages": [{"role": "user", "content": "wait"}]},
            timeout=0.7,
        )
    for _ in range(50):
        events = [json.loads(line) for line in path.read_text().splitlines()[before:]]
        if {"waiting", "cancel", "close"} <= {event["event"] for event in events}:
            break
        time.sleep(0.1)
    else:
        pytest.fail("disconnected request retained its native owner")


def test_drain_rejects_new_turns_and_allows_owned_continuation(proxy):
    client, _ = proxy
    initial = body(tools=[TOOL])
    headers = {"X-Conversation-ID": "drain-owned"}
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
    try:
        assert (
            client.post("/_agent-rpc/control/drain", headers=CONTROL).status_code == 200
        )
        assert (
            client.post("/_agent-rpc/control/stop", headers=CONTROL).status_code == 409
        )
        new = client.post("/v1/chat/completions", json=body())
        assert new.status_code == 503, new.text
        continued = client.post("/v1/chat/completions", json=follow, headers=headers)
        assert continued.status_code == 200, continued.text
        assert continued.json()["choices"][0]["message"]["content"] == "answer"
    finally:
        assert (
            client.post("/_agent-rpc/control/resume", headers=CONTROL).status_code
            == 200
        )


def test_verified_key_scopes_isolate_handoff_and_revoke(virtual_proxy):
    client, directory = virtual_proxy
    cid = "shared-cid"
    alice = {"Authorization": "Bearer sk-fixture-alice", "X-Conversation-ID": cid}
    bob = {"Authorization": "Bearer sk-fixture-bob", "X-Conversation-ID": cid}
    initial = body(tools=[TOOL])
    first = client.post("/v1/chat/completions", json=initial, headers=alice)
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
    crossed = client.post("/v1/chat/completions", json=follow, headers=bob)
    assert crossed.status_code in {400, 409, 410}, crossed.text
    assert "submit" not in (directory / "driver.events").read_text()
    (directory / "revoked").write_text("alice\n")
    for _ in range(80):
        status = client.get("/_agent-rpc/control/status", headers=CONTROL).json()["v2"]
        if status["counts"]["awaiting_client"] == 0:
            break
        time.sleep(0.1)
    else:
        pytest.fail("revoked pending session did not close within 8 seconds")
    result = client.post("/v1/chat/completions", json=follow, headers=alice)
    assert result.status_code in {401, 403}, result.text


def test_answer_only_text_is_buffered_and_uses_one_closed_invocation(answer_only_proxy):
    client, directory = answer_only_proxy
    event_log = directory / "driver.events"
    before = (
        event_log.read_text().count('"event": "answer_start"')
        if event_log.exists()
        else 0
    )
    response = client.post("/v1/chat/completions", json=body())

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "answer-only answer"
    events = [
        json.loads(line)
        for line in (directory / "driver.events").read_text().splitlines()
    ]
    names = [event["event"] for event in events]
    assert names.count("answer_start") == before + 1
    assert names.count("answer_close") == before + 1


def test_answer_only_tool_handoff_closes_each_invocation(answer_only_proxy):
    client, directory = answer_only_proxy
    event_log = directory / "driver.events"
    before = (
        event_log.read_text().count('"event": "answer_start"')
        if event_log.exists()
        else 0
    )
    headers = {"X-Conversation-ID": "answer-tool-chat"}
    initial = body(tools=[TOOL])
    first = client.post("/v1/chat/completions", json=initial, headers=headers)
    assert first.status_code == 200, first.text
    assistant = first.json()["choices"][0]["message"]
    assert first.json()["choices"][0]["finish_reason"] == "tool_calls"
    status = client.get("/_agent-rpc/control/status", headers=CONTROL).json()["v2"]
    assert status["counts"]["awaiting_client"] == 1
    assert status["counts"]["native_running"] == 0

    followup = {
        **initial,
        "messages": initial["messages"]
        + [
            assistant,
            {
                "role": "tool",
                "tool_call_id": assistant["tool_calls"][0]["id"],
                "content": "client result",
            },
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    result = client.post("/v1/chat/completions", json=followup, headers=headers)
    assert result.status_code == 200, result.text
    assert "continued" in result.text and "data: [DONE]" in result.text
    events = [
        json.loads(line)
        for line in (directory / "driver.events").read_text().splitlines()
    ]
    names = [event["event"] for event in events]
    assert names.count("answer_start") == before + 2
    assert names.count("answer_close") == before + 2
    assert "submit" not in names
