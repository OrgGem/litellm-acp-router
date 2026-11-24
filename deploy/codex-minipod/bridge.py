"""Authenticated loopback OpenAI chat endpoint backed by a Mini-pod executor."""

import asyncio
import hmac
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType

MODEL_ALIASES = MappingProxyType(
    json.loads(Path(__file__).with_name("models.json").read_text(encoding="utf-8"))
)
RUNTIME_LOCK = threading.Lock()
# The executor allows 16 MiB of stdout. Leave room for RPC envelopes and JSON encoding.
APP_SERVER_FRAME_LIMIT = 24 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
STEP_TIMEOUT_SECONDS = 105
DEFAULT_SESSION_TTL = 180.0
MAX_PENDING_SESSIONS = 16
MAX_SLOTS = 16
DEFAULT_QUEUE_TIMEOUT_SECONDS = 60.0
BUSY_RETRY_AFTER_SECONDS = 5
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def parse_slot_count(value=None, *, settings=None):
    raw = value
    if raw is None:
        env_val = os.environ.get("MINIPOD_SLOTS")
        if env_val is not None and env_val.strip() != "":
            raw = env_val.strip()
        elif isinstance(settings, dict) and settings.get("slots") is not None:
            raw = settings["slots"]
        else:
            return 1
    if isinstance(raw, bool):
        raise ValueError(f"MINIPOD_SLOTS must be an integer between 1 and {MAX_SLOTS}")
    try:
        slots = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MINIPOD_SLOTS must be an integer between 1 and {MAX_SLOTS}") from exc
    if isinstance(raw, str) and ( "." in raw or int(raw, 10) != slots ):
        raise ValueError(f"MINIPOD_SLOTS must be an integer between 1 and {MAX_SLOTS}")
    if not 1 <= slots <= MAX_SLOTS:
        raise ValueError(f"MINIPOD_SLOTS must be an integer between 1 and {MAX_SLOTS}")
    return slots


def parse_queue_timeout(value=None, *, settings=None):
    raw = value
    if raw is None:
        env_val = os.environ.get("MINIPOD_QUEUE_TIMEOUT_SECONDS")
        if env_val is not None and env_val.strip() != "":
            raw = env_val.strip()
        elif isinstance(settings, dict) and settings.get("queue_timeout_seconds") is not None:
            raw = settings["queue_timeout_seconds"]
        else:
            return DEFAULT_QUEUE_TIMEOUT_SECONDS
    if isinstance(raw, bool):
        raise ValueError("MINIPOD_QUEUE_TIMEOUT_SECONDS must be a non-negative number")
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("MINIPOD_QUEUE_TIMEOUT_SECONDS must be a non-negative number") from exc
    if not math.isfinite(timeout) or timeout < 0.0:
        raise ValueError("MINIPOD_QUEUE_TIMEOUT_SECONDS must be a non-negative number")
    return timeout


def slot_environment_id(slot_id):
    return "minipod" if slot_id == 0 else f"minipod-{slot_id}"


def slot_workspace_path(base_workspace, slot_id, total_slots):
    if total_slots <= 1:
        return base_workspace
    target = Path(base_workspace) / f"slot-{slot_id}"
    target.mkdir(parents=True, exist_ok=True)
    return target


class SlotLease:
    def __init__(self, pool, slot_id):
        self._pool = pool
        self.slot_id = slot_id
        self._released = False

    def release(self):
        if not self._released:
            self._released = True
            self._pool._release(self.slot_id)


