"""Manage only gateway instances explicitly started by this manager.

Methods are synchronous so a terminal UI can call them from worker threads.
The per-instance control credential is kept in memory and never returned.
"""

import asyncio
import contextlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .backend_inspect import CLAUDE_SDK_VERSION
from .backend_inspect import inspect as inspect_backend_config
from .config import load_config
from .control import config_digest
from .registry import BACKENDS


class ManagementError(RuntimeError):
    """A sanitized, user-facing management failure."""


class _ControlNotDrained(ManagementError):
    """The atomic stop latch rejected a transient nonquiescent state."""


def _hidden_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _available(port):
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


class ServiceManager:
    """A single selected local or Compose environment, with no PID adoption."""

    def __init__(
        self,
        config_path,
        mode="local",
        compose_path=None,
        project=None,
        service="litellm",
        *,
        startup_timeout=60,
    ):
        if mode not in ("local", "docker"):
            raise ManagementError("Unknown environment")
        self.config_path = Path(config_path).expanduser().resolve()
        self.mode = mode
        self.compose_path = (
            Path(compose_path).expanduser().resolve() if compose_path else None
        )
        self.project, self.service = project, service
        if mode == "docker":
            if not self.compose_path or not self.compose_path.is_file():
                raise ManagementError("Select an existing Compose file")
            if not isinstance(project, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{0,62}", project
            ):
                raise ManagementError("Select a valid explicit Compose project")
            if not isinstance(service, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", service
            ):
                raise ManagementError("Select a valid Compose service")
        self.startup_timeout = startup_timeout
        self._lock = threading.RLock()
        self._child = None
        self._tree = None
        self._container = None
        self._instance = None
        self._token = None
        self._port = None
        self._override_dir = None
        self._override = None

    def _config(self):
        try:
            return load_config(self.config_path)
        except (OSError, ValueError, KeyError, TypeError):
            raise ManagementError("Cannot read a valid gateway configuration") from None

    def _saved_revision(self):
        try:
            return config_digest(self._config())
        except ManagementError:
            return None

    def _base(self, state, *, owned=False, message=None):
        return {
            "mode": self.mode,
            "environment": "local"
            if self.mode == "local"
            else f"docker:{self.project}:{self.service}",
            "state": state,
            "owned": owned,
            "pid": None,
            "instance_id": None,
            "port": self._port,
            "uptime_seconds": None,
            "active_requests": None,
            "config_revision": None,
            "saved_config_revision": self._saved_revision(),
            "usage": None,
            "v2": None,
            "message": message,
        }

    def _request(self, action, *, post=False, body=None):
        if not self._port or not self._token or not self._instance:
            raise ManagementError("Managed service control is unavailable")
        data = (
            json.dumps(body).encode("utf-8")
            if body is not None
            else b""
            if post
            else None
        )
        headers = {"Authorization": "Bearer " + self._token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/_agent-rpc/control/{action}",
            data=data,
            headers=headers,
            method="POST" if post else "GET",
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            # Cleanup may wait for the owner and then close the process, each
            # bounded by the validated 60s limit. Keep status checks short.
            timeout = 125 if action == "cancel" else 2
            with opener.open(request, timeout=timeout) as response:
                data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError
            result = json.loads(data)
            if (
                not isinstance(result, dict)
                or result.get("instance_id") != self._instance
            ):
                raise ValueError
            return result
        except urllib.error.HTTPError as error:
            if action == "stop" and error.code == 409:
                try:
                    result = json.loads(error.read(4097))
                    if (
                        isinstance(result, dict)
                        and result.get("instance_id") == self._instance
                        and result.get("error") == "service_not_drained"
                    ):
                        raise _ControlNotDrained("Service is not drained") from None
                except (ValueError, TypeError):
                    pass
            if action == "cancel":
                raise ManagementError(
                    "Cancel was not confirmed; refresh V2 status"
                ) from None
            raise ManagementError("Managed service control is unavailable") from None
        except (OSError, ValueError, TypeError, urllib.error.URLError):
            if action == "cancel":
                raise ManagementError(
                    "Cancel outcome unknown; refresh V2 status before retrying"
                ) from None
            raise ManagementError("Managed service control is unavailable") from None

    def _status_response(self, payload):
        if (
            payload.get("state") not in ("running", "draining")
            or type(payload.get("active_requests")) is not int
            or payload["active_requests"] < 0
        ):
            raise ManagementError("Invalid managed service status")
        if self.mode == "local":
            pid = payload.get("pid")
            if (
                not self._child
                or type(pid) is not int
                or not (
                    self._tree.contains(pid) if self._tree else pid == self._child.pid
                )
            ):
                raise ManagementError("Managed process identity changed")
        result = self._base(payload["state"], owned=True)
        result.update(
            pid=payload.get("pid"),
            instance_id=self._instance,
            uptime_seconds=payload.get("uptime_seconds"),
            active_requests=payload["active_requests"],
            config_revision=payload.get("config_digest"),
            usage=payload.get("usage"),
            v2=payload.get("v2"),
        )
        return result

    @staticmethod
    def _quiescent(state):
        if state.get("state") != "draining" or state.get("active_requests") != 0:
            return False
        v2 = state.get("v2")
        if v2 is None:
            return True  # A legacy-only managed instance.
        if not isinstance(v2, dict) or v2.get("available") is not True:
            return False
        counts = v2.get("counts")
        names = (
            "native_running",
            "prepared",
            "awaiting_client",
            "cleaning",
            "generation_permits",
            "quarantined",
        )
        return isinstance(counts, dict) and all(
            type(counts.get(name)) is int and counts[name] == 0 for name in names
        )

    def _close_tree(self, *, kill=False):
        tree, self._tree = self._tree, None
        if tree is not None:
            # The Windows job handle owns the whole tree even after the root
            # exits. Closing it also removes any stray owned descendants.
            if kill:
                tree.kill()
            if tree.handle:
                tree.api.CloseHandle(tree.handle)
                tree.handle = None
        elif kill and self._child and self._child.poll() is None:
            if os.name == "nt":
                self._child.kill()
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self._child.pid, signal.SIGKILL)

    def _local_status(self):
        if self._child is not None:
            if self._child.poll() is None:
                try:
                    return self._status_response(self._request("status"))
                except ManagementError:
                    return self._base(
                        "starting", owned=True, message="Control not ready"
                    )
            self._close_tree()
            self._child = None
            self._token = self._instance = None
        try:
            port = self._config().get("ports", {}).get("router", 4000)
        except ManagementError:
            return self._base("unknown", message="Invalid saved configuration")
        self._port = port
        if not _available(port):
            return self._base(
                "unknown", message="Port is occupied by an unowned service"
            )
        return self._base("stopped")

    def _override_path(self):
        if self._override is None:
            self._override_dir = tempfile.TemporaryDirectory(
                prefix="agent-rpc-compose-"
            )
            self._override = Path(self._override_dir.name) / "managed.json"
            # JSON is a valid Compose YAML file. Values are interpolated only
            # by Compose from its child environment, never written to this file.
            volume = f"agent-rpc-manager-data-{self.service}"
            override = {
                "services": {
                    self.service: {
                        "environment": {
                            "AGENT_RPC_CONTROL_TOKEN": "${AGENT_RPC_CONTROL_TOKEN:-}",
                            "AGENT_RPC_INSTANCE_ID": "${AGENT_RPC_INSTANCE_ID:-}",
                            "AGENT_RPC_TELEMETRY_PATH": "/home/agent/.local/state/agent-rpc/usage.sqlite",
                            "AGENT_RPC_ENVIRONMENT": f"docker:{self.project}:{self.service}",
                        },
                        "labels": {
                            "io.litellm-agent-rpc.instance": "${AGENT_RPC_INSTANCE_ID:-}"
                        },
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(self.config_path),
                                "target": "/app/config.json",
                                "read_only": True,
                                "bind": {"create_host_path": False},
                            },
                            {
                                "type": "volume",
                                "source": volume,
                                "target": "/home/agent/.local/state/agent-rpc",
                            },
                        ],
                        "command": [
                            "serve",
                            "--config",
                            "/app/config.json",
                            "--host",
                            "0.0.0.0",
                            "--port",
                            "4000",
                        ],
                    }
                },
                "volumes": {volume: {}},
            }
            self._override.write_text(json.dumps(override), encoding="utf-8")
        return self._override

    def _compose(self):
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(self.compose_path),
            "--file",
            str(self._override_path()),
        ]

    def _docker(self, args, *, timeout=30):
        env = os.environ.copy()
        env["AGENT_RPC_CONTROL_TOKEN"] = self._token or ""
        env["AGENT_RPC_INSTANCE_ID"] = self._instance or ""
        try:
            return subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=timeout,
                creationflags=_hidden_flags(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ManagementError("Docker or Compose is unavailable") from None

    def _selected_container(self):
        result = self._docker(
            self._compose() + ["ps", "--all", "--quiet", self.service]
        )
        if result.returncode:
            raise ManagementError("Cannot inspect the selected Compose service")
        ids = result.stdout.decode("ascii", errors="replace").split()
        if len(ids) > 1 or any(
            not re.fullmatch(r"[0-9a-f]{12,64}", item) for item in ids
        ):
            raise ManagementError("Selected service has multiple or invalid containers")
        return ids[0] if ids else None

    def _validate_compose(self):
        # Check the resolved publish rule before starting anything. Compose
        # configuration can contain secrets; never surface its raw output.
        result = self._docker(self._compose() + ["config", "--format", "json"])
        if result.returncode:
            raise ManagementError("Cannot validate the selected Compose service")
        try:
            service = json.loads(result.stdout)["services"][self.service]
            ports = service.get("ports", [])
            if service.get("network_mode") == "host" or service.get("container_name"):
                raise ValueError
            restart = service.get("restart", "no")
            policy = service.get("deploy", {}).get("restart_policy", {})
            if restart not in (None, "", "no") or (
                policy and policy.get("condition") not in ("none",)
            ):
                raise ValueError
            if not isinstance(ports, list) or len(ports) != 1:
                raise ValueError
            binding = ports[0]
            if (
                binding.get("host_ip") != "127.0.0.1"
                or binding.get("target") != 4000
                or binding.get("protocol", "tcp") != "tcp"
                or not 1 <= int(binding.get("published", 0)) <= 65535
            ):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ManagementError(
                "Compose must publish only container port 4000 on 127.0.0.1 "
                "and must not use host networking, a fixed container name, "
                "or an automatic restart policy"
            ) from None

    def _inspect(self, container, template):
        result = self._docker(["docker", "inspect", "--format", template, container])
        if result.returncode:
            raise ManagementError("Cannot inspect the selected container")
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _verify_container(self, container):
        if not container or container != self._container or not self._instance:
            raise ManagementError("Container is not owned by this manager")
        try:
            labels = json.loads(self._inspect(container, "{{json .Config.Labels}}"))
        except (ValueError, TypeError):
            raise ManagementError("Cannot verify container ownership") from None
        if not isinstance(labels, dict) or any(
            (
                labels.get("com.docker.compose.project") != self.project,
                labels.get("com.docker.compose.service") != self.service,
                labels.get("io.litellm-agent-rpc.instance") != self._instance,
            )
        ):
            raise ManagementError("Container identity does not match this manager")

    def _docker_running(self, container):
        self._verify_container(container)
        state = self._inspect(container, "{{.State.Running}}")
        if state not in ("true", "false"):
            raise ManagementError("Cannot determine container state")
        return state == "true"

    def _docker_port(self, container):
        self._verify_container(container)
        try:
            bindings = json.loads(
                self._inspect(
                    container, '{{json (index .NetworkSettings.Ports "4000/tcp")}}'
                )
            )
            if not isinstance(bindings, list) or len(bindings) != 1:
                raise ValueError
            binding = bindings[0]
            if binding["HostIp"] != "127.0.0.1":
                raise ValueError
            port = int(binding["HostPort"])
            if not 1 <= port <= 65535:
                raise ValueError
            return port
        except (TypeError, ValueError, KeyError):
            raise ManagementError(
                "Compose must publish port 4000 only on 127.0.0.1"
            ) from None

    def _docker_status(self):
        verified = False
        try:
            container = self._selected_container()
            if container is None:
                return self._base("stopped")
            if container != self._container:
                return self._base(
                    "unknown", message="Selected container is not owned by this manager"
                )
            self._verify_container(container)
            verified = True
            if not self._docker_running(container):
                return self._base("stopped", owned=True)
            self._port = self._docker_port(container)
            return self._status_response(self._request("status"))
        except ManagementError as error:
            return self._base("unknown", owned=verified, message=str(error))

    def status(self):
        with self._lock:
            return (
                self._local_status() if self.mode == "local" else self._docker_status()
            )

    def _new_identity(self):
        self._token = secrets.token_urlsafe(32)
        self._instance = uuid.uuid4().hex

    def _launch_local(self):
        config = self._config()
        self._port = config.get("ports", {}).get("router", 4000)
        if not _available(self._port):
            raise ManagementError("Port is occupied by an unowned service")
        self._new_identity()
        env = os.environ.copy()
        env.update(
            AGENT_RPC_CONTROL_TOKEN=self._token,
            AGENT_RPC_INSTANCE_ID=self._instance,
            AGENT_RPC_TELEMETRY_PATH=str(self.config_path.parent / "usage.sqlite"),
            AGENT_RPC_ENVIRONMENT="local",
        )
        kwargs = {
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = _hidden_flags() | 0x00000004  # CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True
        try:
            self._child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "litellm_agent_rpc",
                    "serve",
                    "--config",
                    str(self.config_path),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self._port),
                ],
                **kwargs,
            )
            if os.name == "nt":
                from .management_process import ProcessTree

                self._tree = ProcessTree(self._child.pid)
                self._tree.resume()
        except (OSError, RuntimeError):
            if self._child is not None:
                self._close_tree(kill=True)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._child.wait(timeout=5)
                self._child = None
            self._token = self._instance = None
            raise ManagementError("Cannot launch an owned gateway process") from None

    def _launch_docker(self):
        self._config()
        self._validate_compose()
        existing = self._selected_container()
        if existing is not None:
            self._verify_container(existing)
            if self._docker_running(existing):
                raise ManagementError("The managed container is already running")
            removed = self._docker(["docker", "rm", existing])
            if removed.returncode:
                raise ManagementError("Cannot remove the stopped managed container")
            self._container = None
        self._new_identity()
        result = self._docker(
            self._compose()
            + ["up", "--detach", "--no-deps", "--no-recreate", self.service],
            timeout=120,
        )
        container = self._selected_container()
        if container is not None:
            self._container = container
            try:
                self._verify_container(container)
            except ManagementError:
                self._container = None
                raise
        if result.returncode or not container:
            if container and self._docker_running(container):
                self._stop_failed_container(container)
            raise ManagementError("Cannot start the selected managed Compose service")
        try:
            self._port = self._docker_port(container)
        except ManagementError:
            # A surprising published binding is an exposure risk. Stop only
            # this newly created, label-verified container; retain its volumes.
            self._stop_failed_container(container)
            raise ManagementError(
                "Unexpected Docker port binding; managed container stopped"
            ) from None

    def _stop_failed_container(self, container):
        self._verify_container(container)
        result = self._docker(["docker", "stop", "--time", "15", container], timeout=30)
        if result.returncode:
            raise ManagementError(
                "Cannot stop the owned container after failed startup"
            )

    def start(self):
        with self._lock:
            before = self.status()
            if before["state"] in ("running", "draining", "starting"):
                raise ManagementError("Service is already running or starting")
            if before["state"] == "unknown":
                raise ManagementError(
                    before["message"] or "Cannot establish service ownership"
                )
            if self.mode == "local":
                self._launch_local()
            else:
                self._launch_docker()
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                state = self.status()
                if state["state"] == "running" and state["owned"]:
                    return state
                if self.mode == "local" and self._child is None:
                    raise ManagementError("Gateway process exited during startup")
                time.sleep(0.1)
            if self.mode == "local" and self._child is not None:
                self._close_tree(kill=True)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._child.wait(timeout=5)
                self._child = None
                self._token = self._instance = None
            elif self.mode == "docker" and self._container is not None:
                self._stop_failed_container(self._container)
            raise ManagementError(
                "Gateway did not become manageable before the deadline"
            )

    def resume(self):
        with self._lock:
            state = self.status()
            if not state["owned"] or state["state"] not in ("running", "draining"):
                raise ManagementError("No manageable service is owned by this manager")
            return self._status_response(self._request("resume", post=True))

    def cancel_pending(self, pending_id):
        with self._lock:
            if not isinstance(pending_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,128}", pending_id
            ):
                raise ManagementError("Invalid pending session")
            state = self.status()
            if not state["owned"] or state["state"] not in ("running", "draining"):
                raise ManagementError("No manageable service is owned by this manager")
            return self._status_response(
                self._request("cancel", post=True, body={"id": pending_id})
            )

    def stop(self, timeout=60):
        with self._lock:
            state = self.status()
            if state["state"] == "stopped":
                return state
            if not state["owned"] or state["state"] not in ("running", "draining"):
                raise ManagementError("Cannot stop an unowned or unreachable service")
            deadline = time.monotonic() + timeout
            self._request("drain", post=True)
            while True:
                state = self.status()
                if not state["owned"] or state["state"] not in ("running", "draining"):
                    raise ManagementError("Service became unavailable while draining")
                if self._quiescent(state):
                    try:
                        self._request("stop", post=True)
                    except _ControlNotDrained:
                        # A continuation may have entered between status and the
                        # atomic stop latch. Keep draining until it completes.
                        pass
                    else:
                        break
                if time.monotonic() >= deadline:
                    raise ManagementError(
                        "Drain timed out; service remains draining; resume or wait"
                    )
                time.sleep(0.1)
            while time.monotonic() < deadline:
                if self.mode == "local":
                    if self._child is not None and self._child.poll() is not None:
                        return self.status()
                elif not self._docker_running(self._container):
                    return self.status()
                time.sleep(0.1)
            raise ManagementError(
                "Service is stopping but has not exited; no forced termination was used"
            )

    def restart(self, timeout=60):
        with self._lock:
            self.stop(timeout=timeout)
            return self.start()

    def close(self):
        """Release temporary manager files after the owned service has stopped."""
        with self._lock:
            if self._child is not None and self._child.poll() is None:
                raise ManagementError(
                    "Stop the owned service before closing its manager"
                )
            if (
                self.mode == "docker"
                and self._container
                and self.status()["state"] != "stopped"
            ):
                raise ManagementError(
                    "Stop the owned service before closing its manager"
                )
            if self._override_dir is not None:
                self._override_dir.cleanup()
                self._override_dir = self._override = None

    def _auth_stopped(self):
        state = self.status()
        if state["state"] != "stopped":
            raise ManagementError(
                "Stop the selected service before changing or checking CLI login"
            )
        # A selected stopped Docker container must still be one we own.
        if self.mode == "docker" and self._selected_container() is not None:
            self._verify_container(self._container)

    def inspect_backend(self, backend):
        """Inspect the local saved configuration or the running container."""
        with self._lock:
            if backend not in BACKENDS:
                raise ManagementError("Unknown backend")
            if self.mode == "local":
                result = inspect_backend_config(self._config(), backend)
                result["scope"] = "local_saved"
                return result
            state = self.status()
            if state["state"] != "running" or not state["owned"]:
                raise ManagementError(
                    "Start the owned Compose service before inspecting backends"
                )
            completed = self._docker(
                self._compose()
                + [
                    "exec",
                    "-T",
                    self.service,
                    "python",
                    "-m",
                    "litellm_agent_rpc.backend_inspect",
                    "/app/config.json",
                    backend,
                ],
                timeout=25,
            )
            try:
                if completed.returncode or len(completed.stdout) > 1024 * 1024:
                    raise ValueError
                result = json.loads(completed.stdout)
                if not isinstance(result, dict) or result.get("backend") != backend:
                    raise ValueError
                return {
                    "backend": backend,
                    "scope": "docker_running",
                    "configured_executable": result.get("configured_executable"),
                    "resolved_executable": result.get("resolved_executable"),
                    "version": result.get("version"),
                    "dependency": result.get("dependency"),
                    "gated_reason": BACKENDS[backend]["gated_reason"],
                }
            except (ValueError, TypeError):
                raise ManagementError(
                    "Backend inspection failed in the selected container"
                ) from None

    def inspect_capabilities(self):
        """Read driver evidence without opening a native session or CLI login."""
        with self._lock:
            if self.mode == "local":
                from .cli import inspect_capabilities

                return {
                    "scope": "local_saved",
                    "capabilities": asyncio.run(inspect_capabilities(self._config())),
                }
            state = self.status()
            if state["state"] != "running" or not state["owned"]:
                raise ManagementError(
                    "Start the owned Compose service before inspecting its capabilities"
                )
            completed = self._docker(
                self._compose()
                + [
                    "exec",
                    "-T",
                    self.service,
                    "agent-rpc",
                    "capabilities",
                    "--config",
                    "/app/config.json",
                ],
                timeout=25,
            )
            try:
                if completed.returncode or len(completed.stdout) > 1024 * 1024:
                    raise ValueError
                capabilities = {}
                for line in completed.stdout.splitlines():
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError
                    alias = entry.pop("alias")
                    if not isinstance(alias, str) or alias in capabilities:
                        raise ValueError
                    capabilities[alias] = entry
                return {"scope": "docker_running", "capabilities": capabilities}
            except (ValueError, TypeError, KeyError):
                raise ManagementError(
                    "Capability inspection failed in the selected container"
                ) from None

    def install_command(self, backend):
        """Prepare an explicit, pinned local extra install; never execute it."""
        with self._lock:
            if backend != "claude":
                raise ManagementError("No supported installable extra for this backend")
            if self.mode != "local":
                raise ManagementError("Rebuild the Docker image with the Claude extra")
            self._auth_stopped()
            requirement = f"claude-agent-sdk=={CLAUDE_SDK_VERSION}"
            uv = shutil.which("uv")
            if uv:
                return [uv, "pip", "install", "--python", sys.executable, requirement]
            try:
                pip = importlib.util.find_spec("pip")
            except (ImportError, ValueError):
                pip = None
            if pip is not None:
                return [sys.executable, "-m", "pip", "install", requirement]
            raise ManagementError(
                "Install uv or pip before installing the Claude extra"
            )

    def auth_command(self, backend, action):
        with self._lock:
            descriptor = BACKENDS.get(backend)
            if not descriptor or action not in descriptor["auth_actions"]:
                raise ManagementError("This authentication action is not supported")
            self._auth_stopped()
            args = {
                "codex": {
                    "login": ["login"],
                    "logout": ["logout"],
                    "check": ["login", "status"],
                },
                "claude": {
                    "login": ["auth", "login"],
                    "logout": ["auth", "logout"],
                    "check": ["auth", "status"],
                },
                "kiro": {"login": ["login"], "logout": ["logout"], "check": ["whoami"]},
            }[backend][action]
            if self.mode == "docker":
                if backend != "codex":
                    raise ManagementError(
                        "The example container only includes the Codex CLI"
                    )
                if action == "login":
                    args = ["login", "--device-auth"]
                return self._compose() + [
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "codex",
                    self.service,
                    *args,
                ]
            configured = self._config().get("executables", {}).get(backend)
            if not configured and backend == "claude":
                try:
                    spec = importlib.util.find_spec("claude_agent_sdk")
                    configured = (
                        str(
                            Path(spec.origin).parent
                            / "_bundled"
                            / ("claude.exe" if os.name == "nt" else "claude")
                        )
                        if spec and spec.origin
                        else None
                    )
                except (ImportError, ValueError):
                    configured = None
            if not configured:
                configured = {"codex": "codex", "kiro": "kiro-cli"}.get(backend)
            resolved = shutil.which(configured) if isinstance(configured, str) else None
            if not resolved:
                raise ManagementError("The selected CLI executable is unavailable")
            return [resolved, *args]

    def doctor(self):
        with self._lock:
            self._config()
            if self.mode == "docker":
                state = self.status()
                if state["state"] != "running" or not state["owned"]:
                    raise ManagementError(
                        "Start the owned Compose service before running doctor"
                    )
                command = self._compose() + [
                    "exec",
                    "-T",
                    self.service,
                    "agent-rpc",
                    "doctor",
                    "--config",
                    "/app/config.json",
                ]
            else:
                command = [
                    sys.executable,
                    "-m",
                    "litellm_agent_rpc",
                    "doctor",
                    "--config",
                    str(self.config_path),
                ]
            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=120,
                    check=False,
                    creationflags=_hidden_flags(),
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ManagementError("Doctor could not complete") from None
            reports = []
            for line in completed.stdout.splitlines():
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(item, dict) or item.get("backend") not in BACKENDS:
                    continue
                passed = item.get("preflight") == "passed"
                reason = str(item.get("reason", ""))
                known = (
                    "claude_live_acceptance_pending",
                    "kiro_profile_isolation_not_verified",
                    "backend_not_ready",
                    "cli_login_required",
                    "unsupported_cli_version",
                )
                code = next(
                    (candidate for candidate in known if candidate in reason), None
                )
                reports.append(
                    {
                        "backend": item["backend"],
                        "preflight": "passed" if passed else "failed",
                        "reason": None if passed else code or "preflight_failed",
                        "model_access_verified": False,
                    }
                )
            return {
                "state": "passed"
                if completed.returncode == 0 and reports
                else "failed",
                "backends": reports,
            }
