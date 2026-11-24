import asyncio
import http.client
import importlib
import json
import runpy
import sys
import threading
import time
import tomllib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

DEPLOY_DIR = Path(__file__).parents[1] / "deploy" / "codex-minipod"
sys.path.insert(0, str(DEPLOY_DIR))
try:
    prepare_module = importlib.import_module("prepare")
    sandbox_module = importlib.import_module("sandbox_manager")
finally:
    sys.path.remove(str(DEPLOY_DIR))

bridge = runpy.run_path(str(DEPLOY_DIR / "bridge.py"))
BRIDGE_GLOBALS = bridge["Handler"].do_POST.__globals__

PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "probe_ping",
        "description": "Readiness probe tool.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


class StubbornCodexProcess:
    """Simulates a Codex subprocess that ignores stdin EOF and only exits when killed."""

    def __init__(self):
        self.stdin = SimpleNamespace(close=self._close_stdin)
        self.stdout = None
        self.returncode = None
        self._killed = threading.Event()

    def _close_stdin(self):
        pass

    def kill(self):
        self.returncode = -9
        self._killed.set()

    async def wait(self):
        while not self._killed.is_set():
            await asyncio.sleep(0.01)
        return self.returncode


class SlotRecordingAppServer:
    def __init__(self, process, steps, record, *, on_turn_start=None, selected_model="gpt-6.1-sol"):
        self.process = process
        self.steps = deque(steps)
        self.record = record
        self.on_turn_start = on_turn_start
        self.selected_model = selected_model
        self.current_events = deque()
        self._fs_probe_id = None

    async def send(self, message):
        self.record.append(("send", message))
        if message.get("method") == "fs/readFile":
            self._fs_probe_id = message["id"]
            return
        if "id" in message and "result" in message and self.steps and not self.current_events:
            nxt = self.steps.popleft()
            self.current_events.extend(nxt(message) if callable(nxt) else nxt)

    async def call(self, method, params, pending=None):
        self.record.append((method, params))
        if method == "environment/info":
            return {"cwd": "file:///workspace"}
        if method == "thread/start":
            return {
                "approvalPolicy": "never",
                "model": self.selected_model,
                "thread": {"id": "t1"},
            }
        if method == "turn/start":
            if self.on_turn_start is not None:
                await self.on_turn_start(self.record)
            if self.steps:
                nxt = self.steps.popleft()
                self.current_events.extend(nxt(params) if callable(nxt) else nxt)
            return {"turn": {"id": "u1"}}
        return {}

    async def read(self):
        if self._fs_probe_id is not None:
            pid = self._fs_probe_id
            self._fs_probe_id = None
            return {"id": pid, "error": {"message": "local filesystem is not configured"}}
        return self.current_events.popleft()


def text_step(text="OK"):
    return [
        {
            "method": "item/completed",
            "params": {"threadId": "t1", "turnId": "u1", "item": {"id": "m1", "type": "agentMessage", "text": text}},
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "t1", "turnId": "u1", "turn": {"id": "u1", "status": "completed"}},
        },
    ]


def tool_step(call_id="c1", rpc_id=7):
    return [
        {
            "id": rpc_id,
            "method": "item/tool/call",
            "params": {"threadId": "t1", "turnId": "u1", "callId": call_id, "tool": "probe_ping", "arguments": {}},
        }
    ]


def stop_bridge_server(server, thread):
    close_fn = bridge.get("close_server_sessions")
    if close_fn is not None:
        close_fn(server)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def post_json(port, payload, *, timeout=5):
    req = Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.status, json.load(resp)