class SlotPool:
    """Bounded concurrency gate and fair FIFO queue over Mini-pod VM execution slots."""

    def __init__(self, slots=1):
        self.slots = parse_slot_count(slots)
        self._cond = threading.Condition()
        self._busy = set()
        self._paused_counts = [0] * self.slots
        self._waiters = deque()
        self._next_waiter_id = 0

    def busy_count(self):
        with self._cond:
            return len(self._busy)

    def note_paused(self, slot_id, delta):
        if not 0 <= slot_id < self.slots:
            return
        with self._cond:
            self._paused_counts[slot_id] = max(0, self._paused_counts[slot_id] + delta)

    def _try_assign_fifo(self, target_waiter_id):
        avail_set = {s for s in range(self.slots) if s not in self._busy}
        if not avail_set:
            return None
        for wid, pinned in self._waiters:
            if not avail_set:
                return None
            if pinned is not None:
                if pinned in avail_set:
                    if wid == target_waiter_id:
                        return pinned
                    avail_set.remove(pinned)
            else:
                chosen = min(avail_set, key=lambda s: (self._paused_counts[s], s))
                if wid == target_waiter_id:
                    return chosen
                avail_set.remove(chosen)
        return None

    def acquire(self, timeout=DEFAULT_QUEUE_TIMEOUT_SECONDS, *, pinned_slot=None):
        if pinned_slot is not None and not 0 <= pinned_slot < self.slots:
            pinned_slot = 0
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            self._next_waiter_id += 1
            waiter = (self._next_waiter_id, pinned_slot)
            self._waiters.append(waiter)
            try:
                while True:
                    chosen = self._try_assign_fifo(waiter[0])
                    if chosen is not None:
                        self._busy.add(chosen)
                        return SlotLease(self, chosen)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        return None
                    self._cond.wait(remaining)
            finally:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                self._cond.notify_all()

    def _release(self, slot_id):
        with self._cond:
            self._busy.discard(slot_id)
            self._cond.notify_all()


def usage_from_total(total):
    if not isinstance(total, dict):
        return None

    def count(name):
        value = total.get(name)
        return value if type(value) is int and 0 <= value <= (2**63 - 1) else None

    prompt = count("inputTokens")
    completion = count("outputTokens")
    total_tokens = count("totalTokens")
    if prompt is None or completion is None or total_tokens is None:
        return None

    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total_tokens,
    }
    prompt_details = {}
    for source, target in (
        ("cachedInputTokens", "cached_tokens"),
        ("cacheWriteInputTokens", "cache_write_tokens"),
    ):
        value = count(source)
        if value is not None and value <= prompt:
            prompt_details[target] = value
    if prompt_details:
        usage["prompt_tokens_details"] = prompt_details

    reasoning = count("reasoningOutputTokens")
    if reasoning is not None and reasoning <= completion:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return usage


def step_usage(total, prev_total):
    if not isinstance(total, dict):
        return None
    if not isinstance(prev_total, dict):
        return usage_from_total(total)
    delta = {}
    for key in (
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "cachedInputTokens",
        "cacheWriteInputTokens",
        "reasoningOutputTokens",
    ):
        cur = total.get(key)
        prev = prev_total.get(key, 0)
        if type(cur) is int and 0 <= cur <= (2**63 - 1):
            if type(prev) is int and 0 <= prev <= cur:
                delta[key] = cur - prev
            else:
                return usage_from_total(total)
    return usage_from_total(delta)


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


def text_messages(messages):
    if not isinstance(messages, list) or not messages:
        raise ValueError("expected at least one message")
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("expected message objects")
        role, content = message["role"], message.get("content")
        if (
            role not in ("system", "developer", "user", "assistant")
            or message.get("tool_calls")
            or message.get("function_call")
        ):
            raise ValueError("unsupported message role or external tool history")
        text = _extract_text_content(content)
        normalized.append({"role": role, "content": text})
    return normalized


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
    """Convert OpenAI function tools and tool_choice into Codex dynamicTools."""
    if raw_tools is None:
        raw_tools = []
    if not isinstance(raw_tools, list):
        raise ValueError("tools must be a list")

    dynamic_tools = []
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
        dynamic_tools.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "inputSchema": schema,
            }
        )

    if raw_choice in (None, "auto"):
        return dynamic_tools, None, None
    if raw_choice == "none":
        return [], None, None
    if raw_choice == "required":
        if not dynamic_tools:
            raise ValueError("tool_choice=required requires at least one tool")
        instruction = (
            "IMPORTANT: tool_choice is 'required'. You MUST invoke at least one of the "
            "declared dynamic client tools in this turn before giving a final answer."
        )
        return dynamic_tools, instruction, True
    if (
        isinstance(raw_choice, dict)
        and raw_choice.get("type") == "function"
        and isinstance(raw_choice.get("function"), dict)
    ):
        target = raw_choice["function"].get("name")
        selected = [tool for tool in dynamic_tools if tool["name"] == target]
        if not selected:
            raise ValueError("named tool_choice does not match any declared tool")
        instruction = (
            f"IMPORTANT: tool_choice requires calling '{target}'. You MUST invoke "
            f"the dynamic client tool '{target}' in this turn."
        )
        return selected, instruction, target
    raise ValueError("unsupported tool_choice")


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


