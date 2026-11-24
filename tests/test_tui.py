import asyncio
import concurrent.futures
import contextlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.app import SuspendNotSupported
from textual.widgets import (
    Button,
    Input,
    Select,
    Static,
    Switch,
    TabbedContent,
    TextArea,
)

from litellm_agent_rpc.config import load_config
from litellm_agent_rpc.control import config_digest
from litellm_agent_rpc.management import ManagementError
from litellm_agent_rpc.tui import DraftStore, RouterApp, launch


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "keys": {"router": "sk-fixture-keep-this-key-123456789"},
                "ports": {"router": 45123},
                "enabled": {
                    "codex": True,
                    "antigravity": False,
                    "claude": False,
                    "kiro": False,
                },
                "executables": {"codex": "codex"},
                "antigravity_models": {
                    "antigravity": {"model": "example-manual-model"}
                },
                "extra_unrelated": {"keep": "this"},
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeManager:
    def __init__(self, config_path, mode="local", **kwargs):
        self.mode = mode
        self.config_path = Path(config_path)
        self.calls = []
        self.started = False
        self.gate = None

    def status(self):
        self.calls.append("status")
        digest = config_digest(load_config(self.config_path))
        return {
            "state": "running" if self.started else "stopped",
            "owned": self.started,
            "mode": self.mode,
            "active_requests": 0 if self.started else None,
            "saved_config_revision": digest,
            "config_revision": digest if self.started else None,
            "usage": None,
        }

    def start(self):
        self.calls.append("start")
        if self.gate is not None:
            self.gate.wait(timeout=5)
        self.started = True
        return self.status()

    def stop(self):
        self.calls.append("stop")
        self.started = False
        return self.status()

    def restart(self):
        self.calls.append("restart")
        return self.status()

    def resume(self):
        self.calls.append("resume")
        return self.status()

    def doctor(self):
        self.calls.append("doctor")
        return {"state": "passed", "backends": []}

    def inspect_backend(self, backend):
        self.calls.append(("inspect", backend))
        return {
            "backend": backend,
            "scope": "local_saved" if self.mode == "local" else "docker_running",
            "configured_executable": "fake-cli",
            "resolved_executable": "fake-cli",
            "version": "1.2.3",
            "dependency": {
                "name": "claude-agent-sdk",
                "installed": False,
                "version": None,
                "required_version": "0.2.180",
                "compatible": False,
            },
            "gated_reason": "claude_live_acceptance_pending",
        }

    def install_command(self, backend):
        self.calls.append(("install", backend))
        return ["fake-uv", "install", "claude-agent-sdk==0.2.180"]

    def auth_command(self, backend, action):
        self.calls.append(("auth", backend, action))
        return ["fake-cli", action]

    def close(self):
        self.calls.append("close")


async def settled(app):
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not app.busy:
            return
    pytest.fail("UI operation did not finish")


async def test_open_is_read_only_and_unsupported_auth_stays_disabled(config_file):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(120, 42)) as pilot:
        await settled(app)
        assert manager.calls == ["status"]
        assert "N/A" in str(app.query_one("#usage-info", Static).content)
        app._select_backend("antigravity")
        assert app.query_one("#login", Button).disabled
        assert app.query_one("#logout", Button).disabled
        app._select_backend("kiro")
        assert "Blocked" in str(app.query_one("#notice", Static).content)
        assert not app.draft_store.path.exists()
        await pilot.pause()
    assert not any(isinstance(item, tuple) for item in manager.calls)


async def test_runtime_port_does_not_report_stale_saved_port(config_file):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(100, 35)):
        await settled(app)
        status = manager.status()
        status.update(state="running", owned=True, port=48200)
        app._set_status(status)
        text = str(app.query_one("#service-status", Static).content)
        assert "Port: 48200" in text
        assert "Port: 45123" not in text


