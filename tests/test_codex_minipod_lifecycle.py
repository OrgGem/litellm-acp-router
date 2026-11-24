import importlib
import json
import runpy
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

DEPLOY_DIR = Path(__file__).parents[1] / "deploy" / "codex-minipod"
sys.path.insert(0, str(DEPLOY_DIR))
try:
    sandbox_module = importlib.import_module("sandbox_manager")
finally:
    sys.path.remove(str(DEPLOY_DIR))

SandboxManager = sandbox_module.SandboxManager

bridge = runpy.run_path(str(DEPLOY_DIR / "bridge.py"))


def create_manager(tmp_path, *, endpoint="pipe://mini-pod", sandbox_id="old-lease", settings_data=None):
    state = tmp_path / "bridge"
    profile = state / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    launcher = tmp_path / "mini-pod" / "target" / "debug" / "minipod-stdio.exe"
    environments = (
        'default = "minipod"\n'
        'include_local = false\n\n'
        '[[environments]]\n'
        'id = "minipod"\n'
        f'program = "{launcher.as_posix()}"\n'
        f'args = ["{endpoint}", "{sandbox_id}", "exec"]\n'
    )
    (profile / "environments.toml").write_text(environments, encoding="utf-8")
    settings_path = state / "settings.json"
    if settings_data is not None:
        settings_path.write_text(json.dumps(settings_data), encoding="utf-8")
    return SandboxManager(settings_path, profile)


def lease(sandbox_id, *, alive=True, network="Isolated", remaining_ttl_ms=86_400_000, pid=None):
    result = {
        "id": sandbox_id,
        "alive": alive,
        "network": {"type": network},
        "docker_endpoint": None,
        "remaining_ttl_ms": remaining_ttl_ms,
    }
    if pid is not None:
        result["pid"] = pid
    return result


def install_fake_keyed_daemon(manager, monkeypatch, *, state=None, lost_first=False):
    if state is None:
        state = SimpleNamespace(
            leases={}, receipts={}, calls=[], creations=[], expired=set(), drop_response=lost_first
        )
    elif lost_first:
        state.drop_response = True

    def command(*args, **kwargs):
        if args[0] == "list":
            return {"sandboxes": list(state.leases.values())}
        if args[0] == "spawn":
            operation_id = args[args.index("--operation-id") + 1]
            state.calls.append((manager.endpoint, operation_id))
            if operation_id in state.expired:
                raise subprocess.CalledProcessError(
                    1, args, stderr=b"sandbox-cli error: sandbox lease expired: lease is not active"
                )
            if operation_id not in state.receipts:
                sandbox_id = f"keyed-{len(state.creations) + 1}"
                state.receipts[operation_id] = sandbox_id
                state.leases[sandbox_id] = lease(sandbox_id)
                state.creations.append(sandbox_id)
            if state.drop_response:
                state.drop_response = False
                raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 30))
            return {"id": state.receipts[operation_id]}
        if args[0] in {"exec", "renew"}:
            return {
                "exit_code": 0,
                "timed_out": False,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "stdout": "true",
            }
        raise AssertionError(f"unexpected Mini-pod command: {args[0]}")

    monkeypatch.setattr(manager, "_command", command)
    return state


