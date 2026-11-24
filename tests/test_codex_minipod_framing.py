import asyncio
import json
import runpy
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

bridge = runpy.run_path(str(Path(__file__).parents[1] / "deploy/codex-minipod/bridge.py"))


async def test_large_tool_event_does_not_discard_final_message_or_completion():
    reader = asyncio.StreamReader(limit=bridge["APP_SERVER_FRAME_LIMIT"])
    events = [
        {
            "method": "item/completed",
            "params": {"item": {"type": "commandExecution", "aggregatedOutput": "x" * (5 * 1024 * 1024)}},
        },
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "READY"}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    for event in events:
        reader.feed_data(json.dumps(event).encode() + b"\n")
    reader.feed_eof()

    app = bridge["AppServer"](SimpleNamespace(stdout=reader))
    assert await app.read() == events[1]
    assert await app.read() == events[2]


async def test_oversized_rpc_frame_fails_with_bounded_error():
    reader = asyncio.StreamReader(limit=64)
    reader.feed_data(b"x" * 65 + b"\n")
    app = bridge["AppServer"](SimpleNamespace(stdout=reader))

    with pytest.raises(RuntimeError, match="frame exceeded the bridge limit"):
        await app.read()


async def test_codex_deltas_are_forwarded_before_turn_completion():
    reader = asyncio.StreamReader(limit=bridge["APP_SERVER_FRAME_LIMIT"])
    app = bridge["AppServer"](SimpleNamespace(stdout=reader))
    chunks = []
    first_chunk = asyncio.Event()

    def emit(text):
        chunks.append(text)
        first_chunk.set()

    def feed(method, **params):
        event = {"method": method, "params": {"threadId": "thread", "turnId": "turn", **params}}
        reader.feed_data(json.dumps(event).encode() + b"\n")

    task = asyncio.create_task(bridge["collect_turn"](app, "thread", "turn", deque(), emit))
    try:
        feed("item/agentMessage/delta", itemId="message", delta="First")
        await asyncio.wait_for(first_chunk.wait(), 1)
        assert not task.done()
        assert chunks == ["First"]

        feed("item/agentMessage/delta", itemId="message", delta=" part")
        feed("item/completed", item={"id": "message", "type": "agentMessage", "text": "First part"})
        feed("item/completed", item={"id": "no-deltas", "type": "agentMessage", "text": "Final part"})
        feed("turn/completed", turn={"id": "turn", "status": "completed"})

        answer, _ = await asyncio.wait_for(task, 1)
        assert chunks == ["First", " part", "\n\n", "Final part"]
        assert "".join(chunks) == answer
    finally:
        if not task.done():
            task.cancel()
