import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from litellm_agent_rpc.management import ManagementError, ServiceManager
from litellm_agent_rpc.registry import BACKENDS


@pytest.fixture
def config_file(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "enabled": {"codex": True},
                "keys": {"router": "sk-management-test-12345678901234567890"},
                "ports": {"router": port},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_capabilities_do_not_promote_gated_backends():
    assert BACKENDS["claude"]["gated_reason"] is None
    assert BACKENDS["kiro"]["gated_reason"] == "kiro_profile_isolation_not_verified"
    assert BACKENDS["antigravity"]["auth_actions"] == ()
    assert "login" in BACKENDS["claude"]["auth_actions"]


def test_unowned_listener_cannot_be_stopped_or_logged_out(config_file):
    port = json.loads(config_file.read_text())["ports"]["router"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen()
        manager = ServiceManager(config_file)
        assert manager.status()["state"] == "unknown"
        with pytest.raises(ManagementError, match="unowned"):
            manager.stop()
        with pytest.raises(ManagementError, match="Stop the selected service"):
            manager.auth_command("codex", "logout")


@pytest.fixture
def controlled(config_file):
    state = {"active": 1, "draining": False, "exited": False, "pid": 271828}
    token, instance = "test-control-token", "test-instance"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.handle_action()

        def do_POST(self):
            self.handle_action()

        def handle_action(self):
            if self.headers.get("Authorization") != "Bearer " + token:
                self.send_response(401)
                self.end_headers()
                return
            action = self.path.rsplit("/", 1)[-1]
            if action == "drain":
                state["draining"] = True
            elif action == "resume":
                state["draining"] = False
            elif action == "stop":
                assert state["draining"] and not state["active"]
                state["exited"] = True
            payload = json.dumps(
                {
                    "state": "draining" if state["draining"] else "running",
                    "active_requests": state["active"],
                    "pid": state["pid"],
                    "instance_id": instance,
                    "config_digest": "running-revision",
                    "uptime_seconds": 1,
                    "usage": {"available": False},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Process:
        pid = state["pid"]

        def poll(self):
            return 0 if state["exited"] else None

    manager = ServiceManager(config_file)
    manager._child = Process()
    manager._instance = instance
    manager._token = token
    manager._port = server.server_port
    try:
        yield manager, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_drain_timeout_does_not_kill_and_resume_reopens(controlled):
    manager, state = controlled
    with pytest.raises(ManagementError, match="remains draining"):
        manager.stop(timeout=0.2)
    assert state["draining"] and not state["exited"]
    assert manager.status()["active_requests"] == 1
    assert manager.resume()["state"] == "running"
    state["active"] = 0
    assert manager.stop(timeout=3)["state"] == "stopped"
    assert state["exited"]


def test_control_identity_mismatch_is_never_owned_as_running(controlled):
    manager, state = controlled
    state["pid"] += 1
    status = manager.status()
    assert status["state"] != "running"
    with pytest.raises(ManagementError):
        manager.stop()
    assert not state["exited"]


def test_saved_and_running_revision_are_distinct(controlled):
    manager, _ = controlled
    status = manager.status()
    assert status["config_revision"] == "running-revision"
    assert status["saved_config_revision"] != status["config_revision"]


def test_auth_is_explicit_fixed_argv_and_not_executed(config_file, monkeypatch):
    manager = ServiceManager(config_file)
    monkeypatch.setattr("litellm_agent_rpc.management.shutil.which", lambda name: name)
    assert manager.auth_command("codex", "login") == ["codex", "login"]
    assert manager.auth_command("kiro", "check") == ["kiro-cli", "whoami"]
    with pytest.raises(ManagementError, match="not supported"):
        manager.auth_command("antigravity", "login")
    with pytest.raises(ManagementError, match="not supported"):
        manager.auth_command("codex", "login --with-api-key")


def test_backend_inspection_uses_version_only_and_keeps_gate(config_file, monkeypatch):
    manager = ServiceManager(config_file)
    calls = []
    monkeypatch.setattr(
        "litellm_agent_rpc.backend_inspect.shutil.which", lambda path: path
    )
    monkeypatch.setattr(
        "litellm_agent_rpc.backend_inspect._bundled_claude", lambda: "claude-binary"
    )
    monkeypatch.setattr(
        "litellm_agent_rpc.backend_inspect.importlib.metadata.version",
        lambda _: "0.2.180",
    )

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=b"2.1.321\nsecret should not be returned", stderr=b""
        )

    monkeypatch.setattr("litellm_agent_rpc.backend_inspect.subprocess.run", run)
    result = manager.inspect_backend("claude")
    assert calls[0][0] == ["claude-binary", "--version"]
    assert calls[0][1]["timeout"] == 10
    assert result["version"] == "2.1.321"
    assert result["dependency"]["compatible"]
    assert result["gated_reason"] is None
    assert result["scope"] == "local_saved"
    assert "secret" not in str(result)


def test_inspection_does_not_replace_invalid_executable_with_default(
    config_file, monkeypatch
):
    data = json.loads(config_file.read_text())
    data["executables"] = {"codex": "invalid\npath"}
    config_file.write_text(json.dumps(data))
    monkeypatch.setattr(
        "litellm_agent_rpc.backend_inspect.subprocess.run",
        lambda *a, **kw: pytest.fail("must not run"),
    )
    result = ServiceManager(config_file).inspect_backend("codex")
    assert result["resolved_executable"] is None
    assert result["version"] is None


def test_local_extra_install_is_explicit_pinned_and_requires_stopped(
    config_file, monkeypatch
):
    manager = ServiceManager(config_file)
    monkeypatch.setattr(
        "litellm_agent_rpc.management.shutil.which",
        lambda name: "uv" if name == "uv" else name,
    )
    monkeypatch.setattr(
        "litellm_agent_rpc.management.subprocess.run",
        lambda *a, **kw: pytest.fail("must not install"),
    )
    assert manager.install_command("claude") == [
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "claude-agent-sdk==0.2.180",
    ]
    monkeypatch.setattr(manager, "status", lambda: {"state": "running"})
    with pytest.raises(ManagementError, match="Stop the selected service"):
        manager.install_command("claude")
    with pytest.raises(ManagementError, match="No supported"):
        manager.install_command("codex")


def test_docker_backend_inspection_uses_owned_container(
    config_file, tmp_path, monkeypatch
):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}", encoding="utf-8")
    manager = ServiceManager(config_file, "docker", compose, "test-project")
    monkeypatch.setattr(manager, "status", lambda: {"state": "running", "owned": True})
    calls = []
    response = {
        "backend": "kiro",
        "resolved_executable": "/bin/kiro-cli",
        "version": "3.1.0",
    }

    def fake_docker(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(response).encode())

    monkeypatch.setattr(manager, "_docker", fake_docker)
    result = manager.inspect_backend("kiro")
    assert calls[0][0][-3:] == [
        "litellm_agent_rpc.backend_inspect",
        "/app/config.json",
        "kiro",
    ]
    assert result["scope"] == "docker_running"
    assert result["gated_reason"] == "kiro_profile_isolation_not_verified"
    with pytest.raises(ManagementError, match="Rebuild"):
        manager.install_command("claude")


def test_docker_refuses_unowned_selected_container(config_file, tmp_path, monkeypatch):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}", encoding="utf-8")
    manager = ServiceManager(config_file, "docker", compose, "test-project")
    monkeypatch.setattr(manager, "_selected_container", lambda: "a" * 64)
    assert manager.status()["state"] == "unknown"
    with pytest.raises(ManagementError, match="not owned"):
        manager.start()
    with pytest.raises(ManagementError):
        manager.auth_command("codex", "logout")


def test_docker_failed_start_stops_its_owned_container(
    config_file, tmp_path, monkeypatch
):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}", encoding="utf-8")
    manager = ServiceManager(
        config_file, "docker", compose, "test-project", startup_timeout=0
    )
    stopped = []
    monkeypatch.setattr(manager, "status", lambda: {"state": "stopped"})
    monkeypatch.setattr(
        manager, "_launch_docker", lambda: setattr(manager, "_container", "a" * 64)
    )
    monkeypatch.setattr(manager, "_stop_failed_container", stopped.append)
    with pytest.raises(ManagementError, match="deadline"):
        manager.start()
    assert stopped == ["a" * 64]


