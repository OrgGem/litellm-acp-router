"""Keep the bridge's isolated Mini-pod executor available."""

import json
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from pathlib import Path

from prepare import (
    atomic_write,
    parse_disk_mb,
    parse_slot_count,
    prepare_sandbox,
    publish_sandbox,
    slot_environment_id,
)

LEASE_SECONDS = 86400
RENEW_BELOW_MS = 12 * 60 * 60 * 1000
CHECK_INTERVAL = 30


class SpawnLeaseExpired(RuntimeError):
    pass


class SandboxManager:
    def __init__(self, settings_path, profile, *, slots=None, disk_mb=None):
        self.state = Path(settings_path).parent
        self.profile = Path(profile)
        with (self.profile / "environments.toml").open("rb") as source:
            environments = tomllib.load(source)["environments"]
        environment = next(item for item in environments if item["id"] == "minipod")
        self.launcher = environment["program"]
        args = environment["args"]
        if len(args) < 3:
            raise ValueError("Unexpected Mini-pod launcher args")
        self.endpoint, self.active_id, action = args[:3]
        self.active_endpoint = self.endpoint
        if action != "exec":
            raise ValueError("Unexpected Mini-pod launcher action")
        self.minipod = Path(self.launcher).resolve().parents[2]
        settings_file = Path(settings_path)
        settings = json.loads(settings_file.read_text(encoding="utf-8-sig")) if settings_file.exists() else {}
        self.slots = parse_slot_count(slots, settings=settings)
        self.disk_mb = parse_disk_mb(disk_mb, settings=settings)
        self.spawn_endpoint = settings.get("spawn_endpoint", self.endpoint)
        self.cli = settings.get("sandbox_cli", str(self.minipod / "target/release/sandbox-cli.exe"))
        self.legacy_sandbox = settings.get("legacy_sandbox")
        self.operation_id = None
        self.pending_path = self.state / "sandbox-pending.json"
        self.active_path = self.state / "sandbox-active.json"
        self._condition = threading.Condition()
        self._requested = True
        self._checking = False
        self._ready = False
        self._verified_id = None
        self._invalidate = False
        self._generation = 0
        self._stopping = False
        self._thread = None
        self._stage = "startup"
        self._next_retry = 0.0

    def _published_slots_match(self, sandbox_id):
        env_path = self.profile / "environments.toml"
        if not env_path.exists():
            return False
        try:
            with env_path.open("rb") as source:
                parsed = tomllib.load(source)
        except (OSError, tomllib.TOMLDecodeError):
            return False
        entries = parsed.get("environments")
        if not isinstance(entries, list) or len(entries) != self.slots:
            return False
        for slot in range(self.slots):
            expected_id = slot_environment_id(slot)
            expected_args = (
                [self.endpoint, sandbox_id, "exec"]
                if slot == 0
                else [self.endpoint, sandbox_id, "exec", str(slot)]
            )
            entry = next((item for item in entries if item.get("id") == expected_id), None)
            if entry is None or entry.get("args") != expected_args:
                return False
        return True

    def start(self):
        with self._condition:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="minipod-lifecycle", daemon=True)
            self._thread.start()

    def stop(self):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()

    def kick(self):
        with self._condition:
            self._invalidate = True
            self._ready = False
            self._requested = True
            self._condition.notify_all()

    def is_ready(self):
        with self._condition:
            return self._ready

    def wait_ready(self, timeout):
        deadline = time.monotonic() + timeout
        with self._condition:
            target = self._generation + 1
            if not self._checking:
                self._requested = True
                self._condition.notify_all()
            while self._generation < target and not self._stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return self._ready

    def _run(self):
        while True:
            with self._condition:
                if not self._requested and not self._stopping:
                    self._condition.wait(timeout=CHECK_INTERVAL)
                if self._stopping:
                    return
                self._requested = False
                self._checking = True
                if self._invalidate or self._verified_id is None:
                    self._ready = False
                if self._invalidate:
                    self._verified_id = None
                    self._invalidate = False
            try:
                if time.monotonic() < self._next_retry:
                    ready = False
                else:
                    ready = self.reconcile()
                    self._next_retry = 0.0
            except Exception as exc:
                # The subprocess output may contain credentials or workspace content.
                print(f"Mini-pod lifecycle {self._stage} failed: {type(exc).__name__}", file=sys.stderr, flush=True)
                self._next_retry = time.monotonic() + CHECK_INTERVAL
                ready = False
            with self._condition:
                self._ready = ready
                self._checking = False
                self._generation += 1
                self._condition.notify_all()

    def _command(self, *args, timeout=30):
        result = subprocess.run(
            [self.cli, "--endpoint", self.endpoint, "--json", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=timeout,
        )
        return json.loads(result.stdout)

    def _list(self):
        sandboxes = self._command("list")["sandboxes"]
        if not isinstance(sandboxes, list):
            raise ValueError("Invalid Mini-pod sandbox list")
        return sandboxes

    def _spawn(self):
        if self.slots <= 1:
            vcpus, ram_mb, rss_mb = 2, 1536, 2304
        else:
            vcpus = min(4, max(2, (self.slots + 1) // 2))
            ram_mb = max(1536, min(4096, 1024 + self.slots * 256))
            rss_mb = ram_mb + 1024
        storage_mb = self.disk_mb + 512
        try:
            lease = self._command(
                "spawn", "--operation-id", self.operation_id,
                "--isolation", "vm", "--network", "isolated", "--accel", "preferred",
                "--vcpus", str(vcpus), "--ram-mb", str(ram_mb), "--rss-mb", str(rss_mb),
                "--disk-mb", str(self.disk_mb), "--storage-mb", str(storage_mb),
                "--ttl-secs", str(LEASE_SECONDS), timeout=300,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr.startswith(b"sandbox-cli error: sandbox lease expired:"):
                raise SpawnLeaseExpired("Mini-pod keyed lease expired") from None
            raise
        sandbox_id = lease.get("id")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ValueError("Invalid Mini-pod lease ID")
        return sandbox_id

    def _renew(self, sandbox_id):
        self._command("renew", sandbox_id, "--ttl-secs", str(LEASE_SECONDS))

    def _prepare(self, sandbox_id):
        return prepare_sandbox(
            self.minipod, self.endpoint, sandbox_id, self.state,
            sandbox_cli=self.cli, slots=self.slots,
        )

    def _verify(self, sandbox_id):
        slot_args = (str(self.slots),) if self.slots > 1 else ()
        deadline = 60 if self.slots <= 1 else max(60, 30 + self.slots * 15)
        for action, expected in (("verify", "true"), ("check", "codex-cli 0.167.0")):
            result = self._command(
                "exec", sandbox_id, "--deadline-secs", str(deadline), "--", "/bin/sh",
                "/workspace/minipod-codex/rootless.sh", action, *slot_args, timeout=deadline + 15,
            )
            output = result.get("stdout")
            if (
                result.get("exit_code") != 0 or result.get("timed_out") is not False
                or result.get("stdout_truncated") is not False or result.get("stderr_truncated") is not False
                or not isinstance(output, str) or output.strip() != expected
            ):
                raise RuntimeError("Mini-pod executor readiness check failed")

    def _publish(self, sandbox_id, launcher):
        publish_sandbox(
            self.profile, self.state, launcher, self.endpoint, sandbox_id, slots=self.slots
        )

    @staticmethod
    def _safe(lease):
        return lease["alive"] and lease["network"]["type"] == "Isolated" and not lease["docker_endpoint"]

    def _legacy_terminal(self):
        legacy = self.legacy_sandbox
        if not isinstance(legacy, dict) or legacy.get("endpoint") != self.active_endpoint or legacy.get("id") != self.active_id:
            raise RuntimeError("Legacy sandbox ownership is not configured")
        journal = Path(legacy["journal_path"])
        try:
            text = journal.read_text(encoding="utf-8")
        except FileNotFoundError:
            return True
        fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        if fields.get("sandbox_id") != legacy["id"]:
            raise RuntimeError("Legacy sandbox journal identity changed")
        state = fields.get("state")
        pid, start_token = fields.get("pid"), fields.get("start_token")
        if state == "destroying" and pid == "none" and start_token == "none":
            return True
        if pid != str(legacy["pid"]) or start_token != str(legacy["start_token"]):
            raise RuntimeError("Legacy sandbox journal identity changed")
        return False

    def reconcile(self):
        pending = None
        if self.pending_path.exists():
            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
            if pending.get("endpoint"):
                self.endpoint = pending["endpoint"]
        self._stage = "list"
        sandboxes = self._list()
        leases = {item["id"]: item for item in sandboxes}
        pending_id = None
        expired_pending = False
        active_record = None
        expired_active = False
        if pending is None and self.active_path.exists():
            active_record = json.loads(self.active_path.read_text(encoding="utf-8"))
            if active_record.get("id") != self.active_id or active_record.get("endpoint") != self.active_endpoint:
                raise RuntimeError("Active sandbox receipt does not match the profile")
            self.operation_id = active_record["operation_id"]
            self._stage = "active receipt"
            try:
                receipt_id = self._spawn()
            except SpawnLeaseExpired:
                expired_active = True
            else:
                if receipt_id != self.active_id:
                    raise RuntimeError("Active sandbox keyed receipt changed")
        if pending is not None:
            pending_id = pending.get("id")
            if not pending_id and not pending.get("operation_id"):
                raise RuntimeError("Legacy pending spawn has no operation UUID")
            if pending_id and (pending_id not in leases or not leases[pending_id]["alive"]) and not pending.get("operation_id"):
                self.pending_path.unlink()
                pending = None
                pending_id = None
            elif pending.get("operation_id"):
                self._stage = "spawn"
                self.operation_id = pending["operation_id"]
                try:
                    received_id = self._spawn()
                except SpawnLeaseExpired:
                    self.pending_path.unlink()
                    pending = None
                    pending_id = None
                    expired_pending = True
                else:
                    if pending_id and pending_id != received_id:
                        raise RuntimeError("Mini-pod keyed receipt changed")
                    pending_id = received_id
                    pending["id"] = pending_id
                    atomic_write(self.pending_path, json.dumps(pending))

        sandbox_id = pending_id or self.active_id
        lease = leases.get(sandbox_id) if not (expired_pending or expired_active) and (pending_id or self.endpoint == self.active_endpoint) else None
        if lease is not None and lease["alive"] and not self._safe(lease):
            raise RuntimeError("Mini-pod lease has an unexpected isolation policy")
        if (pending_id is None and active_record is None and lease is not None and lease["alive"]
            and self.endpoint == self.active_endpoint and self.endpoint != self.spawn_endpoint):
            self._stage = "legacy ownership"
            if self._legacy_terminal() or str(lease.get("pid")) != str(self.legacy_sandbox["pid"]):
                raise RuntimeError("Legacy sandbox no longer matches its recorded identity")
        if pending_id is None and (lease is None or not lease["alive"]):
            if active_record is None and self.endpoint == self.active_endpoint and self.endpoint != self.spawn_endpoint:
                self._stage = "legacy cleanup"
                if not self._legacy_terminal():
                    raise RuntimeError("Legacy sandbox is still being removed")
            self._stage = "spawn"
            self.endpoint = self.spawn_endpoint
            self.operation_id = str(uuid.uuid4())
            print("Mini-pod lifecycle creating isolated sandbox", file=sys.stderr, flush=True)
            pending = {"operation_id": self.operation_id, "endpoint": self.endpoint}
            atomic_write(self.pending_path, json.dumps(pending))
            try:
                sandbox_id = self._spawn()
            except SpawnLeaseExpired:
                self.pending_path.unlink()
                raise
            pending["id"] = sandbox_id
            atomic_write(self.pending_path, json.dumps(pending))
            pending_id = sandbox_id
            lease = None

        if lease is not None and lease.get("remaining_ttl_ms", 0) < RENEW_BELOW_MS:
            self._stage = "renew"
            self._renew(sandbox_id)

        if pending_id:
            self._stage = "prepare"
            print("Mini-pod lifecycle preparing executor", file=sys.stderr, flush=True)
            launcher = self._prepare(sandbox_id)
            self._stage = "verify"
            print("Mini-pod lifecycle checking executor", file=sys.stderr, flush=True)
            self._verify(sandbox_id)
            self._stage = "publish"
            self._publish(sandbox_id, launcher)
            if pending.get("operation_id"):
                atomic_write(self.active_path, json.dumps({
                    "operation_id": pending["operation_id"], "endpoint": self.endpoint, "id": sandbox_id,
                }))
            self.active_id = sandbox_id
            self.active_endpoint = self.endpoint
            self.launcher = launcher
            self._verified_id = sandbox_id
            self.pending_path.unlink(missing_ok=True)
            print("Mini-pod lifecycle ready", file=sys.stderr, flush=True)
        elif self._verified_id != sandbox_id or not self._published_slots_match(sandbox_id):
            if not self._published_slots_match(sandbox_id):
                self._stage = "prepare"
                launcher = self._prepare(sandbox_id)
                self._stage = "verify"
                self._verify(sandbox_id)
                self._stage = "publish"
                self._publish(sandbox_id, launcher)
                self.launcher = launcher
            else:
                try:
                    self._stage = "verify"
                    self._verify(sandbox_id)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    self._stage = "prepare"
                    launcher = self._prepare(sandbox_id)
                    self._stage = "verify"
                    self._verify(sandbox_id)
                    self._stage = "publish"
                    self._publish(sandbox_id, launcher)
                    self.launcher = launcher
            self._verified_id = sandbox_id
        return True
