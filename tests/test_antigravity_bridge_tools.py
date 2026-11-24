import asyncio
import json
import os
import runpy
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

REPO_ROOT = Path(__file__).parents[1]
bridge = runpy.run_path(str(REPO_ROOT / "deploy/antigravity-rpc/bridge.py"))
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

SUMMARY_TOOL = {
    "type": "function",
    "function": {
        "name": "audit_action",
        "description": "Tool that explicitly declares toolSummary.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "toolSummary": {"type": "string"},
            },
            "required": ["action", "toolSummary"],
        },
    },
}

CONFIG_OPTIONS = [
    {
        "id": "model",
        "category": "model",
        "type": "select",
        "currentValue": "gemini-4-neon",
        "options": [
            {"value": "gemini-4-neon", "name": "Gemini 4 Neon"},
            {"value": "gemini-4-carbon", "name": "Gemini 4 Carbon"},
            {"value": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
        ],
    },
    {
        "id": "thought_level",
        "category": "thought_level",
        "type": "select",
        "currentValue": "high",
        "options": [
            {"value": "low", "name": "Low"},
            {"value": "medium", "name": "Medium"},
            {"value": "high", "name": "High"},
        ],
    },
]


class FakeACPClient:
    """Drives real mcp_stdio_bridge.py subprocess when mcpServers is configured."""

    def __init__(self, *, allowed_mcp_names, on_update, on_permission_approved, script_fn, log):
        self.allowed_mcp_names = allowed_mcp_names
        self.on_update = on_update
        self.on_permission_approved = on_permission_approved
        self.script_fn = script_fn
        self.log = log
        self.failure = asyncio.get_running_loop().create_future()
        self.options = json.loads(json.dumps(CONFIG_OPTIONS))
        self.mcp_servers = []
        self.closed = False

    async def start(self, command, cwd, env):
        self.log.append({"command": command, "cwd": str(cwd)})

    async def close(self):
        self.closed = True

    async def notify(self, method, params):
        self.log.append({"notify": method, "params": params})

    def simulate_permission(self, tool_name: str) -> bool:
        if tool_name in self.allowed_mcp_names:
            self.on_permission_approved(tool_name)
            return True
        err = RuntimeError(f"Rejected unauthorized tool permission: {tool_name!r}")
        if not self.failure.done():
            self.failure.set_result(err)
        return False

    async def request(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {
                "protocolVersion": 1,
                "agentCapabilities": {},
                "agentInfo": {"name": "Antigravity ACP Server", "version": "1.5.9"},
            }
        if method == "session/new":
            self.mcp_servers = params.get("mcpServers") or []
            return {"sessionId": "sid-1", "configOptions": self.options}
        if method == "session/set_config_option":
            cid = params["configId"]
            val = params["value"]
            for opt in self.options:
                if opt["id"] == cid:
                    opt["currentValue"] = val
            return {"configOptions": self.options}
        if method == "session/close":
            return {}
        if method == "session/prompt":
            self.log.append({"prompt": params["prompt"][0]["text"]})
            return await self.script_fn(self, params)
        raise AssertionError(f"Unexpected ACP method: {method}")


def call_stdio_mcp_server(mcp_spec: dict, calls: list[tuple[int, str, dict]]):
    """Spawns real mcp_stdio_bridge.py and executes concurrent tools/call requests."""
    env = dict(os.environ)
    for item in mcp_spec.get("env", []):
        env[item["name"]] = item["value"]
    proc = subprocess.Popen(
        [mcp_spec["command"], *mcp_spec["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            + "\n"
        )
        proc.stdin.flush()
        init_line = json.loads(proc.stdout.readline())
        assert init_line["id"] == 1

        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            + "\n"
        )
        proc.stdin.flush()
        list_line = json.loads(proc.stdout.readline())
        listed_tools = list_line["result"]["tools"]

        for req_id, name, args in calls:
            proc.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": args},
                    }
                )
                + "\n"
            )
        proc.stdin.flush()

        replies = {}
        for _ in calls:
            line = proc.stdout.readline()
            assert line, "MCP stdio bridge closed before returning all results"
            parsed = json.loads(line)
            replies[parsed["id"]] = parsed["result"]
        return listed_tools, replies
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=5)


