import asyncio
import json
import runpy
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

bridge = runpy.run_path(str(Path(__file__).parents[1] / "deploy/codex-minipod/bridge.py"))
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


class ScriptedProcess:
    def __init__(self):
        self.stdin = SimpleNamespace(close=self._close_stdin)
        self.stdout = None
        self.returncode = None
        self.killed = False
        self.closed_stdin = False

    def _close_stdin(self):
        self.closed_stdin = True
        if self.returncode is None:
            self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class ScriptedAppServer:
    """Simulates codex app-server across multi-round dynamic tool calls."""

    def __init__(self, process, steps, calls, *, selected_model="gpt-6-luna"):
        self.process = process
        self.steps = deque(steps)
        self.calls = calls
        self.selected_model = selected_model
        self.current_events = deque()
        self._fs_probe_id = None

    async def send(self, message):
        self.calls.append(("send", message))
        if message.get("method") == "fs/readFile":
            self._fs_probe_id = message["id"]
            return
        if "id" in message and "result" in message:
            # Client responded to a server-initiated item/tool/call request.
            if self.steps and not self.current_events:
                next_events = self.steps.popleft()
                if callable(next_events):
                    next_events = next_events(message)
                self.current_events.extend(next_events)

    async def call(self, method, params, pending=None):
        self.calls.append((method, params))
        if method == "environment/info":
            return {"cwd": "file:///workspace"}
        if method == "thread/start":
            return {
                "approvalPolicy": "never",
                "model": self.selected_model,
                "thread": {"id": "thread-1"},
            }
        if method == "turn/start":
            if self.steps:
                next_events = self.steps.popleft()
                if callable(next_events):
                    next_events = next_events(params)
                self.current_events.extend(next_events)
            return {"turn": {"id": "turn-1"}}
        return {}

    async def read(self):
        if self._fs_probe_id is not None:
            probe_id = self._fs_probe_id
            self._fs_probe_id = None
            return {"id": probe_id, "error": {"message": "local filesystem is not configured"}}
        if not self.current_events:
            raise AssertionError("ScriptedAppServer ran out of events")
        return self.current_events.popleft()


def start_scripted_bridge(monkeypatch, scripts, *, selected_model="gpt-6-luna"):
    spawned = []
    script_queue = deque(scripts)

    async def fake_subprocess(*args, **kwargs):
        proc = ScriptedProcess()
        spawned.append((proc, args, kwargs))
        return proc

    def make_app_server(process, *args, **kwargs):
        if not script_queue:
            raise AssertionError("Unexpected extra Codex subprocess spawn")
        steps, calls = script_queue.popleft()
        return ScriptedAppServer(process, steps, calls, selected_model=selected_model)

    monkeypatch.setattr(BRIDGE_GLOBALS["asyncio"], "create_subprocess_exec", fake_subprocess)
    monkeypatch.setitem(BRIDGE_GLOBALS, "AppServer", make_app_server)

    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer test-key"
    server.profile = Path("profile")
    server.workspace = Path("workspace")
    server.codex = "codex"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, spawned


def stop_bridge(server, thread):
    close_sessions = bridge.get("close_server_sessions")
    if close_sessions is not None:
        close_sessions(server)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def post_chat(server, payload, *, timeout=5):
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
    )
    return urlopen(request, timeout=timeout)


def read_sse(response):
    events = []
    saw_done = False
    for line in response:
        if not line.startswith(b"data: "):
            continue
        raw = line[6:].strip()
        if raw == b"[DONE]":
            saw_done = True
        else:
            events.append(json.loads(raw))
    return events, saw_done


def usage_notification(input_tokens, output_tokens, total_tokens):
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "total": {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "totalTokens": total_tokens,
                    "cachedInputTokens": 0,
                    "reasoningOutputTokens": 0,
                },
                "last": {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "totalTokens": total_tokens,
                    "cachedInputTokens": 0,
                    "reasoningOutputTokens": 0,
                },
            },
        },
    }


