"""Authenticated loopback OpenAI chat + tool-calling endpoint backed by Antigravity ACP."""

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import shutil
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
AGY_RPC_SRC = REPO_ROOT / "litellm-antigravity-rpc" / "src"
if str(AGY_RPC_SRC) not in sys.path:
    sys.path.insert(0, str(AGY_RPC_SRC))

MCP_STDIO_SCRIPT = Path(__file__).with_name("mcp_stdio_bridge.py").resolve()
MODEL_ALIASES = MappingProxyType(
    json.loads(Path(__file__).with_name("models.json").read_text(encoding="utf-8"))
)
RUNTIME_LOCK = threading.Lock()
MAX_REQUEST_BYTES = 4 * 1024 * 1024
FRAME_LIMIT = 4 * 1024 * 1024
STEP_TIMEOUT_SECONDS = 105
CLEANUP_TIMEOUT_SECONDS = 8.0
DEFAULT_SESSION_TTL = 180.0
MAX_PENDING_SESSIONS = 16
DEFAULT_CONCURRENCY = 4
MCP_SERVER_NAME = "client_tools"
AGY_MCP_PREFIX = f"mcp_{MCP_SERVER_NAME}_"
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")

TEXT_ONLY_AGENT = """---
name: contract-text-only
description: Text-only protocol provider
tools: []
mainAgent: true
subagent: false
mcpServers: []
skills: []
plugins: []
commandExecutionPolicy: off
---
Respond accurately to the user's request. Never invoke local tools or commands.
"""


def build_client_tools_agent(tool_names: list[str]) -> str:
    tool_lines = "\n".join(f"  - {AGY_MCP_PREFIX}{name}" for name in tool_names)
    return (
        "---\n"
        "name: contract-client-tools\n"
        "description: Client tool bridge provider\n"
        f"tools:\n{tool_lines}\n"
        "mainAgent: true\n"
        "subagent: false\n"
        "mcpServers:\n"
        f"  - {MCP_SERVER_NAME}\n"
        "skills: []\n"
        "plugins: []\n"
        "commandExecutionPolicy: off\n"
        "---\n"
        "Use available MCP client_tools when needed to answer the user's request. "
        "Never access local files or commands.\n"
    )


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
            "declared client_tools MCP tools in this turn before giving a final answer."
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
            f"the tool '{AGY_MCP_PREFIX}{target}' in this turn."
        )
        return selected, instruction, target
    raise ValueError("unsupported tool_choice")


def resolve_model_alias(raw_model):
    if not isinstance(raw_model, str) or not raw_model:
        raise ValueError("missing model")
    candidate = raw_model.removeprefix("openai/").removeprefix("antigravity_rpc/")
    if candidate not in MODEL_ALIASES:
        raise ValueError("unsupported model")
    return candidate, MODEL_ALIASES[candidate]


def clean_tool_arguments(raw_arguments, *, keep_tool_summary: bool) -> str:
    if raw_arguments is None:
        return "{}"
    if isinstance(raw_arguments, str):
        text = raw_arguments.strip()
        if not text:
            return "{}"
        parsed = json.loads(text)
        if isinstance(parsed, dict) and not keep_tool_summary and "toolSummary" in parsed:
            parsed = {k: v for k, v in parsed.items() if k != "toolSummary"}
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        return raw_arguments
    if isinstance(raw_arguments, dict) and not keep_tool_summary and "toolSummary" in raw_arguments:
        raw_arguments = {k: v for k, v in raw_arguments.items() if k != "toolSummary"}
    return json.dumps(raw_arguments, ensure_ascii=False, separators=(",", ":"))