class AppServer:
    def __init__(self, process, *, allow_tool_calls=False):
        self.process = process
        self.request_id = 0
        self.allow_tool_calls = allow_tool_calls

    async def send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    async def read(self):
        while True:
            try:
                line = await self.process.stdout.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                raise RuntimeError("Codex JSON-RPC frame exceeded the bridge limit") from exc
            except asyncio.IncompleteReadError as exc:
                raise RuntimeError("Codex app-server disconnected") from exc
            message = json.loads(line)
            if "id" in message:
                if "method" not in message:
                    return message
                if self.allow_tool_calls and message.get("method") == "item/tool/call":
                    return message
                # Reject every other server-initiated request, including all approvals.
                await self.send(
                    {"id": message["id"], "error": {"code": -32601, "message": "Disabled"}}
                )
            elif message.get("method") in {
                "item/agentMessage/delta",
                "turn/completed",
                "thread/tokenUsage/updated",
            }:
                return message
            elif (
                message.get("method") == "item/completed"
                and message.get("params", {}).get("item", {}).get("type") == "agentMessage"
            ):
                return message

    async def call(self, method, params, pending=None):
        self.request_id += 1
        request_id = self.request_id
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self.read()
            if message.get("id") == request_id:
                if "error" in message:
                    err = message["error"]
                    detail = str(err.get("message") or "")[:240]
                    suffix = f" ({detail})" if detail else ""
                    raise RuntimeError(
                        f"Codex RPC {method} failed: {err.get('code')}{suffix}"
                    )
                return message["result"]
            if pending is not None and "method" in message:
                pending.append(message)


class InfrastructureError(RuntimeError):
    """Raised when the Mini-pod executor or environment handshake fails."""


@dataclass
class StepResult:
    finish_reason: str
    text: str
    tool_calls: list
    usage: dict | None


@dataclass
class ToolSession:
    session_id: str
    codex_model: str
    process: object
    app: object
    thread_id: str
    turn_id: str
    slot_id: int = 0
    pending: deque = field(default_factory=deque)
    pending_calls: dict = field(default_factory=dict)
    prev_total: dict | None = None
    expires_at: float = 0.0
    closed: bool = False
    _paused_registered: bool = False
    _expiry_handle: object = None


