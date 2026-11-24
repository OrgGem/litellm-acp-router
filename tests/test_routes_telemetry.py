import asyncio
import json
import sqlite3
import threading
import time

import pytest
from litellm import ModelResponse

from litellm_agent_rpc import telemetry
from litellm_agent_rpc.backends import Handle
from litellm_agent_rpc.config import (
    backend_models,
    load_config,
    revision,
    route_specs,
    routes,
    save_config,
)
from litellm_agent_rpc.policy import Ingress
from litellm_agent_rpc.provider import AgentRPC


@pytest.fixture
def config():
    return {
        "keys": {"router": "sk-test-secret-1234567890123456", "legacy": "preserve"},
        "enabled": {"codex": True, "antigravity": True, "claude": False, "kiro": False},
        "codex_model": "legacy-model",
        "antigravity_models": {
            "antigravity": {"model": "legacy-agy", "thought_level": "medium"}
        },
        "claude_models": {"claude-default": None},
        "kiro_models": {"kiro-default": None},
        "other": {"keep": ["this"]},
    }


def test_legacy_normalization_and_multiple_aliases(config):
    legacy = route_specs(config)
    assert legacy["codex-default"]["model"] == "legacy-model"
    assert legacy["antigravity"]["settings"] == {"thought_level": "medium"}
    assert set(routes(config)) == {"codex-default", "antigravity"}
    config["routes"] = {
        "fast": {"backend": "codex", "model": "model-fast"},
        "deep": {"backend": "codex", "model": "model-deep"},
        "thinking": {
            "backend": "antigravity",
            "model": "agy-model",
            "settings": {"thought_level": "high"},
        },
        "off": {"backend": "codex", "model": None, "enabled": False},
        "backend-off": {"backend": "claude", "model": None},
    }
    assert routes(config) == {
        "fast": "codex",
        "deep": "codex",
        "thinking": "antigravity",
    }
    assert backend_models(config, "codex") == {
        "fast": "model-fast",
        "deep": "model-deep",
    }
    assert backend_models(config, "antigravity") == {
        "thinking": {"model": "agy-model", "thought_level": "high"}
    }
    assert "codex-default" not in route_specs(config)


@pytest.mark.parametrize(
    "entry",
    [
        {"backend": "codex", "model": "x", "settings": {"effort": "high"}},
        {"backend": "antigravity", "model": None},
        {"backend": "codex", "model": " "},
        {"backend": "unknown", "model": None},
        {"backend": "codex", "enabled": 1},
    ],
)
def test_invalid_route_rejected(config, entry):
    config["routes"] = {"model": entry}
    with pytest.raises(ValueError):
        route_specs(config)


def test_atomic_save_detects_conflict_preserves_keys_and_allows_draft(config, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    previous = revision(path)
    edited = load_config(path)
    edited["enabled"] = dict.fromkeys(edited["enabled"], False)
    edited["routes"] = {"draft": {"backend": "codex", "model": None}}
    new_revision = save_config(path, edited, previous)
    assert new_revision == revision(path) and new_revision != previous
    saved = load_config(path)
    assert saved["keys"] == config["keys"]
    assert saved["other"] == config["other"]
    assert saved["codex_model"] == "legacy-model"
    assert routes(saved) == {}
    with pytest.raises(ValueError, match="changed"):
        save_config(path, config, previous)
    assert revision(path) == new_revision


def test_inactive_antigravity_draft_and_enabled_default(config):
    config["enabled"]["antigravity"] = False
    config["routes"] = {
        "fast": {"backend": "codex", "model": None},
        "agy-draft": {"backend": "antigravity", "model": None},
    }
    config["default_route"] = "fast"
    assert route_specs(config)["agy-draft"]["model"] is None
    config["enabled"]["codex"] = False
    with pytest.raises(ValueError, match="default"):
        route_specs(config)
    config.pop("default_route")
    assert routes(config) == {}
    config["enabled"]["antigravity"] = True
    with pytest.raises(ValueError, match="explicit model"):
        route_specs(config)


def test_absent_revision_prevents_overwriting_a_new_file(config, tmp_path):
    path = tmp_path / "config.json"
    assert revision(path) is None
    path.write_text("another editor wrote this", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        save_config(path, config, expected_revision=None)
    assert path.read_text(encoding="utf-8") == "another editor wrote this"


@pytest.mark.asyncio
async def test_dynamic_ingress_enforces_text_and_limits(monkeypatch):
    monkeypatch.setenv("ROUTER_AGENT_ROUTES", json.dumps({"fast": "codex"}))
    monkeypatch.setenv(
        "ROUTER_AGENT_LIMITS", json.dumps({"fast": {"max_input_bytes": 1024}})
    )
    called = []

    async def app(scope, receive, send):
        called.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def post(payload, path="/v1/chat/completions"):
        sent = []
        events = [{"type": "http.request", "body": json.dumps(payload).encode()}]

        async def receive():
            return events.pop(0)

        async def send(event):
            sent.append(event)

        await Ingress(app)(
            {"type": "http", "method": "POST", "path": path, "headers": []},
            receive,
            send,
        )
        return sent[0]["status"]

    assert (
        await post(
            {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}
        )
        == 200
    )
    assert (
        await post(
            {
                "model": "fast",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": "x"}],
                    }
                ],
            }
        )
        == 400
    )
    assert (
        await post(
            {
                "model": "fast",
                "messages": [{"role": "user", "content": "x"}],
                "tools": [],
            }
        )
        == 400
    )
    assert (
        await post(
            {"model": "fast", "messages": [{"role": "user", "content": "x"}]},
            "/v1/embeddings",
        )
        == 400
    )
    assert len(called) == 1