async def test_route_edit_save_keeps_key_and_unrelated_configuration(config_file):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(120, 42)) as pilot:
        await settled(app)
        app.query_one("#route-alias", Input).value = "coding-fast"
        app.query_one("#route-backend", Select).value = "codex"
        app.query_one("#route-model", Input).value = "manual-model"
        app.query_one("#route-enabled", Switch).value = True
        app._stage_route()
        assert app.dirty
        app._default_route()
        await pilot.click("#save-config")
        await settled(app)
        saved = load_config(config_file)
        assert saved["routes"]["coding-fast"] == {
            "backend": "codex",
            "model": "manual-model",
            "settings": {},
            "enabled": True,
        }
        assert saved["default_route"] == "coding-fast"
        assert saved["keys"]["router"] == "sk-fixture-keep-this-key-123456789"
        assert saved["extra_unrelated"] == {"keep": "this"}
        assert not app.dirty


async def test_disable_backend_clears_default_and_invalid_route_is_rejected(
    config_file,
):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(120, 42)):
        await settled(app)
        app._select_route("codex-default")
        app._default_route()
        app._select_backend("codex")
        app.query_one("#backend-enabled", Switch).value = False
        app._stage_backend()
        assert "default_route" not in app.config
        assert app.config["enabled"]["codex"] is False
        app._new_route()
        app.query_one("#route-alias", Input).value = "bad alias!"
        app._stage_route()
        assert "bad alias!" not in app.config.get("routes", {})
        assert "Invalid route" in str(app.query_one("#notice", Static).content)


async def test_save_detects_external_edit_without_overwriting(config_file):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(120, 42)):
        await settled(app)
        app._select_backend("codex")
        app.query_one("#backend-executable", Input).value = "new-codex"
        app._stage_backend()
        value = json.loads(config_file.read_text(encoding="utf-8"))
        value["outside_change"] = "preserve"
        config_file.write_text(json.dumps(value), encoding="utf-8")
        app._save_config()
        await settled(app)
        assert app.dirty
        assert (
            json.loads(config_file.read_text(encoding="utf-8"))["outside_change"]
            == "preserve"
        )


async def test_service_operation_is_nonblocking_and_quit_drains_local(config_file):
    manager = FakeManager(config_file)
    manager.gate = threading.Event()
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(120, 42)) as pilot:
        await settled(app)
        app._service_operation("start")
        await asyncio.sleep(0.05)
        assert app.busy
        await pilot.press("tab")
        assert app.is_running
        manager.gate.set()
        await settled(app)
        assert manager.started
        app.action_quit()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not app.is_running:
                break
        assert not manager.started
        assert "stop" in manager.calls


async def test_switch_back_reuses_manager_and_quit_drains_local_and_docker(config_file):
    local = FakeManager(config_file)
    app = RouterApp(config_file, manager=local, manager_factory=FakeManager)
    async with app.run_test(size=(120, 42)):
        await settled(app)
        app._service_operation("start")
        await settled(app)
        app.query_one("#target-mode", Select).value = "docker"
        app.query_one("#target-compose", Input).value = "example/docker/compose.yaml"
        app.query_one("#target-project", Input).value = "test-project"
        app._switch_target()
        await settled(app)
        docker = app.manager
        assert docker is not local
        app._service_operation("start")
        await settled(app)
        app.query_one("#target-mode", Select).value = "local"
        app._switch_target()
        await settled(app)
        assert app.manager is local
        app.query_one("#target-mode", Select).value = "docker"
        app.query_one("#target-compose", Input).value = "example/docker/compose.yaml"
        app.query_one("#target-project", Input).value = "test-project"
        app._switch_target()
        await settled(app)
        assert app.manager is docker
        app.action_quit()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not app.is_running:
                break
        assert not app.is_running
        assert "stop" in local.calls and "stop" in docker.calls
        assert "close" in local.calls and "close" in docker.calls


