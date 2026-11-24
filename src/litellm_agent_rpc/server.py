"""One LiteLLM proxy process, one CustomLLM provider, local CLI children."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from .config import flatten_routes, routes, v2_route_specs


def proxy_config(config):
    timeout = config.get("router_timeout", 350)
    v2_aliases = set(v2_route_specs(config))
    return {
        "model_list": [
            {
                "model_name": alias,
                "litellm_params": {
                    "model": "agent_rpc/" + alias,
                    "max_retries": 0,
                    "timeout": timeout,
                    **(
                        {"use_chat_completions_api": True}
                        if alias not in v2_aliases
                        else {}
                    ),
                },
            }
            for alias in routes(config)
        ],
        "litellm_settings": {
            "custom_provider_map": [
                {
                    "provider": "agent_rpc",
                    "custom_handler": "litellm_agent_rpc.provider.plugin",
                }
            ],
            "callbacks": ["litellm_agent_rpc.policy.policy"],
            "drop_params": False,
            "num_retries": 0,
            "request_timeout": timeout,
            "telemetry": False,
            "set_verbose": False,
            "turn_off_message_logging": True,
        },
        "router_settings": {"num_retries": 0, "timeout": timeout, "fallbacks": []},
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "background_health_checks": False,
            "cancel_on_disconnect": True,
            "forward_client_headers_to_llm_api": False,
            "disable_spend_logs": True,
            "disable_spend_updates": True,
        },
    }


def run(
    config,
    port=None,
    factory=None,
    host="127.0.0.1",
    *,
    _v2_factory=None,
    _proxy_fixture=None,
):
    # Backend CLI children must not inherit the management authority.
    control_token = os.environ.pop("AGENT_RPC_CONTROL_TOKEN", None)
    import uvicorn
    import yaml

    from .provider import plugin

    if not routes(config):
        raise ValueError("Enable at least one route before starting the gateway")

    v2_host = None
    if v2_route_specs(config):
        if _v2_factory is not None:
            # Explicit Python dependency injection is for isolated protocol tests.
            v2_host = _v2_factory(config)
        else:
            from .v2.host import ProxyV2Host

            v2_host = ProxyV2Host(config)
    plugin.configure(config, factory, v2_host)
    os.environ.update(
        LITELLM_MASTER_KEY=config["keys"]["router"],
        LITELLM_TELEMETRY="False",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        LITELLM_MODE="PRODUCTION",
        ROUTER_TIMEOUT=str(config.get("router_timeout", 350)),
        ROUTER_ALLOW_FLATTENING="1" if config.get("allow_prompt_flattening") else "0",
        ROUTER_AGENT_FLATTEN=json.dumps(flatten_routes(config)),
        ROUTER_AGENT_ROUTES=json.dumps(routes(config)),
        ROUTER_AGENT_LIMITS=json.dumps(
            {
                alias: config.get(backend + "_limits", {})
                for alias, backend in routes(config).items()
            }
        ),
    )
    from . import telemetry
    from .control import ManagedControl
    from .policy import Ingress, policy

    policy.configure(v2_host)

    telemetry.configure(
        os.environ.get("AGENT_RPC_TELEMETRY_PATH"),
        os.environ.get("AGENT_RPC_ENVIRONMENT", "local"),
    )

    with tempfile.TemporaryDirectory(prefix="agent-rpc-proxy-") as directory:
        path = Path(directory) / "proxy.yaml"
        path.write_text(yaml.safe_dump(proxy_config(config)), encoding="utf-8")
        os.environ["CONFIG_FILE_PATH"] = str(path)
        from litellm.proxy.proxy_server import app

        if _proxy_fixture is not None:
            _proxy_fixture(app)

        proxy_server = None

        def request_stop():
            proxy_server.should_exit = True

        inner = ManagedControl(
            telemetry.TelemetryMiddleware(Ingress(app, v2_host)),
            config,
            token=control_token,
            instance_id=os.environ.get("AGENT_RPC_INSTANCE_ID"),
            stop=request_stop,
            usage=telemetry.current_summary,
            v2_host=v2_host,
        )

        async def owned(scope, receive, send):
            if scope["type"] == "lifespan":
                try:
                    await inner(scope, receive, send)
                finally:
                    if v2_host is not None:
                        await v2_host.close()
                    await asyncio.to_thread(plugin.close)
            else:
                await inner(scope, receive, send)

        try:
            proxy_server = uvicorn.Server(
                uvicorn.Config(
                    owned,
                    host=host,
                    port=port or config.get("ports", {}).get("router", 4000),
                    access_log=False,
                    log_level="warning",
                    timeout_graceful_shutdown=15,
                    loop="asyncio",
                )
            )
            proxy_server.run()
        finally:
            plugin.close()
