"""Synthetic drivers through the actual LiteLLM Proxy; no agent process starts."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

from fastapi import HTTPException, Request
from litellm.caching.caching import Cache
from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS
from litellm.proxy._types import UserAPIKeyAuth
from test_v2_answer_only import answer_manifest
from test_v2_host import PROFILE, Driver, manifest

from litellm_agent_rpc import server
from litellm_agent_rpc.v2.broker import Binding
from litellm_agent_rpc.v2.host import ProxyV2Host

directory = Path(sys.argv[1])
config = json.loads((directory / "config.json").read_text())
KEYS = {"sk-fixture-alice": "alice", "sk-fixture-bob": "bob"}


def record(event):
    with (directory / "driver.events").open("a", encoding="utf-8") as file:
        file.write(json.dumps(event) + "\n")


# Enable a real in-process LiteLLM response cache and observe both directions.
# V2's trusted policy must prevent the cache methods from being called.
for method_name in ("get_cache", "async_get_cache", "add_cache", "async_add_cache"):
    original = getattr(Cache, method_name)
    if method_name.startswith("async_"):

        async def observed(
            self, *args, _original=original, _name=method_name, **kwargs
        ):
            record({"event": "cache", "method": _name})
            return await _original(self, *args, **kwargs)
    else:

        def observed(self, *args, _original=original, _name=method_name, **kwargs):
            record({"event": "cache", "method": _name})
            return _original(self, *args, **kwargs)

    setattr(Cache, method_name, observed)

original_proxy_config = server.proxy_config


def fixture_proxy_config(config):
    result = original_proxy_config(config)
    result["litellm_settings"].update(cache=True, cache_params={"type": "local"})
    return result


server.proxy_config = fixture_proxy_config


class FakeDriver(Driver):
    async def capabilities(self, profile, model):
        return {**manifest(), "transport": "stdio"}

    async def open_session(self, context, config):
        record({"event": "open", "scope": context["key_scope_id"]})
        return await super().open_session(context, config)

    async def begin_turn(self, session, turn_input):
        turn = await super().begin_turn(session, turn_input)
        turn["invocation"] = "fixture-" + str(self.opens)
        self.tool = bool(turn_input["tools"])
        return turn

    async def events(self, turn):
        if turn["input"]["messages"][-1].get("content") == "wait":
            record({"event": "waiting"})
            await asyncio.Event().wait()
        async for event in super().events(turn):
            event["invocation_id"] = turn["invocation"]
            if event["type"] == "usage":
                event["payload"]["invocation_id"] = turn["invocation"]
            yield event

    async def submit_tool_results(self, turn, results):
        record({"event": "submit"})
        await super().submit_tool_results(turn, results)

    async def cancel(self, turn, reason, cleanup_deadline):
        record({"event": "cancel", "reason": reason})
        await super().cancel(turn, reason, cleanup_deadline)

    async def close_session(self, session, cleanup_deadline):
        record({"event": "close"})
        await super().close_session(session, cleanup_deadline)


class FakeAnswerOnlyDriver:
    def __init__(self):
        self.number = 0

    async def capabilities(self, profile, model):
        result = answer_manifest(verified=True)
        result.update(
            backend="codex",
            model=model,
            profile_id=profile["profile_id"],
            profile_revision=profile["profile_revision"],
            transport=profile["transport"],
        )
        return result

    async def start(self, context, input):
        invocation = {"number": self.number, "prompt": input["prompt"]}
        self.number += 1
        record({"event": "answer_start", **invocation})
        return invocation

    async def events(self, invocation):
        data = json.loads(invocation["prompt"].split("\n\nDATA (JSON):\n", 1)[1])
        if any(
            message["content"] == "wait"
            for message in data["messages"]
            if isinstance(message.get("content"), str)
        ):
            record({"event": "answer_waiting"})
            await asyncio.Event().wait()
        has_tools = bool(data["tools"])
        if (
            has_tools
            and data["messages"][-1]["role"] != "tool"
            and data["tool_choice"] != "none"
        ):
            output = json.dumps(
                {
                    "kind": "tool_request",
                    "name": "echo",
                    "arguments": {"text": "routed"},
                }
            )
        elif has_tools:
            output = json.dumps({"kind": "final", "text": "continued"})
        else:
            output = "answer-only answer"
        yield {
            "type": "text_delta",
            "invocation_id": f"answer-{invocation['number']}",
            "sequence": 0,
            "payload": {"text": output},
        }
        yield {
            "type": "native_turn_ended",
            "invocation_id": f"answer-{invocation['number']}",
            "sequence": 1,
            "payload": {
                "kind": "native_terminal",
                "finish_reason": "stop",
                "usage_completeness": "unknown",
            },
        }

    async def cancel(self, invocation, deadline):
        record({"event": "answer_cancel", "number": invocation["number"]})

    async def close(self, invocation, deadline):
        record({"event": "answer_close", "number": invocation["number"]})


def factory(config):
    execution_mode = config["v2"]["routes"]["test"].get("execution_mode", "native")
    driver = FakeAnswerOnlyDriver() if execution_mode == "answer_only" else FakeDriver()
    profile = {
        **PROFILE,
        "transport": "stdio",
        **({"execution_mode": execution_mode} if execution_mode != "native" else {}),
    }
    source = None
    if config["v2"].get("auth_mode") == "virtual_keys":

        async def source():
            revoked = (
                (directory / "revoked").read_text().splitlines()
                if (directory / "revoked").exists()
                else []
            )
            return {
                hashlib.sha256(key.encode()).hexdigest(): {
                    "scope_id": name,
                    "revision": "1",
                    "grants": ["test"],
                    "max_sessions": 4,
                }
                for key, name in KEYS.items()
                if name not in revoked
            }

    return ProxyV2Host(
        config,
        _bindings={"test": Binding(driver, profile, "fake")},
        _fixture=True,
        _registry_source=source,
    )


def fake_litellm_auth(app):
    """Test-only replacement for the DB-backed authentication dependency."""
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

    async def verified_fixture(request: Request):
        raw = request.headers.get("authorization", "").removeprefix("Bearer ")
        if raw == config["keys"]["router"]:
            digest = LITELLM_PROXY_MASTER_KEY_ALIAS
        elif raw in KEYS:
            digest = hashlib.sha256(raw.encode()).hexdigest()
        else:
            raise HTTPException(status_code=401, detail="unauthorized")
        result = UserAPIKeyAuth(api_key=digest)
        result.via_virtual_key = True
        return result

    app.dependency_overrides[user_api_key_auth] = verified_fixture


server.run(
    config,
    _v2_factory=factory,
    _proxy_fixture=fake_litellm_auth
    if config["v2"].get("auth_mode") == "virtual_keys"
    else None,
)