async def test_quit_drain_failure_keeps_app_open(config_file):
    class TimeoutManager(FakeManager):
        def stop(self):
            self.calls.append("stop")
            raise ManagementError("Drain timed out; service remains draining")

    manager = TimeoutManager(config_file, mode="docker")
    manager.started = True
    app = RouterApp(config_file, mode="docker", manager=manager)
    async with app.run_test(size=(120, 42)):
        await settled(app)
        app.action_quit()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "stop" in manager.calls and not app.shutting_down:
                break
        assert app.is_running
        assert manager.started
        assert not app.shutting_down
        assert "Drain timed out" in str(app.query_one("#notice", Static).content)


async def test_explicit_auth_uses_inherited_terminal_and_logout_confirmation(
    config_file, monkeypatch
):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    invoked = []
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    monkeypatch.setattr(
        "litellm_agent_rpc.tui.subprocess.run",
        lambda command, **kwargs: (
            invoked.append((command, kwargs)) or SimpleNamespace(returncode=0)
        ),
    )
    async with app.run_test(size=(120, 42)) as pilot:
        await settled(app)
        app._auth("login")
        await settled(app)
        assert invoked == [(["fake-cli", "login"], {"check": False})]
        app._auth("logout")
        await pilot.pause()
        assert len(invoked) == 1
        await pilot.click("#confirm-yes")
        await settled(app)
        assert invoked[1][0] == ["fake-cli", "logout"]


async def test_auth_reports_terminal_handoff_unavailable(config_file, monkeypatch):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    invoked = []

    @contextlib.contextmanager
    def unsupported_terminal():
        raise SuspendNotSupported()
        yield

    monkeypatch.setattr(app, "suspend", unsupported_terminal)
    monkeypatch.setattr(
        "litellm_agent_rpc.tui.subprocess.run", lambda *a, **k: invoked.append(a)
    )
    async with app.run_test(size=(100, 35)):
        await settled(app)
        app._auth("login")
        await settled(app)
        assert "Terminal handoff is unavailable" in str(
            app.query_one("#notice", Static).content
        )
        assert invoked == []


async def test_auth_refuses_running_retained_service_in_same_docker_project(
    config_file, monkeypatch
):
    selected = FakeManager(config_file, mode="docker")
    other = FakeManager(config_file, mode="docker")
    other.started = True
    app = RouterApp(
        config_file, mode="docker", project="same-project", manager=selected
    )
    app.managers[("docker", "another-compose", "same-project", "other-service")] = other
    invoked = []
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    monkeypatch.setattr(
        "litellm_agent_rpc.tui.subprocess.run", lambda *a, **k: invoked.append(a)
    )
    async with app.run_test(size=(100, 35)):
        await settled(app)
        app._auth("login")
        await settled(app)
        assert "sharing this CLI profile" in str(
            app.query_one("#notice", Static).content
        )
        assert not any(
            isinstance(item, tuple) and item[0] == "auth" for item in selected.calls
        )
        assert invoked == []


async def test_backend_inspect_and_extra_install_require_explicit_actions(
    config_file, monkeypatch
):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    invoked = []
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    monkeypatch.setattr(
        "litellm_agent_rpc.tui.subprocess.run",
        lambda command, **kwargs: (
            invoked.append(command) or SimpleNamespace(returncode=0)
        ),
    )
    async with app.run_test(size=(120, 42)) as pilot:
        await settled(app)
        assert manager.calls == ["status"]
        app._select_backend("claude")
        app._inspect_backend()
        await settled(app)
        details = str(app.query_one("#backend-details", Static).content)
        assert "local_saved" in details and "1.2.3" in details
        assert "claude-agent-sdk" in details and "not installed" in details
        assert invoked == []
        app._install_extra()
        await pilot.pause()
        assert invoked == []
        await pilot.click("#confirm-yes")
        await settled(app)
        assert invoked == [["fake-uv", "install", "claude-agent-sdk==0.2.180"]]
        assert "not inspected" in str(app.query_one("#backend-details", Static).content)