def test_streaming_tool_calls_and_live_tool_result_continuation(monkeypatch):
    calls = []
    round_one = [
        usage_notification(50, 12, 62),
        {
            "id": 7,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "codex-call-1",
                "tool": "get_weather",
                "arguments": {"city": "Hanoi"},
            },
        },
    ]
    round_two = [
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "msg-1", "delta": "Hanoi is "},
        },
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "msg-1", "delta": "28C and sunny."},
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "msg-1", "type": "agentMessage", "text": "Hanoi is 28C and sunny."},
            },
        },
        usage_notification(90, 22, 112),
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        },
    ]

    server, thread, spawned = start_scripted_bridge(monkeypatch, [([round_one, round_two], calls)])
    try:
        user_msg = {"role": "user", "content": "What is the weather in Hanoi?"}
        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [user_msg],
                "tools": [WEATHER_TOOL],
                "tool_choice": "auto",
            },
        ) as res1:
            events1, done1 = read_sse(res1)
            assert res1.status == 200

        assert done1
        assert len(spawned) == 1
        proc = spawned[0][0]
        assert proc.returncode is None  # Still alive awaiting tool result!

        thread_start = next(params for method, params in calls if method == "thread/start")
        assert thread_start["dynamicTools"] == [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Look up current weather by city.",
                "inputSchema": WEATHER_TOOL["function"]["parameters"],
            }
        ]

        # Reconstruct streamed tool call from SSE chunks
        first_delta = events1[0]["choices"][0]["delta"]
        assert first_delta["role"] == "assistant"
        tc_start = first_delta["tool_calls"][0]
        call_id = tc_start["id"]
        assert call_id.startswith("call_")
        assert tc_start["type"] == "function"
        assert tc_start["function"]["name"] == "get_weather"

        arg_fragments = [
            ev["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments", "")
            for ev in events1
            if ev["choices"] and ev["choices"][0]["delta"].get("tool_calls")
        ]
        assert json.loads("".join(arg_fragments)) == {"city": "Hanoi"}
        assert events1[-2]["choices"][0]["finish_reason"] == "tool_calls"
        assert events1[-1]["choices"] == []
        assert events1[-1]["usage"] == {
            "prompt_tokens": 50,
            "completion_tokens": 12,
            "total_tokens": 62,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        }

        # Turn 2: send back tool result via standard OpenAI messages (no custom header needed)
        assistant_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "".join(arg_fragments)},
                }
            ],
        }
        tool_msg = {"role": "tool", "tool_call_id": call_id, "content": "28C, sunny"}
        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [user_msg, assistant_msg, tool_msg],
                "tools": [WEATHER_TOOL],
            },
        ) as res2:
            events2, done2 = read_sse(res2)
            assert res2.status == 200

        assert done2
        assert len(spawned) == 1  # Reused the exact same Codex subprocess!
        assert proc.returncode == 0  # Closed cleanly after turn/completed!

        rpc_replies = [msg for kind, msg in calls if kind == "send" and msg.get("id") == 7]
        assert rpc_replies == [
            {
                "id": 7,
                "result": {
                    "contentItems": [{"type": "inputText", "text": "28C, sunny"}],
                    "success": True,
                },
            }
        ]

        text_deltas = [
            ev["choices"][0]["delta"].get("content", "")
            for ev in events2
            if ev["choices"] and ev["choices"][0]["delta"].get("content")
        ]
        assert "".join(text_deltas) == "Hanoi is 28C and sunny."
        assert events2[-2]["choices"][0]["finish_reason"] == "stop"
        # Round 2 usage reports only Round 2's incremental tokens (90-50=40, 22-12=10, 112-62=50)
        assert events2[-1]["usage"] == {
            "prompt_tokens": 40,
            "completion_tokens": 10,
            "total_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        }
    finally:
        stop_bridge(server, thread)


async def test_app_server_allows_only_dynamic_tool_calls_and_rejects_approvals():
    sent = []

    class DummyStdin:
        def write(self, data):
            sent.append(json.loads(data.decode()))

        async def drain(self):
            pass

    reader = asyncio.StreamReader(limit=bridge["APP_SERVER_FRAME_LIMIT"])
    approval_req = {
        "id": 1,
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "t1", "turnId": "u1", "itemId": "i1", "startedAtMs": 0},
    }
    tool_req = {
        "id": 2,
        "method": "item/tool/call",
        "params": {
            "threadId": "t1",
            "turnId": "u1",
            "callId": "c1",
            "tool": "get_weather",
            "arguments": {"city": "Hanoi"},
        },
    }
    reader.feed_data(json.dumps(approval_req).encode() + b"\n")
    reader.feed_data(json.dumps(tool_req).encode() + b"\n")
    reader.feed_eof()

    app = bridge["AppServer"](SimpleNamespace(stdin=DummyStdin(), stdout=reader), allow_tool_calls=True)
    received = await app.read()
    assert received == tool_req
    assert sent == [{"id": 1, "error": {"code": -32601, "message": "Disabled"}}]


