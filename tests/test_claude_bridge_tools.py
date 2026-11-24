import asyncio
import json
import runpy
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent

bridge = runpy.run_path(
    str(Path(__file__).parents[1] / "deploy/claude-rpc/bridge.py")
)
BRIDGE_GLOBALS = bridge["Handler"].do_POST.__globals__

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up current weather by city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}

TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "parameters": {
            "type": "object",
            "properties": {"tz": {"type": "string"}},
        },
    },
}


class FakeWire:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeClaudeClient:
    """Drives real SdkMcpTool async handlers extracted from ClaudeAgentOptions."""

    def __init__(self, options, script_fn, calls_log):
        self.options = options
        self.script_fn = script_fn
        self.calls_log = calls_log
        self.handlers = {}
        if isinstance(options.mcp_servers, dict) and "client_tools" in options.mcp_servers:
            server_cfg = options.mcp_servers["client_tools"]
            # Extract handlers attached via SdkMcpTool closures or inspect mcp_servers
            self.server_cfg = server_cfg
        self._queue = asyncio.Queue()
        self._task = None
        self.disconnected = False

    async def connect(self):
        return None

    async def disconnect(self):
        self.disconnected = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass

    async def query(self, prompt):
        self.calls_log.append(
            {
                "prompt": prompt,
                "system_prompt": self.options.system_prompt,
                "model": self.options.model,
                "handlers": dict(self.handlers),
            }
        )
        self._task = asyncio.create_task(self._run(prompt))

    async def _run(self, prompt):
        try:
            await self.script_fn(prompt, self.options, self.handlers, self._queue)
        finally:
            await self._queue.put(None)

    async def receive_response(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


def start_fake_claude_bridge(monkeypatch, scripts):
    script_iter = iter(scripts)
    calls_log = []
    captured_tools = {}

    orig_create_mcp = None
    import claude_agent_sdk

    orig_create_mcp = claude_agent_sdk.create_sdk_mcp_server

    def spy_create_mcp(name, version="1.0.0", tools=None):
        captured_tools[name] = {t.name: t.handler for t in (tools or [])}
        return orig_create_mcp(name, version=version, tools=tools)

    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server", spy_create_mcp)

    def fake_create_sdk_client(options, _config):
        script_fn = next(script_iter)
        wire = FakeWire()
        client = FakeClaudeClient(options, script_fn, calls_log)
        if isinstance(options.mcp_servers, dict) and "client_tools" in options.mcp_servers:
            client.handlers = dict(captured_tools.get("client_tools", {}))
        return client, wire

    BRIDGE_GLOBALS["_create_sdk_client"] = fake_create_sdk_client

    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer secret-claude-bridge-key"
    server.session_ttl = 30.0
    server.concurrency = 2
    server.is_ready = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, calls_log


def stop_bridge(server, thread):
    try:
        BRIDGE_GLOBALS["close_server_sessions"](server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def post_chat(port, body, *, auth="Bearer secret-claude-bridge-key"):
    req = Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, resp.headers.get("Content-Type", ""), raw


def parse_sse(raw):
    chunks = []
    done = False
    for block in raw.split("\n\n"):
        line = block.strip()
        if not line:
            continue
        assert line.startswith("data: "), line
        payload = line[6:]
        if payload == "[DONE]":
            done = True
        else:
            chunks.append(json.loads(payload))
    return chunks, done


def reassemble_streamed_tool_calls(chunks):
    by_index = {}
    finish_reasons = []
    streamed_text = []
    for chunk in chunks:
        for choice in chunk.get("choices", []):
            if choice.get("finish_reason"):
                finish_reasons.append(choice["finish_reason"])
            delta = choice.get("delta", {})
            if delta.get("content"):
                streamed_text.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc["index"]
                entry = by_index.setdefault(
                    idx,
                    {
                        "id": None,
                        "type": None,
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tc.get("id"):
                    entry["id"] = tc["id"]
                if tc.get("type"):
                    entry["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    entry["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    entry["function"]["arguments"] += fn["arguments"]
    return (
        [by_index[k] for k in sorted(by_index)],
        "".join(streamed_text),
        finish_reasons,
    )


def test_normalize_tools_and_model_aliases():
    normalize_tools = bridge["normalize_tools"]
    resolve_model_alias = bridge["resolve_model_alias"]

    assert resolve_model_alias("claude-code") == ("claude-code", None)
    assert resolve_model_alias("openai/claude-sonnet-4-6") == (
        "claude-sonnet-4-6",
        "claude-sonnet-4-6",
    )
    with pytest.raises(ValueError):
        resolve_model_alias("unknown-model")

    tools, instr, req = normalize_tools([WEATHER_TOOL, TIME_TOOL], "required")
    assert len(tools) == 2
    assert req is True and "required" in instr

    tools_none, _, _ = normalize_tools([WEATHER_TOOL], "none")
    assert tools_none == []

    named, instr_named, req_named = normalize_tools(
        [WEATHER_TOOL, TIME_TOOL],
        {"type": "function", "function": {"name": "get_time"}},
    )
    assert [t["name"] for t in named] == ["get_time"]
    assert req_named == "get_time"
    assert "get_time" in instr_named


def test_streaming_tool_call_and_resume_roundtrip(monkeypatch):
    long_city = "Ho Chi Minh City - " + ("District 1 " * 30)

    async def session_one(_prompt, _options, handlers, queue):
        await queue.put(
            SystemMessage(
                subtype="init",
                data={"tools": ["mcp__client_tools__get_weather"]},
            )
        )
        await queue.put(
            StreamEvent(
                uuid="u1",
                session_id="s1",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Checking..."},
                },
            )
        )
        await queue.put(
            AssistantMessage(
                content=[
                    TextBlock("Checking..."),
                    ToolUseBlock(
                        id="tu_1",
                        name="mcp__client_tools__get_weather",
                        input={"city": long_city},
                    ),
                ],
                model="claude-opus-5-5",
                message_id="msg_step_1",
                usage={"input_tokens": 20, "output_tokens": 10},
            )
        )
        tool_res = await handlers["get_weather"]({"city": long_city})
        result_text = tool_res["content"][0]["text"]
        await queue.put(
            StreamEvent(
                uuid="u2",
                session_id="s1",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": f"Result: {result_text}"},
                },
            )
        )
        await queue.put(
            AssistantMessage(
                content=[TextBlock(f"Result: {result_text}")],
                model="claude-opus-5-5",
                message_id=None,
                usage={
                    "input_tokens": 30,
                    "output_tokens": 8,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": None,
                },
            )
        )
        await queue.put(
            ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=8,
                is_error=False,
                num_turns=2,
                session_id="s1",
            )
        )

    server, thread, calls_log = start_fake_claude_bridge(monkeypatch, [session_one])
    try:
        port = server.server_address[1]
        # Check healthz and /v1/models
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as hresp:
            assert hresp.status == 200
        mreq = Request(
            f"http://127.0.0.1:{port}/v1/models",
            headers={"Authorization": "Bearer secret-claude-bridge-key"},
        )
        with urlopen(mreq, timeout=5) as mresp:
            models_data = json.loads(mresp.read().decode())
            assert any(m["id"] == "claude-code" for m in models_data["data"])

        status, ctype, raw1 = post_chat(
            port,
            {
                "model": "claude-code",
                "stream": True,
                "stream_options": {"include_usage": True},
                "temperature": 0.1,
                "tools": [WEATHER_TOOL],
                "messages": [{"role": "user", "content": "Weather please"}],
            },
        )
        assert status == 200
        assert "text/event-stream" in ctype
        chunks1, done1 = parse_sse(raw1)
        assert done1 is True
        tcs1, text1, reasons1 = reassemble_streamed_tool_calls(chunks1)
        assert text1 == "Checking..."
        assert reasons1 == ["tool_calls"]
        assert len(tcs1) == 1
        assert tcs1[0]["id"].startswith("call_")
        assert tcs1[0]["function"]["name"] == "get_weather"
        assert json.loads(tcs1[0]["function"]["arguments"]) == {"city": long_city}
        assert chunks1[-1]["usage"]["total_tokens"] == 30

        # Round 2: send role="tool" result without custom headers
        status2, _, raw2 = post_chat(
            port,
            {
                "model": "claude-code",
                "stream": True,
                "stream_options": {"include_usage": True},
                "tools": [WEATHER_TOOL],
                "messages": [
                    {"role": "user", "content": "Weather please"},
                    {"role": "assistant", "content": text1, "tool_calls": tcs1},
                    {
                        "role": "tool",
                        "tool_call_id": tcs1[0]["id"],
                        "content": "31C Sunny",
                    },
                ],
            },
        )
        assert status2 == 200
        chunks2, done2 = parse_sse(raw2)
        assert done2 is True
        tcs2, text2, reasons2 = reassemble_streamed_tool_calls(chunks2)
        assert tcs2 == []
        assert text2 == "Result: 31C Sunny"
        assert reasons2 == ["stop"]
        assert chunks2[-1]["usage"]["prompt_tokens"] == 40
        assert chunks2[-1]["usage"]["prompt_tokens_details"]["cached_tokens"] == 10
        assert len(calls_log) == 1
    finally:
        stop_bridge(server, thread)


def test_parallel_and_serial_tool_calls_plus_empty_post_tool_turn(monkeypatch):
    async def parallel_script(_prompt, _options, handlers, queue):
        await queue.put(
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu_a",
                        name="mcp__client_tools__get_weather",
                        input={"city": "Hanoi"},
                    ),
                    ToolUseBlock(
                        id="tu_b",
                        name="mcp__client_tools__get_time",
                        input={"tz": "Asia/Bangkok"},
                    ),
                ],
                model="claude-sonnet-4-6",
                message_id="m1",
                usage={"input_tokens": 10, "output_tokens": 6},
            )
        )
        res_a, res_b = await asyncio.gather(
            handlers["get_weather"]({"city": "Hanoi"}),
            handlers["get_time"]({"tz": "Asia/Bangkok"}),
        )
        assert res_a["content"][0]["text"] == "25C"
        assert res_b["content"][0]["text"] == "14:00"
        # Complete with empty assistant text after tool execution!
        await queue.put(
            AssistantMessage(
                content=[TextBlock("")],
                model="claude-sonnet-4-6",
                message_id="m2",
                usage={"input_tokens": 15, "output_tokens": 1},
            )
        )
        await queue.put(
            ResultMessage(
                subtype="success",
                duration_ms=5,
                duration_api_ms=4,
                is_error=False,
                num_turns=2,
                session_id="s2",
            )
        )

    server, thread, _ = start_fake_claude_bridge(
        monkeypatch, [parallel_script, parallel_script]
    )
    try:
        port = server.server_address[1]
        # 1) parallel_tool_calls=True -> both calls emitted in a single turn
        _, _, raw1 = post_chat(
            port,
            {
                "model": "claude-sonnet-4-6",
                "parallel_tool_calls": True,
                "tools": [WEATHER_TOOL, TIME_TOOL],
                "messages": [{"role": "user", "content": "Hanoi weather and time"}],
            },
        )
        body1 = json.loads(raw1)
        calls1 = body1["choices"][0]["message"]["tool_calls"]
        assert len(calls1) == 2

        _, _, raw2 = post_chat(
            port,
            {
                "model": "claude-sonnet-4-6",
                "tools": [WEATHER_TOOL, TIME_TOOL],
                "messages": [
                    {"role": "user", "content": "Hanoi weather and time"},
                    {"role": "assistant", "content": None, "tool_calls": calls1},
                    {"role": "tool", "tool_call_id": calls1[0]["id"], "content": "25C"},
                    {"role": "tool", "tool_call_id": calls1[1]["id"], "content": "14:00"},
                ],
            },
        )
        body2 = json.loads(raw2)
        assert body2["choices"][0]["finish_reason"] == "stop"
        assert body2["choices"][0]["message"]["content"] == ""

        # 2) parallel_tool_calls=False -> serialized one tool call per HTTP response!
        _, _, s_raw1 = post_chat(
            port,
            {
                "model": "claude-sonnet-4-6",
                "parallel_tool_calls": False,
                "tools": [WEATHER_TOOL, TIME_TOOL],
                "messages": [{"role": "user", "content": "Hanoi weather and time"}],
            },
        )
        s_calls1 = json.loads(s_raw1)["choices"][0]["message"]["tool_calls"]
        assert len(s_calls1) == 1
        assert s_calls1[0]["function"]["name"] == "get_weather"

        _, _, s_raw2 = post_chat(
            port,
            {
                "model": "claude-sonnet-4-6",
                "parallel_tool_calls": False,
                "tools": [WEATHER_TOOL, TIME_TOOL],
                "messages": [
                    {"role": "user", "content": "Hanoi weather and time"},
                    {"role": "assistant", "content": None, "tool_calls": s_calls1},
                    {"role": "tool", "tool_call_id": s_calls1[0]["id"], "content": "25C"},
                ],
            },
        )
        s_calls2 = json.loads(s_raw2)["choices"][0]["message"]["tool_calls"]
        assert len(s_calls2) == 1
        assert s_calls2[0]["function"]["name"] == "get_time"

        _, _, s_raw3 = post_chat(
            port,
            {
                "model": "claude-sonnet-4-6",
                "parallel_tool_calls": False,
                "tools": [WEATHER_TOOL, TIME_TOOL],
                "messages": [
                    {"role": "user", "content": "Hanoi weather and time"},
                    {"role": "assistant", "content": None, "tool_calls": s_calls1},
                    {"role": "tool", "tool_call_id": s_calls1[0]["id"], "content": "25C"},
                    {"role": "assistant", "content": None, "tool_calls": s_calls2},
                    {"role": "tool", "tool_call_id": s_calls2[0]["id"], "content": "14:00"},
                ],
            },
        )
        assert json.loads(s_raw3)["choices"][0]["finish_reason"] == "stop"
    finally:
        stop_bridge(server, thread)


