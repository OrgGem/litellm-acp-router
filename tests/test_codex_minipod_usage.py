import asyncio
import json
import runpy
import threading
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

bridge = runpy.run_path(str(Path(__file__).parents[1] / "deploy/codex-minipod/bridge.py"))
BRIDGE_GLOBALS = bridge["Handler"].do_POST.__globals__

BASE_USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 40,
    "total_tokens": 999,
    "prompt_tokens_details": {"cached_tokens": 25, "cache_write_tokens": 12},
    "completion_tokens_details": {"reasoning_tokens": 15},
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


def usage_event(thread_id, turn_id, total, last):
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "tokenUsage": {"total": total, "last": last},
        },
    }


def snapshot(input_tokens, output_tokens, total_tokens, **details):
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
        **details,
    }


def test_usage_from_total_maps_cache_and_reasoning_fields():
    total = snapshot(
        100,
        40,
        999,
        cachedInputTokens=25,
        cacheWriteInputTokens=12,
        reasoningOutputTokens=15,
    )

    assert bridge["usage_from_total"](total) == BASE_USAGE


def test_usage_from_total_preserves_explicit_zero_and_omits_absent_details():
    zero_total = snapshot(
        0,
        0,
        0,
        cachedInputTokens=0,
        cacheWriteInputTokens=0,
        reasoningOutputTokens=0,
    )
    no_details = snapshot(0, 0, 0)

    assert bridge["usage_from_total"](zero_total) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    assert bridge["usage_from_total"](no_details) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


@pytest.mark.parametrize(
    "total",
    [
        None,
        [],
        {},
        {"inputTokens": 1, "outputTokens": 2},
        {"inputTokens": -1, "outputTokens": 2, "totalTokens": 1},
        {"inputTokens": 1, "outputTokens": -2, "totalTokens": -1},
        {"inputTokens": True, "outputTokens": 1, "totalTokens": 2},
        {"inputTokens": 1, "outputTokens": 2.0, "totalTokens": 3},
        {"inputTokens": 1, "outputTokens": 2, "totalTokens": 2**63},
    ],
)
def test_usage_from_total_returns_none_when_required_counts_are_missing_or_invalid(total):
    assert bridge["usage_from_total"](total) is None


def test_usage_from_total_drops_invalid_optional_details_and_bounds_reasoning():
    total = snapshot(
        100,
        40,
        777,
        cachedInputTokens=101,
        cacheWriteInputTokens=10,
        reasoningOutputTokens=41,
    )

    assert bridge["usage_from_total"](total) == {
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "total_tokens": 777,
        "prompt_tokens_details": {"cache_write_tokens": 10},
    }


async def collect(events):
    app = type("FakeApp", (), {"read": lambda self: None})()
    iterator = iter(events)

    async def read():
        return next(iterator)

    app.read = read
    return await bridge["collect_turn"](app, "thread", "turn", deque(), None)


def test_collect_turn_uses_latest_total_snapshot_without_adding_repeated_events():
    initial_total = snapshot(100, 30, 140, cachedInputTokens=20, reasoningOutputTokens=12)
    initial_last = snapshot(30, 10, 40, cachedInputTokens=5, reasoningOutputTokens=4)
    latest_total = snapshot(
        180,
        55,
        260,
        cachedInputTokens=60,
        cacheWriteInputTokens=15,
        reasoningOutputTokens=22,
    )
    latest_last = snapshot(80, 25, 105, cachedInputTokens=40, reasoningOutputTokens=10)
    events = [
        usage_event("another-thread", "turn", snapshot(900, 900, 1800), snapshot(1, 1, 2)),
        usage_event("thread", "another-turn", snapshot(800, 800, 1600), snapshot(1, 1, 2)),
        usage_event("thread", "turn", initial_total, initial_last),
        usage_event("thread", "turn", initial_total, initial_last),
        usage_event("thread", "turn", latest_total, latest_last),
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "item": {"id": "message", "type": "agentMessage", "text": "READY"},
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}},
        },
    ]

    answer, usage = asyncio.run(collect(events))

    assert answer == "READY"
    assert usage == {
        "prompt_tokens": 180,
        "completion_tokens": 55,
        "total_tokens": 260,
        "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 15},
        "completion_tokens_details": {"reasoning_tokens": 22},
    }