@pytest.mark.parametrize("stream_first", [False, True])
def test_slot_releases_before_200_completes_even_with_zero_queue_and_expired_session(monkeypatch, tmp_path, stream_first):
    batches = deque([[tool_step("abandoned")], [tool_step("probe-1")], [text_step("probe-2-ok")]])
    spawned = []

    async def fake_subprocess(*args, **kwargs):
        proc = StubbornCodexProcess()
        spawned.append((proc, kwargs))
        return proc

    def make_app(process, *args, **kwargs):
        return SlotRecordingAppServer(process, batches.popleft(), [])

    monkeypatch.setattr(BRIDGE_GLOBALS["asyncio"], "create_subprocess_exec", fake_subprocess)
    monkeypatch.setitem(BRIDGE_GLOBALS, "AppServer", make_app)

    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer test-key"
    server.profile = tmp_path / "profile"
    server.workspace = tmp_path / "workspace"
    server.codex = "codex"
    server.slots = 1
    server.queue_timeout = 0.0  # Strict non-blocking to prove zero post-200 lock hold!
    server.session_ttl = 0.05
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        headers = {"Authorization": "Bearer test-key", "Content-Type": "application/json"}
        post_json(server.server_port, {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "old"}], "tools": [PROBE_TOOL]})
        time.sleep(0.08)

        c1 = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        c1.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps({
                "model": "gpt-6.1-sol",
                "stream": stream_first,
                "messages": [{"role": "user", "content": "probe 1"}],
                "tools": [PROBE_TOOL],
            }),
            headers=headers,
        )
        r1 = c1.getresponse()
        assert r1.status == 200
        if stream_first:
            while True:
                line = r1.fp.readline()
                if not line or line.strip() == b"data: [DONE]":
                    break
        else:
            r1.read(int(r1.getheader("Content-Length")))

        # Immediately send Request 2 with 0ms wait and queue_timeout=0.0: must still be 200, never 429!
        status2, body2 = post_json(
            server.server_port,
            {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "probe 2"}]},
        )
        c1.close()
        assert status2 == 200
        assert body2["choices"][0]["message"]["content"] == "probe-2-ok"
    finally:
        stop_bridge_server(server, thread)


def test_abandoned_tool_session_is_proactively_killed_on_ttl_without_new_requests(monkeypatch, tmp_path):
    spawned = []

    async def fake_subprocess(*args, **kwargs):
        proc = StubbornCodexProcess()
        spawned.append(proc)
        return proc

    monkeypatch.setattr(BRIDGE_GLOBALS["asyncio"], "create_subprocess_exec", fake_subprocess)
    monkeypatch.setitem(
        BRIDGE_GLOBALS,
        "AppServer",
        lambda process, *a, **kw: SlotRecordingAppServer(process, [tool_step("orphan")], []),
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer test-key"
    server.profile = tmp_path / "profile"
    server.workspace = tmp_path / "workspace"
    server.codex = "codex"
    server.session_ttl = 0.08
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        post_json(
            server.server_port,
            {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "probe"}], "tools": [PROBE_TOOL]},
        )
        assert len(spawned) == 1
        # Even with zero incoming requests, the background TTL reaper must kill the orphaned Codex process
        assert spawned[0]._killed.wait(timeout=2.5)
        assert spawned[0].returncode == -9
    finally:
        stop_bridge_server(server, thread)