class NativeProvider:
    async def acompletion(self, model, messages, **kwargs):
        return ModelResponse(
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 2},
                "completion_tokens_details": {"reasoning_tokens": 1},
            }
        )

    async def astreaming(self, model, messages, **kwargs):
        yield {"text": "some text", "is_finished": False, "usage": None}
        yield {
            "text": "",
            "is_finished": True,
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }


async def run_http(app):
    async def receive():
        return {"type": "http.request", "body": b"SECRET PROMPT"}

    async def send(event):
        pass

    await telemetry.TelemetryMiddleware(app)(
        {"type": "http", "method": "POST", "path": "/v1/chat/completions"},
        receive,
        send,
    )


@pytest.mark.asyncio
async def test_native_usage_provenance_and_private_metadata(tmp_path):
    db = tmp_path / "usage.sqlite"
    telemetry.configure(db, "local")
    provider = AgentRPC(
        {
            "enabled": {"codex": True},
            "routes": {"fast": {"backend": "codex", "model": "m"}},
        },
        lambda name, config: Handle(NativeProvider(), lambda: None),
    )

    async def app(scope, receive, send):
        await receive()
        await provider.acompletion(
            "fast", [{"role": "user", "content": "SECRET PROMPT"}]
        )
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"SECRET RESPONSE"})

    await run_http(app)
    summary = telemetry.summary(db)
    assert summary["success"] == 1 and summary["error"] == 0
    assert summary["usage"]["source"] == "backend"
    assert summary["usage"]["input_tokens"] == 10
    assert summary["usage"]["output_tokens"] == 4
    assert summary["usage"]["cache_read_tokens"] == 2
    assert summary["usage"]["cache_write_tokens"] is None
    assert summary["usage"]["reasoning_tokens"] == 1
    assert summary["usage"]["by_source"]["codex_rpc"]["requests"] == 1
    assert b"SECRET PROMPT" not in db.read_bytes()
    assert b"SECRET RESPONSE" not in db.read_bytes()
    provider.close()
    telemetry.configure(None)



@pytest.mark.asyncio
async def test_stream_once_unavailable_error_and_cancellation(tmp_path):
    db = tmp_path / "usage.sqlite"
    telemetry.configure(db, "local")
    provider = AgentRPC(
        {"enabled": {"codex": True}, "routes": {"fast": {"backend": "codex"}}},
        lambda name, config: Handle(NativeProvider(), lambda: None),
    )

    async def stream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        async for chunk in provider.astreaming("fast", []):
            await send(
                {
                    "type": "http.response.body",
                    "body": b"data: {}\n\n",
                    "more_body": True,
                }
            )
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})

    async def failed(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b'data: {"error":{"message":"secret"}}\n\n',
            }
        )

    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError()

    await run_http(stream)
    await run_http(failed)
    with pytest.raises(asyncio.CancelledError):
        await run_http(cancelled)
    summary = telemetry.current_summary()
    assert (
        summary["requests"],
        summary["success"],
        summary["error"],
        summary["cancelled"],
    ) == (3, 1, 1, 1)
    assert summary["usage"]["reported_requests"] == 1
    assert summary["usage"]["input_tokens"] == 3
    assert summary["avg_ttft_ms"] is not None
    assert b"secret" not in db.read_bytes()
    provider.close()
    telemetry.configure(None)