def start_fake_agy_bridge(monkeypatch, scripts):
    script_iter = iter(scripts)
    log = []

    BRIDGE_GLOBALS["resolve_agy_executable"] = lambda override=None: "agy-fixture"

    def fake_create_acp_client(*, allowed_mcp_names, on_update, on_permission_approved):
        return FakeACPClient(
            allowed_mcp_names=allowed_mcp_names,
            on_update=on_update,
            on_permission_approved=on_permission_approved,
            script_fn=next(script_iter),
            log=log,
        )

    BRIDGE_GLOBALS["_create_acp_client"] = fake_create_acp_client

    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer secret-agy-bridge-key"
    server.session_ttl = 30.0
    server.concurrency = 2
    server.is_ready = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, log


def stop_bridge(server, thread):
    try:
        BRIDGE_GLOBALS["close_server_sessions"](server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def post_chat(port, body, *, auth="Bearer secret-agy-bridge-key"):
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


def test_parse_agy_usage_handles_additive_cached_tokens():
    parse_agy_usage = bridge["parse_agy_usage"]
    # Exact live pattern from agy 1.5.9 where cachedReadTokens > inputTokens
    usage = parse_agy_usage(
        {
            "inputTokens": 500,
            "outputTokens": 126,
            "cachedReadTokens": 7357,
            "cachedWriteTokens": 0,
            "thoughtTokens": 84,
            "totalTokens": 7983,
        }
    )
    assert usage == {
        "prompt_tokens": 7857,
        "completion_tokens": 126,
        "total_tokens": 7983,
        "prompt_tokens_details": {"cached_tokens": 7357, "cache_write_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 84},
    }


def test_streaming_mcp_stdio_bridge_strips_undeclared_tool_summary_and_resumes(monkeypatch):
    async def agy_script(client: FakeACPClient, _params: dict):
        client.on_update(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sid-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Looking up..."},
                    },
                },
            }
        )
        assert client.simulate_permission("mcp_client_tools_get_weather")
        assert client.simulate_permission("mcp_client_tools_audit_action")

        _, replies = await asyncio.to_thread(
            call_stdio_mcp_server,
            client.mcp_servers[0],
            [
                (
                    10,
                    "get_weather",
                    {"city": "Tokyo", "toolSummary": "Auto-injected by agy"},
                ),
                (
                    11,
                    "audit_action",
                    {"action": "check", "toolSummary": "Explicitly declared"},
                ),
            ],
        )
        w_text = replies[10]["content"][0]["text"]
        a_text = replies[11]["content"][0]["text"]
        client.on_update(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sid-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"Done: {w_text} / {a_text}"},
                    },
                },
            }
        )
        return {
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 100,
                "outputTokens": 20,
                "cachedReadTokens": 50,
                "thoughtTokens": 5,
                "totalTokens": 170,
            },
        }

    server, thread, log = start_fake_agy_bridge(monkeypatch, [agy_script])
    try:
        port = server.server_address[1]
        status1, ctype1, raw1 = post_chat(
            port,
            {
                "model": "antigravity",
                "stream": True,
                "stream_options": {"include_usage": True},
                "tools": [WEATHER_TOOL, SUMMARY_TOOL],
                "messages": [{"role": "user", "content": "Run both"}],
            },
        )
        assert status1 == 200
        assert "text/event-stream" in ctype1
        chunks1, done1 = parse_sse(raw1)
        assert done1 is True
        tcs1, text1, reasons1 = reassemble_streamed_tool_calls(chunks1)
        assert text1 == "Looking up..."
        assert reasons1 == ["tool_calls"]
        assert len(tcs1) == 2

        by_name = {tc["function"]["name"]: tc for tc in tcs1}
        # Undeclared toolSummary stripped from get_weather, preserved on audit_action!
        assert json.loads(by_name["get_weather"]["function"]["arguments"]) == {
            "city": "Tokyo"
        }
        assert json.loads(by_name["audit_action"]["function"]["arguments"]) == {
            "action": "check",
            "toolSummary": "Explicitly declared",
        }

        status2, _, raw2 = post_chat(
            port,
            {
                "model": "antigravity",
                "stream": True,
                "stream_options": {"include_usage": True},
                "tools": [WEATHER_TOOL, SUMMARY_TOOL],
                "messages": [
                    {"role": "user", "content": "Run both"},
                    {"role": "assistant", "content": text1, "tool_calls": tcs1},
                    {
                        "role": "tool",
                        "tool_call_id": by_name["get_weather"]["id"],
                        "content": "18C Clear",
                    },
                    {
                        "role": "tool",
                        "tool_call_id": by_name["audit_action"]["id"],
                        "content": "AUDITED",
                    },
                ],
            },
        )
        assert status2 == 200
        chunks2, done2 = parse_sse(raw2)
        assert done2 is True
        _, text2, reasons2 = reassemble_streamed_tool_calls(chunks2)
        assert text2 == "Done: 18C Clear / AUDITED"
        assert reasons2 == ["stop"]
        assert chunks2[-1]["usage"]["prompt_tokens"] == 150
        assert chunks2[-1]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 5
        assert len([entry for entry in log if "prompt" in entry]) == 1
    finally:
        stop_bridge(server, thread)


