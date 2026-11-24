"""Validate every agent route before LiteLLM consumes transport parameters.

The only client header allowed to reach an agent backend is X-Conversation-ID.
The loopback gateway has one master-key owner, not independent tenant accounts.
"""

import asyncio
import contextvars
import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from .v2.auth import RequestContext, current_request
from .v2.delivery import DeliveryObserver
from .v2.errors import ProtocolError
from .v2.validation import request as validate_v2


@dataclass(frozen=True)
class AgentResponsesContext:
    alias: str
    instructions: str | None
    phases: tuple
    roles: tuple


conversation = contextvars.ContextVar("conversation", default=None)
validated = contextvars.ContextVar("validated_agent_route", default=False)
responses_context = contextvars.ContextVar("agent_responses_context", default=None)
AGENT_MODELS = {"codex-default", "antigravity", "claude-default", "kiro-default"}
_RESPONSES_FIELDS = {
    "model",
    "input",
    "instructions",
    "stream",
    "store",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "text",
    "background",
    "truncation",
}


def validate_responses_input(value):
    if isinstance(value, str):
        value.encode("utf-8")
        return
    if not isinstance(value, list) or not value:
        raise ValueError("invalid_responses_input")
    for message in value:
        if (
            not isinstance(message, dict)
            or set(message) - {"type", "role", "content", "id", "status", "phase"}
            or message.get("type", "message") != "message"
            or not isinstance(message.get("role"), str)
            or message.get("role") not in {"user", "assistant", "system", "developer"}
            or ("id" in message and not isinstance(message["id"], str))
            or message.get("status", "completed") != "completed"
            or message.get("phase") not in (None, "commentary", "final_answer")
            or (message.get("phase") is not None and message.get("role") != "assistant")
        ):
            raise ValueError("invalid_responses_message")
        content = message.get("content")
        if isinstance(content, str):
            content.encode("utf-8")
            continue
        if not isinstance(content, list) or not content:
            raise ValueError("invalid_responses_content")
        accepted = (
            {"input_text", "output_text"}
            if message["role"] == "assistant"
            else {"input_text"}
        )
        for part in content:
            if (
                not isinstance(part, dict)
                or set(part) - {"type", "text", "annotations", "logprobs"}
                or not isinstance(part.get("type"), str)
                or part.get("type") not in accepted
                or not isinstance(part.get("text"), str)
                or part.get("annotations", []) != []
                or (
                    part.get("logprobs") is not None
                    and not isinstance(part["logprobs"], list)
                )
            ):
                raise ValueError("invalid_responses_part")
            part["text"].encode("utf-8")


def validate_responses_payload(payload):
    if any(
        key not in _RESPONSES_FIELDS and value is not None
        for key, value in payload.items()
    ):
        raise ValueError("unsupported_parameters")
    if payload.get("store") is not None and payload["store"] is not False:
        raise ValueError("unsupported_responses_store")
    if payload.get("tools") not in (None, []):
        raise ValueError("unsupported_responses_tools")
    if payload.get("tool_choice") not in (None, "auto", "none"):
        raise ValueError("unsupported_responses_tool_choice")
    if (
        payload.get("parallel_tool_calls") is not None
        and type(payload["parallel_tool_calls"]) is not bool
    ):
        raise ValueError("invalid_parallel_tool_calls")
    if payload.get("text") not in (None, {}, {"format": {"type": "text"}}):
        raise ValueError("unsupported_responses_text")
    if payload.get("background") is not None and payload["background"] is not False:
        raise ValueError("unsupported_responses_background")
    if payload.get("truncation") not in (None, "disabled"):
        raise ValueError("unsupported_responses_truncation")
    if payload.get("stream") is not None and type(payload["stream"]) is not bool:
        raise ValueError("invalid_stream")
    if payload.get("instructions") is not None:
        if not isinstance(payload["instructions"], str):
            raise ValueError("invalid_instructions")
        payload["instructions"].encode("utf-8")
    validate_responses_input(payload.get("input"))


