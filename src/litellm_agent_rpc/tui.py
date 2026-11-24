"""Terminal management UI. Nothing calls a backend until the user requests it."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)

from . import config as config_api
from .control import config_digest
from .management import ManagementError, ServiceManager
from .registry import BACKENDS


def _read_config(path):
    """Read a consistent revision for optimistic concurrency on save."""
    for _ in range(3):
        before = config_api.revision(path)
        value = config_api.load_config(path)
        after = config_api.revision(path)
        if before == after:
            return value, after
    raise ValueError("Configuration is changing; try again")


def _file_revision(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _target_key(mode, compose_path, project, service):
    if mode == "local":
        return "local", None, None, "litellm"
    compose = str(Path(compose_path).resolve()) if compose_path else None
    return mode, compose, project, service


class DraftStore:
    """An inert, user-owned JSON draft; the router does not consume this file."""

    def __init__(self, config_path):
        config_path = Path(config_path)
        self.path = config_path.with_name(config_path.stem + ".workflow-draft.json")
        self.revision = _file_revision(self.path)

    def read(self):
        if self.revision is None:
            return "{}"
        if self.path.stat().st_size > 262144:
            raise ValueError("Draft is too large")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("enabled") is not False:
            raise ValueError("Invalid workflow draft envelope")
        draft = value.get("draft")
        if not isinstance(draft, dict):
            raise ValueError("Invalid workflow draft")
        return json.dumps(draft, ensure_ascii=False, indent=2)

    def save(self, text):
        if len(text.encode("utf-8")) > 262144:
            raise ValueError("Draft is too large")
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("Draft must be a JSON object")
        data = json.dumps(
            {"enabled": False, "draft": value}, ensure_ascii=False, indent=2
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError("Draft is being edited elsewhere") from None
        temp = None
        try:
            os.close(descriptor)
            if _file_revision(self.path) != self.revision:
                raise ValueError("Draft changed outside this app")
            fd, temp = tempfile.mkstemp(prefix=".workflow-", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
                file.write(data + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
            self.revision = _file_revision(self.path)
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
            lock.unlink(missing_ok=True)


class Confirm(ModalScreen[bool]):
    CSS = """
    Confirm { align: center middle; background: $background 70%; }
    #confirm-box { width: 74; height: auto; border: solid $accent; padding: 1 2; background: $surface; }
    #confirm-text { height: auto; margin-bottom: 1; }
    #confirm-actions { height: 3; }
    #confirm-actions Button { margin-right: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.prompt, id="confirm-text", markup=False)
            with Horizontal(id="confirm-actions"):
                yield Button("Confirm", variant="warning", id="confirm-yes")
                yield Button("Cancel", id="confirm-no")

    @on(Button.Pressed, "#confirm-yes")
    def confirmed(self):
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def cancelled(self):
        self.dismiss(False)

    def action_cancel(self):
        self.dismiss(False)