def test_non_streaming_multi_hop_tool_chain_and_nested_request_slot_release(monkeypatch):
    main_calls = []
    nested_calls = []
    step1 = [
        {
            "id": 10,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "native-1",
                "tool": "get_weather",
                "arguments": {"city": "Hue"},
            },
        }
    ]
    step2 = [
        {
            "id": 11,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "native-2",
                "tool": "get_time",
                "arguments": {"tz": "Asia/Ho_Chi_Minh"},
            },
        }
    ]
    step3 = [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "m1", "type": "agentMessage", "text": "Hue is 26C at 13:00."},
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        },
    ]
    nested_turn = [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "m-nested", "type": "agentMessage", "text": "NESTED-OK"},
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        },
    ]

    server, thread, spawned = start_scripted_bridge(
        monkeypatch,
        [
            ([step1, step2, step3], main_calls),
            ([nested_turn], nested_calls),
        ],
    )
    try:
        messages = [{"role": "user", "content": "Weather and time in Hue?"}]
        with post_chat(
            server,
            {"model": "codex-minipod", "messages": messages, "tools": [WEATHER_TOOL, TIME_TOOL]},
        ) as r1:
            b1 = json.load(r1)
        choice1 = b1["choices"][0]
        assert choice1["finish_reason"] == "tool_calls"
        assert choice1["message"]["content"] is None
        tc1 = choice1["message"]["tool_calls"][0]
        assert tc1["function"]["name"] == "get_weather"

        # While the main session is paused awaiting client tool execution, SLOT must be free
        # so a nested helper LLM call succeeds instead of failing with HTTP 429!
        with post_chat(
            server,
            {"model": "codex-minipod", "messages": [{"role": "user", "content": "helper call"}]},
        ) as r_nested:
            b_nested = json.load(r_nested)
        assert b_nested["choices"][0]["message"]["content"] == "NESTED-OK"

        # Resume main session -> triggers second tool call (get_time)
        messages.append(choice1["message"])
        messages.append({"role": "tool", "tool_call_id": tc1["id"], "content": "26C"})
        with post_chat(
            server,
            {"model": "codex-minipod", "messages": messages, "tools": [WEATHER_TOOL, TIME_TOOL]},
        ) as r2:
            b2 = json.load(r2)
        choice2 = b2["choices"][0]
        assert choice2["finish_reason"] == "tool_calls"
        tc2 = choice2["message"]["tool_calls"][0]
        assert tc2["function"]["name"] == "get_time"

        # Submit second tool result -> finishes with text
        messages.append(choice2["message"])
        messages.append({"role": "tool", "tool_call_id": tc2["id"], "content": "13:00"})
        with post_chat(
            server,
            {"model": "codex-minipod", "messages": messages, "tools": [WEATHER_TOOL, TIME_TOOL]},
        ) as r3:
            b3 = json.load(r3)
        choice3 = b3["choices"][0]
        assert choice3["finish_reason"] == "stop"
        assert choice3["message"]["content"] == "Hue is 26C at 13:00."
        assert len(spawned) == 2  # 1 for 3-hop main chain + 1 for nested helper call
    finally:
        stop_bridge(server, thread)