async def close_process(process):
    try:
        process.stdin.close()
    except Exception:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
    except Exception:
        pid = getattr(process, "pid", None)
        if os.name == "nt" and type(pid) is int and pid > 0:
            try:
                kill_tree = await asyncio.create_subprocess_exec(
                    "taskkill.exe",
                    "/PID",
                    str(pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(kill_tree.wait(), timeout=2.0)
            except Exception:
                pass
        try:
            process.kill()
        except Exception:
            pass
        try:
            await process.wait()
        except Exception:
            pass


async def _has_buffered_message(app, pending):
    if pending:
        return True
    stdout = getattr(getattr(app, "process", None), "stdout", None)
    if isinstance(stdout, asyncio.StreamReader):
        if b"\n" not in stdout._buffer:
            await asyncio.sleep(0.015)
        return b"\n" in stdout._buffer
    current_events = getattr(app, "current_events", None)
    if current_events is not None:
        return bool(current_events)
    return False


def _build_public_tool_call(session, message):
    params = message.get("params") or {}
    if (
        params.get("threadId") != session.thread_id
        or params.get("turnId") != session.turn_id
        or not isinstance(params.get("tool"), str)
        or not params["tool"]
    ):
        raise RuntimeError("Codex emitted a malformed item/tool/call request")
    public_id = "call_" + uuid.uuid4().hex[:24]
    session.pending_calls[public_id] = message["id"]
    return {
        "id": public_id,
        "type": "function",
        "function": {
            "name": params["tool"],
            "arguments": format_arguments_json(params.get("arguments")),
        },
    }


async def collect_step(
    session,
    emit_text,
    *,
    parallel_tool_calls=True,
    required_tool=None,
    allow_empty_text=False,
):
    final = []
    streamed = {}
    latest_total = None

    def emit_item_text(item_id, text):
        if not text:
            return
        if item_id not in streamed and streamed:
            emit_text("\n\n")
        emit_text(text)
        streamed.setdefault(item_id, []).append(text)

    while True:
        message = (
            session.pending.popleft()
            if session.pending
            else await session.app.read()
        )
        if "id" in message and message.get("method") == "item/tool/call":
            tool_calls = [_build_public_tool_call(session, message)]
            if parallel_tool_calls:
                deferred = deque()
                while await _has_buffered_message(session.app, session.pending):
                    nxt = (
                        session.pending.popleft()
                        if session.pending
                        else await session.app.read()
                    )
                    if "id" in nxt and nxt.get("method") == "item/tool/call":
                        tool_calls.append(_build_public_tool_call(session, nxt))
                    elif (
                        nxt.get("method") == "thread/tokenUsage/updated"
                        and nxt.get("params", {}).get("threadId") == session.thread_id
                        and nxt.get("params", {}).get("turnId") == session.turn_id
                    ):
                        token_usage = nxt.get("params", {}).get("tokenUsage")
                        if isinstance(token_usage, dict) and isinstance(
                            token_usage.get("total"), dict
                        ):
                            latest_total = token_usage["total"]
                    else:
                        deferred.append(nxt)
                while deferred:
                    session.pending.appendleft(deferred.pop())
            usage = step_usage(latest_total, session.prev_total)
            if isinstance(latest_total, dict):
                session.prev_total = latest_total
            text = (
                "\n\n".join(final)
                if final
                else "\n\n".join("".join(parts) for parts in streamed.values())
            )
            return StepResult("tool_calls", text, tool_calls, usage)

        params = message.get("params", {})
        if params.get("threadId") != session.thread_id:
            continue
        event_turn_id = params.get("turnId") or params.get("turn", {}).get("id")
        if event_turn_id and event_turn_id != session.turn_id:
            continue
        method = message.get("method")
        if method == "item/agentMessage/delta" and emit_text is not None:
            emit_item_text(params["itemId"], params["delta"])
        elif (
            method == "item/completed"
            and params.get("item", {}).get("type") == "agentMessage"
        ):
            item = params["item"]
            text = item["text"]
            if text:
                final.append(text)
            if emit_text is not None:
                prefix = "".join(streamed.get(item["id"], ()))
                if text.startswith(prefix):
                    emit_item_text(item["id"], text[len(prefix) :])
        elif method == "thread/tokenUsage/updated" and params.get("turnId") == session.turn_id:
            token_usage = params.get("tokenUsage")
            if isinstance(token_usage, dict) and isinstance(
                token_usage.get("total"), dict
            ):
                latest_total = token_usage["total"]
        elif method == "turn/completed":
            turn = params["turn"]
            if required_tool:
                raise RuntimeError("Codex completed turn without calling required tool")
            if turn["status"] != "completed" or (
                not allow_empty_text and not (final or streamed)
            ):
                error = turn.get("error") or {}
                print(
                    f"Codex turn {turn['status']}: {error.get('codexErrorInfo', 'no response')}",
                    file=sys.stderr,
                    flush=True,
                )
                raise RuntimeError("Codex turn did not produce a response")
            usage = step_usage(latest_total, session.prev_total)
            return StepResult("stop", "\n\n".join(final), [], usage)


async def collect_turn(app, thread_id, turn_id, pending, emit_text):
    session = ToolSession(
        session_id="stateless",
        codex_model="",
        process=None,
        app=app,
        thread_id=thread_id,
        turn_id=turn_id,
        pending=pending,
    )
    result = await collect_step(session, emit_text)
    return result.text, result.usage


async def _open_codex_session_once(
    prompt,
    profile,
    workspace,
    codex,
    *,
    model,
    environment_id="minipod",
    dynamic_tools=None,
    developer_instructions=None,
):
    allow_tools = bool(dynamic_tools)
    process = await asyncio.create_subprocess_exec(
        codex,
        "app-server",
        "--listen",
        "stdio://",
        "--strict-config",
        cwd=workspace,
        env={**os.environ, "CODEX_HOME": str(profile)},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=APP_SERVER_FRAME_LIMIT,
    )
    try:
        app = (
            AppServer(process, allow_tool_calls=True)
            if allow_tools
            else AppServer(process)
        )
        await app.call(
            "initialize",
            {
                "clientInfo": {"name": "minipod_rpc_bridge", "version": "1"},
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [
                        "item/commandExecution/outputDelta",
                        "item/fileChange/outputDelta",
                        "turn/diff/updated",
                    ],
                },
            },
        )
        await app.send({"method": "initialized"})
        try:
            info = await app.call("environment/info", {"environmentId": environment_id})
        except RuntimeError as exc:
            raise InfrastructureError(str(exc)) from exc
        if info.get("cwd") != "file:///workspace":
            raise InfrastructureError("Unexpected Mini-pod workspace")
        # Probe a harmless known host file. No turn starts if the host FS is available.
        await app.send(
            {
                "id": 99999,
                "method": "fs/readFile",
                "params": {"path": str(profile / "environments.toml")},
            }
        )
        while (reply := await app.read()).get("id") != 99999:
            pass
        if reply.get("error", {}).get("message") != "local filesystem is not configured":
            raise RuntimeError("Host filesystem RPC is not disabled")
        thread_params = {
            "ephemeral": True,
            "approvalPolicy": "never",
            "model": model,
            # The externally selected Linux VM is the execution boundary.
            "sandbox": "danger-full-access",
            "environments": [{"environmentId": environment_id, "cwd": "/workspace"}],
        }
        if dynamic_tools:
            thread_params["dynamicTools"] = dynamic_tools
        if developer_instructions:
            thread_params["developerInstructions"] = developer_instructions
        start = await app.call("thread/start", thread_params)
        if start.get("approvalPolicy") != "never":
            raise RuntimeError("Unexpected Codex approval policy")
        if start.get("model") != model:
            raise RuntimeError("Codex selected an unexpected model")
        thread_id = start["thread"]["id"]
        pending = deque()
        turn = await app.call(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
            pending,
        )
        return ToolSession(
            session_id=uuid.uuid4().hex,
            codex_model=model,
            process=process,
            app=app,
            thread_id=thread_id,
            turn_id=turn["turn"]["id"],
            pending=pending,
        )
    except BaseException:
        await close_process(process)
        raise


async def _open_codex_session(
    prompt,
    profile,
    workspace,
    codex,
    *,
    model,
    environment_id="minipod",
    dynamic_tools=None,
    developer_instructions=None,
):
    for attempt in range(2):
        try:
            return await _open_codex_session_once(
                prompt,
                profile,
                workspace,
                codex,
                model=model,
                environment_id=environment_id,
                dynamic_tools=dynamic_tools,
                developer_instructions=developer_instructions,
            )
        except InfrastructureError:
            if attempt == 1:
                raise


async def run_turn(
    prompt,
    profile,
    workspace,
    codex,
    emit_text=None,
    *,
    model,
    environment_id="minipod",
):
    async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
        session = await _open_codex_session(
            prompt,
            profile,
            workspace,
            codex,
            model=model,
            environment_id=environment_id,
        )
        try:
            return await collect_turn(
                session.app, session.thread_id, session.turn_id, session.pending, emit_text
            )
        finally:
            await close_process(session.process)


async def start_tool_step(
    prompt,
    profile,
    workspace,
    codex,
    emit_text,
    *,
    model,
    environment_id="minipod",
    dynamic_tools,
    developer_instructions,
    parallel_tool_calls,
    required_tool,
    allow_empty_text=False,
):
    session = None
    try:
        async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
            session = await _open_codex_session(
                prompt,
                profile,
                workspace,
                codex,
                model=model,
                environment_id=environment_id,
                dynamic_tools=dynamic_tools,
                developer_instructions=developer_instructions,
            )
            result = await collect_step(
                session,
                emit_text,
                parallel_tool_calls=parallel_tool_calls,
                required_tool=required_tool,
                allow_empty_text=allow_empty_text,
            )
        if result.finish_reason != "tool_calls":
            await close_process(session.process)
            session.closed = True
            return None, result
        return session, result
    except BaseException:
        if session is not None and not session.closed:
            await close_process(session.process)
            session.closed = True
        raise


async def resume_tool_step(
    session,
    results_by_id,
    emit_text,
    *,
    parallel_tool_calls,
    required_tool,
):
    try:
        async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
            pending_calls = dict(session.pending_calls)
            session.pending_calls.clear()
            for public_id, rpc_id in pending_calls.items():
                await session.app.send(
                    {
                        "id": rpc_id,
                        "result": {
                            "contentItems": [
                                {"type": "inputText", "text": results_by_id[public_id]}
                            ],
                            "success": True,
                        },
                    }
                )
            result = await collect_step(
                session,
                emit_text,
                parallel_tool_calls=parallel_tool_calls,
                required_tool=required_tool,
                allow_empty_text=True,
            )
        if result.finish_reason != "tool_calls":
            await close_process(session.process)
            session.closed = True
            return None, result
        return session, result
    except BaseException:
        if not session.closed:
            await close_process(session.process)
            session.closed = True
        raise


class SessionConflictError(ValueError):
    pass


class BridgeRuntime:
    """Persistent asyncio loop, slot pool, and bounded store for paused tool-call sessions."""

    def __init__(self, *, slots=1):
        self.slot_pool = SlotPool(slots)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="minipod-bridge-loop", daemon=True
        )
        self.thread.start()
        self.lock = threading.Lock()
        self.sessions = {}
        self.call_to_session = {}
        self._close_futures = set()
        self.closed = False

    def ensure_slots(self, slots):
        desired = parse_slot_count(slots)
        with self.lock:
            if (
                self.slot_pool.slots != desired
                and not self.sessions
                and self.slot_pool.busy_count() == 0
            ):
                self.slot_pool = SlotPool(desired)

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def _schedule_close_process(self, session):
        if session is None or session.closed:
            return
        session.closed = True
        try:
            session.process.stdin.close()
        except Exception:
            pass
        try:
            fut = asyncio.run_coroutine_threadsafe(
                close_process(session.process), self.loop
            )
            self._close_futures.add(fut)
            fut.add_done_callback(self._close_futures.discard)
        except RuntimeError:
            pass

    def _cancel_expiry_timer(self, session):
        handle = session._expiry_handle
        session._expiry_handle = None
        if handle is not None:
            try:
                self.loop.call_soon_threadsafe(handle.cancel)
            except RuntimeError:
                pass

    def _on_ttl_timer(self, session_id):
        expired_session = None
        with self.lock:
            session = self.sessions.get(session_id)
            if session is not None and session.expires_at <= time.monotonic():
                expired_session = self._pop_unlocked(session_id)
        if expired_session is not None:
            self._schedule_close_process(expired_session)

    def reap_expired(self):
        now = time.monotonic()
        expired = []
        with self.lock:
            for sid, session in list(self.sessions.items()):
                if session.expires_at <= now:
                    self._pop_unlocked(sid)
                    expired.append(session)
        for session in expired:
            self._schedule_close_process(session)

    def _pop_unlocked(self, session_id):
        session = self.sessions.pop(session_id, None)
        if session is not None:
            self._cancel_expiry_timer(session)
            if session._paused_registered:
                session._paused_registered = False
                self.slot_pool.note_paused(session.slot_id, -1)
            for call_id in list(session.pending_calls):
                self.call_to_session.pop(call_id, None)
        return session

    def save(self, session, ttl):
        self.reap_expired()
        evicted = None
        delay = max(0.0, float(ttl))
        with self.lock:
            if self.closed:
                evicted = session
            else:
                if len(self.sessions) >= MAX_PENDING_SESSIONS:
                    oldest_id = min(
                        self.sessions, key=lambda sid: self.sessions[sid].expires_at
                    )
                    evicted = self._pop_unlocked(oldest_id)
                session.expires_at = time.monotonic() + delay
                if not session._paused_registered:
                    session._paused_registered = True
                    self.slot_pool.note_paused(session.slot_id, 1)
                self.sessions[session.session_id] = session
                for call_id in session.pending_calls:
                    self.call_to_session[call_id] = session.session_id
                sid = session.session_id
                try:
                    self.loop.call_soon_threadsafe(
                        self._arm_timer_in_loop, session, sid, delay
                    )
                except RuntimeError:
                    pass
        if evicted is not None:
            self._schedule_close_process(evicted)

    def _arm_timer_in_loop(self, session, session_id, delay):
        if not self.closed and not session.closed:
            session._expiry_handle = self.loop.call_later(
                delay, self._on_ttl_timer, session_id
            )

    def claim(self, trailing_results, codex_model):
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
                session.codex_model != codex_model
                or set(trailing_results) != set(session.pending_calls)
            ):
                raise SessionConflictError("tool results do not match pending tool_calls")
            return self._pop_unlocked(sid)

    def discard(self, session):
        if session is None:
            return
        with self.lock:
            self._pop_unlocked(session.session_id)
        self._schedule_close_process(session)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            active = [
                self._pop_unlocked(sid) for sid in list(self.sessions)
            ]
        for session in active:
            self._schedule_close_process(session)
        for fut in list(self._close_futures):
            try:
                fut.result(timeout=2)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