class RouterApp(App):
    TITLE = "Agent RPC Manager"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("f5", "refresh", "Refresh", priority=True),
    ]
    CSS = """
    Screen { layout: vertical; }
    #target-line { height: 2; padding: 0 1; background: $boost; }
    #notice { height: 2; padding: 0 1; color: $text; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0 1; }
    .page { height: 1fr; }
    .info { height: auto; padding: 0 1; }
    .form-row { height: 3; }
    .form-row Label { width: 18; padding: 1 0; }
    .form-row Input, .form-row Select { width: 1fr; }
    .form-row Switch { margin-top: 1; }
    .actions { height: 3; }
    .actions Button { min-width: 10; margin-right: 1; }
    DataTable { height: 8; margin-bottom: 1; }
    #route-table { height: 9; }
    #workflow-editor { height: 12; border: solid $accent; }
    """

    def __init__(
        self,
        config_path,
        mode="local",
        compose_path=None,
        project=None,
        service="litellm",
        manager=None,
        manager_factory=ServiceManager,
    ):
        super().__init__()
        self.config_path = Path(config_path)
        self.mode = mode
        self.compose_path = compose_path
        self.project = project
        self.service = service
        self.manager_factory = manager_factory
        self.manager = manager or manager_factory(
            config_path,
            mode=mode,
            compose_path=compose_path,
            project=project,
            service=service,
        )
        self.managers = {
            _target_key(mode, compose_path, project, service): self.manager
        }
        self.managers_closed = False
        self.inspections = {}
        self.capability_inspections = {}
        self.config, self.revision = _read_config(self.config_path)
        self.dirty = False
        self.workflow_dirty = False
        self.selected_backend = next(iter(BACKENDS))
        self.selected_route = None
        self.selected_pending = None
        self.status_data = {"state": "unknown", "mode": mode}
        self.busy = False
        self.shutting_down = False
        self.draft_store = DraftStore(self.config_path)
        self.draft_error = False
        try:
            self.draft_text = self.draft_store.read()
        except (OSError, ValueError, json.JSONDecodeError):
            self.draft_text = "{}"
            self.draft_error = True

    def compose(self) -> ComposeResult:
        yield Static("", id="target-line", markup=False)
        yield Static("", id="notice", markup=False)
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                with VerticalScroll(classes="page"):
                    yield Static("", id="service-status", classes="info", markup=False)
                    with Horizontal(classes="actions"):
                        yield Button("Refresh", id="refresh")
                        yield Button("Start", id="start", variant="success")
                        yield Button("Stop", id="stop", variant="warning")
                        yield Button("Restart", id="restart")
                        yield Button("Resume", id="resume")
                        yield Button("Doctor", id="doctor")
                    yield Static("", id="doctor-result", classes="info", markup=False)
                    yield Label("Target for future actions")
                    with Horizontal(classes="form-row"):
                        yield Label("Environment")
                        yield Select(
                            [("Local", "local"), ("Docker Compose", "docker")],
                            value=self.mode,
                            allow_blank=False,
                            id="target-mode",
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Compose file")
                        yield Input(
                            value=str(self.compose_path or ""),
                            id="target-compose",
                            placeholder="Required for Docker",
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Compose project")
                        yield Input(
                            value=str(self.project or ""),
                            id="target-project",
                            placeholder="Explicit project required for Docker",
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Service")
                        yield Input(value=self.service, id="target-service")
                    with Horizontal(classes="actions"):
                        yield Button("Switch target", id="switch-target")
                    yield Static(
                        "Quitting drains and stops services started by this TUI, "
                        "including local and Docker services. Other services are untouched.",
                        classes="info",
                        markup=False,
                    )
            with TabPane("Backends", id="backends"):
                with VerticalScroll(classes="page"):
                    yield Static(
                        "Select a backend. Login status is unknown until explicitly checked; gates still apply.",
                        classes="info",
                        markup=False,
                    )
                    yield DataTable(
                        id="backend-table", cursor_type="row", zebra_stripes=True
                    )
                    with Horizontal(classes="form-row"):
                        yield Label("Enabled")
                        yield Switch(id="backend-enabled")
                    with Horizontal(classes="form-row"):
                        yield Label("Executable")
                        yield Input(
                            id="backend-executable",
                            placeholder="Blank = adapter default",
                        )
                    with Horizontal(classes="actions"):
                        yield Button("Stage backend", id="stage-backend")
                        yield Button("Inspect", id="inspect-backend")
                        yield Button("Install extra", id="install-extra")
                    with Horizontal(classes="actions"):
                        yield Button("Login", id="login")
                        yield Button("Logout", id="logout", variant="warning")
                        yield Button("Check login", id="check-auth")
                    yield Static("", id="backend-details", classes="info", markup=False)
                    yield Static(
                        "Auth uses the selected environment and official CLI. Stop the service first.",
                        classes="info",
                        markup=False,
                    )
            with TabPane("Models & Routes", id="routes"):
                with VerticalScroll(classes="page"):
                    yield DataTable(
                        id="route-table", cursor_type="row", zebra_stripes=True
                    )
                    with Horizontal(classes="form-row"):
                        yield Label("Alias")
                        yield Input(id="route-alias", placeholder="Client-facing alias")
                    with Horizontal(classes="form-row"):
                        yield Label("Backend")
                        yield Select(
                            [(item["label"], key) for key, item in BACKENDS.items()],
                            value=self.selected_backend,
                            allow_blank=False,
                            id="route-backend",
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("CLI model ID")
                        yield Input(
                            id="route-model",
                            placeholder="Blank = CLI default (except Antigravity)",
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Thought level")
                        yield Input(
                            id="route-thought", placeholder="Antigravity only; optional"
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Enabled")
                        yield Switch(value=True, id="route-enabled")
                    with Horizontal(classes="form-row"):
                        yield Label("V2 enabled")
                        yield Switch(id="v2-enabled")
                    with Horizontal(classes="form-row"):
                        yield Label("V2 auth mode")
                        yield Select(
                            [
                                ("Owner key", "owner"),
                                ("LiteLLM virtual keys", "virtual_keys"),
                            ],
                            value="owner",
                            allow_blank=False,
                            id="v2-auth-mode",
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Route uses V2")
                        yield Switch(id="route-v2")
                    with Horizontal(classes="form-row"):
                        yield Label("Execution mode")
                        yield Select(
                            [("Native", "native"), ("Answer only", "answer_only")],
                            value="native",
                            allow_blank=False,
                            id="v2-execution-mode",
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Profile ID")
                        yield Input(
                            id="v2-profile-id",
                            placeholder="Host-owned profile identifier",
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Profile revision")
                        yield Input(
                            id="v2-profile-revision", placeholder="Host-owned revision"
                        )
                    with Horizontal(classes="form-row"):
                        yield Label("Transport")
                        yield Input(id="v2-transport", placeholder="stdio or stdio-acp")
                    with Horizontal(classes="form-row"):
                        yield Label("V2 limits JSON")
                        yield Input(
                            id="v2-limits",
                            placeholder='Optional, e.g. {"max_active": 2}',
                        )
                    with Horizontal(classes="actions"):
                        yield Button("New", id="route-new")
                        yield Button("Stage route", id="stage-route")
                        yield Button("Delete", id="delete-route", variant="warning")
                        yield Button("Set default", id="default-route")
                    yield Static(
                        "V2 is opt-in and remains subject to native evidence gates. Model IDs are unverified; clients must send a model.",
                        classes="info",
                        markup=False,
                    )
            with TabPane("Usage", id="usage"):
                with VerticalScroll(classes="page"):
                    yield Static("", id="usage-info", classes="info", markup=False)
                    with Horizontal(classes="actions"):
                        yield Button("Refresh", id="usage-refresh")
            with TabPane("V2 Sessions", id="v2-sessions"):
                with VerticalScroll(classes="page"):
                    yield Static("", id="v2-status", classes="info", markup=False)
                    yield DataTable(
                        id="pending-table", cursor_type="row", zebra_stripes=True
                    )
                    with Horizontal(classes="actions"):
                        yield Button("Refresh", id="v2-refresh")
                        yield Button("Inspect capabilities", id="v2-inspect")
                        yield Button(
                            "Cancel selected", id="v2-cancel", variant="warning"
                        )
                    yield Static("", id="v2-capabilities", classes="info", markup=False)
            with TabPane("Workflows", id="workflows"):
                with VerticalScroll(classes="page"):
                    yield Static(
                        "Optional module: DISABLED. There is no workflow execution engine.\n"
                        "Drafts are JSON objects only; their structure is not validated or executed.\n"
                        f"Draft file: {self.draft_store.path}",
                        classes="info",
                        markup=False,
                    )
                    yield TextArea(
                        self.draft_text, id="workflow-editor", show_line_numbers=True
                    )
                    with Horizontal(classes="actions"):
                        yield Button("Save draft", id="save-draft")
                    yield Static(
                        "Drafts can contain private task data; save only when intended.",
                        classes="info",
                        markup=False,
                    )
        with Horizontal(classes="actions"):
            yield Button("Save config", id="save-config", variant="primary")
            yield Button("Reload config", id="reload-config")
            yield Button("Quit", id="quit")
        yield Footer()

    def on_mount(self):
        self.query_one("#backend-table", DataTable).add_columns(
            "Backend", "Enabled", "Executable", "Gate"
        )
        self.query_one("#route-table", DataTable).add_columns(
            "Alias", "Backend", "CLI model", "Route", "Default"
        )
        self.query_one("#pending-table", DataTable).add_columns(
            "Pending ID", "Model", "State", "TTL seconds"
        )
        self._render_config()
        self._select_backend(self.selected_backend)
        self._set_notice(
            "Changes must be staged, then saved. Restart applies saved configuration."
        )
        if self.draft_error:
            self.query_one("#save-draft", Button).disabled = True
            self._set_notice(
                "Existing workflow draft could not be read; saving is disabled to protect it."
            )
        self.action_refresh()

    def _set_notice(self, message):
        self.query_one("#notice", Static).update(message)

    def _render_config(self):
        target = self.mode
        if self.mode == "docker":
            target += f" | project: {self.project or 'unset'} | service: {self.service}"
        changes = "unsaved edits" if self.dirty else "saved"
        self.query_one("#target-line", Static).update(
            f"Target: {target}  |  Config: {self.config_path}  |  {changes}"
        )
        enabled = self.config.get("enabled", {})
        executables = self.config.get("executables", {})
        table = self.query_one("#backend-table", DataTable)
        table.clear()
        for name, spec in BACKENDS.items():
            gate = "Blocked" if spec.get("gated_reason") else "Not checked"
            table.add_row(
                Text(spec["label"]),
                "Yes" if enabled.get(name) else "No",
                Text(str(executables.get(name) or "adapter default")),
                gate,
                key=name,
            )
        routes = config_api.route_specs(self.config)
        v2 = config_api.v2_config(self.config)
        self.query_one("#v2-enabled", Switch).value = v2["enabled"]
        self.query_one("#v2-auth-mode", Select).value = v2["auth_mode"]
        self.query_one("#v2-limits", Input).value = (
            json.dumps(v2["limits"], ensure_ascii=False, sort_keys=True)
            if v2["limits"]
            else ""
        )
        table = self.query_one("#route-table", DataTable)
        table.clear()
        for alias, spec in routes.items():
            state = "On" if spec["enabled"] else "Off"
            if spec["enabled"] and not enabled.get(spec["backend"], False):
                state = "Backend off"
            if alias in v2["routes"]:
                state += " / V2" if v2["enabled"] else " / V2 staged"
            table.add_row(
                Text(alias),
                Text(spec["backend"]),
                Text(spec["model"] or "CLI default (unresolved)"),
                state,
                "Yes" if self.config.get("default_route") == alias else "",
                key=alias,
            )
        self._render_status()

    def _render_status(self):
        status = self.status_data
        state = status.get("state", "unknown")
        active = status.get("active_requests")
        active_text = "N/A" if active is None else str(active)
        running_rev = status.get("config_revision")
        saved_rev = status.get("saved_config_revision")
        if state == "stopped":
            applied = "Not running"
        elif running_rev and saved_rev:
            applied = (
                "Current" if running_rev == saved_rev else "Pending restart / recreate"
            )
        else:
            applied = "Unknown"
        port = status.get("port")
        if port is None:
            port = (
                self.config.get("ports", {}).get("router", 4000)
                if state == "stopped"
                else "N/A"
            )
        lines = [
            f"State: {state}  |  Owned: {'Yes' if status.get('owned') else 'No / unknown'}  |  Port: {port}  |  Active requests: {active_text}",
            f"PID: {status.get('pid') or 'N/A'}  |  Uptime: {status.get('uptime_seconds') if status.get('uptime_seconds') is not None else 'N/A'} s  |  Config: {applied}",
        ]
        if status.get("message"):
            lines.append(str(status["message"]))
        self.query_one("#service-status", Static).update("\n".join(lines))
        self._render_usage(status.get("usage"))
        self._render_v2(status.get("v2"))
        self._auth_buttons()

    def _render_v2(self, v2):
        table = self.query_one("#pending-table", DataTable)
        table.clear()
        target = _target_key(self.mode, self.compose_path, self.project, self.service)
        inspection = self.capability_inspections.get(target)
        if not isinstance(v2, dict) or not v2.get("available"):
            self.selected_pending = None
            self.query_one("#v2-cancel", Button).disabled = True
            self.query_one("#v2-status", Static).update(
                "V2 runtime: unavailable or not running."
            )
            self._render_capabilities(
                inspection.get("capabilities", {}) if inspection else {},
                inspection.get("scope") if inspection else None,
            )
            return
        counts = v2.get("counts") or {}
        labels = (
            "native_running",
            "prepared",
            "awaiting_client",
            "cleaning",
            "generation_permits",
            "quarantined",
        )
        self.query_one("#v2-status", Static).update(
            "V2: "
            + " | ".join(f"{name}: {counts.get(name, 'unknown')}" for name in labels)
        )
        current = set()
        for entry in v2.get("pending") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            pending_id = entry["id"]
            if pending_id in current:
                continue
            current.add(pending_id)
            ttl = entry.get("expires_in_seconds")
            ttl_text = (
                str(round(ttl, 1))
                if type(ttl) in (int, float) and math.isfinite(ttl)
                else "unknown"
            )
            table.add_row(
                Text(pending_id),
                Text(str(entry.get("model", "unknown"))),
                Text(str(entry.get("state", "unknown"))),
                ttl_text,
                key=pending_id,
            )
        if self.selected_pending not in current:
            self.selected_pending = None
        self.query_one("#v2-cancel", Button).disabled = self.selected_pending is None
        self._render_capabilities(v2.get("capabilities") or {}, "running runtime")

    def _render_capabilities(self, capabilities, scope):
        lines = [
            f"Capability evidence ({scope or 'unavailable'}; live verification required):"
        ]
        for alias, capability in capabilities.items():
            if not isinstance(capability, dict):
                continue
            manifest = capability.get("manifest")
            if not isinstance(manifest, dict):
                lines.append(f"{alias}: {capability.get('error', 'unavailable')}")
                continue
            features = manifest.get("features")
            if not isinstance(features, dict):
                lines.append(f"{alias}: unavailable")
                continue
            values = []
            for name in (
                "text",
                "client_tools",
                "reasoning_output",
                "usage",
                "profile_isolation",
            ):
                evidence = features.get(name, {})
                if not isinstance(evidence, dict):
                    continue
                values.append(
                    f"{name}={evidence.get('state', 'unknown')} (live={evidence.get('live_verified', False)})"
                )
            mode = capability.get("execution_mode", "native")
            source = capability.get("tool_proposal_source")
            reasons = sorted(
                {
                    item.get("reason")
                    for item in features.values()
                    if isinstance(item, dict) and item.get("reason")
                }
            )
            details = f"mode={mode}, ready={capability.get('ready') is True}"
            if source:
                details += f", tool proposals={source}"
            if reasons:
                details += ", blocked=" + ", ".join(reasons)
            lines.append(f"{alias}: {details}, " + ", ".join(values))
        self.query_one("#v2-capabilities", Static).update("\n".join(lines))

    def _render_usage(self, usage):
        if not isinstance(usage, dict) or not usage.get("available"):
            text = "Usage: N/A for this target. No gateway telemetry is available.\nToken counts and account quota: N/A."
        else:

            def display(value):
                return "N/A" if value is None else str(value)

            tokens = usage.get("usage") or {}
            text = (
                f"Target: {self.mode} | Source: retained gateway metadata\n"
                f"Requests: {display(usage.get('requests'))}  Success: {display(usage.get('success'))}  "
                f"Errors: {display(usage.get('error'))}  Cancelled: {display(usage.get('cancelled'))}\n"
                f"Active: {display(usage.get('active'))}  Average latency (ms): {display(usage.get('avg_latency_ms'))}  "
                f"Average TTFT (ms): {display(usage.get('avg_ttft_ms'))}\n"
                f"Token source: {display(tokens.get('source'))}  Input: {display(tokens.get('input_tokens'))}  "
                f"Output: {display(tokens.get('output_tokens'))}  Reported requests: {display(tokens.get('reported_requests'))}\n"
                f"Cache read: {display(tokens.get('cache_read_tokens'))}  Cache write: {display(tokens.get('cache_write_tokens'))}  "
                f"Reasoning: {display(tokens.get('reasoning_tokens'))}\n"
                f"Backend sources: {', '.join((tokens.get('by_source') or {}).keys()) or 'N/A'}\n"
                f"Account quota / credit: N/A  |  Data degraded: {'Yes' if usage.get('degraded') else 'No'}"
            )
        v2 = self.status_data.get("v2") or {}
        observed = v2.get("usage") if isinstance(v2, dict) else None
        if isinstance(observed, dict) and observed.get("available") is True:

            def show(value):
                return "unknown" if value is None else str(value)

            totals = observed.get("totals") or {}
            text += (
                f"\n\nV2 observed token metadata | Storage: {show(observed.get('persistence'))}"
                f" | Degraded: {show(observed.get('degraded'))}\n"
                "Totals cover observed counters only; incomplete usage may be missing.\n"
                f"Completeness — complete: {show(totals.get('complete'))} "
                f"partial: {show(totals.get('partial'))} unknown: {show(totals.get('unknown'))}\n"
                f"Input: {show(totals.get('input_tokens'))} Output: {show(totals.get('output_tokens'))} "
                f"Cache read: {show(totals.get('cache_read_tokens'))} Cache write: {show(totals.get('cache_write_tokens'))} "
                f"Reasoning: {show(totals.get('reasoning_tokens'))} | Cost: N/A"
            )
            for item in observed.get("invocations") or []:
                if not isinstance(item, dict):
                    continue
                sources = item.get("source")
                source = (
                    ", ".join(sources)
                    if isinstance(sources, list)
                    and all(isinstance(s, str) for s in sources)
                    else "unknown"
                )
                text += (
                    f"\n{show(item.get('model'))} ({show(item.get('backend'))}) "
                    f"key scope: {show(item.get('key_scope'))} | {show(item.get('usage_completeness'))} "
                    f"| source: {source} | input: {show(item.get('input_tokens'))} "
                    f"output: {show(item.get('output_tokens'))} "
                    f"cache read: {show(item.get('cache_read_tokens'))} "
                    f"cache write: {show(item.get('cache_write_tokens'))} "
                    f"reasoning: {show(item.get('reasoning_tokens'))}"
                )
        else:
            text += "\n\nV2 usage: unknown or unavailable. Cost: N/A."
        self.query_one("#usage-info", Static).update(text)

    def _auth_buttons(self):
        actions = BACKENDS[self.selected_backend].get("auth_actions", ())
        can_run = (
            self.status_data.get("state") == "stopped"
            and not self.busy
            and not self.shutting_down
            and not self.dirty
            and (self.mode != "docker" or self.selected_backend == "codex")
        )
        for button, action in (
            ("login", "login"),
            ("logout", "logout"),
            ("check-auth", "check"),
        ):
            self.query_one("#" + button, Button).disabled = not (
                can_run and action in actions
            )
        self.query_one("#install-extra", Button).disabled = not (
            can_run and self.mode == "local" and self.selected_backend == "claude"
        )

    def _backend_details(self):
        target = _target_key(self.mode, self.compose_path, self.project, self.service)
        details = self.inspections.get((target, self.selected_backend))
        if not details:
            self.query_one("#backend-details", Static).update(
                "Executable, version, and dependency: not inspected. "
                "Inspect checks version only, without login or prompts."
            )
            return

        def display(value):
            return str(value) if value is not None else "N/A"

        dependency = details.get("dependency")
        dep_text = "N/A"
        if isinstance(dependency, dict):
            dep_text = (
                f"{display(dependency.get('name'))}: "
                f"{'installed' if dependency.get('installed') else 'not installed'}; "
                f"version {display(dependency.get('version'))}; "
                f"required {display(dependency.get('required_version'))}; "
                f"{'compatible' if dependency.get('compatible') else 'not verified / incompatible'}"
            )
        text = (
            f"Inspection scope: {display(details.get('scope'))}\n"
            f"Configured executable: {display(details.get('configured_executable'))}\n"
            f"Resolved executable: {display(details.get('resolved_executable'))}\n"
            f"CLI version: {display(details.get('version'))} | Dependency: {dep_text}\n"
            f"Gate: {display(details.get('gated_reason'))} | Readiness and model access: not verified"
        )
        self.query_one("#backend-details", Static).update(text)

    def _select_backend(self, backend):
        self.selected_backend = backend
        self.query_one("#backend-enabled", Switch).value = self.config.get(
            "enabled", {}
        ).get(backend, False)
        executable = self.config.get("executables", {}).get(backend)
        self.query_one("#backend-executable", Input).value = executable or ""
        self._auth_buttons()
        self._backend_details()
        reason = BACKENDS[backend].get("gated_reason")
        if reason:
            self._set_notice(f"{backend}: Blocked — {reason}")

    def _select_route(self, alias):
        spec = config_api.route_specs(self.config)[alias]
        self.selected_route = alias
        self.query_one("#route-alias", Input).value = alias
        self.query_one("#route-backend", Select).value = spec["backend"]
        self.query_one("#route-model", Input).value = spec["model"] or ""
        self.query_one("#route-thought", Input).value = (
            spec["settings"].get("thought_level") or ""
        )
        self.query_one("#route-enabled", Switch).value = spec["enabled"]
        profile = config_api.v2_config(self.config)["routes"].get(alias)
        self.query_one("#route-v2", Switch).value = profile is not None
        self.query_one("#v2-execution-mode", Select).value = (profile or {}).get(
            "execution_mode", "native"
        )
        for field, widget in (
            ("profile_id", "v2-profile-id"),
            ("profile_revision", "v2-profile-revision"),
            ("transport", "v2-transport"),
        ):
            self.query_one("#" + widget, Input).value = (profile or {}).get(field, "")

    @on(DataTable.RowSelected)
    def row_selected(self, event):
        value = event.row_key.value
        if event.data_table.id == "backend-table":
            self._select_backend(value)
        elif event.data_table.id == "route-table":
            self._select_route(value)
        elif event.data_table.id == "pending-table":
            self.selected_pending = value
            self.query_one("#v2-cancel", Button).disabled = False

    @on(Select.Changed, "#route-backend")
    def route_backend_changed(self, event):
        self.query_one("#route-thought", Input).disabled = event.value != "antigravity"

    @on(TextArea.Changed, "#workflow-editor")
    def workflow_changed(self, event):
        self.workflow_dirty = event.text_area.text != self.draft_text

    def _stage_backend(self):
        enabled = self.query_one("#backend-enabled", Switch).value
        executable = self.query_one("#backend-executable", Input).value.strip()
        candidate = copy.deepcopy(self.config)
        default = candidate.get("default_route")
        default_spec = (
            config_api.route_specs(candidate).get(default) if default else None
        )
        candidate.setdefault("enabled", {})[self.selected_backend] = enabled
        executables = candidate.setdefault("executables", {})
        if executable:
            executables[self.selected_backend] = executable
        else:
            executables.pop(self.selected_backend, None)
        if (
            not enabled
            and default_spec
            and default_spec["backend"] == self.selected_backend
        ):
            candidate.pop("default_route", None)
        try:
            config_api.validate_config(candidate)
        except ValueError:
            self._set_notice(
                "Cannot enable backend with invalid routes; check the model IDs."
            )
            return
        self.config = candidate
        self.dirty = True
        self._render_config()
        self._set_notice("Backend staged. Save config to persist; restart to apply.")

    def _stage_route(self):
        alias = self.query_one("#route-alias", Input).value.strip()
        backend = self.query_one("#route-backend", Select).value
        model = self.query_one("#route-model", Input).value.strip() or None
        thought = (
            self.query_one("#route-thought", Input).value.strip() or None
            if backend == "antigravity"
            else None
        )
        if not alias or backend not in BACKENDS:
            self._set_notice("Alias and backend are required.")
            return
        route_enabled = self.query_one("#route-enabled", Switch).value
        if (
            backend == "antigravity"
            and model is None
            and route_enabled
            and self.config.get("enabled", {}).get(backend, False)
        ):
            self._set_notice("Antigravity requires an explicit CLI model ID.")
            return
        routes = copy.deepcopy(config_api.route_specs(self.config))
        if alias in routes and alias != self.selected_route:
            self._set_notice("Alias already exists; select it to edit.")
            return
        candidate = copy.deepcopy(self.config)
        if self.selected_route and self.selected_route != alias:
            routes.pop(self.selected_route, None)
            if candidate.get("default_route") == self.selected_route:
                candidate["default_route"] = alias
        routes[alias] = {
            "backend": backend,
            "model": model,
            "settings": {"thought_level": thought} if thought else {},
            "enabled": route_enabled,
        }
        candidate["routes"] = routes
        v2 = copy.deepcopy(config_api.v2_config(self.config))
        if self.selected_route and self.selected_route != alias:
            v2["routes"].pop(self.selected_route, None)
        if self.query_one("#route-v2", Switch).value:
            v2["routes"][alias] = {
                "profile_id": self.query_one("#v2-profile-id", Input).value.strip(),
                "profile_revision": self.query_one(
                    "#v2-profile-revision", Input
                ).value.strip(),
                "transport": self.query_one("#v2-transport", Input).value.strip(),
            }
            if self.query_one("#v2-execution-mode", Select).value == "answer_only":
                v2["routes"][alias]["execution_mode"] = "answer_only"
        else:
            v2["routes"].pop(alias, None)
        v2["enabled"] = self.query_one("#v2-enabled", Switch).value
        v2["auth_mode"] = self.query_one("#v2-auth-mode", Select).value
        raw_limits = self.query_one("#v2-limits", Input).value.strip()
        try:
            v2["limits"] = json.loads(raw_limits) if raw_limits else {}
        except (ValueError, TypeError):
            self._set_notice("V2 limits must be a JSON object with supported values.")
            return
        if "v2" in self.config or v2["enabled"] or v2["routes"] or v2["limits"]:
            candidate["v2"] = v2
        if candidate.get("default_route") == alias and (
            not routes[alias]["enabled"]
            or not candidate.get("enabled", {}).get(backend, False)
        ):
            candidate.pop("default_route", None)
        try:
            config_api.validate_config(candidate)
        except ValueError:
            self._set_notice(
                "Invalid route or V2 profile, limits, or auth mode. Virtual keys require every active route to use V2."
            )
            return
        self.config = candidate
        self.selected_route = alias
        self.dirty = True
        self._render_config()
        self._set_notice(
            "Route staged. Model ID is unverified; save config, then restart to apply."
        )

    def _new_route(self):
        self.selected_route = None
        for widget in ("route-alias", "route-model", "route-thought"):
            self.query_one("#" + widget, Input).value = ""
        self.query_one("#route-enabled", Switch).value = True
        self.query_one("#route-v2", Switch).value = False
        self.query_one("#v2-execution-mode", Select).value = "native"
        for widget in ("v2-profile-id", "v2-profile-revision", "v2-transport"):
            self.query_one("#" + widget, Input).value = ""
        self._set_notice("Enter a new alias and stage it.")

    def _delete_route(self):
        if not self.selected_route:
            self._set_notice("Select a route to delete.")
            return
        alias = self.selected_route
        routes = copy.deepcopy(config_api.route_specs(self.config))
        routes.pop(alias, None)
        candidate = copy.deepcopy(self.config)
        candidate["routes"] = routes
        if "v2" in candidate:
            candidate["v2"] = copy.deepcopy(config_api.v2_config(self.config))
            candidate["v2"]["routes"].pop(alias, None)
            if not candidate["v2"]["routes"]:
                candidate["v2"]["enabled"] = False
        if candidate.get("default_route") == alias:
            candidate.pop("default_route", None)
        try:
            config_api.validate_config(candidate)
        except ValueError:
            self._set_notice("Complete or remove invalid legacy routes before saving.")
            return
        self.config = candidate
        self.dirty = True
        self._new_route()
        self._render_config()
        self._set_notice("Route deletion staged. Save config to persist.")

    def _default_route(self):
        if not self.selected_route:
            self._set_notice("Select a route first.")
            return
        if self.selected_route not in config_api.routes(self.config):
            self._set_notice(
                "Enable both the route and its backend before selecting a default."
            )
            return
        self.config["default_route"] = self.selected_route
        self.dirty = True
        self._render_config()
        self._set_notice(
            "Default selection is UI metadata; API clients must still send a model."
        )

    def _run_operation(self, title, function, *, result_handler=None):
        if self.busy or self.shutting_down:
            self._set_notice("Another operation is in progress.")
            return
        self.busy = True
        self._auth_buttons()
        self._set_notice(title + "…")

        async def run():
            try:
                result = await asyncio.to_thread(function)
                if result_handler:
                    result_handler(result)
                self._set_notice(title + " completed.")
            except ManagementError as error:
                self._set_notice(str(error))
            except Exception:
                self._set_notice(title + " failed. Check configuration and retry.")
            finally:
                self.busy = False
                self._auth_buttons()

        self.run_worker(run(), exit_on_error=False)

    def _set_status(self, result):
        if isinstance(result, dict):
            self.status_data = result
            self._render_status()

    def action_refresh(self):
        self._run_operation(
            "Refresh", self.manager.status, result_handler=self._set_status
        )

    def _cancel_pending(self):
        pending_id = self.selected_pending
        if not pending_id:
            self._set_notice("Select a pending V2 session first.")
            return
        manager = self.manager

        def start(confirmed):
            if confirmed:
                self._run_operation(
                    "Cancel pending session",
                    lambda: manager.cancel_pending(pending_id),
                    result_handler=self._set_status,
                )

        self.push_screen(Confirm("Cancel the selected pending V2 session?"), start)

    def _inspect_capabilities(self):
        manager = self.manager
        target = _target_key(self.mode, self.compose_path, self.project, self.service)

        def show(result):
            self.capability_inspections[target] = result
            if self.manager is manager:
                self._render_v2(self.status_data.get("v2"))

        self._run_operation(
            "Inspect capabilities", manager.inspect_capabilities, result_handler=show
        )

    def _service_operation(self, action):
        if action in ("start", "restart") and self.dirty:
            self._set_notice("Save or reload unsaved configuration before starting.")
            return
        self._run_operation(
            action.title(),
            getattr(self.manager, action),
            result_handler=self._set_status,
        )

    def _doctor(self):
        def show(result):
            scope = (
                "running container config"
                if self.mode == "docker"
                else "saved local config"
            )
            lines = [f"Doctor ({scope}): " + str(result.get("state", "unknown"))]
            for entry in result.get("backends", []):
                lines.append(
                    f"{entry.get('backend')}: {entry.get('preflight')} — {entry.get('reason') or 'model access not verified'}"
                )
            self.query_one("#doctor-result", Static).update("\n".join(lines))

        self._run_operation("Doctor", self.manager.doctor, result_handler=show)

    def _inspect_backend(self):
        backend = self.selected_backend
        manager = self.manager
        target = _target_key(self.mode, self.compose_path, self.project, self.service)

        def show(result):
            self.inspections[(target, backend)] = result
            if self.manager is manager and self.selected_backend == backend:
                self._backend_details()

        self._run_operation(
            "Inspect backend",
            lambda: manager.inspect_backend(backend),
            result_handler=show,
        )

    def _save_config(self):
        if not self.dirty:
            self._set_notice("No staged changes to save.")
            return
        snapshot = copy.deepcopy(self.config)
        expected = self.revision

        def save():
            try:
                return config_api.save_config(
                    self.config_path, snapshot, expected_revision=expected
                )
            except ValueError:
                if config_api.revision(self.config_path) != expected:
                    raise ManagementError(
                        "Config changed outside this app; reload or reconcile your edits before saving."
                    ) from None
                raise ManagementError(
                    "Config is invalid; check route and backend settings."
                ) from None

        def saved(revision):
            self.revision = revision
            self.dirty = False
            self.status_data["saved_config_revision"] = config_digest(snapshot)
            self.inspections.clear()
            self.capability_inspections.clear()
            self._render_config()
            self._backend_details()

        self._run_operation("Save config", save, result_handler=saved)

    def _reload_config(self):
        def loaded(result):
            self.config, self.revision = result
            self.dirty = False
            self.selected_route = None
            self.inspections.clear()
            self.capability_inspections.clear()
            self._render_config()
            self._select_backend(self.selected_backend)

        self._run_operation(
            "Reload config",
            lambda: _read_config(self.config_path),
            result_handler=loaded,
        )

    def _switch_target(self):
        mode = self.query_one("#target-mode", Select).value
        compose = self.query_one("#target-compose", Input).value.strip() or None
        project = self.query_one("#target-project", Input).value.strip() or None
        service = self.query_one("#target-service", Input).value.strip()
        if mode == "docker" and (not compose or not project or not service):
            self._set_notice(
                "Docker requires a Compose file, explicit project, and service."
            )
            return

        key = _target_key(mode, compose, project, service or "litellm")

        def switch():
            manager = self.managers.get(key)
            if manager is None:
                manager = self.manager_factory(
                    self.config_path,
                    mode=mode,
                    compose_path=compose,
                    project=project,
                    service=service or "litellm",
                )
            return manager, manager.status()

        def switched(result):
            self.manager, self.status_data = result
            self.managers[key] = self.manager
            self.mode, self.compose_path, self.project, self.service = (
                mode,
                compose,
                project,
                service or "litellm",
            )
            self._render_config()
            self._backend_details()

        self._run_operation("Switch target", switch, result_handler=switched)

    def _terminal_action(self, label, command_factory, on_success=None):
        if self.busy or self.shutting_down:
            self._set_notice("Another operation is in progress.")
            return
        self.busy = True
        self._auth_buttons()

        async def run():
            try:
                command = await asyncio.to_thread(command_factory)
                # Inherit the terminal, never capture or log CLI output or credentials.
                with self.suspend():
                    result = subprocess.run(command, check=False)
                if result.returncode == 0 and on_success:
                    on_success()
                self._set_notice(f"{label} exited with status {result.returncode}.")
                self._set_status(await asyncio.to_thread(self.manager.status))
            except ManagementError as error:
                self._set_notice(str(error))
            except SuspendNotSupported:
                self._set_notice(
                    "Terminal handoff is unavailable; use a separate terminal."
                )
            except (OSError, RuntimeError, ValueError):
                self._set_notice("Terminal action could not be started.")
            finally:
                self.busy = False
                self._auth_buttons()

        self.run_worker(run(), exit_on_error=False)

    def _auth(self, action):
        backend = self.selected_backend

        def command_when_safe():
            if action in ("login", "logout"):
                for key, manager in self.managers.items():
                    shares_profile = key[0] == self.mode and (
                        self.mode == "local" or key[2] == self.project
                    )
                    if shares_profile and manager.status().get("state") != "stopped":
                        raise ManagementError(
                            "Stop all managed services sharing this CLI profile before login or logout."
                        )
            return self.manager.auth_command(backend, action)

        def start(confirmed=True):
            if confirmed:
                self._terminal_action(f"{backend} {action}", command_when_safe)

        if action == "logout":
            self.push_screen(
                Confirm(
                    f"Logout {backend} in {self.mode}? This affects the real CLI account and existing sessions. The selected service must be stopped."
                ),
                start,
            )
        else:
            start()

    def _install_extra(self):
        if self.mode != "local" or self.selected_backend != "claude":
            self._set_notice("Only the Claude SDK extra can be installed locally.")
            return
        backend = self.selected_backend
        target = _target_key(self.mode, self.compose_path, self.project, self.service)

        def command_when_safe():
            for key, manager in self.managers.items():
                if key[0] == "local" and manager.status().get("state") != "stopped":
                    raise ManagementError(
                        "Stop all managed local services before changing the Python environment."
                    )
            return self.manager.install_command(backend)

        def invalidate():
            self.inspections.pop((target, backend), None)
            self._backend_details()

        def start(confirmed):
            if confirmed:
                self._terminal_action(
                    "Install Claude SDK extra", command_when_safe, invalidate
                )

        self.push_screen(
            Confirm(
                "Install the pinned Claude SDK extra into this local Python environment? "
                "All managed local services must be stopped. This can download packages."
            ),
            start,
        )

    def action_quit(self):
        if self.busy:
            self._set_notice("Wait for the current operation before quitting.")
            return
        if (self.dirty or self.workflow_dirty) and not self.shutting_down:
            self.push_screen(
                Confirm(
                    "Discard unsaved configuration or workflow draft changes and quit?"
                ),
                lambda yes: self._begin_managed_shutdown() if yes else None,
            )
            return
        self._begin_managed_shutdown()

    def _begin_managed_shutdown(self):
        if self.busy or self.shutting_down:
            self._set_notice("Wait for the current operation before quitting.")
            return
        self.shutting_down = True
        self._auth_buttons()

        async def shutdown():
            try:
                for key, manager in self.managers.items():
                    status = await asyncio.to_thread(manager.status)
                    if status.get("owned") and status.get("state") != "stopped":
                        self._set_notice(
                            f"Draining and stopping the owned {key[0]} service…"
                        )
                        result = await asyncio.to_thread(manager.stop)
                        if result.get("state") != "stopped":
                            if manager is self.manager:
                                self._set_status(result)
                            self._set_notice(
                                "Service has not stopped. Switch to its target and resume or wait."
                            )
                            self.shutting_down = False
                            self._auth_buttons()
                            return
                    await asyncio.to_thread(manager.close)
                self.managers_closed = True
                self.exit()
            except ManagementError as error:
                self.shutting_down = False
                self._auth_buttons()
                self._set_notice(
                    f"Could not stop owned {key[0]} service: {error}. Switch target to resume or wait."
                )
            except (OSError, RuntimeError):
                self.shutting_down = False
                self._auth_buttons()
                self._set_notice(
                    "Could not stop an owned service; resume or wait, then retry."
                )

        self.run_worker(shutdown(), exit_on_error=False)

    @on(Button.Pressed)
    def button_pressed(self, event):
        if self.busy or self.shutting_down:
            self._set_notice("Another operation is in progress.")
            return
        button = event.button.id
        if button in ("refresh", "usage-refresh", "v2-refresh"):
            self.action_refresh()
        elif button == "v2-cancel":
            self._cancel_pending()
        elif button == "v2-inspect":
            self._inspect_capabilities()
        elif button in ("start", "stop", "restart", "resume"):
            self._service_operation(button)
        elif button == "doctor":
            self._doctor()
        elif button == "switch-target":
            self._switch_target()
        elif button == "stage-backend":
            self._stage_backend()
        elif button == "inspect-backend":
            self._inspect_backend()
        elif button == "install-extra":
            self._install_extra()
        elif button == "stage-route":
            self._stage_route()
        elif button == "route-new":
            self._new_route()
        elif button == "delete-route":
            self._delete_route()
        elif button == "default-route":
            self._default_route()
        elif button in ("login", "logout", "check-auth"):
            self._auth("check" if button == "check-auth" else button)
        elif button == "save-config":
            self._save_config()
        elif button == "reload-config":
            if self.dirty:
                self.push_screen(
                    Confirm("Discard all staged configuration changes and reload?"),
                    lambda yes: self._reload_config() if yes else None,
                )
            else:
                self._reload_config()
        elif button == "save-draft":
            text = self.query_one("#workflow-editor", TextArea).text

            def saved(_):
                self.draft_text = text
                self.workflow_dirty = (
                    self.query_one("#workflow-editor", TextArea).text != text
                )

            self._run_operation(
                "Save workflow draft",
                lambda: self.draft_store.save(text),
                result_handler=saved,
            )
        elif button == "quit":
            self.action_quit()


def launch(
    config_path, mode="local", compose_path=None, project=None, service="litellm"
):
    if not Path(config_path).is_file():
        print(
            "Config file not found. Create one first, for example with "
            "`python example/init_config.py <path>` in a source checkout.",
            file=sys.stderr,
        )
        return 1
    app = RouterApp(
        config_path,
        mode=mode,
        compose_path=compose_path,
        project=project,
        service=service,
    )
    try:
        app.run()
    finally:
        if not app.managers_closed:
            for manager in app.managers.values():
                status = manager.status()
                if status.get("owned") and status.get("state") != "stopped":
                    manager.stop()
                manager.close()
    return 0
