"""Validate new routes before LiteLLM consumes transport parameters.

The only client header allowed to reach an agent backend is X-Conversation-ID.
The loopback gateway has one master-key owner, not independent tenant accounts.
"""

import asyncio
import contextvars
import hashlib
import json
import os
import re

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

conversation = contextvars.ContextVar("conversation", default=None)
validated = contextvars.ContextVar("validated_agent_route", default=False)
AGENT_MODELS = {"claude-default", "kiro-default"}


class Policy(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if isinstance(data.get("model"), str) and data["model"] in AGENT_MODELS:
            if not validated.get():
                raise HTTPException(
                    status_code=400, detail="Unsupported agent endpoint"
                )
            cid = conversation.get()
            data["headers"] = {"X-Conversation-ID": cid} if cid else {}
        return data


policy = Policy()


class Ingress:
    def __init__(self, app):
        self.app = app
        limits = json.loads(os.environ.get("ROUTER_AGENT_LIMITS", "{}"))
        self.input_limits = {
            alias: limits.get(alias, {}).get("max_input_bytes", 128 * 1024)
            for alias in AGENT_MODELS
        }
        self.read_limit = max(4 * 1024 * 1024, *self.input_limits.values())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(float(os.environ.get("ROUTER_TIMEOUT", "350"))):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > self.read_limit:
                        raise ValueError("request_too_large")
                    if not event.get("more_body"):
                        break

            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("invalid_json")
                    result[key] = value
                return result

            payload = json.loads(body, object_pairs_hook=pairs)
            if not isinstance(payload, dict) or (
                "model" in payload and not isinstance(payload["model"], str)
            ):
                raise ValueError("invalid_request")
            cid = None
            path_alias = next(
                (
                    part
                    for part in scope.get("path", "").split("/")
                    if part in AGENT_MODELS
                ),
                None,
            )
            agent_route = path_alias is not None or (
                isinstance(payload, dict) and payload.get("model") in AGENT_MODELS
            )
            if agent_route:
                if scope.get("path") not in (
                    "/chat/completions",
                    "/v1/chat/completions",
                ):
                    raise ValueError("unsupported_agent_endpoint")
                if len(body) > self.input_limits[payload["model"]]:
                    raise ValueError("request_too_large")
                if set(payload) - {"model", "messages", "stream", "stream_options"}:
                    raise ValueError("unsupported_parameters")
                if type(payload.get("stream", False)) is not bool:
                    raise ValueError("invalid_stream")
                if "stream_options" in payload:
                    options = payload["stream_options"]
                    if (
                        not payload.get("stream")
                        or not isinstance(options, dict)
                        or set(options) != {"include_usage"}
                        or type(options["include_usage"]) is not bool
                    ):
                        raise ValueError("unsupported_stream_options")
                messages = payload.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise ValueError("invalid_messages")
                non_system = False
                for message in messages:
                    if (
                        not isinstance(message, dict)
                        or set(message) != {"role", "content"}
                        or message["role"] not in ("system", "user", "assistant")
                    ):
                        raise ValueError("invalid_message")
                    role, content = message["role"], message["content"]
                    if role == "system" and non_system:
                        raise ValueError("invalid_system_position")
                    non_system |= role != "system"
                    if isinstance(content, list):
                        if not content or any(
                            not isinstance(b, dict)
                            or set(b) != {"type", "text"}
                            or b["type"] != "text"
                            or not isinstance(b["text"], str)
                            for b in content
                        ):
                            raise ValueError("text_only")
                        content = "".join(b["text"] for b in content)
                    if not isinstance(content, str):
                        raise ValueError("invalid_text")
                    content.encode("utf-8")
                if messages[-1]["role"] != "user":
                    raise ValueError("last_message_must_be_user")
                headers = [
                    v
                    for k, v in scope.get("headers", [])
                    if k.lower() == b"x-conversation-id"
                ]
                if headers:
                    if len(headers) != 1 or not re.fullmatch(
                        rb"[A-Za-z0-9_.:-]{1,128}", headers[0]
                    ):
                        raise ValueError("invalid_conversation_id")
                    # Stable opaque correlation only; the raw client ID must
                    # never enter LiteLLM callback metadata or upstream logs.
                    cid = hashlib.sha256(headers[0]).hexdigest()
                # Prevent generic header-forwarding and metadata from retaining ID.
                scope = {
                    **scope,
                    "headers": [
                        (k, v)
                        for k, v in scope.get("headers", [])
                        if not k.lower().startswith(b"x-")
                    ],
                }
        except (ValueError, UnicodeError, TimeoutError):
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":{"type":"invalid_request_error","message":"Unsupported or malformed request"}}',
                }
            )
            return
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        token = conversation.set(cid)
        validated_token = validated.set(agent_route)
        stream = False
        failed = False
        buffer = bytearray()

        async def guarded_send(message):
            nonlocal stream, failed
            if not agent_route:
                return await send(message)
            if message["type"] == "http.response.start":
                stream = any(
                    k.lower() == b"content-type" and b"text/event-stream" in v
                    for k, v in message.get("headers", [])
                )
                # Body rewriting changes length; proxy-estimated cost is not a
                # backend token counter and must not be published either.
                message = {
                    **message,
                    "headers": [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower()
                        not in (b"content-length", b"x-litellm-response-cost")
                    ],
                }
            elif message["type"] == "http.response.body":
                buffer.extend(message.get("body", b""))
                output = bytearray()
                if stream:
                    while b"\n\n" in buffer:
                        frame, _, rest = buffer.partition(b"\n\n")
                        buffer[:] = rest
                        if failed:
                            continue
                        if (
                            frame.startswith(b"data:")
                            and frame[5:].strip() != b"[DONE]"
                        ):
                            data = json.loads(frame[5:])
                            if "usage" in data:
                                # LiteLLM can estimate missing usage. Omit all
                                # proxy usage until true provenance is available.
                                data["usage"] = None
                            if "error" in data:
                                failed = True
                            frame = b"data: " + json.dumps(data).encode()
                        output.extend(frame + b"\n\n")
                elif not message.get("more_body"):
                    data = json.loads(buffer)
                    data.pop("usage", None)
                    output.extend(json.dumps(data).encode())
                    buffer.clear()
                if len(buffer) > 4 * 1024 * 1024:
                    raise RuntimeError("Invalid proxy response framing")
                message = {**message, "body": bytes(output)}
            await send(message)

        try:
            await self.app(scope, replay, guarded_send)
        finally:
            conversation.reset(token)
            validated.reset(validated_token)