@pytest.mark.asyncio
async def test_unknown_usage_and_active_request_remain_distinct_from_zero(tmp_path):
    db = tmp_path / "usage.sqlite"
    telemetry.configure(db, "local")
    entered, release = asyncio.Event(), asyncio.Event()

    class NoUsage:
        async def acompletion(self, model, messages, **kwargs):
            return ModelResponse()

    provider = AgentRPC(
        {"enabled": {"codex": True}},
        lambda name, config: Handle(NoUsage(), lambda: None),
    )

    async def app(scope, receive, send):
        await provider.acompletion("codex-default", [])
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    request = asyncio.create_task(run_http(app))
    await asyncio.wait_for(entered.wait(), 3)
    assert telemetry.summary(db)["active"] == 1
    release.set()
    await request
    result = telemetry.summary(db)
    assert result["success"] == 1 and result["active"] == 0
    assert result["usage"]["reported_requests"] == 0
    assert result["usage"]["input_tokens"] is None
    provider.close()
    telemetry.configure(None)



@pytest.mark.asyncio
async def test_cancel_during_sqlite_start_finishes_the_started_row(tmp_path):
    db = tmp_path / "usage.sqlite"
    telemetry.configure(db, "local")
    store = telemetry._store
    original = store.start
    entered, release = threading.Event(), threading.Event()

    def blocked_start(trace):
        entered.set()
        release.wait(timeout=5)
        original(trace)

    store.start = blocked_start

    async def app(scope, receive, send):
        pytest.fail("A cancelled request must not be dispatched")

    task = asyncio.create_task(run_http(app))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "Cancellation must wait for the pending start write"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = telemetry.summary(db)
    assert (result["requests"], result["active"], result["cancelled"]) == (1, 0, 1)
    telemetry.configure(None)


@pytest.mark.asyncio
async def test_unexpected_telemetry_start_failure_does_not_fail_request(tmp_path):
    db = tmp_path / "usage.sqlite"
    telemetry.configure(db, "local")

    def fail(_trace):
        raise RuntimeError("internal telemetry failure")

    telemetry._store.start = fail

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    await run_http(app)
    assert telemetry.current_summary()["degraded"]
    telemetry.configure(None)


def test_missing_store_reports_usage_unavailable(tmp_path):
    result = telemetry.summary(tmp_path / "missing.sqlite")
    assert not result["available"]
    assert result["usage"]["input_tokens"] is None
    assert result["usage"]["output_tokens"] is None


def test_telemetry_accepts_longest_valid_compose_environment(tmp_path):
    environment = "docker:" + "p" * 63 + ":" + "s" * 63
    assert len(environment) == 134
    telemetry.configure(tmp_path / "usage.sqlite", environment)
    assert telemetry.current_summary()["environment"] == environment
    with pytest.raises(ValueError, match="environment"):
        telemetry.configure(tmp_path / "usage.sqlite", environment + "s")
    telemetry.configure(None)


def test_old_metadata_is_pruned_on_startup(tmp_path):
    db_path = tmp_path / "usage.sqlite"
    telemetry.configure(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO requests (id, environment, started_at, status) VALUES (?, ?, ?, ?)",
            ("old", "local", time.time() - 31 * 86400, "success"),
        )
        db.execute(
            "INSERT INTO requests (id, environment, started_at, status) VALUES (?, ?, ?, ?)",
            ("recent", "local", time.time(), "success"),
        )
    telemetry.configure(db_path)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT id FROM requests").fetchall() == [("recent",)]
    telemetry.configure(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", ["antigravity", "claude"])
async def test_antigravity_and_claude_telemetry_details_and_responses_ingress(
    tmp_path, monkeypatch, backend_name
):
    from litellm_agent_rpc.config import backend_flatten

    cfg = {
        "keys": {"router": "sk-test-secret-1234567890123456"},
        "enabled": {"antigravity": True, "claude": True},
        "routes": {
            "agy": {
                "backend": "antigravity",
                "model": "gemini-4-carbon",
                "settings": {"thought_level": "medium", "flatten": True},
            },
            "cld": {
                "backend": "claude",
                "model": None,
                "settings": {"flatten": True},
            },
        },
    }
    assert backend_flatten(cfg, "antigravity") is True
    assert backend_flatten(cfg, "claude") is True

    db = tmp_path / f"{backend_name}.sqlite"
    telemetry.configure(db, "local")
    alias = "agy" if backend_name == "antigravity" else "cld"
    provider = AgentRPC(
        cfg,
        lambda name, config: Handle(NativeProvider(), lambda: None),
    )

    async def app(scope, receive, send):
        await receive()
        await provider.acompletion(alias, [{"role": "user", "content": "hi"}])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b'{"id":"resp_1","object":"response","usage":{"input_tokens":10}}',
            }
        )

    monkeypatch.setenv("ROUTER_AGENT_ROUTES", json.dumps({alias: backend_name}))
    sent = []

    async def send(msg):
        sent.append(msg)

    events = [
        {
            "type": "http.request",
            "body": json.dumps({"model": alias, "input": "hello"}).encode(),
        }
    ]
    await telemetry.TelemetryMiddleware(Ingress(app))(
        {"type": "http", "method": "POST", "path": "/v1/responses", "headers": []},
        lambda: asyncio.sleep(0, events.pop(0)),
        send,
    )
    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"])["usage"] is None
    summary = telemetry.summary(db)
    assert summary["usage"]["cache_read_tokens"] == 2
    assert summary["usage"]["reasoning_tokens"] == 1
    provider.close()
    telemetry.configure(None)