def test_reconcile_spawns_prepares_verifies_and_publishes_when_lease_is_missing(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    leases = {}
    events = []

    def spawn():
        events.append("spawn")
        leases["new-lease"] = lease("new-lease")
        return "new-lease"

    monkeypatch.setattr(manager, "_list", lambda: list(leases.values()))
    monkeypatch.setattr(manager, "_spawn", spawn)
    monkeypatch.setattr(manager, "_prepare", lambda sandbox_id: events.append(("prepare", sandbox_id)) or "new-launcher")
    monkeypatch.setattr(manager, "_verify", lambda sandbox_id: events.append(("verify", sandbox_id)))
    monkeypatch.setattr(manager, "_publish", lambda sandbox_id, launcher: events.append(("publish", sandbox_id, launcher)))

    assert manager.reconcile() is True

    assert events == [
        "spawn",
        ("prepare", "new-lease"),
        ("verify", "new-lease"),
        ("publish", "new-lease", "new-launcher"),
    ]
    assert manager.active_id == "new-lease"
    assert manager._verified_id == "new-lease"
    assert not manager.pending_path.exists()
    active = json.loads((manager.state / "sandbox-active.json").read_text(encoding="utf-8"))
    assert active["id"] == "new-lease"
    assert active["endpoint"] == manager.endpoint
    assert active["operation_id"]


def test_reconcile_renews_expiring_active_lease_without_recreating_it(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    manager._verified_id = manager.active_id
    renewals = []
    monkeypatch.setattr(manager, "_list", lambda: [lease("old-lease", remaining_ttl_ms=1)])
    monkeypatch.setattr(manager, "_renew", lambda sandbox_id: renewals.append(sandbox_id))
    monkeypatch.setattr(manager, "_spawn", lambda: pytest.fail("active lease should be reused"))
    monkeypatch.setattr(manager, "_prepare", lambda sandbox_id: pytest.fail("healthy active lease should not be prepared"))
    monkeypatch.setattr(manager, "_verify", lambda sandbox_id: pytest.fail("already verified lease should not be rechecked"))

    assert manager.reconcile() is True
    assert renewals == ["old-lease"]


def test_reconcile_does_not_spawn_when_daemon_state_cannot_be_read(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    monkeypatch.setattr(manager, "_list", lambda: (_ for _ in ()).throw(OSError("daemon unavailable")))
    monkeypatch.setattr(manager, "_spawn", lambda: pytest.fail("unknown daemon state must not create a VM"))

    with pytest.raises(OSError, match="daemon unavailable"):
        manager.reconcile()

    assert not manager.pending_path.exists()


def test_reconcile_rejects_an_active_lease_with_the_wrong_isolation_policy(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    monkeypatch.setattr(manager, "_list", lambda: [lease("old-lease", network="Unrestricted")])
    monkeypatch.setattr(manager, "_spawn", lambda: pytest.fail("unsafe active lease must not be replaced silently"))

    with pytest.raises(RuntimeError, match="unexpected isolation policy"):
        manager.reconcile()


def test_failed_readiness_keeps_candidate_for_retry_without_spawning_a_second_vm(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    leases = {}
    events = []
    operation_ids = []
    verify_attempts = 0

    def spawn():
        events.append("spawn")
        operation_ids.append(manager.operation_id)
        leases.setdefault("new-lease", lease("new-lease"))
        return "new-lease"

    def verify(sandbox_id):
        nonlocal verify_attempts
        verify_attempts += 1
        if verify_attempts == 1:
            raise RuntimeError("executor is still starting")
        events.append(("verify", sandbox_id))

    monkeypatch.setattr(manager, "_list", lambda: list(leases.values()))
    monkeypatch.setattr(manager, "_spawn", spawn)
    monkeypatch.setattr(manager, "_prepare", lambda sandbox_id: events.append(("prepare", sandbox_id)) or "new-launcher")
    monkeypatch.setattr(manager, "_verify", verify)
    monkeypatch.setattr(manager, "_publish", lambda sandbox_id, launcher: events.append(("publish", sandbox_id, launcher)))

    with pytest.raises(RuntimeError, match="still starting"):
        manager.reconcile()

    pending = json.loads(manager.pending_path.read_text(encoding="utf-8"))
    assert pending["id"] == "new-lease"
    assert pending["operation_id"]
    assert pending["endpoint"] == manager.endpoint
    assert not any(event[0] == "publish" for event in events if isinstance(event, tuple))

    assert manager.reconcile() is True
    assert events.count("spawn") == 2
    assert operation_ids == [pending["operation_id"], pending["operation_id"]]
    assert len(leases) == 1
    assert [event for event in events if isinstance(event, tuple) and event[0] == "publish"] == [
        ("publish", "new-lease", "new-launcher")
    ]
    assert not manager.pending_path.exists()
    assert json.loads((manager.state / "sandbox-active.json").read_text(encoding="utf-8"))["operation_id"] == pending["operation_id"]


def test_keyed_spawn_replays_same_durable_key_after_lost_response_and_bridge_restart(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    foreign = lease("foreign-live")
    daemon = SimpleNamespace(leases={"foreign-live": foreign}, receipts={}, calls=[], creations=[], expired=set(), drop_response=True)
    install_fake_keyed_daemon(manager, monkeypatch, state=daemon)
    monkeypatch.setattr(manager, "_prepare", lambda sandbox_id: "retry-launcher")
    monkeypatch.setattr(manager, "_verify", lambda sandbox_id: None)
    monkeypatch.setattr(manager, "_publish", lambda sandbox_id, launcher: None)

    with pytest.raises(subprocess.TimeoutExpired):
        manager.reconcile()
    pending = json.loads(manager.pending_path.read_text(encoding="utf-8"))
    assert pending["operation_id"]
    assert "id" not in pending or pending["id"] is None
    assert daemon.creations == ["keyed-1"]

    restarted = create_manager(tmp_path)
    install_fake_keyed_daemon(restarted, monkeypatch, state=daemon)
    monkeypatch.setattr(restarted, "_prepare", lambda sandbox_id: "retry-launcher")
    monkeypatch.setattr(restarted, "_verify", lambda sandbox_id: None)
    monkeypatch.setattr(restarted, "_publish", lambda sandbox_id, launcher: None)
    assert restarted.reconcile() is True

    keyed_calls = [operation for _, operation in daemon.calls]
    assert keyed_calls == [pending["operation_id"], pending["operation_id"]]
    assert len(daemon.creations) == 1
    assert restarted.active_id == daemon.receipts[pending["operation_id"]]
    assert restarted.active_id != "foreign-live"
    assert daemon.leases["foreign-live"] == foreign


def test_pending_receipt_replay_rejects_alive_lease_with_reused_printable_id(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    stale_key = "00000000-0000-4000-8000-000000000601"
    foreign = lease("pending-reused-id")
    daemon = SimpleNamespace(
        leases={"pending-reused-id": foreign}, receipts={}, calls=[], creations=[],
        expired={stale_key}, drop_response=False,
    )
    manager.pending_path.write_text(
        json.dumps({"operation_id": stale_key, "endpoint": manager.endpoint, "id": "pending-reused-id"}),
        encoding="utf-8",
    )
    install_fake_keyed_daemon(manager, monkeypatch, state=daemon)
    prepared = []
    monkeypatch.setattr(manager, "_prepare", lambda sandbox_id: prepared.append(sandbox_id) or "launcher")
    monkeypatch.setattr(manager, "_verify", lambda sandbox_id: None)
    monkeypatch.setattr(manager, "_publish", lambda sandbox_id, launcher: None)

    assert manager.reconcile() is True

    assert daemon.calls[0][1] == stale_key
    assert daemon.calls[1][1] != stale_key
    assert manager.active_id == daemon.receipts[daemon.calls[1][1]]
    assert manager.active_id != "pending-reused-id"
    assert prepared == [manager.active_id]
    assert daemon.leases["pending-reused-id"] == foreign


def test_published_receipt_is_replayed_after_restart_and_reused_id_is_not_adopted(tmp_path, monkeypatch):
    operation_id = "00000000-0000-4000-8000-000000000602"
    manager = create_manager(tmp_path, endpoint="pipe://keyed", sandbox_id="published-id")
    active_path = manager.state / "sandbox-active.json"
    active_path.write_text(
        json.dumps({"operation_id": operation_id, "endpoint": manager.active_endpoint, "id": manager.active_id}),
        encoding="utf-8",
    )
    daemon = SimpleNamespace(
        leases={"published-id": lease("published-id")}, receipts={operation_id: "published-id"},
        calls=[], creations=[], expired=set(), drop_response=False,
    )
    install_fake_keyed_daemon(manager, monkeypatch, state=daemon)
    monkeypatch.setattr(manager, "_verify", lambda sandbox_id: None)
    monkeypatch.setattr(manager, "_prepare", lambda sandbox_id: "launcher")
    monkeypatch.setattr(manager, "_publish", lambda sandbox_id, launcher: None)

    assert manager.reconcile() is True
    assert daemon.calls == [(manager.endpoint, operation_id)]
    assert daemon.creations == []
    assert manager.active_id == "published-id"

    # A restart with the same printable ID present but the old operation expired
    # must create a new receipt instead of trusting list membership by ID.
    restarted = create_manager(tmp_path, endpoint="pipe://keyed", sandbox_id="published-id")
    foreign = lease("published-id")
    daemon.leases["published-id"] = foreign
    daemon.expired.add(operation_id)
    prior_calls = len(daemon.calls)
    install_fake_keyed_daemon(restarted, monkeypatch, state=daemon)
    prepared = []
    monkeypatch.setattr(restarted, "_verify", lambda sandbox_id: None)
    monkeypatch.setattr(restarted, "_prepare", lambda sandbox_id: prepared.append(sandbox_id) or "new-launcher")
    monkeypatch.setattr(restarted, "_publish", lambda sandbox_id, launcher: None)

    assert restarted.reconcile() is True

    assert daemon.calls[prior_calls][1] == operation_id
    assert daemon.calls[prior_calls + 1][1] != operation_id
    assert restarted.active_id == daemon.receipts[daemon.calls[prior_calls + 1][1]]
    assert restarted.active_id != "published-id"
    assert prepared == [restarted.active_id]
    assert daemon.leases["published-id"] == foreign


def test_migration_barrier_waits_for_recorded_legacy_journal_to_finish(tmp_path, monkeypatch):
    old_endpoint = "pipe://mini-pod"
    new_endpoint = "pipe://mini-pod-keyed"
    journal = tmp_path / "legacy.journal"
    journal.write_text(
        "sandbox_id=old-lease\nstate=active\npid=1234\nstart_token=5678\n", encoding="utf-8"
    )
    settings = {
        "spawn_endpoint": new_endpoint,
        "legacy_sandbox": {
            "endpoint": old_endpoint, "id": "old-lease", "journal_path": str(journal),
            "pid": 1234, "start_token": 5678,
        },
    }
    manager = create_manager(tmp_path, settings_data=settings)
    daemon = install_fake_keyed_daemon(manager, monkeypatch)
    monkeypatch.setattr(manager, "_prepare", lambda sandbox_id: "new-launcher")
    monkeypatch.setattr(manager, "_verify", lambda sandbox_id: None)
    monkeypatch.setattr(manager, "_publish", lambda sandbox_id, launcher: None)

    with pytest.raises(RuntimeError, match="still being removed"):
        manager.reconcile()
    assert daemon.calls == []
    assert not manager.pending_path.exists()

    journal.write_text(
        "sandbox_id=old-lease\nstate=destroying\npid=none\nstart_token=none\n", encoding="utf-8"
    )
    assert manager.reconcile() is True
    assert daemon.calls[0][0] == new_endpoint
    assert manager.active_id == "keyed-1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exit_code", 1),
        ("timed_out", True),
        ("stdout_truncated", True),
        ("stderr_truncated", True),
        ("stdout", "false"),
    ],
)
def test_verify_rejects_unsuccessful_rootless_probe(tmp_path, monkeypatch, field, value):
    manager = create_manager(tmp_path)
    result = {
        "exit_code": 0,
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout": "true",
    }
    result[field] = value
    monkeypatch.setattr(manager, "_command", lambda *args, **kwargs: result)

    with pytest.raises(RuntimeError, match="readiness check failed"):
        manager._verify("lease")


def test_verify_checks_rootless_podman_and_pinned_codex_cli_version(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    outputs = ["true", "codex-cli 0.167.0"]
    commands = []

    def command(*args, **kwargs):
        commands.append((args, kwargs))
        return {
            "exit_code": 0,
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout": outputs.pop(0),
        }

    monkeypatch.setattr(manager, "_command", command)
    manager._verify("lease")

    assert [call[0][-1] for call in commands] == ["verify", "check"]
    assert all(call[1]["timeout"] == 75 for call in commands)


def test_verify_rejects_a_different_codex_cli_version(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    outputs = ["true", "codex-cli 0.168.0"]

    def command(*args, **kwargs):
        return {
            "exit_code": 0,
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout": outputs.pop(0),
        }

    monkeypatch.setattr(manager, "_command", command)

    with pytest.raises(RuntimeError, match="readiness check failed"):
        manager._verify("lease")


def test_concurrent_startup_waiters_share_one_sandbox_creation(tmp_path, monkeypatch):
    manager = create_manager(tmp_path)
    list_started = threading.Event()
    allow_list = threading.Event()
    waiters_entered = threading.Event()
    waiter_count = 0
    waiter_lock = threading.Lock()
    leases = {}
    spawned = []

    def list_leases():
        list_started.set()
        assert allow_list.wait(timeout=3)
        return list(leases.values())

    def spawn():
        spawned.append("new-lease")
        leases["new-lease"] = lease("new-lease")
        return "new-lease"

    monkeypatch.setattr(manager, "_list", list_leases)
    monkeypatch.setattr(manager, "_spawn", spawn)
    monkeypatch.setattr(manager, "_prepare", lambda sandbox_id: "new-launcher")
    monkeypatch.setattr(manager, "_verify", lambda sandbox_id: None)
    monkeypatch.setattr(manager, "_publish", lambda sandbox_id, launcher: None)

    original_wait_ready = manager.wait_ready

    def tracked_wait_ready(timeout):
        nonlocal waiter_count
        with waiter_lock:
            waiter_count += 1
            if waiter_count == 8:
                waiters_entered.set()
        return original_wait_ready(timeout)

    monkeypatch.setattr(manager, "wait_ready", tracked_wait_ready)
    manager.start()
    assert list_started.wait(timeout=2)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            waiters = [pool.submit(manager.wait_ready, 3) for _ in range(8)]
            assert waiters_entered.wait(timeout=2)
            allow_list.set()
            assert all(waiter.result(timeout=4) for waiter in waiters)
    finally:
        allow_list.set()
        manager.stop()
        manager._thread.join(timeout=2)

    assert spawned == ["new-lease"]
    assert manager.active_id == "new-lease"


def test_unready_manager_returns_retryable_health_and_chat_responses(monkeypatch):
    bridge_globals = bridge["Handler"].do_POST.__globals__
    monkeypatch.setitem(bridge_globals, "run_turn", lambda *args, **kwargs: pytest.fail("chat must wait for readiness"))
    server = bridge["ThreadingHTTPServer"](("127.0.0.1", 0), bridge["Handler"])
    server.authorization = "Bearer test-key"
    server.profile = Path("profile")
    server.workspace = Path("workspace")
    server.codex = "codex"

    class UnreadyManager:
        def is_ready(self):
            return False

        def wait_ready(self, timeout):
            assert timeout == 5
            return False

        def kick(self):
            pass

    server.manager = UnreadyManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as health:
            urlopen(f"http://127.0.0.1:{server.server_port}/healthz", timeout=5)
        assert health.value.code == 503
        assert health.value.headers["Retry-After"] == "10"
        assert json.loads(health.value.read()) == {"status": "initializing"}

        request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            data=json.dumps({"model": "gpt-6-luna", "messages": [{"role": "user", "content": "Reply OK."}]}).encode(),
            headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as chat:
            urlopen(request, timeout=5)
        assert chat.value.code == 503
        assert chat.value.headers["Retry-After"] == "10"
        assert json.loads(chat.value.read()) == {"error": "Mini-pod executor is initializing"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
