"""Prepare an isolated Mini-pod lease and Codex profile for the local bridge."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".local" / "minipod-codex" / "bridge"
IMAGE = "minipod-codex:0.167.0"
MAX_SLOTS = 16
DEFAULT_DISK_MB = 2048
MIN_DISK_MB = 1536
MAX_DISK_MB = 65536


def parse_disk_mb(value=None, *, settings=None):
    raw = value
    if raw is None:
        env_val = os.environ.get("MINIPOD_DISK_MB")
        if env_val is not None and env_val.strip() != "":
            raw = env_val.strip()
        elif isinstance(settings, dict) and settings.get("disk_mb") is not None:
            raw = settings["disk_mb"]
        else:
            return DEFAULT_DISK_MB
    if isinstance(raw, bool):
        raise ValueError(f"MINIPOD_DISK_MB must be an integer between {MIN_DISK_MB} and {MAX_DISK_MB}")
    try:
        disk_mb = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MINIPOD_DISK_MB must be an integer between {MIN_DISK_MB} and {MAX_DISK_MB}") from exc
    if isinstance(raw, str) and ("." in raw or int(raw, 10) != disk_mb):
        raise ValueError(f"MINIPOD_DISK_MB must be an integer between {MIN_DISK_MB} and {MAX_DISK_MB}")
    if not MIN_DISK_MB <= disk_mb <= MAX_DISK_MB:
        raise ValueError(f"MINIPOD_DISK_MB must be an integer between {MIN_DISK_MB} and {MAX_DISK_MB}")
    return disk_mb


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
    if isinstance(raw, str) and raw != str(slots):
        # Allow surrounding whitespace already stripped, reject floats like "2.0" or "+2" if non-canonical
        try:
            if int(raw, 10) != slots or "." in raw:
                raise ValueError
        except ValueError as exc:
            raise ValueError(f"MINIPOD_SLOTS must be an integer between 1 and {MAX_SLOTS}") from exc
    if not 1 <= slots <= MAX_SLOTS:
        raise ValueError(f"MINIPOD_SLOTS must be an integer between 1 and {MAX_SLOTS}")
    return slots


def slot_environment_id(slot):
    return "minipod" if slot == 0 else f"minipod-{slot}"


def require_rootless(sandbox_cli, endpoint, sandbox_id, *, slots=1):
    slot_args = [str(slots)] if slots > 1 else []
    deadline = 60 if slots <= 1 else max(60, 30 + slots * 15)
    result = subprocess.check_output([
        sandbox_cli, "--endpoint", endpoint, "--json", "exec", sandbox_id,
        "--deadline-secs", str(deadline), "--", "/bin/sh",
        "/workspace/minipod-codex/rootless.sh", "init", *slot_args,
    ], timeout=max(120, deadline + 30))
    if json.loads(result)["stdout"].strip() != "true":
        raise RuntimeError("Podman did not report rootless mode")


def executor_ready(sandbox_cli, endpoint, sandbox_id, *, slots=1):
    slot_args = [str(slots)] if slots > 1 else []
    deadline = 60 if slots <= 1 else max(60, 30 + slots * 15)
    try:
        result = subprocess.check_output([
            sandbox_cli, "--endpoint", endpoint, "--json", "exec", sandbox_id,
            "--deadline-secs", str(deadline), "--", "/bin/sh",
            "/workspace/minipod-codex/rootless.sh", "check", *slot_args,
        ], stderr=subprocess.DEVNULL, timeout=deadline + 15)
        output = json.loads(result)
        version = output.get("stdout")
        return (
            output.get("exit_code") == 0 and output.get("timed_out") is False
            and output.get("stdout_truncated") is False and output.get("stderr_truncated") is False
            and isinstance(version, str) and version.strip() == "codex-cli 0.167.0"
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_sandbox(minipod, endpoint, sandbox_id, state=STATE, *, sandbox_cli=None, slots=1):
    slots = parse_slot_count(slots)
    minipod = Path(minipod).resolve()
    cli = str(sandbox_cli or (minipod / "target/release/sandbox-cli.exe"))
    prefix = [cli, "--endpoint", endpoint]
    sandboxes = json.loads(subprocess.check_output([*prefix, "--json", "list"], timeout=15))["sandboxes"]
    lease = next((lease for lease in sandboxes if lease["id"] == sandbox_id), None)
    if not lease or not lease["alive"] or lease["network"]["type"] != "Isolated" or lease["docker_endpoint"]:
        raise RuntimeError("Use a live Isolated VM lease without a rootful Docker endpoint")

    build = state.parent / "stdio"
    build.mkdir(parents=True, exist_ok=True)
    manifest = build / "Cargo.toml"
    source = (Path(__file__).parent / "stdio.rs").as_posix()
    manifest.write_text(f'''[package]
name = "minipod-stdio"
version = "0.1.0"
edition = "2024"

[[bin]]
name = "minipod-stdio"
path = {json.dumps(source)}

[dependencies]
sandbox-client = {{ path = {json.dumps((minipod / 'crates/sandbox-client').as_posix())} }}
sandbox-core = {{ path = {json.dumps((minipod / 'crates/sandbox-core').as_posix())} }}
tokio = {{ version = "1", features = ["rt-multi-thread", "macros", "io-std", "io-util", "sync"] }}
''', encoding="utf-8")
    target = minipod / "target"
    subprocess.run(["cargo", "build", "--manifest-path", str(manifest), "--target-dir", str(target)], check=True, timeout=300)
    launcher = str(target / "debug/minipod-stdio.exe")

    sandbox = [*prefix, "exec", sandbox_id, "--"]
    guest_config = "/workspace/minipod-codex"
    subprocess.run([*sandbox, "/bin/mkdir", "-p", guest_config], check=True, timeout=60)
    subprocess.run([*prefix, "copy-in", sandbox_id, str(Path(__file__).parent / "rootless.sh"), guest_config], check=True, timeout=60)
    require_rootless(cli, endpoint, sandbox_id, slots=slots)
    if not executor_ready(cli, endpoint, sandbox_id, slots=slots):
        container_id = subprocess.check_output(
            ["docker", "create", "--platform", "linux/amd64", IMAGE],
            text=True,
            timeout=60,
        ).strip()
        if not container_id:
            raise RuntimeError("docker create returned an empty container ID")
        try:
            export_proc = subprocess.Popen(
                ["docker", "export", container_id],
                stdout=subprocess.PIPE,
            )
            try:
                subprocess.run(
                    [launcher, endpoint, sandbox_id, "load"],
                    stdin=export_proc.stdout,
                    timeout=310,
                    check=True,
                )
            finally:
                if export_proc.stdout is not None:
                    export_proc.stdout.close()
                if export_proc.wait(timeout=30) != 0:
                    raise RuntimeError("docker export failed")
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        require_rootless(cli, endpoint, sandbox_id, slots=slots)

    return launcher


def publish_sandbox(profile, state, launcher, endpoint, sandbox_id, *, slots=1):
    slots = parse_slot_count(slots)
    blocks = ['default = "minipod"\ninclude_local = false\n']
    for slot in range(slots):
        env_id = slot_environment_id(slot)
        command_args = [endpoint, sandbox_id, "exec"] if slot == 0 else [endpoint, sandbox_id, "exec", str(slot)]
        blocks.append(
            f"\n[[environments]]\nid = {json.dumps(env_id)}\n"
            f"program = {json.dumps(launcher)}\nargs = {json.dumps(command_args)}\n"
        )
    atomic_write(profile / "environments.toml", "".join(blocks))
    atomic_write(state / "sandbox-id", sandbox_id + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mini-pod", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--slots", type=int, default=None)
    args = parser.parse_args()

    profile = STATE / "profile"
    host_cwd = STATE / "host-work"
    if not (profile / "config.toml").exists() or not (STATE / "bridge-key").exists():
        raise RuntimeError("The private host profile and gateway bridge key must already be provisioned")
    slots = parse_slot_count(args.slots)
    launcher = prepare_sandbox(args.mini_pod, args.endpoint, args.sandbox_id, slots=slots)
    publish_sandbox(profile, STATE, launcher, args.endpoint, args.sandbox_id, slots=slots)
    codex = shutil.which("codex")
    settings = {"profile": str(profile), "host_cwd": str(host_cwd), "codex": codex}
    if args.slots is not None:
        settings["slots"] = slots
    atomic_write(STATE / "settings.json", json.dumps(settings))
    print(f"Rootless executor prepared for {args.sandbox_id} ({slots} slot(s)).")


if __name__ == "__main__":
    main()
