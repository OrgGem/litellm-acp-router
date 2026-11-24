import asyncio
import json
import runpy
import threading
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

bridge = runpy.run_path(str(Path(__file__).parents[1] / "deploy/codex-minipod/bridge.py"))
BRIDGE_GLOBALS = bridge["Handler"].do_POST.__globals__
MODEL_ALIASES = BRIDGE_GLOBALS["MODEL_ALIASES"]
EXPECTED_CODEX_MODELS = {
    "gpt-6.2-sol",
    "gpt-6.2-astra",
    "gpt-6.1-terra",
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
}


def start_test_server(monkeypatch, run_turn):
    monkeypatch.setitem(BRIDGE_GLOBALS, "run_turn", run_turn)
    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer test-key"
    server.profile = Path("profile")
    server.workspace = Path("workspace")
    server.codex = "codex"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def send_chat(server, payload):
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
    )
    return urlopen(request, timeout=5)


def test_model_catalog_has_ten_codex_models_and_luna_default_alias():
    assert set(MODEL_ALIASES) == EXPECTED_CODEX_MODELS | {"codex-minipod"}
    assert MODEL_ALIASES["codex-minipod"] == "gpt-6-luna"
    assert {model for alias, model in MODEL_ALIASES.items() if alias != "codex-minipod"} == EXPECTED_CODEX_MODELS


@pytest.mark.parametrize(("alias", "codex_model"), sorted(MODEL_ALIASES.items()))
def test_request_model_routes_to_explicit_codex_model(monkeypatch, alias, codex_model):
    selected_models = []

    async def fake_run_turn(prompt, profile, workspace, codex, emit_text=None, *, model):
        selected_models.append(model)
        return "READY", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    server, thread = start_test_server(monkeypatch, fake_run_turn)
    try:
        with send_chat(
            server,
            {"model": alias, "messages": [{"role": "user", "content": "check"}]},
        ) as response:
            body = json.load(response)
            assert response.status == 200

        assert body["model"] == alias
        assert selected_models == [codex_model]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_unknown_request_model_is_rejected_before_codex_starts(monkeypatch):
    started = False

    async def fail_if_started(*args, **kwargs):
        nonlocal started
        started = True
        raise AssertionError("Codex must not start for an unknown model")

    server, thread = start_test_server(monkeypatch, fail_if_started)
    try:
        with pytest.raises(HTTPError) as error:
            send_chat(
                server,
                {"model": "gpt-unknown", "messages": [{"role": "user", "content": "check"}]},
            )
        assert error.value.code == 400
        assert json.loads(error.value.read())["error"] == "unsupported model"
        assert not started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class FakeProcess:
    def __init__(self):
        self.stdin = SimpleNamespace(close=lambda: None)
        self.returncode = 0

    async def wait(self):
        return 0


class FakeAppServer:
    def __init__(self, process, selected_model, calls):
        self.selected_model = selected_model
        self.calls = calls
        self.events = deque(
            [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-id",
                        "turnId": "turn-id",
                        "item": {"id": "message-id", "type": "agentMessage", "text": "READY"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-id",
                        "turn": {"id": "turn-id", "status": "completed"},
                    },
                },
            ]
        )

    async def send(self, message):
        self.calls.append(("send", message))

    async def call(self, method, params, pending=None):
        self.calls.append((method, params))
        if method == "environment/info":
            return {"cwd": "file:///workspace"}
        if method == "thread/start":
            return {
                "approvalPolicy": "never",
                "model": self.selected_model,
                "thread": {"id": "thread-id"},
            }
        if method == "turn/start":
            return {"turn": {"id": "turn-id"}}
        return {}

    async def read(self):
        if self.calls and self.calls[-1][0] == "send":
            message = self.calls[-1][1]
            if message.get("method") == "fs/readFile":
                return {"id": 99999, "error": {"message": "local filesystem is not configured"}}
        return self.events.popleft()


def patch_fake_app_server(monkeypatch, selected_model):
    calls = []
    process = FakeProcess()

    async def create_subprocess_exec(*args, **kwargs):
        calls.append(("subprocess", args, kwargs))
        return process

    monkeypatch.setattr(BRIDGE_GLOBALS["asyncio"], "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setitem(
        BRIDGE_GLOBALS,
        "AppServer",
        lambda current_process: FakeAppServer(current_process, selected_model, calls),
    )
    return calls


@pytest.mark.parametrize("model", sorted(EXPECTED_CODEX_MODELS))
def test_thread_start_selects_the_requested_codex_model(monkeypatch, model):
    calls = patch_fake_app_server(monkeypatch, model)

    answer, _ = asyncio.run(
        bridge["run_turn"]("check", Path("profile"), Path("workspace"), "codex", model=model)
    )

    thread_start = next(params for method, params, *_ in calls if method == "thread/start")
    turn_start = next(params for method, params, *_ in calls if method == "turn/start")
    assert thread_start["model"] == model
    assert turn_start["threadId"] == "thread-id"
    assert answer == "READY"


def test_codex_model_mismatch_fails_before_turn_start(monkeypatch):
    calls = patch_fake_app_server(monkeypatch, "gpt-6-sol")

    with pytest.raises(RuntimeError, match="unexpected model"):
        asyncio.run(
            bridge["run_turn"](
                "check",
                Path("profile"),
                Path("workspace"),
                "codex",
                model="gpt-6-luna",
            )
        )

    assert any(method == "thread/start" for method, *_ in calls)
    assert not any(method == "turn/start" for method, *_ in calls)


def test_streaming_response_preserves_alias_and_emits_text_in_chunks(monkeypatch):
    async def fake_run_turn(prompt, profile, workspace, codex, emit_text=None, *, model):
        emit_text("LUNA-")
        emit_text("CHECK")
        return "LUNA-CHECK", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}

    server, thread = start_test_server(monkeypatch, fake_run_turn)
    try:
        with send_chat(
            server,
            {
                "model": "codex-minipod",
                "stream": True,
                "messages": [{"role": "user", "content": "check"}],
            },
        ) as response:
            events = [
                json.loads(line[6:])
                for line in response
                if line.startswith(b"data: ") and line.strip() != b"data: [DONE]"
            ]
            assert events[-1]["choices"][0]["finish_reason"] == "stop"
            assert response.status == 200

        text_chunks = [
            event["choices"][0]["delta"].get("content", "")
            for event in events
            if event["choices"][0]["delta"].get("content")
        ]
        assert "".join(text_chunks) == "LUNA-CHECK"
        assert all(event["model"] == "codex-minipod" for event in events)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