def test_single_slot_queues_concurrent_requests_by_default_and_times_out_with_429(monkeypatch, tmp_path):
    first_entered = threading.Event()
    release_first = threading.Event()

    async def fake_run_turn(prompt, profile, workspace, codex, emit_text=None, *, model):
        if prompt == "slow":
            first_entered.set()
            await asyncio.to_thread(release_first.wait, 2.0)
            return "SLOW-DONE", None
        return "QUEUED-DONE", None

    monkeypatch.setitem(BRIDGE_GLOBALS, "run_turn", fake_run_turn)
    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer test-key"
    server.profile = tmp_path / "profile"
    server.workspace = tmp_path / "workspace"
    server.codex = "codex"
    server.slots = 1
    server.queue_timeout = 1.5
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(post_json, server.server_port, {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "slow"}]})
            assert first_entered.wait(timeout=1.0)
            f2 = pool.submit(post_json, server.server_port, {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "queued"}]})
            time.sleep(0.08)
            assert not f2.done()  # Queued waiting for slot 0!
            release_first.set()
            s1, b1 = f1.result(timeout=2)
            s2, b2 = f2.result(timeout=2)
        assert (s1, b1["choices"][0]["message"]["content"]) == (200, "SLOW-DONE")
        assert (s2, b2["choices"][0]["message"]["content"]) == (200, "QUEUED-DONE")

        # Now verify bounded queue timeout still returns 429 with Retry-After when saturated
        first_entered.clear()
        release_first.clear()
        server.queue_timeout = 0.06
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(post_json, server.server_port, {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "slow"}]})
            assert first_entered.wait(timeout=1.0)
            req = Request(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                data=json.dumps({"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "overflow"}]}).encode(),
                headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
            )
            with pytest.raises(HTTPError) as busy:
                urlopen(req, timeout=2)
            assert busy.value.code == 429
            assert busy.value.headers.get("Retry-After") is not None
            assert json.loads(busy.value.read()) == {"error": "Mini-pod workspace is busy"}
            release_first.set()
            f1.result(timeout=2)
    finally:
        release_first.set()
        stop_bridge_server(server, thread)


def test_minipod_slots_env_runs_parallel_turns_on_isolated_slot_environments_and_queues_overflow(monkeypatch, tmp_path):
    monkeypatch.setenv("MINIPOD_SLOTS", "2")
    monkeypatch.setenv("MINIPOD_QUEUE_TIMEOUT_SECONDS", "2.0")

    both_in_flight = threading.Barrier(2)
    records = []
    spawn_cwds = []
    lock = threading.Lock()

    async def fake_subprocess(*args, **kwargs):
        with lock:
            spawn_cwds.append(Path(kwargs["cwd"]))
        proc = StubbornCodexProcess()
        proc.stdin = SimpleNamespace(close=proc.kill)
        return proc

    async def on_turn_start(record):
        prompt = next(p["input"][0]["text"] for m, p in record if m == "turn/start")
        if prompt in {"par-1", "par-2"}:
            await asyncio.to_thread(both_in_flight.wait, 1.5)

    def make_app(process, *args, **kwargs):
        rec = []
        with lock:
            records.append(rec)
        return SlotRecordingAppServer(
            process,
            [lambda params: text_step(f"done:{params['input'][0]['text']}")],
            rec,
            on_turn_start=on_turn_start,
        )

    monkeypatch.setattr(BRIDGE_GLOBALS["asyncio"], "create_subprocess_exec", fake_subprocess)
    monkeypatch.setitem(BRIDGE_GLOBALS, "AppServer", make_app)

    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer test-key"
    server.profile = tmp_path / "profile"
    server.workspace = tmp_path / "workspace"
    server.codex = "codex"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            f1 = pool.submit(post_json, server.server_port, {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "par-1"}]})
            f2 = pool.submit(post_json, server.server_port, {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "par-2"}]})
            f3 = pool.submit(post_json, server.server_port, {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "queued-3"}]})
            results = {f.result(timeout=4)[1]["choices"][0]["message"]["content"] for f in (f1, f2, f3)}

        assert results == {"done:par-1", "done:par-2", "done:queued-3"}

        # First two parallel turns must have targeted distinct Mini-pod slot environments and distinct host cwds
        env_ids = [
            next(p["environmentId"] for m, p in rec if m == "environment/info")
            for rec in records
        ]
        thread_envs = [
            next(p["environments"][0]["environmentId"] for m, p in rec if m == "thread/start")
            for rec in records
        ]
        assert env_ids == thread_envs
        assert set(env_ids[:2]) == {"minipod", "minipod-1"}
        assert {p.name for p in spawn_cwds[:2]} == {"slot-0", "slot-1"}
    finally:
        stop_bridge_server(server, thread)


def test_paused_tool_session_prefers_clean_slot_for_other_traffic_and_resumes_on_pinned_slot(monkeypatch, tmp_path):
    records = []
    slot1_busy = threading.Event()
    release_slot1 = threading.Event()

    async def fake_subprocess(*args, **kwargs):
        proc = StubbornCodexProcess()
        proc.stdin = SimpleNamespace(close=proc.kill)
        return proc

    scripts = deque(
        [
            [tool_step("c-slot0", rpc_id=11), text_step("RESUMED-ON-ORIGINAL-SLOT")],
            [text_step("RAN-ON-CLEAN-SLOT")],
        ]
    )

    async def on_turn_start(record):
        prompt = next(p["input"][0]["text"] for m, p in record if m == "turn/start")
        if prompt == "other-app":
            slot1_busy.set()
            await asyncio.to_thread(release_slot1.wait, 2.0)

    def make_app(process, *args, **kwargs):
        rec = []
        records.append(rec)
        return SlotRecordingAppServer(process, scripts.popleft(), rec, on_turn_start=on_turn_start)

    monkeypatch.setattr(BRIDGE_GLOBALS["asyncio"], "create_subprocess_exec", fake_subprocess)
    monkeypatch.setitem(BRIDGE_GLOBALS, "AppServer", make_app)

    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer test-key"
    server.profile = tmp_path / "profile"
    server.workspace = tmp_path / "workspace"
    server.codex = "codex"
    server.slots = 2
    server.queue_timeout = 2.0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 1) Session A starts on slot 0 ("minipod") and pauses awaiting tool output
        u_a = {"role": "user", "content": "start-tool-a"}
        _, body_a1 = post_json(
            server.server_port,
            {"model": "gpt-6.1-sol", "messages": [u_a], "tools": [PROBE_TOOL]},
        )
        msg_a1 = body_a1["choices"][0]["message"]
        call_id = msg_a1["tool_calls"][0]["id"]
        first_env = next(p["environmentId"] for m, p in records[0] if m == "environment/info")
        assert first_env == "minipod"

        # 2) While Session A is paused on slot 0, another app sends a request -> picks clean slot 1 ("minipod-1")!
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_other = pool.submit(
                post_json,
                server.server_port,
                {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "other-app"}]},
            )
            assert slot1_busy.wait(timeout=1.5)
            second_env = next(p["environmentId"] for m, p in records[1] if m == "environment/info")
            assert second_env == "minipod-1"

            # 3) While slot 1 is busy with other-app, Session A resumes immediately on its pinned slot 0!
            _, body_a2 = post_json(
                server.server_port,
                {
                    "model": "gpt-6.1-sol",
                    "messages": [u_a, msg_a1, {"role": "tool", "tool_call_id": call_id, "content": "pong"}],
                    "tools": [PROBE_TOOL],
                },
            )
            assert body_a2["choices"][0]["message"]["content"] == "RESUMED-ON-ORIGINAL-SLOT"

            release_slot1.set()
            _, body_other = f_other.result(timeout=2)
            assert body_other["choices"][0]["message"]["content"] == "RAN-ON-CLEAN-SLOT"
    finally:
        release_slot1.set()
        stop_bridge_server(server, thread)