def resolve_server_slots(server):
    explicit = getattr(server, "slots", None)
    settings = getattr(server, "settings", None)
    return parse_slot_count(explicit, settings=settings)


def resolve_server_queue_timeout(server):
    explicit = getattr(server, "queue_timeout", None)
    settings = getattr(server, "settings", None)
    return parse_queue_timeout(explicit, settings=settings)


def get_runtime(server):
    slots = resolve_server_slots(server)
    with RUNTIME_LOCK:
        runtime = getattr(server, "_bridge_runtime", None)
        if runtime is None or runtime.closed:
            runtime = BridgeRuntime(slots=slots)
            server._bridge_runtime = runtime
        else:
            runtime.ensure_slots(slots)
        return runtime


def close_server_sessions(server):
    with RUNTIME_LOCK:
        runtime = getattr(server, "_bridge_runtime", None)
        server._bridge_runtime = None
    if runtime is not None:
        runtime.close()


def build_prompt(messages, *, stateless_tool_continuation=False):
    if (
        len(messages) == 1
        and messages[0]["role"] == "user"
        and "tool_calls" not in messages[0]
    ):
        return messages[0]["content"]
    transcript = json.dumps(messages, ensure_ascii=False)
    if stateless_tool_continuation:
        return (
            "Continue the conversation below using the supplied tool outputs "
            "(role='tool'). Do not repeat already-completed tool calls unless new "
            "information is required.\n\n" + transcript
        )
    return transcript


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

    def do_GET(self):
        if self.path != "/healthz":
            self.json_response(404, {"error": "not found"})
            return
        manager = getattr(self.server, "manager", None)
        if manager is not None and not manager.is_ready():
            self.json_response(503, {"status": "initializing"}, retry_after=10)
        else:
            self.json_response(200, {"status": "ready"})

    def sse(self, payload):
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def do_POST(self):
        if not hmac.compare_digest(
            self.headers.get("Authorization", ""), self.server.authorization
        ):
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
            model_alias = body.get("model")
            if not isinstance(model_alias, str) or model_alias not in MODEL_ALIASES:
                self.json_response(400, {"error": "unsupported model"})
                return
            codex_model = MODEL_ALIASES[model_alias]
            dynamic_tools, tool_instruction, required_tool = normalize_tools(
                body.get("tools"), body.get("tool_choice")
            )
            parallel_raw = body.get("parallel_tool_calls")
            if parallel_raw is not None and type(parallel_raw) is not bool:
                raise ValueError("parallel_tool_calls must be a boolean")
            parallel_tool_calls = True if parallel_raw is None else parallel_raw
            messages, trailing_results = normalize_messages(body["messages"])
        except (KeyError, TypeError, ValueError):
            self.json_response(
                400, {"error": "expected a supported model and valid chat/tool messages"}
            )
            return

        manager = getattr(self.server, "manager", None)
        if manager is not None and not manager.wait_ready(timeout=5):
            self.json_response(
                503, {"error": "Mini-pod executor is initializing"}, retry_after=10
            )
            return

        runtime = get_runtime(self.server)
        claimed_session = None
        if trailing_results is not None:
            try:
                claimed_session = runtime.claim(trailing_results, codex_model)
            except SessionConflictError:
                self.json_response(
                    400, {"error": "tool results conflict with pending tool_calls"}
                )
                return

        queue_timeout = resolve_server_queue_timeout(self.server)
        pinned_slot = claimed_session.slot_id if claimed_session is not None else None
        lease = runtime.slot_pool.acquire(timeout=queue_timeout, pinned_slot=pinned_slot)
        if lease is None:
            if claimed_session is not None:
                runtime.save(
                    claimed_session,
                    getattr(self.server, "session_ttl", DEFAULT_SESSION_TTL),
                )
            print(
                f"Bridge slots busy ({runtime.slot_pool.busy_count()}/{runtime.slot_pool.slots} active, waited {queue_timeout:.2f}s)",
                file=sys.stderr,
                flush=True,
            )
            self.json_response(
                429,
                {"error": "Mini-pod workspace is busy"},
                retry_after=BUSY_RETRY_AFTER_SECONDS,
            )
            return

        response = {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "created": int(time.time()),
            "model": model_alias,
        }
        stream = body.get("stream") is True
        stream_options = body.get("stream_options")
        include_usage = (
            isinstance(stream_options, dict)
            and stream_options.get("include_usage") is True
        )
        started = False
        has_tool_history = any(
            "tool_calls" in msg or msg["role"] == "tool" for msg in messages
        )
        pure_text_turn = (
            not dynamic_tools and trailing_results is None and not has_tool_history
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

        slot_id = lease.slot_id
        env_id = slot_environment_id(slot_id)
        slot_workspace = slot_workspace_path(
            self.server.workspace, slot_id, runtime.slot_pool.slots
        )
        env_kwargs = {} if env_id == "minipod" else {"environment_id": env_id}
        paused_session = None
        saved_session = None
        try:
            if pure_text_turn:
                prompt = build_prompt(messages)
                answer, usage = runtime.submit(
                    run_turn(
                        prompt,
                        self.server.profile,
                        slot_workspace,
                        self.server.codex,
                        emit_text if stream else None,
                        model=codex_model,
                        **env_kwargs,
                    )
                )
                step_result = StepResult("stop", answer, [], usage)
            elif claimed_session is not None:
                paused_session, step_result = runtime.submit(
                    resume_tool_step(
                        claimed_session,
                        trailing_results,
                        emit_text if stream else None,
                        parallel_tool_calls=parallel_tool_calls,
                        required_tool=required_tool,
                    )
                )
            else:
                prompt = build_prompt(
                    messages,
                    stateless_tool_continuation=(trailing_results is not None),
                )
                paused_session, step_result = runtime.submit(
                    start_tool_step(
                        prompt,
                        self.server.profile,
                        slot_workspace,
                        self.server.codex,
                        emit_text if stream else None,
                        model=codex_model,
                        dynamic_tools=dynamic_tools,
                        developer_instructions=tool_instruction,
                        parallel_tool_calls=parallel_tool_calls,
                        required_tool=required_tool,
                        allow_empty_text=has_tool_history,
                        **env_kwargs,
                    )
                )

            if step_result.finish_reason == "tool_calls":
                if paused_session is not None:
                    paused_session.slot_id = slot_id
                    runtime.save(
                        paused_session,
                        getattr(self.server, "session_ttl", DEFAULT_SESSION_TTL),
                    )
                    saved_session = paused_session
                    paused_session = None
                # Release the slot BEFORE delivering HTTP 200 / [DONE] so back-to-back callers never hit 429.
                lease.release()
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
            else:
                lease.release()
                if stream:
                    if not started:
                        emit_text("")
                    chunk({}, "stop")
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
                                    "finish_reason": "stop",
                                }
                            ],
                        },
                    )
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if paused_session is not None:
                runtime.discard(paused_session)
            if saved_session is not None:
                runtime.discard(saved_session)
            return
        except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as exc:
            if paused_session is not None:
                runtime.discard(paused_session)
            if saved_session is not None:
                runtime.discard(saved_session)
            print(
                f"Bridge turn failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            if manager is not None and isinstance(exc, (InfrastructureError, OSError)):
                manager.kick()
            if started:
                try:
                    self.sse(
                        {
                            "error": {
                                "message": "Codex or Mini-pod failed",
                                "type": "server_error",
                            }
                        }
                    )
                except OSError:
                    pass
            else:
                self.json_response(
                    504 if isinstance(exc, TimeoutError) else 503,
                    {"error": "Codex or Mini-pod failed"},
                )
        finally:
            lease.release()


if __name__ == "__main__":
    from sandbox_manager import SandboxManager

    settings_path = Path(sys.argv[1]).resolve()
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    slots = parse_slot_count(settings=settings)
    queue_timeout = parse_queue_timeout(settings=settings)
    with ThreadingHTTPServer(("127.0.0.1", 14002), Handler) as server:
        server.authorization = (
            "Bearer " + (settings_path.parent / "bridge-key").read_text().strip()
        )
        server.settings = settings
        server.slots = slots
        server.queue_timeout = queue_timeout
        server.profile = Path(settings["profile"])
        server.workspace = Path(settings["host_cwd"])
        server.codex = settings["codex"]
        server.manager = SandboxManager(settings_path, server.profile, slots=slots)
        server.manager.start()
        try:
            server.serve_forever()
        finally:
            close_server_sessions(server)
            server.manager.stop()