def test_compose_override_keeps_control_secret_out_of_file(config_file, tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}", encoding="utf-8")
    manager = ServiceManager(config_file, "docker", compose, "test-project")
    manager._new_identity()
    contents = manager._override_path().read_text(encoding="utf-8")
    assert manager._token not in contents
    assert manager._instance not in contents
    document = json.loads(contents)
    override = document["services"]["litellm"]
    assert override["environment"]["AGENT_RPC_TELEMETRY_PATH"] == (
        "/home/agent/.local/state/agent-rpc/usage.sqlite"
    )
    assert override["volumes"][0]["source"] == str(config_file.resolve())
    assert override["volumes"][1]["source"] == "agent-rpc-manager-data-litellm"
    assert "agent-rpc-manager-data-litellm" in document["volumes"]
    assert (
        override["environment"]["AGENT_RPC_ENVIRONMENT"]
        == "docker:test-project:litellm"
    )


@pytest.mark.parametrize("host_ip", ["0.0.0.0", "", "::"])
def test_compose_rejects_external_publish_before_start(
    config_file, tmp_path, monkeypatch, host_ip
):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}", encoding="utf-8")
    manager = ServiceManager(config_file, "docker", compose, "test-project")
    config = {
        "services": {
            "litellm": {
                "ports": [
                    {
                        "host_ip": host_ip,
                        "target": 4000,
                        "published": "4000",
                        "protocol": "tcp",
                    }
                ]
            }
        }
    }
    monkeypatch.setattr(
        manager,
        "_docker",
        lambda *_: SimpleNamespace(returncode=0, stdout=json.dumps(config).encode()),
    )
    with pytest.raises(ManagementError, match="127.0.0.1"):
        manager._validate_compose()