@pytest.mark.asyncio
async def test_per_alias_flatten_isolated_across_ingress_and_shared_backend(monkeypatch):
    import httpx
    from litellm.llms.custom_llm import CustomLLMError
    from litellm_antigravity_rpc.backend import ACPBackend
    from litellm_antigravity_rpc.contracts import Event as AgyEvent

    from litellm_agent_rpc import backends
    from litellm_agent_rpc.config import flatten_routes

    cfg = {
        "allow_prompt_flattening": False,
        "keys": {"router": "sk-test-secret-1234567890123456"},
        "enabled": {"antigravity": True},
        "routes": {
            "agy-strict": {
                "backend": "antigravity",
                "model": "gemini-4-carbon",
                "settings": {"flatten": False},
            },
            "agy-flat": {
                "backend": "antigravity",
                "model": "gemini-4-carbon",
                "settings": {"flatten": True},
            },
        },
    }

    async def fake_startup(self):
        self.ready = True

    async def fake_stream(self, request, alias=None):
        yield AgyEvent(text="ok:" + (request.model or ""))
        yield AgyEvent(done=True, finish_reason="stop")

    monkeypatch.setattr(ACPBackend, "startup", fake_startup)
    monkeypatch.setattr(ACPBackend, "stream", fake_stream)

    provider = AgentRPC(cfg, backends.create_backend)

    async def downstream(scope, receive, send):
        body = b""
        while True:
            ev = await receive()
            body += ev.get("body", b"")
            if not ev.get("more_body"):
                break
        payload = json.loads(body)
        try:
            res = await provider.acompletion(payload["model"], payload["messages"])
        except CustomLLMError as exc:
            await send(
                {
                    "type": "http.response.start",
                    "status": exc.status_code,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps({"error": {"message": str(exc)}}).encode(),
                }
            )
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(res.model_dump(exclude_none=True)).encode(),
            }
        )

    monkeypatch.setenv("ROUTER_AGENT_ROUTES", json.dumps(routes(cfg)))
    monkeypatch.setenv(
        "ROUTER_ALLOW_FLATTENING",
        "1" if cfg.get("allow_prompt_flattening") else "0",
    )
    monkeypatch.setenv("ROUTER_AGENT_FLATTEN", json.dumps(flatten_routes(cfg)))

    ingress = Ingress(downstream)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(ingress), base_url="http://router"
        ) as client:
            # agy-strict (flatten=false) rejects user -> assistant -> user with 400
            strict_res = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agy-strict",
                    "messages": [
                        {"role": "user", "content": "u1"},
                        {"role": "assistant", "content": "a1"},
                        {"role": "user", "content": "u2"},
                    ],
                },
            )
            assert strict_res.status_code == 400, strict_res.text

            # agy-flat (flatten=true) accepts user -> developer -> user with 200
            flat_res = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agy-flat",
                    "messages": [
                        {"role": "user", "content": "u1"},
                        {"role": "developer", "content": "d1"},
                        {"role": "user", "content": "u2"},
                    ],
                },
            )
            assert flat_res.status_code == 200, flat_res.text
            assert flat_res.json()["choices"][0]["message"]["content"] == "ok:agy-flat"

            # agy-strict still accepts single user message
            single_res = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agy-strict",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            assert single_res.status_code == 200, single_res.text
    finally:
        provider.close()

    # Global allow_prompt_flattening=True still honors explicit alias flatten=False
    cfg_global_on = {**cfg, "allow_prompt_flattening": True}
    assert flatten_routes(cfg_global_on) == {"agy-strict": False, "agy-flat": True}