def test_parallel_tool_calls_coalescing_and_sequential_mode(monkeypatch):
    parallel_calls = []
    seq_calls = []
    two_tool_events = [
        {
            "id": 21,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "c1",
                "tool": "get_weather",
                "arguments": {"city": "Hanoi"},
            },
        },
        {
            "id": 22,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "c2",
                "tool": "get_time",
                "arguments": {"tz": "UTC"},
            },
        },
    ]
    final_events = [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "m1", "type": "agentMessage", "text": "Both complete."},
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        },
    ]

    server, thread, _ = start_scripted_bridge(
        monkeypatch,
        [
            ([list(two_tool_events), list(final_events)], parallel_calls),
            ([list(two_tool_events), list(final_events)], seq_calls),
        ],
    )
    try:
        # 1) parallel_tool_calls=True (default): both tool calls returned in one batch
        u = {"role": "user", "content": "Weather and UTC time?"}
        with post_chat(
            server,
            {"model": "codex-minipod", "messages": [u], "tools": [WEATHER_TOOL, TIME_TOOL]},
        ) as r1:
            msg1 = json.load(r1)["choices"][0]["message"]
        assert len(msg1["tool_calls"]) == 2
        assert [c["function"]["name"] for c in msg1["tool_calls"]] == ["get_weather", "get_time"]

        # Sending only 1 of the 2 required tool results is rejected with 400
        with pytest.raises(HTTPError) as incomplete:
            post_chat(
                server,
                {
                    "model": "codex-minipod",
                    "messages": [
                        u,
                        msg1,
                        {"role": "tool", "tool_call_id": msg1["tool_calls"][0]["id"], "content": "28C"},
                    ],
                    "tools": [WEATHER_TOOL, TIME_TOOL],
                },
            )
        assert incomplete.value.code == 400

        # Sending both results (even in reverse order) resumes and finishes cleanly
        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "messages": [
                    u,
                    msg1,
                    {"role": "tool", "tool_call_id": msg1["tool_calls"][1]["id"], "content": "06:00"},
                    {"role": "tool", "tool_call_id": msg1["tool_calls"][0]["id"], "content": "28C"},
                ],
                "tools": [WEATHER_TOOL, TIME_TOOL],
            },
        ) as r2:
            b2 = json.load(r2)
        assert b2["choices"][0]["message"]["content"] == "Both complete."

        # 2) parallel_tool_calls=False: emits one tool call at a time
        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "messages": [u],
                "tools": [WEATHER_TOOL, TIME_TOOL],
                "parallel_tool_calls": False,
            },
        ) as s1:
            sm1 = json.load(s1)["choices"][0]["message"]
        assert len(sm1["tool_calls"]) == 1
        assert sm1["tool_calls"][0]["function"]["name"] == "get_weather"

        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "messages": [
                    u,
                    sm1,
                    {"role": "tool", "tool_call_id": sm1["tool_calls"][0]["id"], "content": "28C"},
                ],
                "tools": [WEATHER_TOOL, TIME_TOOL],
                "parallel_tool_calls": False,
            },
        ) as s2:
            sm2 = json.load(s2)["choices"][0]["message"]
        assert len(sm2["tool_calls"]) == 1
        assert sm2["tool_calls"][0]["function"]["name"] == "get_time"

        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "messages": [
                    u,
                    sm1,
                    {"role": "tool", "tool_call_id": sm1["tool_calls"][0]["id"], "content": "28C"},
                    sm2,
                    {"role": "tool", "tool_call_id": sm2["tool_calls"][0]["id"], "content": "06:00"},
                ],
                "tools": [WEATHER_TOOL, TIME_TOOL],
                "parallel_tool_calls": False,
            },
        ) as s3:
            sb3 = json.load(s3)
        assert sb3["choices"][0]["message"]["content"] == "Both complete."
    finally:
        stop_bridge(server, thread)