def parse_agy_usage(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None

    def count(name):
        val = raw.get(name)
        return val if type(val) is int and 0 <= val <= (2**63 - 1) else None

    inp = count("inputTokens")
    out = count("outputTokens")
    total = count("totalTokens")
    if inp is None or out is None:
        return None

    cached_read = count("cachedReadTokens")
    if cached_read is None:
        cached_read = count("cachedInputTokens")
    cache_write = count("cachedWriteTokens")
    if cache_write is None:
        cache_write = count("cacheWriteInputTokens")
    thought = count("thoughtTokens")
    if thought is None:
        thought = count("reasoningOutputTokens")

    extra_cache = (cached_read or 0) + (cache_write or 0)
    if total is not None and total == inp + extra_cache + out:
        prompt_tokens = inp + extra_cache
    else:
        prompt_tokens = inp
        if total is None:
            total = prompt_tokens + out

    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": out,
        "total_tokens": total,
    }
    prompt_details = {}
    if cached_read is not None and cached_read <= prompt_tokens:
        prompt_details["cached_tokens"] = cached_read
    if cache_write is not None and cache_write <= prompt_tokens:
        prompt_details["cache_write_tokens"] = cache_write
    if prompt_details:
        usage["prompt_tokens_details"] = prompt_details

    if thought is not None and thought <= out:
        usage["completion_tokens_details"] = {"reasoning_tokens": thought}
    return usage


def build_prompt(messages, *, extra_instructions: list[str], stateless_tool_continuation=False) -> str:
    prefix = "\n\n".join(part for part in extra_instructions if part)
    if (
        len(messages) == 1
        and messages[0]["role"] == "user"
        and "tool_calls" not in messages[0]
        and not stateless_tool_continuation
    ):
        user_text = messages[0]["content"]
        if user_text.lstrip().startswith("/"):
            user_text = "Literal user message (not a CLI command):\n" + json.dumps(
                {"role": "user", "content": user_text}, ensure_ascii=False
            )
        return f"{prefix}\n\n{user_text}" if prefix else user_text

    transcript = json.dumps(messages, ensure_ascii=False)
    if stateless_tool_continuation:
        core = (
            "Continue the conversation below using the supplied tool outputs "
            "(role='tool'). Do not repeat already-completed tool calls unless new "
            "information is required.\n\n" + transcript
        )
    else:
        core = (
            "Conversation supplied by the API client (respond to the final user message):\n"
            + transcript
        )
    return f"{prefix}\n\n{core}" if prefix else core


def resolve_agy_executable(override: str | None = None) -> str:
    if override:
        return override
    env_bin = os.environ.get("ANTIGRAVITY_RPC_EXECUTABLE")
    if env_bin:
        return env_bin
    for name in ("agy.exe", "agy"):
        found = shutil.which(name)
        if found and not found.lower().endswith((".bat", ".cmd")):
            return found
    raise RuntimeError("Could not find native agy executable on PATH")


async def run_agy_doctor(executable_override: str | None = None):
    from litellm_antigravity_rpc.config import is_supported_cli_version

    exe = resolve_agy_executable(executable_override)
    proc = await asyncio.create_subprocess_exec(
        exe,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    version_text = out.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 or not is_supported_cli_version(version_text):
        raise RuntimeError(f"Unsupported Antigravity CLI version: {version_text!r}")
    return version_text


class IsolatedAgyProfile:
    def __init__(self, client_tools: list[dict], callback_port: int, callback_token: str):
        self.temp = tempfile.TemporaryDirectory(
            prefix="agy-bridge-", ignore_cleanup_errors=True
        )
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.gemini = self.root / "gemini"
        (self.gemini / "config").mkdir(parents=True)
        (self.workspace / ".git").mkdir(parents=True)

        self.client_tools = client_tools
        self.mcp_servers_param = []
        agents_dir = self.workspace / ".agents" / "agents"
        agents_dir.mkdir(parents=True)

        if client_tools:
            self.agent_name = "contract-client-tools"
            tools_json_path = self.root / "client_tools.json"
            tools_json_path.write_text(
                json.dumps(client_tools, ensure_ascii=False), encoding="utf-8"
            )
            (agents_dir / "contract-client-tools.md").write_text(
                build_client_tools_agent([t["name"] for t in client_tools]),
                encoding="utf-8",
            )
            mcp_args = ["-u", str(MCP_STDIO_SCRIPT), str(tools_json_path), str(callback_port)]
            (self.gemini / "config" / "mcp_config.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            MCP_SERVER_NAME: {
                                "command": sys.executable,
                                "args": mcp_args,
                                "env": {"AGY_BRIDGE_TOKEN": callback_token},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.mcp_servers_param = [
                {
                    "name": MCP_SERVER_NAME,
                    "command": sys.executable,
                    "args": mcp_args,
                    "env": [{"name": "AGY_BRIDGE_TOKEN", "value": callback_token}],
                }
            ]
        else:
            self.agent_name = "contract-text-only"
            (agents_dir / "contract-text-only.md").write_text(
                TEXT_ONLY_AGENT, encoding="utf-8"
            )

        self.env = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "SYSTEMROOT",
                "WINDIR",
                "PATH",
                "PATHEXT",
                "COMSPEC",
                "SYSTEMDRIVE",
                "NUMBER_OF_PROCESSORS",
                "LANG",
                "LC_ALL",
            }
        }
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
            folder = self.root / key.lower()
            folder.mkdir()
            self.env[key] = str(folder)

    def command(self, executable: str) -> list[str]:
        return [
            executable,
            "acp",
            "--agent",
            self.agent_name,
            "--gemini-dir",
            str(self.gemini),
            "--app-data-dir",
            "contract-probe",
            "--include-default-customizations=false",
            "--disable-telemetry",
            "--log-file",
            os.devnull,
        ]

    def close(self):
        self.temp.cleanup()