def agent_routes():
    """Use the same startup route snapshot as the LiteLLM model list."""
    raw = os.environ.get("ROUTER_AGENT_ROUTES")
    if raw is None:
        return {
            "codex-default": "codex",
            "antigravity": "antigravity",
            "claude-default": "claude",
            "kiro-default": "kiro",
        }
    value = json.loads(raw)
    if not isinstance(value, dict) or any(
        not isinstance(alias, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", alias)
        or backend not in {"codex", "antigravity", "claude", "kiro"}
        for alias, backend in value.items()
    ):
        raise ValueError("Invalid agent routes")
    return value


class Policy(CustomLogger):
    def __init__(self):
        super().__init__()
        self.v2_host = None

    def configure(self, v2_host):
        self.v2_host = v2_host

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        model = data.get("model")
        alias = model.removeprefix("agent_rpc/") if isinstance(model, str) else None
        if self.v2_host is not None and alias not in self.v2_host.broker.bindings:
            try:
                self.v2_host.guard_legacy()
            except ProtocolError as error:
                raise HTTPException(
                    status_code=error.status, detail=error.code
                ) from None
        if (
            isinstance(model, str)
            and model.removeprefix("agent_rpc/") in agent_routes()
        ):
            if not validated.get():
                raise HTTPException(
                    status_code=400, detail="Unsupported agent endpoint"
                )
            alias = model.removeprefix("agent_rpc/")
            context = current_request.get()
            try:
                if self.v2_host is not None and alias in self.v2_host.broker.bindings:
                    if call_type == "aresponses":
                        raise ProtocolError("unsupported_agent_endpoint", 400)
                    if (
                        context is None
                        or context.host is not self.v2_host
                        or context.payload.get("model") != alias
                    ):
                        raise ProtocolError("trusted_proxy_context_required", 400)
                    context.principal = await self.v2_host.authenticate(
                        user_api_key_dict, alias
                    )
                    data["headers"] = {}
                else:
                    if self.v2_host is not None:
                        self.v2_host.guard_legacy()
                    if call_type == "aresponses":
                        resp_ctx = responses_context.get()
                        if resp_ctx is None or resp_ctx.alias != alias:
                            raise HTTPException(
                                status_code=400, detail="Unsupported agent endpoint"
                            )
                        for key in (
                            "tools",
                            "tool_choice",
                            "parallel_tool_calls",
                            "text",
                            "background",
                            "truncation",
                        ):
                            data.pop(key, None)
                        data["store"] = False
                        data["headers"] = {}
                    else:
                        cid = conversation.get()
                        data["headers"] = {"X-Conversation-ID": cid} if cid else {}
            except ProtocolError as error:
                raise HTTPException(
                    status_code=error.status, detail=error.code
                ) from None
            # Session-affecting routes cannot read OR populate response caches.
            # LiteLLM 1.109.0 still writes when only caching=False is supplied.
            data["caching"] = False
            data["cache"] = {"no-cache": True, "no-store": True}
        return data


policy = Policy()


class Ingress:
    def __init__(self, app, v2_host=None):
        self.app = app
        self.v2_host = v2_host
        self.v2_aliases = set(v2_host.broker.bindings) if v2_host is not None else set()
        self.v2_app = (
            DeliveryObserver(app, v2_host.broker) if v2_host is not None else None
        )
        self.routes = agent_routes()
        raw_flatten = os.environ.get("ROUTER_AGENT_FLATTEN")
        if raw_flatten is not None:
            parsed_flatten = json.loads(raw_flatten)
            if not isinstance(parsed_flatten, dict) or any(
                not isinstance(k, str) or type(v) is not bool
                for k, v in parsed_flatten.items()
            ):
                raise ValueError("Invalid agent flatten map")
            self.flatten_routes = parsed_flatten
        else:
            self.flatten_routes = None
        limits = json.loads(os.environ.get("ROUTER_AGENT_LIMITS", "{}"))
        self.input_limits = {
            alias: limits.get(alias, {}).get(
                "max_input_bytes",
                4 * 1024 * 1024 if backend == "codex" else 128 * 1024,
            )
            for alias, backend in self.routes.items()
        }
        self.read_limit = max(4 * 1024 * 1024, *self.input_limits.values())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        resp_meta = None
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
            path = scope.get("path", "")
            path_alias = next(
                (part for part in path.split("/") if part in self.routes),
                None,
            )
            model = payload.get("model")
            alias = model.removeprefix("agent_rpc/") if isinstance(model, str) else None
            agent_route = path_alias is not None or alias in self.routes
            v2_route = alias in self.v2_aliases
            if agent_route:
                is_chat = path in ("/chat/completions", "/v1/chat/completions")
                is_responses = path in ("/responses", "/v1/responses") and not v2_route
                if not is_chat and not is_responses:
                    raise ValueError("unsupported_agent_endpoint")
                if alias not in self.routes:
                    raise ValueError("unknown_agent_route")
                if len(body) > self.input_limits[alias]:
                    raise ValueError("request_too_large")
                headers = [
                    v
                    for k, v in scope.get("headers", [])
                    if k.lower() == b"x-conversation-id"
                ]
                if is_responses:
                    if headers:
                        raise ValueError("invalid_conversation_id")
                    validate_responses_payload(payload)
                    input_items = payload["input"]
                    phases = (
                        tuple(item.get("phase") for item in input_items)
                        if isinstance(input_items, list)
                        else ()
                    )
                    roles = (
                        tuple(item["role"] for item in input_items)
                        if isinstance(input_items, list)
                        else ()
                    )
                    resp_meta = AgentResponsesContext(
                        alias,
                        payload.get("instructions") or None,
                        phases,
                        roles,
                    )
                elif not v2_route:
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
                    default_flatten = os.environ.get("ROUTER_ALLOW_FLATTENING") == "1"
                    allow_flatten = (
                        self.flatten_routes.get(alias, default_flatten)
                        if self.flatten_routes is not None
                        else default_flatten
                    )
                    for message in messages:
                        if (
                            not isinstance(message, dict)
                            or set(message) != {"role", "content"}
                            or message["role"]
                            not in ("system", "developer", "user", "assistant")
                        ):
                            raise ValueError("invalid_message")
                        role, content = message["role"], message["content"]
                        if (
                            role in ("system", "developer")
                            and non_system
                            and not allow_flatten
                        ):
                            raise ValueError("invalid_system_position")
                        non_system |= role not in ("system", "developer")
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
                    if messages[-1]["role"] != "user" and not allow_flatten:
                        raise ValueError("last_message_must_be_user")
                if headers:
                    if len(headers) != 1 or not re.fullmatch(
                        rb"[A-Za-z0-9_.:-]{1,128}", headers[0]
                    ):
                        raise ValueError("invalid_conversation_id")
                    # Stable opaque correlation only; the raw client ID must
                    # never enter LiteLLM callback metadata or upstream logs.
                    cid = hashlib.sha256(headers[0]).hexdigest()
                if v2_route:
                    original = copy.deepcopy(payload)
                    original["model"] = alias
                    validate_v2(copy.deepcopy(original), cid)
                # Prevent generic header-forwarding and metadata from retaining ID.
                scope = {
                    **scope,
                    "headers": [
                        (k, v)
                        for k, v in scope.get("headers", [])
                        if not k.lower().startswith(b"x-")
                    ],
                }
        except ProtocolError as error:
            await send(
                {
                    "type": "http.response.start",
                    "status": error.status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps(
                        {
                            "error": {
                                "type": error.code,
                                "message": "Unsupported or malformed request",
                            }
                        }
                    ).encode(),
                }
            )
            return
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
        responses_token = responses_context.set(resp_meta)
        request_token = current_request.set(
            RequestContext(self.v2_host, original, cid) if v2_route else None
        )
        stream = False
        failed = False
        buffer = bytearray()

        async def guarded_send(message):
            nonlocal stream, failed
            if not agent_route or v2_route:
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
                        lines = frame.split(b"\n")
                        rebuilt = []
                        for line in lines:
                            if (
                                line.startswith(b"data:")
                                and line[5:].strip() != b"[DONE]"
                            ):
                                data = json.loads(line[5:])
                                if isinstance(data, dict):
                                    if "usage" in data:
                                        # LiteLLM can estimate missing usage. Omit all
                                        # proxy usage until true provenance is available.
                                        data["usage"] = None
                                    if isinstance(data.get("response"), dict) and (
                                        "usage" in data["response"]
                                    ):
                                        data["response"]["usage"] = None
                                    if (
                                        "error" in data
                                        or data.get("type") == "response.failed"
                                    ):
                                        failed = True
                                line = b"data: " + json.dumps(data).encode()
                            rebuilt.append(line)
                        output.extend(b"\n".join(rebuilt) + b"\n\n")
                elif not message.get("more_body"):
                    data = json.loads(buffer)
                    if isinstance(data, dict):
                        if data.get("object") == "response":
                            data["usage"] = None
                        else:
                            data.pop("usage", None)
                    output.extend(json.dumps(data).encode())
                    buffer.clear()
                if len(buffer) > 4 * 1024 * 1024:
                    raise RuntimeError("Invalid proxy response framing")
                message = {**message, "body": bytes(output)}
            await send(message)

        try:
            target = self.v2_app if v2_route else self.app
            await target(scope, replay, guarded_send)
        finally:
            current_request.reset(request_token)
            responses_context.reset(responses_token)
            conversation.reset(token)
            validated.reset(validated_token)