def test_stateless_tool_result_fallback_and_tool_choice_modes(monkeypatch):
    stateless_calls = []
    named_calls = []
    none_calls = []
    ok_events = [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "m1", "type": "agentMessage", "text": "Synthesized from external tool."},
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        },
    ]
    named_tool_events = [
        {
            "id": 31,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "c-named",
                "tool": "get_time",
                "arguments": {"tz": "UTC"},
            },
        }
    ]

    server, thread, _ = start_scripted_bridge(
        monkeypatch,
        [
            ([list(ok_events)], stateless_calls),
            ([list(named_tool_events)], named_calls),
            ([list(ok_events)], none_calls),
        ],
    )
    try:
        # 1) Stateless synthetic tool result request (call_id was never opened on this server)
        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "messages": [
                    {"role": "user", "content": "Weather in Da Nang?"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_external_probe_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city":"Da Nang"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_external_probe_1", "content": "30C, clear"},
                ],
                "tools": [WEATHER_TOOL],
            },
        ) as res:
            body = json.load(res)
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["choices"][0]["message"]["content"] == "Synthesized from external tool."
        turn_start = next(params for method, params in stateless_calls if method == "turn/start")
        assert "30C, clear" in turn_start["input"][0]["text"]

        # 2) Named tool_choice filters dynamicTools to only the requested tool
        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "messages": [{"role": "user", "content": "Check time"}],
                "tools": [WEATHER_TOOL, TIME_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "get_time"}},
            },
        ) as res_named:
            body_named = json.load(res_named)
        assert body_named["choices"][0]["finish_reason"] == "tool_calls"
        named_thread_start = next(params for method, params in named_calls if method == "thread/start")
        assert [t["name"] for t in named_thread_start["dynamicTools"]] == ["get_time"]

        # 3) tool_choice="none" suppresses dynamicTools even when tools are passed
        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "messages": [{"role": "user", "content": "Just answer"}],
                "tools": [WEATHER_TOOL],
                "tool_choice": "none",
            },
        ) as res_none:
            body_none = json.load(res_none)
        assert body_none["choices"][0]["finish_reason"] == "stop"
        none_thread_start = next(params for method, params in none_calls if method == "thread/start")
        assert "dynamicTools" not in none_thread_start

        # 4) Invalid tool_choice (required without tools, or unknown named tool, or orphan tool role) -> HTTP 400
        for bad_payload in (
            {
                "model": "codex-minipod",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "required",
            },
            {
                "model": "codex-minipod",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [WEATHER_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "missing_tool"}},
            },
            {
                "model": "codex-minipod",
                "messages": [{"role": "tool", "tool_call_id": "call_orphan", "content": "x"}],
            },
        ):
            with pytest.raises(HTTPError) as err:
                post_chat(server, bad_payload)
            assert err.value.code == 400
    finally:
        stop_bridge(server, thread)


def test_expired_tool_session_is_reaped_and_kills_subprocess(monkeypatch):
    calls = []
    step1 = [
        {
            "id": 99,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "c-exp",
                "tool": "get_weather",
                "arguments": {"city": "Saigon"},
            },
        }
    ]
    fallback_events = [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "m-fb", "type": "agentMessage", "text": "Recovered after expiry."},
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        },
    ]

    server, thread, spawned = start_scripted_bridge(
        monkeypatch,
        [
            ([step1], calls),
            ([fallback_events], []),
        ],
    )
    server.session_ttl = 0.05
    try:
        u = {"role": "user", "content": "Weather in Saigon?"}
        with post_chat(server, {"model": "codex-minipod", "messages": [u], "tools": [WEATHER_TOOL]}) as r1:
            msg1 = json.load(r1)["choices"][0]["message"]
        first_proc = spawned[0][0]
        assert first_proc.returncode is None

        time.sleep(0.08)

        # Sending the late tool result reaps the expired first subprocess and seamlessly falls back!
        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "messages": [
                    u,
                    msg1,
                    {"role": "tool", "tool_call_id": msg1["tool_calls"][0]["id"], "content": "33C"},
                ],
                "tools": [WEATHER_TOOL],
            },
        ) as r2:
            b2 = json.load(r2)
        assert first_proc.returncode is not None
        assert b2["choices"][0]["message"]["content"] == "Recovered after expiry."
    finally:
        stop_bridge(server, thread)


