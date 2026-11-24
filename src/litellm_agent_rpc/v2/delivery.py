"""ASGI observer for a host-owned prepared response; one receive owner."""

import asyncio
import contextlib
import contextvars
from dataclasses import dataclass

from .contract import canonical, parse
from .errors import ProtocolError


@dataclass
class Delivery:
    result: object = None


current = contextvars.ContextVar("agent_rpc_v2_delivery", default=None)


def attach(result):
    delivery = current.get()
    if delivery is None:
        raise ProtocolError("delivery_context_required", 503)
    delivery.result = result


class DeliveryObserver:
    """Wrap the trusted proxy, not a second HTTP server or a second receive pump.

    The provider calls attach after broker.prepare. Observer validates public
    terminal output against that Result; errors, disconnect and no-op sends never
    ACK. Post-ACK disconnect retains the finite broker lease.
    """

    def __init__(self, app, broker):
        self.app, self.broker = app, broker

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        delivery = Delivery()
        token = current.set(delivery)
        queue = asyncio.Queue(maxsize=2)
        disconnected = asyncio.Event()
        status, stream, failed, acked = None, False, False, False
        buffer = bytearray()
        aggregate = {"role": "assistant", "content": None}
        calls = {}
        terminal = None
        output_bytes = 0
        application = None

        async def pump():
            request_complete = False
            try:
                while True:
                    event = await receive()
                    if event["type"] == "http.request":
                        if request_complete:
                            disconnected.set()
                            await queue.put({"type": "http.disconnect"})
                            return
                        request_complete = not event.get("more_body", False)
                    if event["type"] == "http.disconnect":
                        disconnected.set()
                        if application is not None and not acked:
                            application.cancel()
                    await queue.put(event)
                    if event["type"] == "http.disconnect":
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                disconnected.set()
                await queue.put({"type": "http.disconnect"})

        receiver = asyncio.create_task(pump())

        def valid_done():
            result = delivery.result
            if result is None or terminal != result.finish_reason:
                return False
            if calls:
                if sorted(calls) != list(range(len(calls))):
                    return False
                aggregate["tool_calls"] = [calls[i] for i in sorted(calls)]
            return aggregate == result.message

        def frame(data):
            nonlocal failed, terminal
            if "error" in data:
                failed = True
                return
            for choice in data.get("choices", []):
                if choice.get("index") != 0 or terminal is not None:
                    raise ProtocolError("invalid_delivery", 502)
                delta = choice.get("delta", {})
                if set(delta) - {"role", "content", "reasoning_content", "tool_calls"}:
                    raise ProtocolError("invalid_delivery", 502)
                if "role" in delta and delta["role"] != "assistant":
                    raise ProtocolError("invalid_delivery", 502)
                for name in ("content", "reasoning_content"):
                    if delta.get(name) is not None:
                        aggregate[name] = (aggregate.get(name) or "") + delta[name]
                for call in delta.get("tool_calls", []):
                    index = call["index"]
                    if type(index) is not int or not 0 <= index < 8:
                        raise ProtocolError("invalid_delivery", 502)
                    value = calls.setdefault(
                        index,
                        {
                            "id": None,
                            "type": "function",
                            "function": {"name": None, "arguments": ""},
                        },
                    )
                    if "id" in call:
                        if value["id"] is not None:
                            raise ProtocolError("invalid_delivery", 502)
                        value["id"] = call["id"]
                    function = call.get("function", {})
                    if "name" in function:
                        if value["function"]["name"] is not None:
                            raise ProtocolError("invalid_delivery", 502)
                        value["function"]["name"] = function["name"]
                    value["function"]["arguments"] += function.get("arguments", "")
                if choice.get("finish_reason") is not None:
                    terminal = choice["finish_reason"]

        async def observed_send(event):
            nonlocal status, stream, failed, acked, output_bytes
            marker = False
            result = delivery.result
            if event["type"] == "http.response.start":
                status = event["status"]
                stream = any(
                    k.lower() == b"content-type" and b"text/event-stream" in v
                    for k, v in event.get("headers", [])
                )
                if result is not None:
                    event = {
                        **event,
                        "headers": [
                            (k, v)
                            for k, v in event.get("headers", [])
                            if k.lower()
                            not in {b"content-length", b"x-litellm-response-cost"}
                        ],
                    }
            elif event["type"] == "http.response.body" and result is not None:
                output_bytes += len(event.get("body", b""))
                if output_bytes > 8 * 1024 * 1024:
                    raise ProtocolError("delivery_too_large", 502)
                buffer.extend(event.get("body", b""))
                if len(buffer) > 4 * 1024 * 1024:
                    raise ProtocolError("invalid_delivery", 502)
                if stream:
                    output = bytearray()
                    while b"\n\n" in buffer:
                        chunk, _, rest = buffer.partition(b"\n\n")
                        buffer[:] = rest
                        if failed:
                            continue
                        if not chunk.startswith(b"data:"):
                            raise ProtocolError("invalid_delivery", 502)
                        body = chunk[5:].strip()
                        if body == b"[DONE]":
                            if marker or acked or buffer:
                                raise ProtocolError("invalid_delivery", 502)
                            marker = valid_done()
                            if not marker:
                                raise ProtocolError("invalid_delivery", 502)
                            output.extend(b"data: [DONE]\n\n")
                        else:
                            data = parse(body)
                            frame(data)
                            if failed:
                                data = {
                                    "error": {
                                        "type": "backend_error",
                                        "message": "Backend delivery failed",
                                    }
                                }
                            elif "usage" in data:
                                data["usage"] = result.usage
                            output.extend(b"data: " + canonical(data) + b"\n\n")
                    event = {**event, "body": bytes(output)}
                elif not event.get("more_body"):
                    body = parse(buffer)
                    expected = body.get("choices", [])
                    marker = (
                        len(expected) == 1
                        and expected[0].get("message") == result.message
                        and expected[0].get("finish_reason") == result.finish_reason
                        and "error" not in body
                    )
                    if not marker:
                        raise ProtocolError("invalid_delivery", 502)
                    body["usage"] = result.usage
                    event = {**event, "body": canonical(body)}
                    buffer.clear()
                else:
                    event = {**event, "body": b""}
            # Give the sole receive pump a turn before and after send: Uvicorn
            # can return normally from send once it has observed disconnect.
            # Uvicorn also emits http.disconnect after an ordinary final body.
            # Keep its response open until this observer has ACKed the validated
            # marker, then end it with an empty body. During the marker send a
            # receive disconnect can therefore only be a transport disconnect.
            finish_after_ack = (
                marker
                and event["type"] == "http.response.body"
                and not event.get("more_body", False)
            )
            if finish_after_ack:
                event = {**event, "more_body": True}
            await asyncio.sleep(0)
            await send(event)
            await asyncio.sleep(0)
            if marker and status == 200 and not failed and not disconnected.is_set():
                await self.broker.acknowledge(result)
                acked = True
            if finish_after_ack:
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )

        try:
            application = asyncio.create_task(self.app(scope, queue.get, observed_send))
            try:
                await application
            except asyncio.CancelledError:
                if not disconnected.is_set():
                    raise
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver
            if delivery.result is not None and not acked:
                await self.broker.abort_delivery(delivery.result)
            current.reset(token)