async def test_backend_table_selection_and_inspect_button_at_100_by_35(config_file):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(100, 35)) as pilot:
        await settled(app)
        app.query_one(TabbedContent).active = "backends"
        await pilot.pause()
        table = app.query_one("#backend-table")
        table.move_cursor(row=2)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected_backend == "claude"
        inspect = app.query_one("#inspect-backend")
        inspect.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert await pilot.click("#inspect-backend")
        await settled(app)
        assert ("inspect", "claude") in manager.calls


async def test_extra_install_refuses_while_local_service_runs(config_file, monkeypatch):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    invoked = []
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    monkeypatch.setattr(
        "litellm_agent_rpc.tui.subprocess.run", lambda *a, **k: invoked.append(a)
    )
    async with app.run_test(size=(120, 42)) as pilot:
        await settled(app)
        app._select_backend("claude")
        manager.started = True
        app._install_extra()
        await pilot.pause()
        await pilot.click("#confirm-yes")
        await settled(app)
        assert "Stop all managed local services" in str(
            app.query_one("#notice", Static).content
        )
        assert not any(
            isinstance(item, tuple) and item[0] == "install" for item in manager.calls
        )
        assert invoked == []


def test_workflow_draft_is_disabled_and_checks_external_edits(config_file):
    store = DraftStore(config_file)
    assert store.read() == "{}"
    store.save('{"purpose":"future task"}')
    value = json.loads(store.path.read_text(encoding="utf-8"))
    assert value == {"enabled": False, "draft": {"purpose": "future task"}}
    store.path.write_text('{"external":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        store.save("{}")
    with pytest.raises(ValueError):
        store.save("[]")


def test_simultaneous_draft_saves_cannot_overwrite_one_another(
    config_file, monkeypatch
):
    first, second = DraftStore(config_file), DraftStore(config_file)
    entered = threading.Event()
    release = threading.Event()
    real_replace = os.replace

    def blocked_replace(*args):
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test timed out")
        return real_replace(*args)

    monkeypatch.setattr("litellm_agent_rpc.tui.os.replace", blocked_replace)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first.save, '{"name":"first"}')
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(ValueError, match="edited elsewhere"):
                second.save('{"name":"second"}')
        finally:
            release.set()
        future.result(timeout=5)
    assert json.loads(first.path.read_text(encoding="utf-8"))["draft"] == {
        "name": "first"
    }


async def test_workflow_editor_saves_only_explicitly(config_file):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(120, 42)):
        await settled(app)
        app.query_one("#workflow-editor", TextArea).load_text('{"task":"draft"}')
        assert not app.draft_store.path.exists()
        draft = app.query_one("#workflow-editor", TextArea).text
        app._run_operation("Save workflow draft", lambda: app.draft_store.save(draft))
        await settled(app)
        assert (
            json.loads(app.draft_store.path.read_text(encoding="utf-8"))["enabled"]
            is False
        )
        assert all(item in ("status",) for item in manager.calls)


async def test_edit_while_saving_workflow_keeps_unsaved_indicator(
    config_file, monkeypatch
):
    manager = FakeManager(config_file)
    app = RouterApp(config_file, manager=manager)
    entered = threading.Event()
    release = threading.Event()
    original = app.draft_store.save

    def blocking_save(text):
        entered.set()
        release.wait(timeout=5)
        original(text)

    monkeypatch.setattr(app.draft_store, "save", blocking_save)
    async with app.run_test(size=(100, 35)) as pilot:
        await settled(app)
        app.query_one(TabbedContent).active = "workflows"
        await pilot.pause()
        editor = app.query_one("#workflow-editor", TextArea)
        editor.load_text('{"draft":"first"}')
        await pilot.click("#save-draft")
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        editor.load_text('{"draft":"second"}')
        await pilot.pause()
        release.set()
        await settled(app)
        assert app.workflow_dirty
        data = json.loads(app.draft_store.path.read_text(encoding="utf-8"))
        assert data["draft"] == {"draft": "first"}


def test_missing_config_has_actionable_guidance(tmp_path, capsys):
    assert launch(tmp_path / "missing.json") == 1
    assert "Create one first" in capsys.readouterr().err