def test_publish_and_sandbox_manager_reconcile_multi_slot_environments(monkeypatch, tmp_path):
    state = tmp_path / "bridge"
    profile = state / "profile"
    profile.mkdir(parents=True)
    launcher = tmp_path / "mini-pod" / "target" / "debug" / "minipod-stdio.exe"

    # Initially published with 1 slot
    prepare_module.publish_sandbox(profile, state, str(launcher), "pipe://mini-pod", "lease-1", slots=1)
    single_cfg = tomllib.loads((profile / "environments.toml").read_text(encoding="utf-8"))
    assert [e["id"] for e in single_cfg["environments"]] == ["minipod"]
    assert single_cfg["environments"][0]["args"] == ["pipe://mini-pod", "lease-1", "exec"]

    # Upgrade to MINIPOD_SLOTS=3 on existing live lease -> reconcile prepares & publishes 3 slots without spawning new VM
    monkeypatch.setenv("MINIPOD_SLOTS", "3")
    manager = sandbox_module.SandboxManager(state / "settings.json", profile)
    assert manager.slots == 3

    events = []
    monkeypatch.setattr(
        manager,
        "_list",
        lambda: [{"id": "lease-1", "alive": True, "network": {"type": "Isolated"}, "docker_endpoint": None, "remaining_ttl_ms": 86_400_000}],
    )
    monkeypatch.setattr(manager, "_spawn", lambda: pytest.fail("existing lease must be reused when scaling slots"))
    monkeypatch.setattr(manager, "_prepare", lambda sid: events.append(("prepare", sid, manager.slots)) or str(launcher))

    verify_calls = []

    def fake_command(*args, **kwargs):
        verify_calls.append(args)
        action = args[args.index("/workspace/minipod-codex/rootless.sh") + 1]
        return {
            "exit_code": 0,
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout": "true" if action == "verify" else "codex-cli 0.167.0",
        }

    monkeypatch.setattr(manager, "_command", fake_command)

    assert manager.reconcile() is True
    # Because environments.toml only had 1 slot (< 3), reconcile must prepare & publish 3 slots!
    assert events == [("prepare", "lease-1", 3)]
    assert [c[-2:] for c in verify_calls] == [("verify", "3"), ("check", "3")]

    multi_cfg = tomllib.loads((profile / "environments.toml").read_text(encoding="utf-8"))
    assert multi_cfg["default"] == "minipod"
    assert multi_cfg["include_local"] is False
    assert [(e["id"], e["args"]) for e in multi_cfg["environments"]] == [
        ("minipod", ["pipe://mini-pod", "lease-1", "exec"]),
        ("minipod-1", ["pipe://mini-pod", "lease-1", "exec", "1"]),
        ("minipod-2", ["pipe://mini-pod", "lease-1", "exec", "2"]),
    ]


@pytest.mark.parametrize("bad_slots", ["0", "-2", "17", "abc"])
def test_invalid_minipod_slots_rejected(monkeypatch, bad_slots):
    monkeypatch.setenv("MINIPOD_SLOTS", bad_slots)
    with pytest.raises(ValueError, match="MINIPOD_SLOTS"):
        bridge["parse_slot_count"]()