def test_expired_session_falls_back_to_stateless_transcript(monkeypatch):
    async def round_one(_prompt, _options, handlers, queue):
        await queue.put(
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu_1",
                        name="mcp__client_tools__get_weather",
                        input={"city": "Hue"},
                    )
                ],
                model="claude-opus-5-5",
                message_id="m1",
                usage={"input_tokens": 5, "output_tokens": 5},
            )
        )
        await handlers["get_weather"]({"city": "Hue"})

    async def stateless_round(prompt, _options, _handlers, queue):
        assert "Continue the conversation below" in prompt
        assert "Rainy 24C" in prompt
        await queue.put(
            AssistantMessage(
                content=[TextBlock("Hue is Rainy 24C")],
                model="claude-opus-5-5",
                message_id="m2",
                usage={"input_tokens": 12, "output_tokens": 4},
            )
        )
        await queue.put(
            ResultMessage(
                subtype="success",
                duration_ms=5,
                duration_api_ms=4,
                is_error=False,
                num_turns=1,
                session_id="s3",
            )
        )

    server, thread, calls_log = start_fake_claude_bridge(
        monkeypatch, [round_one, stateless_round]
    )
    server.session_ttl = 0.05
    try:
        port = server.server_address[1]
        _, _, raw1 = post_chat(
            port,
            {
                "model": "claude-code",
                "tools": [WEATHER_TOOL],
                "messages": [{"role": "user", "content": "Hue weather"}],
            },
        )
        tc = json.loads(raw1)["choices"][0]["message"]["tool_calls"][0]
        time.sleep(0.12)
        _, _, raw2 = post_chat(
            port,
            {
                "model": "claude-code",
                "tools": [WEATHER_TOOL],
                "messages": [
                    {"role": "user", "content": "Hue weather"},
                    {"role": "assistant", "content": None, "tool_calls": [tc]},
                    {"role": "tool", "tool_call_id": tc["id"], "content": "Rainy 24C"},
                ],
            },
        )
        assert json.loads(raw2)["choices"][0]["message"]["content"] == "Hue is Rainy 24C"
        assert len(calls_log) == 2
    finally:
        stop_bridge(server, thread)