def test_post_tool_empty_completion_succeeds_without_kicking_manager(monkeypatch):
    calls = []
    step1 = [
        {
            "id": 77,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "c-done",
                "tool": "get_weather",
                "arguments": {"city": "Can Tho"},
            },
        }
    ]
    step2_empty = [
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        }
    ]

    kicks = []

    class ReadyManager:
        def is_ready(self):
            return True

        def wait_ready(self, timeout):
            return True

        def kick(self):
            kicks.append("kicked")

    server, thread, _ = start_scripted_bridge(monkeypatch, [([step1, step2_empty], calls)])
    server.manager = ReadyManager()
    try:
        u = {"role": "user", "content": "Run weather tool"}
        with post_chat(server, {"model": "codex-minipod", "messages": [u], "tools": [WEATHER_TOOL]}) as r1:
            msg1 = json.load(r1)["choices"][0]["message"]

        with post_chat(
            server,
            {
                "model": "codex-minipod",
                "stream": True,
                "messages": [
                    u,
                    msg1,
                    {"role": "tool", "tool_call_id": msg1["tool_calls"][0]["id"], "content": "done"},
                ],
                "tools": [WEATHER_TOOL],
            },
        ) as r2:
            events2, done2 = read_sse(r2)
            assert r2.status == 200

        assert done2
        assert events2[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
        assert events2[-1]["choices"][0]["finish_reason"] == "stop"
        assert kicks == []
    finally:
        stop_bridge(server, thread)


def test_transient_environment_info_timeout_retries_once_before_turn_start(monkeypatch):
    calls_ok = []
    ok_step = [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "m-1", "type": "agentMessage", "text": "RECOVERED"},
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        },
    ]

    class FlakyEnvAppServer(ScriptedAppServer):
        def __init__(self, process, fail_env, steps, calls):
            super().__init__(process, steps, calls)
            self.fail_env = fail_env

        async def call(self, method, params, pending=None):
            if method == "environment/info" and self.fail_env:
                raise RuntimeError(
                    "Codex RPC environment/info failed: -32603 (timed out waiting for exec-server initialize handshake after 10s)"
                )
            return await super().call(method, params, pending)

    attempts = deque(
        [
            (True, [], []),
            (False, [ok_step], calls_ok),
            (True, [], []),
            (True, [], []),
        ]
    )
    spawned = []

    async def fake_subprocess(*args, **kwargs):
        proc = ScriptedProcess()
        spawned.append(proc)
        return proc

    def make_app(process, *_args, **_kwargs):
        fail_env, steps, calls = attempts.popleft()
        return FlakyEnvAppServer(process, fail_env, steps, calls)

    monkeypatch.setattr(BRIDGE_GLOBALS["asyncio"], "create_subprocess_exec", fake_subprocess)
    monkeypatch.setitem(BRIDGE_GLOBALS, "AppServer", make_app)

    kicks = []

    class ReadyManager:
        def is_ready(self):
            return True

        def wait_ready(self, timeout):
            return True

        def kick(self):
            kicks.append("kicked")

    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer test-key"
    server.profile = Path("profile")
    server.workspace = Path("workspace")
    server.codex = "codex"
    server.manager = ReadyManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 1st request: attempt 1 fails environment/info, attempt 2 succeeds -> 200 without kick
        with post_chat(
            server,
            {"model": "codex-minipod", "messages": [{"role": "user", "content": "hi"}]},
        ) as resp:
            data = json.load(resp)
            assert data["choices"][0]["message"]["content"] == "RECOVERED"
        assert kicks == []
        assert spawned[0].closed_stdin or spawned[0].killed

        # 2nd request: both attempts fail environment/info -> 503 and manager.kick()
        with pytest.raises(HTTPError) as err:
            post_chat(
                server,
                {"model": "codex-minipod", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert err.value.code == 503
        assert kicks == ["kicked"]
    finally:
        stop_bridge(server, thread)