def test_sandbox_manager_defaults_to_2gb_disk_and_honors_minipod_disk_mb(monkeypatch, tmp_path):
    state = tmp_path / "bridge"
    profile = state / "profile"
    profile.mkdir(parents=True)
    launcher = tmp_path / "mini-pod" / "target" / "debug" / "minipod-stdio.exe"
    prepare_module.publish_sandbox(profile, state, str(launcher), "pipe://mini-pod", "lease-1", slots=1)

    monkeypatch.delenv("MINIPOD_DISK_MB", raising=False)
    manager = sandbox_module.SandboxManager(state / "settings.json", profile)
    assert manager.disk_mb == 2048

    spawn_calls = []
    manager.operation_id = "op-default"
    monkeypatch.setattr(manager, "_command", lambda *args, **kw: spawn_calls.append(args) or {"id": "lease-2"})
    assert manager._spawn() == "lease-2"
    default_args = list(spawn_calls[-1])
    assert default_args[default_args.index("--disk-mb") + 1] == "2048"
    assert default_args[default_args.index("--storage-mb") + 1] == "2560"

    monkeypatch.setenv("MINIPOD_DISK_MB", "3072")
    custom = sandbox_module.SandboxManager(state / "settings.json", profile)
    custom.operation_id = "op-custom"
    monkeypatch.setattr(custom, "_command", lambda *args, **kw: spawn_calls.append(args) or {"id": "lease-3"})
    custom._spawn()
    custom_args = list(spawn_calls[-1])
    assert custom_args[custom_args.index("--disk-mb") + 1] == "3072"
    assert custom_args[custom_args.index("--storage-mb") + 1] == "3584"

    monkeypatch.setenv("MINIPOD_DISK_MB", "1024")
    with pytest.raises(ValueError, match="MINIPOD_DISK_MB"):
        sandbox_module.SandboxManager(state / "settings.json", profile)


def test_prepare_sandbox_streams_docker_export_directly_and_cleans_container(monkeypatch, tmp_path):
    minipod_dir = tmp_path / "mini-pod"
    (minipod_dir / "crates" / "sandbox-client").mkdir(parents=True)
    (minipod_dir / "crates" / "sandbox-core").mkdir(parents=True)
    state = tmp_path / "state" / "bridge"
    state.mkdir(parents=True)

    calls = []
    ready_checks = deque([False])

    monkeypatch.setattr(prepare_module, "require_rootless", lambda *a, **kw: calls.append(("init", kw.get("slots"))))
    monkeypatch.setattr(prepare_module, "executor_ready", lambda *a, **kw: ready_checks.popleft())

    def fake_check_output(cmd, **kwargs):
        calls.append(("check_output", list(cmd)))
        if "list" in cmd:
            return json.dumps({
                "sandboxes": [{"id": "s1", "alive": True, "network": {"type": "Isolated"}, "docker_endpoint": None}]
            }).encode()
        if cmd[:2] == ["docker", "create"]:
            return "temp-container-123\n"
        raise AssertionError(f"unexpected check_output: {cmd}")

    class FakeExportPopen:
        def __init__(self, cmd, **kwargs):
            calls.append(("popen", list(cmd)))
            self.stdout = SimpleNamespace(close=lambda: calls.append(("close_pipe",)))

        def wait(self, timeout=None):
            return 0

    def fake_run(cmd, **kwargs):
        calls.append(("run", list(cmd), kwargs.get("stdin") is not None))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(prepare_module.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(prepare_module.subprocess, "Popen", FakeExportPopen)
    monkeypatch.setattr(prepare_module.subprocess, "run", fake_run)

    launcher = prepare_module.prepare_sandbox(minipod_dir, "pipe://ep", "s1", state=state, sandbox_cli="scli", slots=2)
    assert launcher.endswith("minipod-stdio.exe")
    assert ("check_output", ["docker", "create", "--platform", "linux/amd64", prepare_module.IMAGE]) in calls
    assert ("popen", ["docker", "export", "temp-container-123"]) in calls
    assert any(c[0] == "run" and c[1][-3:] == ["pipe://ep", "s1", "load"] and c[2] is True for c in calls)
    assert any(c[0] == "run" and c[1] == ["docker", "rm", "-f", "temp-container-123"] for c in calls)
    assert not any(c[0] == "run" and c[1][:3] == ["docker", "image", "save"] for c in calls)