def test_unauthorized_tool_rejected_and_expired_stateless_fallback(monkeypatch):
    async def rogue_script(client: FakeACPClient, _params: dict):
        client.simulate_permission("run_command")
        await asyncio.sleep(0.2)
        return {"stopReason": "end_turn"}

    async def round_one(client: FakeACPClient, _params: dict):
        assert client.simulate_permission("mcp_client_tools_get_weather")
        await asyncio.to_thread(
            call_stdio_mcp_server,
            client.mcp_servers[0],
            [(1, "get_weather", {"city": "Seoul"})],
        )
        return {"stopReason": "end_turn"}

    async def stateless_round(client: FakeACPClient, params: dict):
        assert "Continue the conversation below" in params["prompt"][0]["text"]
        # Complete with empty post-tool text!
        return {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12}}

    server, thread, _ = start_fake_agy_bridge(
        monkeypatch, [rogue_script, round_one, stateless_round]
    )
    server.session_ttl = 0.05
    try:
        port = server.server_address[1]
        with pytest.raises(HTTPError) as exc_info:
            post_chat(
                port,
                {
                    "model": "antigravity",
                    "tools": [WEATHER_TOOL],
                    "messages": [{"role": "user", "content": "rogue"}],
                },
            )
        assert exc_info.value.code == 503

        _, _, raw1 = post_chat(
            port,
            {
                "model": "antigravity",
                "tools": [WEATHER_TOOL],
                "messages": [{"role": "user", "content": "Seoul weather"}],
            },
        )
        tc = json.loads(raw1)["choices"][0]["message"]["tool_calls"][0]
        time.sleep(0.12)

        _, _, raw2 = post_chat(
            port,
            {
                "model": "antigravity",
                "tools": [WEATHER_TOOL],
                "messages": [
                    {"role": "user", "content": "Seoul weather"},
                    {"role": "assistant", "content": None, "tool_calls": [tc]},
                    {"role": "tool", "tool_call_id": tc["id"], "content": "12C"},
                ],
            },
        )
        body2 = json.loads(raw2)
        assert body2["choices"][0]["finish_reason"] == "stop"
        assert body2["choices"][0]["message"]["content"] == ""
    finally:
        stop_bridge(server, thread)
