"""Local deployment commands; stdlib only, no CLI login or model calls at setup."""

import argparse
import json
import math
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from litellm_agent_rpc.config import route_specs, routes  # noqa: E402

LOCAL = ROOT / ".local" / "config.json"
PROJECTS = {
    "router": ROOT,
    "codex": ROOT / "litellm-codex-rpc",
    "antigravity": ROOT / "litellm-antigravity-rpc",
    "claude": ROOT / "litellm-claude-rpc",
    "kiro": ROOT / "litellm-kiro-rpc",
}
ALIASES = {
    "codex": "codex-default",
    "antigravity": "antigravity",
    "claude": "claude-default",
    "kiro": "kiro-default",
}


def enabled_services(config):
    return [
        "router",
        *(
            s
            for s in ALIASES
            if config.get("enabled", {}).get(s, s in ("codex", "antigravity"))
        ),
    ]


def migrate(path):
    original = path.read_bytes()
    config = json.loads(original)
    if config.get("schema_version") == 2:
        return
    if config.get("schema_version", 1) != 1:
        raise ValueError("Unsupported config schema")
    config["schema_version"] = 2
    config["enabled"] = {s: s in ("codex", "antigravity") for s in ALIASES}
    for service, port in (("claude", 4103), ("kiro", 4104)):
        # Existing ports and credentials remain byte-for-byte values.
        while port in config["ports"].values():
            port += 1
        config["ports"][service] = port
        config["keys"][service] = "sk-" + secrets.token_urlsafe(32)
        config["executables"][service] = None if service == "claude" else "kiro-cli"
        config[service + "_models"] = {ALIASES[service]: None}
    validate_config(config)
    backup = path.with_suffix(path.suffix + ".v1.bak")
    # Exclusive backup, atomic replacement. A previous failed migration's backup
    # must match before proceeding; never overwrite a different credential file.
    if backup.exists():
        if backup.read_bytes() != original:
            raise ValueError("Migration backup mismatch")
    else:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(original)
            file.flush()
            os.fsync(file.fileno())
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix="config-migration-",
        delete=False,
    ) as file:
        temp = Path(file.name)
        json.dump(config, file, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def python_for(service):
    suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return PROJECTS[service] / ".venv" / suffix


def initialize():
    if LOCAL.exists():
        print("Keeping existing .local/config.json")
        return
    config = json.loads((ROOT / "config/local.example.json").read_text("utf-8"))
    config["keys"] = {
        service: "sk-" + secrets.token_urlsafe(32) for service in PROJECTS
    }
    LOCAL.parent.mkdir(mode=0o700, exist_ok=True)
    # Exclusive creation never overwrites a previously provisioned credential.
    descriptor = os.open(LOCAL, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print("Created .local/config.json (local service keys; excluded from Git).")


def read_config(path=LOCAL):
    migrate(path)
    config = json.loads(path.read_text("utf-8"))
    return validate_config(config)


def validate_config(config):
    if set(config["enabled"]) != set(ALIASES) or any(
        type(v) is not bool for v in config["enabled"].values()
    ):
        raise ValueError("Invalid enabled backends")
    ports = config["ports"]
    if set(ports) != set(PROJECTS) or any(
        type(port) is not int or not 1024 <= port <= 65535 for port in ports.values()
    ):
        raise ValueError("Ports must be integers from 1024 to 65535 for all services")
    if len(set(ports.values())) != len(ports):
        raise ValueError("Each service needs a distinct port")
    keys = config["keys"]
    if set(keys) != set(PROJECTS) or any(
        not isinstance(key, str)
        or not key.startswith("sk-")
        or len(key) < 24
        or not key.isascii()
        or any(char.isspace() for char in key)
        for key in keys.values()
    ):
        raise ValueError(
            "Each service needs its own ASCII sk- API key (24+ characters)"
        )
    if len(set(keys.values())) != len(keys):
        raise ValueError("Do not reuse keys between services")
    if "routes" not in config and set(config["antigravity_models"]) != {"antigravity"}:
        raise ValueError("The router profile requires the 'antigravity' alias")
    route_specs(config)
    for service in ("claude", "kiro"):
        models = config[service + "_models"]
        if "routes" not in config and (
            set(models) != {ALIASES[service]}
            or any(
                v is not None and (not isinstance(v, str) or not v)
                for v in models.values()
            )
        ):
            raise ValueError("Invalid agent model mapping")
        limits = config.get(service + "_limits", {})
        durations = {"timeout", "cleanup_timeout", "idle_ttl"}
        counts = {
            "concurrency",
            "max_sessions",
            "max_pending",
            "queue_size",
            "max_input_bytes",
            "max_output_bytes",
        }
        if not isinstance(limits, dict) or set(limits) - durations - counts:
            raise ValueError("Invalid agent limits")
        for key, value in limits.items():
            if (
                type(value) not in ((int, float) if key in durations else (int,))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("Invalid agent limit")
    if (
        type(config.get("router_timeout", 350)) not in (int, float)
        or not math.isfinite(config.get("router_timeout", 350))
        or config.get("router_timeout", 350) <= 0
    ):
        raise ValueError("Invalid router timeout")
    return config


def environment(config, service):
    """Legacy standalone-host environment, retained for regression fixtures."""
    env = os.environ.copy()
    # Do not send local API requests through a user's outbound HTTP proxy.
    bypass = env.get("NO_PROXY", env.get("no_proxy", ""))
    env["NO_PROXY"] = env["no_proxy"] = bypass + ",127.0.0.1,localhost"
    env.update(
        PYTHONUTF8="1",
        LITELLM_TELEMETRY="False",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        LITELLM_MODE="PRODUCTION",
        LITELLM_MASTER_KEY=config["keys"][service],
    )
    if service == "router":
        env.update(
            CODEX_API_BASE=f"http://127.0.0.1:{config['ports']['codex']}/v1",
            CODEX_API_KEY=config["keys"]["codex"],
            ANTIGRAVITY_API_BASE=f"http://127.0.0.1:{config['ports']['antigravity']}/v1",
            ANTIGRAVITY_RPC_API_KEY=config["keys"]["antigravity"],
        )
        for backend in ("claude", "kiro"):
            env[backend.upper() + "_API_BASE"] = (
                f"http://127.0.0.1:{config['ports'][backend]}/v1"
            )
            env[backend.upper() + "_API_KEY"] = config["keys"][backend]
        env["ROUTER_BACKENDS"] = json.dumps(
            [s for s in enabled_services(config) if s != "router"]
        )
        env["ROUTER_TIMEOUT"] = str(config.get("router_timeout", 350))
        env["ROUTER_AGENT_LIMITS"] = json.dumps(
            {ALIASES[s]: config.get(s + "_limits", {}) for s in ("claude", "kiro")}
        )
    elif service == "codex":
        env.update(
            CODEX_MODEL_MAP=json.dumps({"codex-default": config["codex_model"]}),
            CODEX_RPC_ALLOW_PROMPT_FLATTENING="false",
            CODEX_RPC_REQUEST_TIMEOUT="300",
            CODEX_RPC_MAX_CONCURRENT_TURNS="2",
        )
    elif service == "antigravity":
        env["ANTIGRAVITY_RPC_API_KEY"] = config["keys"]["antigravity"]
    else:
        env[service.upper() + "_MODEL_MAP"] = json.dumps(config[service + "_models"])
        env[service.upper() + "_RPC_LIMITS"] = json.dumps(
            config.get(service + "_limits", {})
        )
    return env


def unified_environment(config):
    env = os.environ.copy()
    bypass = env.get("NO_PROXY", env.get("no_proxy", ""))
    env["NO_PROXY"] = env["no_proxy"] = bypass + ",127.0.0.1,localhost"
    env.update(
        PYTHONUTF8="1",
        LITELLM_TELEMETRY="False",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        LITELLM_MODE="PRODUCTION",
        LITELLM_MASTER_KEY=config["keys"]["router"],
    )
    return env


def command(config, service):
    python = str(python_for(service))
    port = str(config["ports"][service])
    if service == "router":
        return [
            python,
            "-m",
            "litellm_agent_rpc",
            "serve",
            "--config",
            str(LOCAL),
            "--port",
            port,
        ]
    if service in ("claude", "kiro"):
        cmd = [python, "-m", f"litellm_{service}_rpc", "serve", "--port", port]
        if config["executables"].get(service):
            cmd += ["--cli-bin", config["executables"][service]]
        return cmd
    if service == "codex":
        return [
            python,
            "-m",
            "serve_codex_rpc",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            port,
            "--codex-bin",
            config["executables"]["codex"],
        ]
    return [
        python,
        "-m",
        "litellm_antigravity_rpc",
        "serve",
        "--port",
        port,
        "--agy-bin",
        config["executables"]["antigravity"],
        "--models",
        str(LOCAL.parent / "antigravity.models.json"),
        "--timeout",
        "120",
        "--concurrency",
        "2",
    ]


def setup(dev=False):
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("Install uv first")
    initialize()
    config = read_config(LOCAL)
    for service in ALIASES:
        if not (PROJECTS[service] / "src").is_dir():
            raise ValueError(f"Missing adapter source: {PROJECTS[service].name}")
    args = [uv, "sync", "--frozen", "--project", str(ROOT)]
    if config["enabled"].get("claude"):
        args += ["--extra", "claude"]
    if not dev:
        args += ["--no-dev"]
    subprocess.run(args, cwd=ROOT, check=True)


def get_models(config, service):
    request = urllib.request.Request(
        f"http://127.0.0.1:{config['ports'][service]}/v1/models",
        headers={"Authorization": "Bearer " + config["keys"][service]},
    )
    # Explicitly disable proxy discovery for loopback status requests.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        request, timeout=5
    ) as response:
        return {model["id"] for model in json.load(response)["data"]}


def check(config):
    installed = python_for("router").is_file()
    print(f"router: environment {'OK' if installed else 'MISSING'}")
    failed = not installed
    try:
        aliases = get_models(config, "router")
        expected = set(routes(config))
        if expected != aliases:
            raise ValueError("Unexpected model aliases")
        print("router: authenticated model list OK")
    except (OSError, ValueError, KeyError):
        print("router: endpoint unavailable or model/key mismatch")
        failed = True
    print("No prompt sent. Model entitlement and quota are not verified.")
    return int(failed)


def serve(config, service):
    if service not in enabled_services(config):
        raise ValueError("Backend disabled; enable explicitly in local config")
    if service != "router":
        raise ValueError("Use serve router: one LiteLLM process owns all backends")
    if not python_for(service).is_file():
        raise ValueError("Environment missing; run setup first")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", config["ports"][service]))
    print(f"Starting {service} on 127.0.0.1:{config['ports'][service]}", flush=True)
    # Foreground child shares the console, so Ctrl+C reaches the API host too.
    child = subprocess.Popen(
        command(config, service), cwd=ROOT, env=unified_environment(config)
    )
    try:
        return child.wait()
    except KeyboardInterrupt:
        try:
            return child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=10)
            return 130


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser(
        "init", help="Create local config and gateway key (preserving legacy fields)"
    )
    setup_parser = commands.add_parser(
        "setup", help="Install one locked environment with all adapter sources"
    )
    setup_parser.add_argument("--dev", action="store_true")
    commands.add_parser("check", help="Check local services without calling any model")
    serve_parser = commands.add_parser(
        "serve", help="Run one service in the foreground"
    )
    serve_parser.add_argument("service", choices=PROJECTS)
    args = parser.parse_args()
    try:
        if args.action == "init":
            initialize()
            return 0
        if args.action == "setup":
            setup(args.dev)
            return 0
        config = read_config()
        if args.action == "check":
            return check(config)
        return serve(config, args.service)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # Do not print configuration values (they contain service keys).
        print(
            f"Workspace command failed ({type(error).__name__}). Check config, paths and service logs.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
