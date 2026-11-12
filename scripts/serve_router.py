"""Run LiteLLM with optional routes and strict agent ingress."""

import argparse
import json
import os
import tempfile
from pathlib import Path

import uvicorn
import yaml
from router_policy import Ingress


def router_config(backends):
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config/router.yaml").read_text())
    limits = json.loads(os.environ.get("ROUTER_AGENT_LIMITS", "{}"))
    router_timeout = float(os.environ.get("ROUTER_TIMEOUT", "350"))
    aliases = {
        "codex": "codex-default",
        "antigravity": "antigravity",
        "claude": "claude-default",
        "kiro": "kiro-default",
    }
    config["model_list"] = [
        m
        for m in config["model_list"]
        if m["model_name"] in {aliases[s] for s in backends}
    ]
    for backend in ("claude", "kiro"):
        if backend in backends:
            alias = aliases[backend]
            config["model_list"].append(
                {
                    "model_name": alias,
                    "litellm_params": {
                        "model": "openai/" + alias,
                        "api_base": "os.environ/" + backend.upper() + "_API_BASE",
                        "api_key": "os.environ/" + backend.upper() + "_API_KEY",
                        "timeout": limits.get(alias, {}).get("timeout", 300) + 30,
                        "max_retries": 0,
                    },
                }
            )
    config["litellm_settings"]["callbacks"] = ["router_policy.policy"]
    config["litellm_settings"]["request_timeout"] = router_timeout
    config["router_settings"]["timeout"] = router_timeout
    config["general_settings"]["cancel_on_disconnect"] = True
    config["general_settings"]["forward_client_headers_to_llm_api"] = False
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4000)
    args = parser.parse_args()
    backends = json.loads(os.environ.get("ROUTER_BACKENDS", '["codex","antigravity"]'))
    with tempfile.TemporaryDirectory(prefix="litellm-router-") as directory:
        config_path = Path(directory) / "router.yaml"
        config_path.write_text(
            yaml.safe_dump(router_config(backends)), encoding="utf-8"
        )
        os.environ["CONFIG_FILE_PATH"] = str(config_path)
        from litellm.proxy.proxy_server import app

        uvicorn.run(
            Ingress(app),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            log_level="warning",
        )


if __name__ == "__main__":
    main()