class BridgeACPClient:
    """Stdio JSON-RPC client for agy acp with strict MCP-only permission gating."""

    def __init__(self, *, allowed_mcp_names: set[str], on_update, on_permission_approved):
        self.allowed_mcp_names = allowed_mcp_names
        self.on_update = on_update
        self.on_permission_approved = on_permission_approved
        self.pending: dict[int, asyncio.Future] = {}
        self.counter = 0
        self.proc = None
        self.tree = None
        self.reader_task = None
        self.closing = False
        self.failure = asyncio.get_running_loop().create_future()
        self.write_lock = asyncio.Lock()

    async def start(self, command: list[str], cwd: Path, env: dict[str, str]):
        from litellm_antigravity_rpc.process import ProcessTree

        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=FRAME_LIMIT,
                start_new_session=os.name != "nt",
                creationflags=0x00000004 if os.name == "nt" else 0,
            )
        )
        try:
            self.proc = await asyncio.shield(spawn)
            self.tree = ProcessTree(self.proc.pid)
            self.tree.resume()
        except BaseException:
            self.proc = await spawn
            await self.close()
            raise
        self.reader_task = asyncio.create_task(self._read_loop())

    def fail(self, error: BaseException):
        if not self.failure.done():
            self.failure.set_result(error)
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(error)

    async def send(self, payload: dict):
        raw = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
        if len(raw) > FRAME_LIMIT:
            raise RuntimeError("ACP frame exceeded limit")
        async with self.write_lock:
            self.proc.stdin.write(raw)
            await self.proc.stdin.drain()

    async def request(self, method: str, params: dict) -> dict:
        if self.failure.done():
            raise self.failure.result()
        self.counter += 1
        req_id = self.counter
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        try:
            await self.send(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            )
            return await fut
        finally:
            self.pending.pop(req_id, None)
            if fut.done() and not fut.cancelled():
                fut.exception()

    async def notify(self, method: str, params: dict):
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _handle_server_request(self, message: dict):
        req_id = message["id"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if message.get("method") == "session/request_permission":
            tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            tool_name = tool_call.get("name")
            options = params.get("options") if isinstance(params.get("options"), list) else []
            if isinstance(tool_name, str) and tool_name in self.allowed_mcp_names:
                allow = next(
                    (
                        opt
                        for opt in options
                        if isinstance(opt, dict)
                        and opt.get("kind") in ("allow_once", "allow_always")
                        and isinstance(opt.get("optionId"), str)
                    ),
                    None,
                )
                if allow is not None:
                    self.on_permission_approved(tool_name)
                    await self.send(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "outcome": {
                                    "outcome": "selected",
                                    "optionId": allow["optionId"],
                                }
                            },
                        }
                    )
                    return

            reject = next(
                (
                    opt
                    for opt in options
                    if isinstance(opt, dict)
                    and opt.get("kind") == "reject_once"
                    and isinstance(opt.get("optionId"), str)
                ),
                None,
            )
            outcome = (
                {"outcome": "selected", "optionId": reject["optionId"]}
                if reject
                else {"outcome": "cancelled"}
            )
            await self.send(
                {"jsonrpc": "2.0", "id": req_id, "result": {"outcome": outcome}}
            )
            raise RuntimeError(f"Rejected unauthorized tool permission: {tool_name!r}")

        await self.send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": "Unsupported client capability"},
            }
        )
        raise RuntimeError(f"Unsupported ACP server request: {message.get('method')}")

    async def _read_loop(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    if not self.closing:
                        raise RuntimeError("Antigravity ACP closed stdout unexpectedly")
                    return
                if not line.endswith(b"\n") or len(line) > FRAME_LIMIT:
                    raise RuntimeError("Invalid ACP frame")
                message = json.loads(line.decode("utf-8"))
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise RuntimeError("Malformed JSON-RPC message")
                if "method" in message:
                    if "id" in message:
                        await self._handle_server_request(message)
                    else:
                        self.on_update(message)
                else:
                    req_id = message.get("id")
                    fut = self.pending.get(req_id)
                    if fut is None or fut.done():
                        raise RuntimeError("Unexpected ACP response id")
                    if "error" in message:
                        err = message["error"]
                        code = err.get("code") if isinstance(err, dict) else err
                        fut.set_exception(RuntimeError(f"ACP error: {code}"))
                    elif isinstance(message.get("result"), dict):
                        fut.set_result(message["result"])
                    else:
                        raise RuntimeError("Invalid ACP result")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.fail(exc)

    async def close(self):
        from litellm_antigravity_rpc.process import stop_process

        self.closing = True
        if self.reader_task:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        for fut in self.pending.values():
            if not fut.done():
                fut.cancel()
        if self.proc:
            await stop_process(self.proc, self.tree, CLEANUP_TIMEOUT_SECONDS)
            self.proc = None


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
class AgyToolSession:
    session_id: str
    model_alias: str
    model_spec: dict
    callback_token: str
    keep_tool_summary: set[str] = field(default_factory=set)
    declared_names: set[str] = field(default_factory=set)
    queued_calls: deque[PendingCall] = field(default_factory=deque)
    pending_calls: dict[str, PendingCall] = field(default_factory=dict)
    step_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    state_changed: asyncio.Event = field(default_factory=asyncio.Event)
    approved_inflight: int = 0
    last_tool_signal: float = 0.0
    owner_task: asyncio.Task | None = None
    parallel_tool_calls: bool = True
    required_tool: object = None
    allow_empty_text: bool = False
    emit_text: object = None
    expires_at: float = 0.0
    closed: bool = False


def _create_acp_client(*, allowed_mcp_names, on_update, on_permission_approved):
    return BridgeACPClient(
        allowed_mcp_names=allowed_mcp_names,
        on_update=on_update,
        on_permission_approved=on_permission_approved,
    )


async def _configure_agy_session(client, sid: str, options, model_spec: dict):
    from litellm_antigravity_rpc.backend import select_option

    for category, value in (
        ("model", model_spec.get("model")),
        ("thought_level", model_spec.get("thought_level")),
    ):
        if value is None:
            continue
        option_id = select_option(options, category, value)
        result = await client.request(
            "session/set_config_option",
            {"sessionId": sid, "configId": option_id, "value": value},
        )
        options = result.get("configOptions")
        if not any(
            isinstance(o, dict)
            and o.get("id") == option_id
            and o.get("currentValue") == value
            for o in (options or [])
        ):
            raise RuntimeError(f"Antigravity failed to select {category}={value!r}")


async def _session_worker(
    session: AgyToolSession,
    *,
    prompt_text: str,
    client_tools: list[dict],
    callback_port: int,
    executable_override: str | None,
):
    from litellm_antigravity_rpc.config import is_supported_cli_version

    allowed_mcp_names = {f"{AGY_MCP_PREFIX}{t['name']}" for t in client_tools}
    profile = IsolatedAgyProfile(client_tools, callback_port, session.callback_token)
    current_text_parts: list[str] = []
    latest_usage: dict | None = None
    sid: str | None = None
    prompt_task: asyncio.Task | None = None
    worker_failed = False

    def on_permission_approved(_tool_name: str):
        session.approved_inflight += 1
        session.last_tool_signal = time.monotonic()
        session.state_changed.set()

    def on_update(message: dict):
        nonlocal latest_usage
        if message.get("method") != "session/update":
            raise RuntimeError("Unexpected ACP notification")
        params = message.get("params")
        if not isinstance(params, dict):
            raise RuntimeError("Invalid session/update params")
        if sid is not None and params.get("sessionId") != sid:
            raise RuntimeError("Mismatched ACP sessionId")
        update = params.get("update")
        if not isinstance(update, dict):
            raise RuntimeError("Invalid session/update payload")
        kind = update.get("sessionUpdate")
        if kind in ("tool_call", "tool_call_update"):
            tname = update.get("name")
            if tname not in allowed_mcp_names:
                raise RuntimeError(f"Unexpected Antigravity tool activity: {tname!r}")
            session.last_tool_signal = time.monotonic()
            session.state_changed.set()
            return
        if kind == "agent_message_chunk":
            content = update.get("content")
            if (
                prompt_task is None
                or not isinstance(content, dict)
                or content.get("type") != "text"
                or not isinstance(content.get("text"), str)
            ):
                raise RuntimeError("Unsupported agent_message_chunk")
            text = content["text"]
            if text:
                current_text_parts.append(text)
                if session.emit_text is not None:
                    session.emit_text(text)
            return
        if kind == "usage_update":
            parsed = parse_agy_usage(update.get("usage") or update)
            if parsed is not None:
                latest_usage = parsed

    def _flush_ready_tool_batch(*, force=False) -> bool:
        nonlocal latest_usage
        if not session.queued_calls or session.pending_calls:
            return False
        if session.parallel_tool_calls and not force:
            if session.approved_inflight > 0:
                return False
            if time.monotonic() - session.last_tool_signal < 0.04:
                return False

        count = 1 if not session.parallel_tool_calls else len(session.queued_calls)
        selected: list[PendingCall] = []
        for _ in range(count):
            call = session.queued_calls.popleft()
            session.pending_calls[call.public_id] = call
            selected.append(call)

        if session.required_tool and isinstance(session.required_tool, str):
            if not any(c.name == session.required_tool for c in selected):
                raise RuntimeError(
                    f"Antigravity called {[c.name for c in selected]} instead of required tool '{session.required_tool}'"
                )

        public_calls = [
            {
                "id": c.public_id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments_json},
            }
            for c in selected
        ]
        text = "".join(current_text_parts)
        current_text_parts.clear()
        step_u = latest_usage
        latest_usage = None
        session.step_queue.put_nowait(
            StepResult("tool_calls", text, public_calls, step_u)
        )
        return True

    client = _create_acp_client(
        allowed_mcp_names=allowed_mcp_names,
        on_update=on_update,
        on_permission_approved=on_permission_approved,
    )
    try:
        exe = resolve_agy_executable(executable_override)
        await client.start(profile.command(exe), profile.workspace, profile.env)
        init_res = await client.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "antigravity-litellm-bridge", "version": "1.0.0"},
            },
        )
        if init_res.get("protocolVersion") != 1:
            raise RuntimeError("ACP protocol version mismatch")
        agent_info = init_res.get("agentInfo")
        if isinstance(agent_info, dict) and agent_info.get("version"):
            if not is_supported_cli_version(agent_info["version"]):
                raise RuntimeError("Unsupported agy ACP version")

        created = await client.request(
            "session/new",
            {
                "cwd": str(profile.workspace),
                "mcpServers": profile.mcp_servers_param,
            },
        )
        sid = created.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise RuntimeError("Missing sessionId from agy session/new")
        await _configure_agy_session(
            client, sid, created.get("configOptions"), session.model_spec
        )

        prompt_task = asyncio.create_task(
            client.request(
                "session/prompt",
                {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": prompt_text}],
                },
            )
        )

        while True:
            if client.failure.done():
                raise client.failure.result()

            _flush_ready_tool_batch()

            if prompt_task.done():
                result = prompt_task.result()
                break

            wait_state = asyncio.create_task(session.state_changed.wait())
            timeout = (
                0.045
                if (session.queued_calls and not session.pending_calls)
                else (0.2 if session.approved_inflight > 0 else None)
            )
            try:
                done, _ = await asyncio.wait(
                    {prompt_task, client.failure, wait_state},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not wait_state.done():
                    wait_state.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await wait_state

            if wait_state in done:
                session.state_changed.clear()
                _flush_ready_tool_batch()
            elif not done and session.queued_calls and not session.pending_calls:
                _flush_ready_tool_batch(force=True)

        reason = result.get("stopReason") if isinstance(result, dict) else None
        stop_map = {
            "end_turn": "stop",
            "max_tokens": "length",
            "max_turn_requests": "length",
            "refusal": "content_filter",
        }
        if reason not in stop_map:
            raise RuntimeError(f"Antigravity stopped with reason={reason!r}")
        if session.required_tool and stop_map[reason] == "stop":
            raise RuntimeError(
                "Antigravity completed turn without calling required tool"
            )
        text = "".join(current_text_parts)
        if not text and not session.allow_empty_text and stop_map[reason] == "stop":
            raise RuntimeError("Antigravity completed turn without text")
        final_usage = parse_agy_usage(result.get("usage")) or latest_usage
        session.step_queue.put_nowait(
            StepResult(stop_map[reason], text, [], final_usage)
        )
    except asyncio.CancelledError:
        worker_failed = True
        raise
    except BaseException as exc:
        worker_failed = True
        session.step_queue.put_nowait(exc)
    finally:
        session.closed = True
        for call in list(session.queued_calls) + list(session.pending_calls.values()):
            if not call.future.done():
                call.future.cancel()
        if sid and prompt_task and not prompt_task.done():
            with contextlib.suppress(BaseException):
                async with asyncio.timeout(2.0):
                    await client.notify("session/cancel", {"sessionId": sid})
        if prompt_task and not prompt_task.done():
            prompt_task.cancel()
            await asyncio.gather(prompt_task, return_exceptions=True)
        if sid and not client.failure.done() and not worker_failed:
            with contextlib.suppress(BaseException):
                async with asyncio.timeout(2.0):
                    await client.request("session/close", {"sessionId": sid})
        try:
            with contextlib.suppress(BaseException):
                await client.close()
        finally:
            profile.close()


async def _await_next_step(session: AgyToolSession) -> StepResult:
    item = await session.step_queue.get()
    if isinstance(item, BaseException):
        raise item
    return item


async def close_session(session: AgyToolSession):
    session.closed = True
    if session.owner_task is not None and not session.owner_task.done():
        session.owner_task.cancel()
        with contextlib.suppress(BaseException):
            await session.owner_task


async def start_step(
    runtime: "BridgeRuntime",
    prompt_text: str,
    *,
    model_alias: str,
    model_spec: dict,
    client_tools: list[dict],
    parallel_tool_calls: bool,
    required_tool: object,
    allow_empty_text: bool,
    emit_text,
    executable_override: str | None = None,
):
    keep_summary = set()
    for spec in client_tools:
        schema = spec.get("inputSchema")
        props = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(props, dict) and "toolSummary" in props:
            keep_summary.add(spec["name"])

    token = secrets.token_hex(32)
    session = AgyToolSession(
        session_id=uuid.uuid4().hex,
        model_alias=model_alias,
        model_spec=model_spec,
        callback_token=token,
        keep_tool_summary=keep_summary,
        declared_names={t["name"] for t in client_tools},
        parallel_tool_calls=parallel_tool_calls,
        required_tool=required_tool,
        allow_empty_text=allow_empty_text,
        emit_text=emit_text,
    )
    callback_port = await runtime.ensure_callback_server()
    runtime.register_callback(token, session)
    try:
        async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
            session.owner_task = asyncio.create_task(
                _session_worker(
                    session,
                    prompt_text=prompt_text,
                    client_tools=client_tools,
                    callback_port=callback_port,
                    executable_override=executable_override,
                )
            )
            result = await _await_next_step(session)
        session.emit_text = None
        if result.finish_reason != "tool_calls":
            runtime.unregister_callback(token)
            await close_session(session)
            return None, result
        return session, result
    except BaseException:
        session.emit_text = None
        runtime.unregister_callback(token)
        await close_session(session)
        raise


async def resume_step(
    runtime: "BridgeRuntime",
    session: AgyToolSession,
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
            runtime.unregister_callback(session.callback_token)
            await close_session(session)
            return None, result
        return session, result
    except BaseException:
        session.emit_text = None
        runtime.unregister_callback(session.callback_token)
        await close_session(session)
        raise


class SessionConflictError(ValueError):
    pass


class BridgeRuntime:
    """Persistent asyncio loop, loopback MCP callback listener, and paused session store."""

    def __init__(self, concurrency=DEFAULT_CONCURRENCY):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run_loop, name="agy-bridge-loop", daemon=True
        )
        self.started = threading.Event()
        self.thread.start()
        self.started.wait()
        self.slots = threading.BoundedSemaphore(concurrency)
        self.lock = threading.Lock()
        self.sessions: dict[str, AgyToolSession] = {}
        self.call_to_session: dict[str, str] = {}
        self.callback_sessions: dict[str, AgyToolSession] = {}
        self.callback_server: asyncio.AbstractServer | None = None
        self.callback_port: int = 0
        self.closed = False

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.started.set()
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def register_callback(self, token: str, session: AgyToolSession):
        with self.lock:
            self.callback_sessions[token] = session

    def unregister_callback(self, token: str):
        with self.lock:
            self.callback_sessions.pop(token, None)

    async def ensure_callback_server(self) -> int:
        if self.callback_server is None:
            self.callback_server = await asyncio.start_server(
                self._handle_mcp_callback, host="127.0.0.1", port=0
            )
            self.callback_port = self.callback_server.sockets[0].getsockname()[1]
        return self.callback_port

    async def _handle_mcp_callback(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if not raw:
                return
            payload = json.loads(raw.decode("utf-8"))
            token = payload.get("token", "")
            name = payload.get("name", "")
            arguments = payload.get("arguments")
            with self.lock:
                session = self.callback_sessions.get(token)
            if (
                session is None
                or session.closed
                or not isinstance(name, str)
                or name not in session.declared_names
            ):
                reply = {"ok": False, "content": "Unauthorized or unknown tool call"}
            else:
                args_json = clean_tool_arguments(
                    arguments,
                    keep_tool_summary=(name in session.keep_tool_summary),
                )
                fut = asyncio.get_running_loop().create_future()
                call = PendingCall(
                    public_id="call_" + uuid.uuid4().hex[:24],
                    name=name,
                    arguments_json=args_json,
                    future=fut,
                )
                if session.approved_inflight > 0:
                    session.approved_inflight -= 1
                session.last_tool_signal = time.monotonic()
                session.queued_calls.append(call)
                session.state_changed.set()
                result_text = await fut
                reply = {"ok": True, "content": result_text}
            writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        except BaseException as exc:
            with contextlib.suppress(BaseException):
                writer.write(
                    (
                        json.dumps(
                            {"ok": False, "content": f"Callback error: {type(exc).__name__}"}
                        )
                        + "\n"
                    ).encode("utf-8")
                )
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(BaseException):
                await writer.wait_closed()

    def reap_expired(self):
        now = time.monotonic()
        expired = []
        with self.lock:
            for sid, session in list(self.sessions.items()):
                if session.expires_at <= now:
                    self._pop_unlocked(sid)
                    self.callback_sessions.pop(session.callback_token, None)
                    expired.append(session)
        for session in expired:
            self.submit(close_session(session))

    def _pop_unlocked(self, session_id):
        session = self.sessions.pop(session_id, None)
        if session is not None:
            for call_id in list(session.pending_calls):
                self.call_to_session.pop(call_id, None)
        return session

    def save(self, session: AgyToolSession, ttl: float):
        self.reap_expired()
        evicted = None
        with self.lock:
            if self.closed:
                evicted = session
                self.callback_sessions.pop(session.callback_token, None)
            else:
                if len(self.sessions) >= MAX_PENDING_SESSIONS:
                    oldest_id = min(
                        self.sessions, key=lambda sid: self.sessions[sid].expires_at
                    )
                    evicted = self._pop_unlocked(oldest_id)
                    if evicted is not None:
                        self.callback_sessions.pop(evicted.callback_token, None)
                session.expires_at = time.monotonic() + ttl
                self.sessions[session.session_id] = session
                for call_id in session.pending_calls:
                    self.call_to_session[call_id] = session.session_id
        if evicted is not None:
            self.submit(close_session(evicted))

    def claim(self, trailing_results: dict[str, str], model_alias: str):
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
                session.model_alias != model_alias
                or set(trailing_results) != set(session.pending_calls)
            ):
                raise SessionConflictError("tool results do not match pending tool_calls")
            return self._pop_unlocked(sid)

    def discard(self, session: AgyToolSession | None):
        if session is None:
            return
        with self.lock:
            self._pop_unlocked(session.session_id)
            self.callback_sessions.pop(session.callback_token, None)
        self.submit(close_session(session))

    async def _close_callback_server(self):
        if self.callback_server is not None:
            self.callback_server.close()
            await self.callback_server.wait_closed()
            self.callback_server = None

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            active = list(self.sessions.values())
            self.sessions.clear()
            self.call_to_session.clear()
            self.callback_sessions.clear()
        for session in active:
            with contextlib.suppress(Exception):
                self.submit(close_session(session))
        with contextlib.suppress(Exception):
            self.submit(self._close_callback_server())
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
                            "owned_by": "antigravity",
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
            model_alias, model_spec = resolve_model_alias(body.get("model"))
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
                claimed_session = runtime.claim(trailing_results, model_alias)
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
            self.json_response(429, {"error": "Antigravity bridge is busy"})
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
                        runtime,
                        claimed_session,
                        trailing_results,
                        emit_text if stream else None,
                        parallel_tool_calls=parallel_tool_calls,
                        required_tool=required_tool,
                    )
                )
            else:
                extra_instructions = []
                if client_tools and not parallel_tool_calls:
                    extra_instructions.append(
                        "Invoke at most one tool call per assistant turn."
                    )
                if tool_instruction:
                    extra_instructions.append(tool_instruction)
                prompt_text = build_prompt(
                    messages,
                    extra_instructions=extra_instructions,
                    stateless_tool_continuation=(trailing_results is not None),
                )
                paused_session, step_result = runtime.submit(
                    start_step(
                        runtime,
                        prompt_text,
                        model_alias=model_alias,
                        model_spec=model_spec,
                        client_tools=client_tools,
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
                f"Antigravity bridge turn failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            if started:
                with contextlib.suppress(OSError):
                    self.sse(
                        {
                            "error": {
                                "message": "Antigravity ACP turn failed",
                                "type": "server_error",
                            }
                        }
                    )
            else:
                self.json_response(
                    504 if isinstance(exc, TimeoutError) else 503,
                    {"error": "Antigravity ACP turn failed"},
                )
        finally:
            runtime.slots.release()


def main():
    settings_path = Path(sys.argv[1]).resolve()
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    port = int(settings.get("port", 14004))
    executable_override = settings.get("executable") or None
    concurrency = int(settings.get("concurrency", DEFAULT_CONCURRENCY))

    asyncio.run(run_agy_doctor(executable_override))

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
