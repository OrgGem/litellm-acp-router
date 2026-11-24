"""No-prompt backend diagnostics for the selected runtime environment."""

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import load_config
from .registry import BACKENDS

CLAUDE_SDK_VERSION = "0.2.180"
_VERSION = re.compile(
    r"(?<![A-Za-z0-9])\d{1,5}\.\d{1,5}(?:\.\d{1,5})?(?:[-+][A-Za-z0-9.]+)?"
)


def _safe_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _claude_dependency():
    try:
        version = importlib.metadata.version("claude-agent-sdk")
        installed = True
    except importlib.metadata.PackageNotFoundError:
        version = None
        installed = False
    # Only metadata version text, never arbitrary output from an installed CLI.
    version = version if version and _VERSION.fullmatch(version) else None
    return {
        "name": "claude-agent-sdk",
        "installed": installed,
        "version": version,
        "required_version": CLAUDE_SDK_VERSION,
        "compatible": version == CLAUDE_SDK_VERSION,
    }


def _bundled_claude():
    try:
        spec = importlib.util.find_spec("claude_agent_sdk")
        if spec and spec.origin:
            return str(
                Path(spec.origin).parent
                / "_bundled"
                / ("claude.exe" if os.name == "nt" else "claude")
            )
    except (ImportError, ValueError):
        pass
    return None


def inspect(config, backend):
    if backend not in BACKENDS:
        raise ValueError("Unknown backend")
    executables = config.get("executables", {})
    raw = executables.get(backend) if isinstance(executables, dict) else None
    configured = _safe_path(raw)
    fallback = {"codex": "codex", "antigravity": "agy", "kiro": "kiro-cli"}
    invalid = (
        not isinstance(executables, dict)
        or (raw is not None and configured is None)
        or (
            isinstance(executables, dict)
            and backend in ("codex", "antigravity")
            and backend in executables
            and raw is None
        )
    )
    command = (
        None
        if invalid
        else configured
        or (_bundled_claude() if backend == "claude" else fallback[backend])
    )
    resolved = _safe_path(shutil.which(command)) if command else None
    version = None
    if resolved:
        try:
            result = subprocess.run(
                [resolved, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0,
            )
            if result.returncode == 0:
                output = (result.stdout[:4096] + b" " + result.stderr[:4096]).decode(
                    "utf-8", errors="replace"
                )
                match = _VERSION.search(output)
                version = match.group(0) if match else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "backend": backend,
        "configured_executable": configured,
        "resolved_executable": resolved,
        "version": version,
        "dependency": _claude_dependency() if backend == "claude" else None,
        "gated_reason": BACKENDS[backend]["gated_reason"],
    }


def main():
    try:
        if len(sys.argv) != 3:
            raise ValueError("Invalid arguments")
        config = load_config(sys.argv[1])
        print(json.dumps(inspect(config, sys.argv[2])))
    except (OSError, ValueError, KeyError, TypeError):
        print('{"error":"Backend inspection failed"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