def test_collect_turn_returns_none_when_no_usage_snapshot_exists():
    events = [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "item": {"id": "message", "type": "agentMessage", "text": "READY"},
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}},
        },
    ]

    answer, usage = asyncio.run(collect(events))

    assert answer == "READY"
    assert usage is None


def test_non_streaming_response_preserves_usage_and_null_when_missing(monkeypatch):
    async def fake_run_turn(prompt, profile, workspace, codex, emit_text=None, *, model):
        return "READY", BASE_USAGE

    server, thread = start_test_server(monkeypatch, fake_run_turn)
    try:
        with send_chat(
            server,
            {"model": "codex-minipod", "messages": [{"role": "user", "content": "check"}]},
        ) as response:
            body = json.load(response)
        assert response.status == 200
        assert body["usage"] == BASE_USAGE
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    async def missing_usage(prompt, profile, workspace, codex, emit_text=None, *, model):
        return "READY", None

    server, thread = start_test_server(monkeypatch, missing_usage)
    try:
        with send_chat(
            server,
            {"model": "codex-minipod", "messages": [{"role": "user", "content": "check"}]},
        ) as response:
            body = json.load(response)
        assert body["usage"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def read_stream(response):
    events = []
    saw_done = False
    for line in response:
        if not line.startswith(b"data: "):
            continue
        data = line[6:].strip()
        if data == b"[DONE]":
            saw_done = True
        else:
            events.append(json.loads(data))
    return events, saw_done


def test_streaming_include_usage_emits_text_stop_usage_then_done(monkeypatch):
    async def fake_run_turn(prompt, profile, workspace, codex, emit_text=None, *, model):
        emit_text("LUNA-")
        emit_text("CHECK")
        return "LUNA-CHECK", BASE_USAGE

    server, thread = start_test_server(monkeypatch, fake_run_turn)
    try:
        with send_chat(
            server,
            {
                "model": "codex-minipod",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "check"}],
            },
        ) as response:
            events, saw_done = read_stream(response)
        assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert saw_done
    assert [event["choices"] for event in events[:2]] == [
        [{"index": 0, "delta": {"content": "LUNA-", "role": "assistant"}, "finish_reason": None}],
        [{"index": 0, "delta": {"content": "CHECK"}, "finish_reason": None}],
    ]
    assert events[0]["usage"] is None
    assert events[1]["usage"] is None
    assert events[2]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    assert events[2]["usage"] is None
    assert events[3]["choices"] == []
    assert events[3]["usage"] == BASE_USAGE


@pytest.mark.parametrize("include_usage", [False, True])
def test_streaming_missing_usage_is_omitted_by_default_and_null_when_requested(monkeypatch, include_usage):
    async def fake_run_turn(prompt, profile, workspace, codex, emit_text=None, *, model):
        emit_text("READY")
        return "READY", None

    server, thread = start_test_server(monkeypatch, fake_run_turn)
    try:
        with send_chat(
            server,
            {
                "model": "codex-minipod",
                "stream": True,
                "stream_options": {"include_usage": include_usage},
                "messages": [{"role": "user", "content": "check"}],
            },
        ) as response:
            events, saw_done = read_stream(response)
        assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert saw_done
    if include_usage:
        assert all(event["usage"] is None for event in events[:-1])
        assert events[-1]["choices"] == []
        assert events[-1]["usage"] is None
    else:
        assert all("usage" not in event for event in events)


def test_streaming_usage_is_not_emitted_without_opt_in(monkeypatch):
    async def fake_run_turn(prompt, profile, workspace, codex, emit_text=None, *, model):
        emit_text("READY")
        return "READY", BASE_USAGE

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
            events, saw_done = read_stream(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert saw_done
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert all("usage" not in event for event in events)
