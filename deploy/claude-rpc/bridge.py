"""Authenticated loopback OpenAI chat + tool-calling endpoint backed by Claude Code SDK."""

import asyncio
import contextlib
import hmac
import json
import re
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_RPC_SRC = REPO_ROOT / "litellm-claude-rpc" / "src"
if str(CLAUDE_RPC_SRC) not in sys.path:
    sys.path.insert(0, str(CLAUDE_RPC_SRC))

MODEL_ALIASES = MappingProxyType(
    json.loads(Path(__file__).with_name("models.json").read_text(encoding="utf-8"))
)
RUNTIME_LOCK = threading.Lock()
MAX_REQUEST_BYTES = 4 * 1024 * 1024
STEP_TIMEOUT_SECONDS = 105
DEFAULT_SESSION_TTL = 180.0
MAX_PENDING_SESSIONS = 16
DEFAULT_CONCURRENCY = 4
MCP_SERVER_NAME = "client_tools"
MCP_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _extract_text_content(content, *, allow_none=False):
    if content is None and allow_none:
        return None
    if (
        isinstance(content, list)
        and content
        and all(
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            for part in content
        )
    ):
        return "\n".join(part["text"] for part in content)
    if isinstance(content, str):
        return content
    raise ValueError("only text messages are supported")


