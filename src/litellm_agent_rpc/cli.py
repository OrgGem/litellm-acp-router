import argparse
import asyncio
import json
import os
from pathlib import Path

from .config import load_config, route_specs, routes, v2_config


async def inspect_capabilities(config):
    """Read driver manifests only; do not open sessions or change CLI auth."""
    from .v2.answer_contract import verify_live_binding
    from .v2.capabilities import check_manifest
    from .v2.errors import ProtocolError
    from .v2.registry import create_driver, inspect_binding

    specs, profiles = route_specs(config), v2_config(config)["routes"]
    result = {}
    for alias, backend in routes(config).items():
        profile = {
            **profiles.get(
                alias,
                {
                    "profile_id": "inspection",
                    "profile_revision": "unverified",
                    "transport": "stdio-acp" if backend == "antigravity" else "stdio",
                },
            ),
            "models": {alias: specs[alias]["model"]},
        }
        if profile.get("execution_mode") == "answer_only":
            profile["executable"] = config.get("executables", {}).get(backend)
        try:
            manifest = await inspect_binding(backend, profile, specs[alias]["model"])
            supports = check_manifest(
                manifest, answer_only=profile.get("execution_mode") == "answer_only"
            )
            ready = (
                manifest["backend"] == backend
                and manifest["profile_id"] == profile["profile_id"]
                and manifest["transport"] == profile["transport"]
                and manifest["model"] == specs[alias]["model"]
                and supports("text")
                and supports("profile_isolation")
            )
            if ready and profile.get("execution_mode") == "answer_only":
                await verify_live_binding(
                    create_driver(backend, "answer_only"),
                    profile,
                    specs[alias]["model"],
                    manifest,
                )
            result[alias] = {
                "ready": ready,
                "manifest": manifest,
                "execution_mode": profile.get("execution_mode", "native"),
                "tool_proposal_source": "structured_output"
                if profile.get("execution_mode") == "answer_only"
                else "native",
            }
        except ProtocolError as error:
            result[alias] = {"ready": False, "error": error.code}
    return result


def main():
    parser = argparse.ArgumentParser(
        description="One LiteLLM gateway for all local agent CLI adapters"
    )
    parser.add_argument("action", choices=("serve", "doctor", "tui", "capabilities"))
    parser.add_argument("--config", default=".local/config.json")
    parser.add_argument("--port", type=int)
    parser.add_argument("--mode", choices=("local", "docker"), default="local")
    parser.add_argument(
        "--compose", type=Path, default=Path("example/docker/compose.yaml")
    )
    parser.add_argument("--project")
    parser.add_argument("--service", default="litellm")
    parser.add_argument(
        "--host",
        choices=("127.0.0.1", "0.0.0.0"),
        default="127.0.0.1",
        help="Bind address; use 0.0.0.0 inside a container with a localhost port mapping",
    )
    args = parser.parse_args()
    try:
        if args.action == "tui":
            try:
                from .tui import launch
            except ModuleNotFoundError as error:
                if error.name == "textual" or (error.name or "").startswith("textual."):
                    print("Install the TUI extra: uv sync --frozen --extra tui")
                    return 1
                raise
            return launch(
                args.config,
                mode=args.mode,
                compose_path=args.compose,
                project=args.project,
                service=args.service,
            )
        config = load_config(args.config)
        if args.port is not None and not 1024 <= args.port <= 65535:
            raise ValueError("Invalid port")
        if args.action == "serve":
            from .server import run

            previous = os.environ.get("AGENT_RPC_CONFIG")
            set_config_path = not os.environ.get("AGENT_RPC_TELEMETRY_PATH")
            if set_config_path:
                os.environ["AGENT_RPC_CONFIG"] = str(
                    Path(args.config).expanduser().resolve()
                )
            try:
                run(config, args.port, host=args.host)
            finally:
                if set_config_path:
                    if previous is None:
                        os.environ.pop("AGENT_RPC_CONFIG", None)
                    else:
                        os.environ["AGENT_RPC_CONFIG"] = previous
            return 0
        if args.action == "capabilities":
            for alias, capability in asyncio.run(inspect_capabilities(config)).items():
                print(json.dumps({"alias": alias, **capability}))
            return 0
        from .provider import AgentRPC

        provider = AgentRPC(config)
        failed = False
        try:
            for backend in dict.fromkeys(routes(config).values()):
                try:
                    provider._get(backend)
                    print(
                        json.dumps(
                            {
                                "backend": backend,
                                "preflight": "passed",
                                "model_access_verified": False,
                            }
                        )
                    )
                except Exception as error:
                    failed = True
                    print(
                        json.dumps(
                            {
                                "backend": backend,
                                "preflight": "failed",
                                "reason": str(error),
                            }
                        )
                    )
        finally:
            provider.close()
        return int(failed)
    except (ValueError, OSError, KeyError, RuntimeError):
        print('{"error":"Invalid configuration or startup failure"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