def test_security_auth_conflict_and_unexpected_builtin_tool_rejected(monkeypatch):
    async def rogue_builtin(_prompt, _options, _handlers, queue):
        await queue.put(
            SystemMessage(
                subtype="init",
                data={"tools": ["Bash", "mcp__client_tools__get_weather"]},
            )
        )

    async def pause_round(_prompt, _options, handlers, queue):
        await queue.put(
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu_1",
                        name="mcp__client_tools__get_weather",
                        input={"city": "Da Nang"},
                    )
                ],
                model="claude-opus-5-5",
                message_id="m1",
                usage={"input_tokens": 5, "output_tokens": 5},
            )
        )
        await handlers["get_weather"]({"city": "Da Nang"})

    server, thread, _ = start_fake_claude_bridge(
        monkeypatch, [rogue_builtin, pause_round]
    )
    try:
        port = server.server_address[1]
        with pytest.raises(HTTPError) as unauth:
            post_chat(
                port,
                {"model": "claude-code", "messages": [{"role": "user", "content": "hi"}]},
                auth="Bearer wrong",
            )
        assert unauth.value.code == 401

        with pytest.raises(HTTPError) as rogue:
            post_chat(
                port,
                {
                    "model": "claude-code",
                    "tools": [WEATHER_TOOL],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert rogue.value.code == 503

        # Now start a valid paused session and test model mismatch conflict -> 400
        _, _, raw1 = post_chat(
            port,
            {
                "model": "claude-code",
                "tools": [WEATHER_TOOL],
                "messages": [{"role": "user", "content": "Da Nang weather"}],
            },
        )
        tc = json.loads(raw1)["choices"][0]["message"]["tool_calls"][0]
        with pytest.raises(HTTPError) as conflict:
            post_chat(
                port,
                {
                    "model": "claude-sonnet-4-6",
                    "tools": [WEATHER_TOOL],
                    "messages": [
                        {"role": "user", "content": "Da Nang weather"},
                        {"role": "assistant", "content": None, "tool_calls": [tc]},
                        {"role": "tool", "tool_call_id": tc["id"], "content": "30C"},
                    ],
                },
            )
        assert conflict.value.code == 400
    finally:
        stop_bridge(server, thread)


def test_bridge_propagates_max_tokens_finish_reason_json_and_sse(monkeypatch):
    async def truncated_turn(_prompt, _options, _handlers, queue):
        await queue.put(
            StreamEvent(
                "u1",
                "s1",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "cut text"},
                },
            )
        )
        await queue.put(
            AssistantMessage(
                content=[TextBlock("cut text")],
                model="claude-sonnet-4-6",
                message_id="m-cut",
                stop_reason="max_tokens",
                usage={"input_tokens": 5, "output_tokens": 10},
            )
        )
        await queue.put(
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s1",
                stop_reason="max_tokens",
                usage={"input_tokens": 5, "output_tokens": 10},
            )
        )

    server, thread, _ = start_fake_claude_bridge(
        monkeypatch, [truncated_turn, truncated_turn]
    )
    try:
        port = server.server_address[1]
        status, _, raw_json = post_chat(
            port,
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "long essay"}],
            },
        )
        assert status == 200
        parsed = json.loads(raw_json)
        assert parsed["choices"][0]["finish_reason"] == "length"
        assert parsed["choices"][0]["message"]["content"] == "cut text"

        status_sse, _, raw_sse = post_chat(
            port,
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "long essay"}],
                "stream": True,
            },
        )
        assert status_sse == 200
        chunks, done = parse_sse(raw_sse)
        assert done
        _, streamed_text, finish_reasons = reassemble_streamed_tool_calls(chunks)
        assert streamed_text == "cut text"
        assert finish_reasons == ["length"]
    finally:
        stop_bridge(server, thread)


