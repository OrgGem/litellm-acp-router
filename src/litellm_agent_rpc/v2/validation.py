"""Bounded text/function request validation before any driver resource exists."""

import re

from jsonschema import Draft202012Validator

from .contract import SCHEMA, canonical, validate
from .errors import ProtocolError

KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "description",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}
MESSAGE_VALIDATOR = Draft202012Validator(
    {"$defs": SCHEMA["$defs"], "$ref": "#/$defs/Message"}
)


def schema_subset(schema, depth=0):
    if not isinstance(schema, dict) or depth > 16 or set(schema) - KEYWORDS:
        raise ProtocolError("unsupported_tool_schema")
    if (
        "additionalProperties" in schema
        and type(schema["additionalProperties"]) is not bool
    ):
        raise ProtocolError("unsupported_tool_schema")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ProtocolError("invalid_tool_schema") from None
    for child in schema.get("properties", {}).values():
        schema_subset(child, depth + 1)
    if "items" in schema:
        schema_subset(schema["items"], depth + 1)


def request(payload, cid):
    allowed = {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning_effort",
        "reasoning_output",
        "max_completion_tokens",
        "stream",
        "stream_options",
    }
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ProtocolError("unsupported_parameters")
    if not isinstance(payload.get("model"), str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,100}", payload["model"]
    ):
        raise ProtocolError("invalid_model")
    if cid is not None and (
        not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", cid)
    ):
        raise ProtocolError("invalid_conversation_id")
    if type(payload.get("stream", False)) is not bool:
        raise ProtocolError("invalid_stream")
    options = payload.get("stream_options")
    if options is not None and (
        not payload.get("stream")
        or not isinstance(options, dict)
        or set(options) != {"include_usage"}
        or type(options["include_usage"]) is not bool
    ):
        raise ProtocolError("invalid_stream_options")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 4096:
        raise ProtocolError("invalid_messages")
    non_system = False
    outstanding = set()
    seen = set()
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and "reasoning_content" in message
        ):
            presentation = message["reasoning_content"]
            if presentation is not None and (
                not isinstance(presentation, str)
                or len(canonical(presentation)) > 131074
            ):
                raise ProtocolError("invalid_reasoning_history")
            # Public presentation is never replayed as native reasoning or part
            # of the exact conversational history fingerprint.
            message.pop("reasoning_content")
        # Message is not an envelope entry; validate against its local definition.
        if not MESSAGE_VALIDATOR.is_valid(message):
            raise ProtocolError("invalid_message")
        role = message["role"]
        if role == "system" and non_system:
            raise ProtocolError("invalid_system_position")
        non_system |= role != "system"
        if role == "tool":
            call_id = message["tool_call_id"]
            if call_id not in outstanding:
                raise ProtocolError("invalid_tool_history")
            outstanding.remove(call_id)
        else:
            if outstanding:
                raise ProtocolError("incomplete_tool_history")
            for call in message.get("tool_calls", []):
                if call["id"] in seen:
                    raise ProtocolError("duplicate_tool_id")
                seen.add(call["id"])
                outstanding.add(call["id"])
    if outstanding or messages[-1]["role"] not in {"user", "tool"}:
        raise ProtocolError("incomplete_tool_history")
    tools = payload.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 32:
        raise ProtocolError("invalid_tools")
    if len(canonical(tools)) > 65536:
        raise ProtocolError("tool_schema_too_large")
    names = set()
    for tool in tools:
        validate("ToolDefinition", tool)
        function = tool["function"]
        if function["name"] in names:
            raise ProtocolError("duplicate_tool_name")
        names.add(function["name"])
        if len(canonical(function["parameters"])) > 65536:
            raise ProtocolError("tool_schema_too_large")
        schema_subset(function["parameters"])
        if function.get("strict"):
            raise ProtocolError("strict_tools_unavailable")
    if (tools or messages[-1]["role"] == "tool") and cid is None:
        raise ProtocolError("conversation_id_required")
    policy = {
        "tool_choice": payload.get("tool_choice", "auto"),
        "parallel_tool_calls": payload.get("parallel_tool_calls", False),
    }
    if "max_completion_tokens" in payload:
        value = payload["max_completion_tokens"]
        if type(value) is not int or value < 1:
            raise ProtocolError("invalid_output_limit")
        policy["max_completion_tokens"] = value
    validate("RoundPolicy", policy)
    if not isinstance(payload.get("reasoning_output", "off"), str) or payload.get(
        "reasoning_output", "off"
    ) not in {
        "off",
        "summary",
        "native_exportable",
    }:
        raise ProtocolError("invalid_reasoning_output")
    if "reasoning_effort" in payload and (
        not isinstance(payload["reasoning_effort"], str)
        or not payload["reasoning_effort"]
        or len(payload["reasoning_effort"]) > 64
    ):
        raise ProtocolError("invalid_reasoning_effort")
    if len(canonical(payload)) > 4 * 1024 * 1024:
        raise ProtocolError("request_too_large", 413)
    return policy


def check_arguments(tool, arguments):
    if not isinstance(arguments, dict) or len(canonical(arguments)) > 65536:
        raise ProtocolError("invalid_native_arguments", 502)
    if not Draft202012Validator(tool["function"]["parameters"]).is_valid(arguments):
        raise ProtocolError("invalid_native_arguments", 502)