@pytest.mark.parametrize("restart", ["always", "unless-stopped", "on-failure"])
def test_compose_rejects_auto_restart(config_file, tmp_path, monkeypatch, restart):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}", encoding="utf-8")
    manager = ServiceManager(config_file, "docker", compose, "test-project")
    config = {
        "services": {
            "litellm": {
                "restart": restart,
                "ports": [
                    {"host_ip": "127.0.0.1", "target": 4000, "published": "4000"}
                ],
            }
        }
    }
    monkeypatch.setattr(
        manager,
        "_docker",
        lambda *_: SimpleNamespace(returncode=0, stdout=json.dumps(config).encode()),
    )
    with pytest.raises(ManagementError, match="automatic restart"):
        manager._validate_compose()


def test_doctor_never_returns_arbitrary_cli_output(config_file, monkeypatch):
    manager = ServiceManager(config_file)
    output = json.dumps(
        {"backend": "claude", "preflight": "failed", "reason": "untrusted secret"}
    ).encode()
    monkeypatch.setattr(
        "litellm_agent_rpc.management.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout=output),
    )
    report = manager.doctor()
    assert report["state"] == "failed"
    assert report["backends"][0]["reason"] == "preflight_failed"
    assert "secret" not in str(report)


def test_real_local_process_starts_and_drains_without_cli_calls(config_file):
    manager = ServiceManager(config_file, startup_timeout=60)
    try:
        running = manager.start()
        assert running["state"] == "running"
        assert running["owned"] and running["pid"]
        assert running["config_revision"] == running["saved_config_revision"]
        assert running["active_requests"] == 0
        assert manager.stop(timeout=30)["state"] == "stopped"
    finally:
        # Keep a failed integration test from leaving its owned process behind.
        if manager._child is not None and manager._child.poll() is None:
            manager._close_tree(kill=True)
            manager._child.wait(timeout=10)