def normalize_messages(messages):
    """Validate OpenAI chat messages, including assistant tool_calls and tool results."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("expected at least one message")
    normalized = []
    pending_ids = set()
    current_results = {}
    collecting_results = False

    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("expected message objects")
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if (
                not collecting_results
                or not isinstance(call_id, str)
                or not call_id
                or call_id not in pending_ids
                or call_id in current_results
            ):
                raise ValueError("unmatched or duplicate tool result message")
            content = _extract_text_content(message.get("content"), allow_none=True)
            if content is None:
                content = ""
            current_results[call_id] = content
            normalized.append(
                {"role": "tool", "tool_call_id": call_id, "content": content}
            )
            continue

        if collecting_results:
            if set(current_results) != pending_ids:
                raise ValueError("incomplete tool results for preceding tool_calls")
            pending_ids = set()
            current_results = {}
            collecting_results = False

        if role not in ("system", "developer", "user", "assistant") or message.get(
            "function_call"
        ):
            raise ValueError("unsupported message role or legacy function_call")

        raw_calls = message.get("tool_calls")
        if raw_calls is not None:
            if role != "assistant" or not isinstance(raw_calls, list) or not raw_calls:
                raise ValueError("invalid assistant tool_calls")
            clean_calls = []
            seen = set()
            for item in raw_calls:
                if not isinstance(item, dict) or item.get("type", "function") != "function":
                    raise ValueError("unsupported tool_call entry")
                call_id = item.get("id")
                fn = item.get("function")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or call_id in seen
                    or not isinstance(fn, dict)
                    or not isinstance(fn.get("name"), str)
                    or not fn["name"]
                    or not isinstance(fn.get("arguments"), str)
                ):
                    raise ValueError("malformed assistant tool_call")
                seen.add(call_id)
                clean_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": fn["name"],
                            "arguments": fn["arguments"],
                        },
                    }
                )
            content = _extract_text_content(message.get("content"), allow_none=True)
            normalized.append(
                {"role": "assistant", "content": content, "tool_calls": clean_calls}
            )
            pending_ids = seen
            current_results = {}
            collecting_results = True
        else:
            content = _extract_text_content(
                message.get("content"), allow_none=(role == "assistant")
            )
            if content is None:
                content = ""
            normalized.append({"role": role, "content": content})

    if collecting_results:
        if set(current_results) != pending_ids:
            raise ValueError("incomplete tool results for trailing tool_calls")
        return normalized, current_results
    return normalized, None


def normalize_tools(raw_tools, raw_choice):
    """Validate OpenAI function tools and tool_choice."""
    if raw_tools is None:
        raw_tools = []
    if not isinstance(raw_tools, list):
        raise ValueError("tools must be a list")

    client_tools = []
    seen_names = set()
    for entry in raw_tools:
        if not isinstance(entry, dict) or entry.get("type") != "function":
            raise ValueError("only function tools are supported")
        spec = entry.get("function") if isinstance(entry.get("function"), dict) else entry
        name = spec.get("name")
        if (
            not isinstance(name, str)
            or not TOOL_NAME_RE.fullmatch(name)
            or name in seen_names
        ):
            raise ValueError("invalid or duplicate tool name")
        description = spec.get("description")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise ValueError("tool description must be a string")
        schema = spec.get("parameters")
        if schema is None:
            schema = {"type": "object", "properties": {}}
        if not isinstance(schema, (dict, bool)):
            raise ValueError("tool parameters must be a JSON Schema object or boolean")
        seen_names.add(name)
        client_tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": schema,
            }
        )

    if raw_choice in (None, "auto"):
        return client_tools, None, None
    if raw_choice == "none":
        return [], None, None
    if raw_choice == "required":
        if not client_tools:
            raise ValueError("tool_choice=required requires at least one tool")
        instruction = (
            "IMPORTANT: tool_choice is 'required'. You MUST invoke at least one of the "
            "available client_tools MCP tools in this turn before giving a final answer."
        )
        return client_tools, instruction, True
    if (
        isinstance(raw_choice, dict)
        and raw_choice.get("type") == "function"
        and isinstance(raw_choice.get("function"), dict)
    ):
        target = raw_choice["function"].get("name")
        selected = [tool for tool in client_tools if tool["name"] == target]
        if not selected:
            raise ValueError("named tool_choice does not match any declared tool")
        instruction = (
            f"IMPORTANT: tool_choice requires calling '{target}'. You MUST invoke "
            f"the tool '{MCP_PREFIX}{target}' in this turn."
        )
        return selected, instruction, target
    raise ValueError("unsupported tool_choice")


def resolve_model_alias(raw_model):
    if not isinstance(raw_model, str) or not raw_model:
        raise ValueError("missing model")
    candidate = raw_model.removeprefix("openai/")
    if candidate not in MODEL_ALIASES:
        raise ValueError("unsupported model")
    return candidate, MODEL_ALIASES[candidate]


def format_arguments_json(raw_arguments):
    if raw_arguments is None:
        return "{}"
    if isinstance(raw_arguments, str):
        text = raw_arguments.strip()
        if not text:
            return "{}"
        json.loads(text)
        return raw_arguments
    return json.dumps(raw_arguments, ensure_ascii=False, separators=(",", ":"))


def extract_system_and_prompt(messages, *, stateless_tool_continuation=False):
    system_parts = []
    idx = 0
    while idx < len(messages) and messages[idx]["role"] in ("system", "developer"):
        if messages[idx]["content"]:
            system_parts.append(messages[idx]["content"])
        idx += 1
    body_messages = messages[idx:] or messages
    system_text = "\n\n".join(system_parts) if system_parts else None

    if (
        len(body_messages) == 1
        and body_messages[0]["role"] == "user"
        and "tool_calls" not in body_messages[0]
        and not stateless_tool_continuation
    ):
        prompt = body_messages[0]["content"]
        if prompt.lstrip().startswith("/"):
            prompt = "Literal user message (not a CLI command):\n" + json.dumps(
                {"role": "user", "content": prompt}, ensure_ascii=False
            )
        return system_text, prompt

    transcript = json.dumps(body_messages, ensure_ascii=False)
    if stateless_tool_continuation:
        prompt = (
            "Continue the conversation below using the supplied tool outputs "
            "(role='tool'). Do not repeat already-completed tool calls unless new "
            "information is required.\n\n" + transcript
        )
    else:
        prompt = (
            "Conversation supplied by the API client (respond to the final user message):\n"
            + transcript
        )
    return system_text, prompt


def usage_from_steps(step_usage_map):
    if not step_usage_map:
        return None
    steps = list(step_usage_map.values())
    inputs = sum(item[0] for item in steps)
    outputs = sum(item[1] for item in steps)
    usage = {
        "prompt_tokens": inputs,
        "completion_tokens": outputs,
        "total_tokens": inputs + outputs,
    }
    details = {}
    if any(item[2] is not None for item in steps):
        details["cached_tokens"] = sum(item[2] or 0 for item in steps)
    if any(item[3] is not None for item in steps):
        details["cache_write_tokens"] = sum(item[3] or 0 for item in steps)
    if details:
        usage["prompt_tokens_details"] = details
    return usage


@dataclass
class StepResult:
    finish_reason: str
    text: str
    tool_calls: list
    usage: dict | None


@dataclass
class PendingCall:
    public_id: str
    name: str
    arguments_json: str
    future: asyncio.Future


@dataclass
class ClaudeToolSession:
    session_id: str
    model_alias: str
    resolved_model: str | None
    step_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    state_changed: asyncio.Event = field(default_factory=asyncio.Event)
    pending_calls: dict[str, PendingCall] = field(default_factory=dict)
    owner_task: asyncio.Task | None = None
    parallel_tool_calls: bool = True
    required_tool: object = None
    allow_empty_text: bool = False
    emit_text: object = None
    expires_at: float = 0.0
    closed: bool = False


def _create_sdk_client(options, config):
    from claude_agent_sdk import ClaudeSDKClient
    from claude_agent_sdk.types import _configure_can_use_tool
    from litellm_claude_rpc.wire import SDKWire

    wire_options = _configure_can_use_tool(options)
    wire = SDKWire(wire_options, config)
    client = ClaudeSDKClient(options=options, transport=wire)
    return client, wire


async def run_claude_doctor(executable_override=None):
    from litellm_claude_rpc.contracts import Config
    from litellm_claude_rpc.driver import doctor

    cfg = Config(api_key="x" * 24, executable=executable_override)
    return await doctor(cfg)


async def _session_worker(
    session: ClaudeToolSession,
    *,
    prompt: str,
    system_prompt: str,
    client_tools: list[dict],
    executable_override: str | None,
):
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        SdkMcpTool,
        StreamEvent,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
        create_sdk_mcp_server,
    )
    from litellm_claude_rpc.contracts import Config
    from litellm_claude_rpc.driver import executable

    cfg = Config(api_key="x" * 24, executable=executable_override)
    temp_dir = tempfile.TemporaryDirectory(
        prefix="claude-bridge-", ignore_cleanup_errors=True
    )
    Path(temp_dir.name, ".git").mkdir(exist_ok=True)

    declared_names = {tool["name"] for tool in client_tools}
    allowed_mcp_names = {f"{MCP_PREFIX}{name}" for name in declared_names}

    queued_calls: deque[PendingCall] = deque()
    expected_batch_size = 0
    assistant_step_ready = False
    state_changed = session.state_changed
    current_text_parts: list[str] = []
    partial_parts: list[str] = []
    step_usage_map: dict[str, tuple[int, int, int | None, int | None]] = {}
    usage_complete = True
    assistant_seen = False
    last_stop_reason: str | None = None
    worker_failed = False

    sdk_tools = []
    for spec in client_tools:
        t_name = spec["name"]
        t_schema = (
            spec["inputSchema"]
            if isinstance(spec["inputSchema"], dict)
            else {"type": "object", "properties": {}}
        )

        async def _tool_handler(args, *, _name=t_name):
            if worker_failed or session.closed:
                raise RuntimeError("Session closed")
            public_id = "call_" + uuid.uuid4().hex[:24]
            fut = asyncio.get_running_loop().create_future()
            call = PendingCall(
                public_id=public_id,
                name=_name,
                arguments_json=format_arguments_json(args),
                future=fut,
            )
            queued_calls.append(call)
            state_changed.set()
            result_text = await fut
            return {"content": [{"type": "text", "text": result_text}]}

        sdk_tools.append(
            SdkMcpTool(
                name=t_name,
                description=spec["description"],
                input_schema=t_schema,
                handler=_tool_handler,
            )
        )

    async def can_use_tool(tool_name, input_data, _context):
        if tool_name in allowed_mcp_names:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="Tools disabled by gateway")

    mcp_servers = (
        {MCP_SERVER_NAME: create_sdk_mcp_server(MCP_SERVER_NAME, tools=sdk_tools)}
        if sdk_tools
        else '{"mcpServers":{}}'
    )

    options = ClaudeAgentOptions(
        cli_path=executable(cfg),
        cwd=temp_dir.name,
        model=session.resolved_model,
        system_prompt=system_prompt,
        tools=[],
        skills=[],
        allowed_tools=[],
        permission_mode="default" if sdk_tools else "dontAsk",
        can_use_tool=can_use_tool,
        mcp_servers=mcp_servers,
        strict_mcp_config=True,
        setting_sources=["user"],
        settings=json.dumps({"disableAllHooks": True, "enabledPlugins": {}}),
        hooks={},
        plugins=[],
        include_partial_messages=True,
        verbatim_prompts=True,
        extra_args={"no-session-persistence": None},
    )

    def _flush_ready_tool_batch(*, force=False):
        nonlocal expected_batch_size, assistant_step_ready, usage_complete, last_stop_reason
        if not queued_calls or session.pending_calls:
            return False
        if not force and not assistant_step_ready:
            return False
        target = (
            1
            if not session.parallel_tool_calls
            else max(1, expected_batch_size)
        )
        if not force and len(queued_calls) < target:
            return False

        selected: list[PendingCall] = []
        count = 1 if not session.parallel_tool_calls else len(queued_calls)
        for _ in range(count):
            call = queued_calls.popleft()
            session.pending_calls[call.public_id] = call
            selected.append(call)

        if session.required_tool and isinstance(session.required_tool, str):
            if not any(c.name == session.required_tool for c in selected):
                raise RuntimeError(
                    f"Claude called {[c.name for c in selected]} instead of required tool '{session.required_tool}'"
                )

        if not queued_calls:
            expected_batch_size = 0
            assistant_step_ready = False

        public_calls = [
            {
                "id": c.public_id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments_json},
            }
            for c in selected
        ]
        usage = usage_from_steps(step_usage_map) if usage_complete else None
        step_usage_map.clear()
        usage_complete = True
        last_stop_reason = None
        text = "".join(current_text_parts)
        current_text_parts.clear()
        partial_parts.clear()
        session.step_queue.put_nowait(
            StepResult("tool_calls", text, public_calls, usage)
        )
        return True

    client, wire = _create_sdk_client(options, cfg)
    reader_task = None
    try:
        await client.connect()
        await client.query(prompt)

        incoming: asyncio.Queue = asyncio.Queue()

        async def _pump_messages():
            try:
                async for msg in client.receive_response():
                    await incoming.put(("msg", msg))
                await incoming.put(("eof", None))
            except BaseException as exc:
                await incoming.put(("err", exc))

        reader_task = asyncio.create_task(_pump_messages())

        while True:
            _flush_ready_tool_batch()

            get_msg = asyncio.create_task(incoming.get())
            wait_state = asyncio.create_task(state_changed.wait())
            try:
                done, _ = await asyncio.wait(
                    {get_msg, wait_state},
                    timeout=0.05 if (queued_calls and not session.pending_calls) else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not get_msg.done():
                    get_msg.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await get_msg
                if not wait_state.done():
                    wait_state.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await wait_state

            if wait_state in done:
                state_changed.clear()
                _flush_ready_tool_batch()

            if not done and queued_calls and not session.pending_calls:
                # Fallback coalescing if an SDK mock invoked tool handler directly.
                _flush_ready_tool_batch(force=True)
                continue

            if get_msg not in done:
                continue

            kind, payload = get_msg.result()
            if kind == "err":
                raise payload
            if kind == "eof":
                raise RuntimeError("Claude SDK ended without terminal ResultMessage")

            message = payload
            if isinstance(message, StreamEvent):
                event = message.event
                if event.get("type") == "content_block_start":
                    cblock = event.get("content_block", {})
                    if cblock.get("type") in ("tool_use", "server_tool_use"):
                        bname = cblock.get("name", "")
                        if bname not in allowed_mcp_names:
                            raise RuntimeError(f"Unexpected tool block: {bname}")
                delta = event.get("delta", {})
                if (
                    event.get("type") == "message_delta"
                    and isinstance(delta, dict)
                    and delta.get("stop_reason") is not None
                ):
                    last_stop_reason = delta.get("stop_reason")
                if (
                    event.get("type") == "content_block_delta"
                    and delta.get("type") == "text_delta"
                ):
                    text = delta.get("text")
                    if not isinstance(text, str):
                        raise RuntimeError("Invalid text_delta from Claude")
                    partial_parts.append(text)
                    current_text_parts.append(text)
                    if session.emit_text is not None and text:
                        session.emit_text(text)

            elif isinstance(message, AssistantMessage):
                assistant_seen = True
                if getattr(message, "stop_reason", None) is not None:
                    last_stop_reason = message.stop_reason
                raw = message.usage if isinstance(message.usage, dict) else None
                keys = (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
                if (
                    raw is not None
                    and all(
                        raw.get(k) is None
                        or (type(raw.get(k)) is int and raw.get(k) >= 0)
                        for k in keys
                    )
                    and type(raw.get("input_tokens")) is int
                    and type(raw.get("output_tokens")) is int
                ):
                    cached_read = raw.get("cache_read_input_tokens")
                    cache_write = raw.get("cache_creation_input_tokens")
                    msg_key = message.message_id or f"step-{len(step_usage_map)}"
                    step_usage_map[msg_key] = (
                        raw["input_tokens"] + (cached_read or 0) + (cache_write or 0),
                        raw["output_tokens"],
                        cached_read,
                        cache_write,
                    )
                elif raw is not None:
                    usage_complete = False

                tool_blocks = [
                    b for b in message.content if isinstance(b, ToolUseBlock)
                ]
                for block in tool_blocks:
                    if block.name not in allowed_mcp_names:
                        raise RuntimeError(
                            f"Unexpected AssistantMessage tool use: {block.name}"
                        )

                block_text = "".join(
                    b.text for b in message.content if isinstance(b, TextBlock)
                )
                if partial_parts:
                    streamed = "".join(partial_parts)
                    if block_text.startswith(streamed):
                        rem = block_text[len(streamed) :]
                        if rem:
                            current_text_parts.append(rem)
                            if session.emit_text is not None:
                                session.emit_text(rem)
                elif block_text:
                    current_text_parts.append(block_text)
                    if session.emit_text is not None:
                        session.emit_text(block_text)
                partial_parts.clear()

                if tool_blocks:
                    expected_batch_size = len(tool_blocks)
                    assistant_step_ready = True
                    _flush_ready_tool_batch()

            elif isinstance(message, SystemMessage):
                if message.subtype == "init":
                    init_tools = message.data.get("tools") or []
                    unexpected = [
                        t for t in init_tools if t not in allowed_mcp_names
                    ]
                    if unexpected:
                        raise RuntimeError(
                            f"Unsafe Claude built-in tools active: {unexpected}"
                        )
                elif message.subtype.startswith("hook_"):
                    raise RuntimeError("Unexpected Claude hook activity")

            elif isinstance(message, ResultMessage):
                if (
                    not assistant_seen
                    or message.is_error
                    or message.subtype != "success"
                    or message.permission_denials
                    or message.terminal_reason
                    in ("aborted_streaming", "aborted_tools", "max_turns")
                ):
                    raise RuntimeError(
                        f"Claude turn failed: subtype={message.subtype} is_error={message.is_error}"
                    )
                if session.required_tool:
                    raise RuntimeError(
                        "Claude completed turn without calling required tool"
                    )
                raw_stop = (
                    getattr(message, "stop_reason", None)
                    if getattr(message, "stop_reason", None) is not None
                    else last_stop_reason
                )
                finish_reason = {
                    None: "stop",
                    "end_turn": "stop",
                    "stop_sequence": "stop",
                    "max_tokens": "length",
                    "refusal": "content_filter",
                }.get(raw_stop)
                if finish_reason is None:
                    raise RuntimeError(
                        f"Unsupported Claude stop_reason: {raw_stop!r}"
                    )
                text = "".join(current_text_parts)
                if (
                    not text
                    and not session.allow_empty_text
                    and finish_reason not in ("length", "content_filter")
                ):
                    raise RuntimeError("Claude completed turn without text")
                if not step_usage_map and isinstance(getattr(message, "usage", None), dict):
                    rm_raw = message.usage
                    if (
                        type(rm_raw.get("input_tokens")) is int
                        and type(rm_raw.get("output_tokens")) is int
                    ):
                        cr = rm_raw.get("cache_read_input_tokens")
                        cw = rm_raw.get("cache_creation_input_tokens")
                        step_usage_map["result"] = (
                            rm_raw["input_tokens"] + (cr if type(cr) is int else 0) + (cw if type(cw) is int else 0),
                            rm_raw["output_tokens"],
                            cr if type(cr) is int else None,
                            cw if type(cw) is int else None,
                        )
                usage = usage_from_steps(step_usage_map) if usage_complete else None
                session.step_queue.put_nowait(
                    StepResult(finish_reason, text, [], usage)
                )
                return
    except asyncio.CancelledError:
        worker_failed = True
        raise
    except BaseException as exc:
        worker_failed = True
        session.step_queue.put_nowait(exc)
    finally:
        session.closed = True
        for call in list(queued_calls) + list(session.pending_calls.values()):
            if not call.future.done():
                call.future.cancel()
        if reader_task is not None and not reader_task.done():
            reader_task.cancel()
            with contextlib.suppress(BaseException):
                await reader_task
        try:
            with contextlib.suppress(BaseException):
                await client.disconnect()
        finally:
            with contextlib.suppress(BaseException):
                await wire.close()
            temp_dir.cleanup()


async def _await_next_step(session: ClaudeToolSession) -> StepResult:
    item = await session.step_queue.get()
    if isinstance(item, BaseException):
        raise item
    return item


async def close_session(session: ClaudeToolSession):
    session.closed = True
    if session.owner_task is not None and not session.owner_task.done():
        session.owner_task.cancel()
        with contextlib.suppress(BaseException):
            await session.owner_task


async def start_step(
    prompt: str,
    *,
    model_alias: str,
    resolved_model: str | None,
    system_text: str | None,
    client_tools: list[dict],
    tool_instruction: str | None,
    parallel_tool_calls: bool,
    required_tool: object,
    allow_empty_text: bool,
    emit_text,
    executable_override: str | None = None,
):
    sys_parts = []
    if system_text:
        sys_parts.append(system_text)
    else:
        sys_parts.append(
            "Answer the user's request accurately. Use declared client_tools when needed."
            if client_tools
            else "Answer the user's text question."
        )
    if client_tools and not parallel_tool_calls:
        sys_parts.append("Invoke at most one tool call per assistant turn.")
    if tool_instruction:
        sys_parts.append(tool_instruction)

    session = ClaudeToolSession(
        session_id=uuid.uuid4().hex,
        model_alias=model_alias,
        resolved_model=resolved_model,
        parallel_tool_calls=parallel_tool_calls,
        required_tool=required_tool,
        allow_empty_text=allow_empty_text,
        emit_text=emit_text,
    )
    try:
        async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
            session.owner_task = asyncio.create_task(
                _session_worker(
                    session,
                    prompt=prompt,
                    system_prompt="\n\n".join(sys_parts),
                    client_tools=client_tools,
                    executable_override=executable_override,
                )
            )
            result = await _await_next_step(session)
        session.emit_text = None
        if result.finish_reason != "tool_calls":
            await close_session(session)
            return None, result
        return session, result
    except BaseException:
        session.emit_text = None
        await close_session(session)
        raise


async def resume_step(
    session: ClaudeToolSession,
    results_by_id: dict[str, str],
    emit_text,
    *,
    parallel_tool_calls: bool,
    required_tool: object,
):
    session.emit_text = emit_text
    session.parallel_tool_calls = parallel_tool_calls
    session.required_tool = required_tool
    session.allow_empty_text = True
    try:
        async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
            pending = dict(session.pending_calls)
            session.pending_calls.clear()
            session.state_changed.set()
            for public_id, call in pending.items():
                if not call.future.done():
                    call.future.set_result(results_by_id[public_id])
            result = await _await_next_step(session)
        session.emit_text = None
        if result.finish_reason != "tool_calls":
            await close_session(session)
            return None, result
        return session, result
    except BaseException:
        session.emit_text = None
        await close_session(session)
        raise


class SessionConflictError(ValueError):
    pass


class BridgeRuntime:
    """Persistent asyncio loop and bounded store for paused Claude tool sessions."""

    def __init__(self, concurrency=DEFAULT_CONCURRENCY):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run_loop, name="claude-bridge-loop", daemon=True
        )
        self.started = threading.Event()
        self.thread.start()
        self.started.wait()
        self.slots = threading.BoundedSemaphore(concurrency)
        self.lock = threading.Lock()
        self.sessions: dict[str, ClaudeToolSession] = {}
        self.call_to_session: dict[str, str] = {}
        self.closed = False

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.started.set()
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def reap_expired(self):
        now = time.monotonic()
        expired = []
        with self.lock:
            for sid, session in list(self.sessions.items()):
                if session.expires_at <= now:
                    self._pop_unlocked(sid)
                    expired.append(session)
        for session in expired:
            self.submit(close_session(session))

    def _pop_unlocked(self, session_id):
        session = self.sessions.pop(session_id, None)
        if session is not None:
            for call_id in list(session.pending_calls):
                self.call_to_session.pop(call_id, None)
        return session

    def save(self, session: ClaudeToolSession, ttl: float):
        self.reap_expired()
        evicted = None
        with self.lock:
            if self.closed:
                evicted = session
            else:
                if len(self.sessions) >= MAX_PENDING_SESSIONS:
                    oldest_id = min(
                        self.sessions, key=lambda sid: self.sessions[sid].expires_at
                    )
                    evicted = self._pop_unlocked(oldest_id)
                session.expires_at = time.monotonic() + ttl
                self.sessions[session.session_id] = session
                for call_id in session.pending_calls:
                    self.call_to_session[call_id] = session.session_id
        if evicted is not None:
            self.submit(close_session(evicted))

    def claim(self, trailing_results: dict[str, str], resolved_model: str | None):
        self.reap_expired()
        with self.lock:
            matched_sids = {
                self.call_to_session[cid]
                for cid in trailing_results
                if cid in self.call_to_session
            }
            if not matched_sids:
                return None
            if len(matched_sids) > 1:
                raise SessionConflictError("tool results reference multiple sessions")
            sid = next(iter(matched_sids))
            session = self.sessions.get(sid)
            if session is None:
                return None
            if (
                session.resolved_model != resolved_model
                or set(trailing_results) != set(session.pending_calls)
            ):
                raise SessionConflictError("tool results do not match pending tool_calls")
            return self._pop_unlocked(sid)

    def discard(self, session: ClaudeToolSession | None):
        if session is None:
            return
        with self.lock:
            self._pop_unlocked(session.session_id)
        self.submit(close_session(session))

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            active = list(self.sessions.values())
            self.sessions.clear()
            self.call_to_session.clear()
        for session in active:
            with contextlib.suppress(Exception):
                self.submit(close_session(session))
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


def get_runtime(server) -> BridgeRuntime:
    with RUNTIME_LOCK:
        runtime = getattr(server, "_bridge_runtime", None)
        if runtime is None or runtime.closed:
            concurrency = getattr(server, "concurrency", DEFAULT_CONCURRENCY)
            runtime = BridgeRuntime(concurrency=concurrency)
            server._bridge_runtime = runtime
        return runtime


def close_server_sessions(server):
    with RUNTIME_LOCK:
        runtime = getattr(server, "_bridge_runtime", None)
        server._bridge_runtime = None
    if runtime is not None:
        runtime.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def json_response(self, status, payload, *, retry_after=None):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        return hmac.compare_digest(
            self.headers.get("Authorization", ""), self.server.authorization
        )

    def do_GET(self):
        if self.path in ("/healthz", "/health/ready"):
            ready = getattr(self.server, "is_ready", True)
            if callable(ready):
                ready = ready()
            if not ready:
                self.json_response(503, {"status": "unready"}, retry_after=10)
            else:
                self.json_response(200, {"status": "ready"})
            return
        if self.path == "/v1/models":
            if not self._authorized():
                self.json_response(401, {"error": "unauthorized"})
                return
            self.json_response(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": alias,
                            "object": "model",
                            "created": 0,
                            "owned_by": "claude-code",
                        }
                        for alias in MODEL_ALIASES
                    ],
                },
            )
            return
        self.json_response(404, {"error": "not found"})

    def sse(self, payload):
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def do_POST(self):
        if not self._authorized():
            self.json_response(401, {"error": "unauthorized"})
            return
        if self.path != "/v1/chat/completions":
            self.json_response(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("invalid content length")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("expected JSON object")
            if body.get("n") not in (None, 1):
                raise ValueError("only n=1 is supported")
            model_alias, resolved_model = resolve_model_alias(body.get("model"))
            client_tools, tool_instruction, required_tool = normalize_tools(
                body.get("tools"), body.get("tool_choice")
            )
            parallel_raw = body.get("parallel_tool_calls")
            if parallel_raw is not None and type(parallel_raw) is not bool:
                raise ValueError("parallel_tool_calls must be a boolean")
            parallel_tool_calls = True if parallel_raw is None else parallel_raw
            stream_raw = body.get("stream", False)
            if type(stream_raw) is not bool:
                raise ValueError("stream must be a boolean")
            messages, trailing_results = normalize_messages(body["messages"])
        except (KeyError, TypeError, ValueError):
            self.json_response(
                400, {"error": "expected a supported model and valid chat/tool messages"}
            )
            return

        runtime = get_runtime(self.server)
        claimed_session = None
        if trailing_results is not None:
            try:
                claimed_session = runtime.claim(trailing_results, resolved_model)
            except SessionConflictError:
                self.json_response(
                    400, {"error": "tool results conflict with pending tool_calls"}
                )
                return

        if not runtime.slots.acquire(blocking=False):
            if claimed_session is not None:
                runtime.save(
                    claimed_session,
                    getattr(self.server, "session_ttl", DEFAULT_SESSION_TTL),
                )
            self.json_response(429, {"error": "Claude Code bridge is busy"})
            return

        response = {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "created": int(time.time()),
            "model": model_alias,
        }
        stream = stream_raw
        stream_options = body.get("stream_options")
        include_usage = (
            isinstance(stream_options, dict)
            and stream_options.get("include_usage") is True
        )
        started = False
        has_tool_history = any(
            "tool_calls" in msg or msg["role"] == "tool" for msg in messages
        )

        def start_sse():
            nonlocal started
            if not started:
                started = True
                self.connection.settimeout(5)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return True
            return False

        def chunk(delta, finish_reason=None):
            payload = {
                **response,
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish_reason}
                ],
            }
            if include_usage:
                payload["usage"] = None
            self.sse(payload)

        def emit_text(text):
            first = start_sse()
            delta = {"content": text}
            if first:
                delta["role"] = "assistant"
            chunk(delta)

        def emit_tool_calls(tool_calls):
            for index, call in enumerate(tool_calls):
                first = start_sse()
                header_delta = {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["function"]["name"],
                                "arguments": "",
                            },
                        }
                    ]
                }
                if first:
                    header_delta["role"] = "assistant"
                    header_delta["content"] = None
                chunk(header_delta)
                args = call["function"]["arguments"]
                for offset in range(0, len(args), 256):
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "function": {
                                        "arguments": args[offset : offset + 256]
                                    },
                                }
                            ]
                        }
                    )

        paused_session = None
        try:
            if claimed_session is not None:
                paused_session, step_result = runtime.submit(
                    resume_step(
                        claimed_session,
                        trailing_results,
                        emit_text if stream else None,
                        parallel_tool_calls=parallel_tool_calls,
                        required_tool=required_tool,
                    )
                )
            else:
                system_text, prompt = extract_system_and_prompt(
                    messages,
                    stateless_tool_continuation=(trailing_results is not None),
                )
                paused_session, step_result = runtime.submit(
                    start_step(
                        prompt,
                        model_alias=model_alias,
                        resolved_model=resolved_model,
                        system_text=system_text,
                        client_tools=client_tools,
                        tool_instruction=tool_instruction,
                        parallel_tool_calls=parallel_tool_calls,
                        required_tool=required_tool,
                        allow_empty_text=has_tool_history,
                        emit_text=emit_text if stream else None,
                        executable_override=getattr(self.server, "executable", None),
                    )
                )

            if step_result.finish_reason == "tool_calls":
                if stream:
                    emit_tool_calls(step_result.tool_calls)
                    chunk({}, "tool_calls")
                    if include_usage:
                        self.sse(
                            {
                                **response,
                                "object": "chat.completion.chunk",
                                "choices": [],
                                "usage": step_result.usage,
                            }
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    self.json_response(
                        200,
                        {
                            **response,
                            "object": "chat.completion",
                            "usage": step_result.usage,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": step_result.text or None,
                                        "tool_calls": step_result.tool_calls,
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ],
                        },
                    )
                if paused_session is not None:
                    runtime.save(
                        paused_session,
                        getattr(self.server, "session_ttl", DEFAULT_SESSION_TTL),
                    )
                    paused_session = None
            else:
                finish = step_result.finish_reason
                if stream:
                    if not started:
                        emit_text("")
                    chunk({}, finish)
                    if include_usage:
                        self.sse(
                            {
                                **response,
                                "object": "chat.completion.chunk",
                                "choices": [],
                                "usage": step_result.usage,
                            }
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    self.json_response(
                        200,
                        {
                            **response,
                            "object": "chat.completion",
                            "usage": step_result.usage,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": step_result.text,
                                    },
                                    "finish_reason": finish,
                                }
                            ],
                        },
                    )
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if paused_session is not None:
                runtime.discard(paused_session)
            return
        except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as exc:
            if paused_session is not None:
                runtime.discard(paused_session)
            print(
                f"Claude bridge turn failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            if started:
                with contextlib.suppress(OSError):
                    self.sse(
                        {
                            "error": {
                                "message": "Claude Code turn failed",
                                "type": "server_error",
                            }
                        }
                    )
            else:
                self.json_response(
                    504 if isinstance(exc, TimeoutError) else 503,
                    {"error": "Claude Code turn failed"},
                )
        finally:
            runtime.slots.release()


def main():
    settings_path = Path(sys.argv[1]).resolve()
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    port = int(settings.get("port", 14003))
    executable_override = settings.get("executable") or None
    concurrency = int(settings.get("concurrency", DEFAULT_CONCURRENCY))

    # Verify CLI version and login once on startup before accepting traffic.
    asyncio.run(run_claude_doctor(executable_override))

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.authorization = (
            "Bearer " + (settings_path.parent / "bridge-key").read_text().strip()
        )
        server.executable = executable_override
        server.concurrency = concurrency
        server.is_ready = True
        try:
            server.serve_forever()
        finally:
            close_server_sessions(server)


if __name__ == "__main__":
    main()
