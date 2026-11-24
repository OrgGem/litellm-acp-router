import asyncio
import json

import pytest
from test_v2_host import continuation, payload, setup

from litellm_agent_rpc.v2.delivery import DeliveryObserver, attach
from litellm_agent_rpc.v2.sdk import AsyncClient, Client
from litellm_agent_rpc.v2.serialization import chunks, response


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("disconnect", [False, True])
async def test_delivery_ack_at_body_or_done_and_noop_send_disconnect(
    stream, disconnect
):
    _, principal, driver, broker = setup()
    gone = asyncio.Event()
    bodies = []
    first = None

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(event):
        if event["type"] == "http.response.body":
            bodies.append(event.get("body", b""))
            if disconnect:
                gone.set()  # Uvicorn send can return normally after disconnect.

    async def app(scope, recv, send):
        nonlocal first
        first = await broker.invoke(principal, payload(), "one")
        attach(first)
        content_type = b"text/event-stream" if stream else b"application/json"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", content_type)],
            }
        )
        if stream:
            for chunk in chunks(first, True):
                data = b"data: " + json.dumps(chunk).encode() + b"\n\n"
                # Deliberately fragment frame bytes, including arguments.
                for offset in range(0, len(data), 17):
                    await send(
                        {
                            "type": "http.response.body",
                            "body": data[offset : offset + 17],
                            "more_body": True,
                        }
                    )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"data: [DONE]\n\n",
                    "more_body": True,
                }
            )
            if not disconnect:
                assert broker.counts()["awaiting_client"] == 1  # Before empty EOF.
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps(response(first)).encode(),
                }
            )

    try:
        await DeliveryObserver(app, broker)({"type": "http"}, receive, send)
        if disconnect:
            assert not broker.records and driver.cancelled == 1
        else:
            assert broker.counts()["awaiting_client"] == 1 and driver.cancelled == 0
            await broker.invoke(principal, continuation(first, payload()), "one")
    finally:
        await broker.close()


async def test_error_frame_followed_by_done_is_not_acknowledged():
    _, principal, driver, broker = setup()
    sent = []

    async def receive():
        await asyncio.Future()

    async def send(event):
        sent.append(event.get("body", b""))

    async def app(scope, recv, send):
        result = await broker.invoke(principal, payload(), "one")
        attach(result)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'data: {"error":{"message":"failed"}}\n\ndata: [DONE]\n\n',
            }
        )

    try:
        await DeliveryObserver(app, broker)({"type": "http"}, receive, send)
        public = b"".join(sent)
        assert b'"error"' in public and b"[DONE]" not in public
        assert b"Backend delivery failed" in public
        assert not broker.records and driver.cancelled == 1
    finally:
        await broker.close()


async def test_receive_transport_failure_unblocks_downstream():
    _, _, _, broker = setup()

    async def receive():
        raise OSError("private transport error")

    async def app(scope, receive, send):
        assert await receive() == {"type": "http.disconnect"}

    async def send(event):
        raise AssertionError("No response expected")

    await asyncio.wait_for(
        DeliveryObserver(app, broker)({"type": "http"}, receive, send), 0.2
    )
    await broker.close()


async def test_disconnect_during_native_generation_cancels_owner():
    _, principal, driver, broker = setup()
    started = asyncio.Event()
    gone = asyncio.Event()

    async def stalled(turn):
        started.set()
        await asyncio.Future()
        yield

    driver.events = stalled

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(event):
        raise AssertionError("Disconnected before response")

    async def app(scope, receive, send):
        await broker.invoke(principal, payload(), "one")

    task = asyncio.create_task(
        DeliveryObserver(app, broker)({"type": "http"}, receive, send)
    )
    await started.wait()
    gone.set()
    await asyncio.wait_for(task, 0.5)
    assert not broker.records and driver.cancelled == 1 and driver.closed == 1
    await broker.close()


async def test_sdk_async_complete_stream_and_consumer_cancellation():
    _, principal, driver, broker = setup()
    client = AsyncClient(broker, principal)
    try:
        body = await client.completion(payload(), "one")
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        assert broker.counts()["awaiting_client"] == 1
        initial = payload()
        messages = initial["messages"] + [
            body["choices"][0]["message"],
            {
                "role": "tool",
                "tool_call_id": body["choices"][0]["message"]["tool_calls"][0]["id"],
                "content": "result",
            },
        ]
        stream = client.stream(
            {
                "model": "test",
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            "one",
        )
        output = [chunk async for chunk in stream]
        assert output[-1]["choices"] == [] and output[-1]["usage"] is None
        early = client.stream({**payload(), "stream": True}, "two")
        await anext(early)
        assert broker.counts()["prepared"] == 1
        await early.aclose()
        assert not broker.records and driver.cancelled == 1
    finally:
        await broker.close()


def test_sdk_sync_owns_one_loop():
    def factory():
        _, principal, _, broker = setup(tool=False)
        return broker, principal

    with Client(factory) as client:
        assert client.completion(payload(False))["usage"]["total_tokens"] == 13
        values = list(
            client.stream(
                {
                    **payload(False),
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
            )
        )
        assert values[-1]["usage"]["total_tokens"] == 13


async def test_sdk_close_immediately_at_terminal_keeps_acknowledged_lease():
    _, principal, driver, broker = setup()
    try:
        stream = AsyncClient(broker, principal).stream(
            {**payload(), "stream": True, "stream_options": None}, "one"
        )
        while True:
            chunk = await anext(stream)
            if chunk["choices"][0]["finish_reason"] == "tool_calls":
                break
        await stream.aclose()
        assert broker.counts()["awaiting_client"] == 1 and driver.cancelled == 0
    finally:
        await broker.close()
