"""Embed the provider directly in LiteLLM Python; one real model call per run."""

import argparse

import litellm

from litellm_agent_rpc.config import load_config
from litellm_agent_rpc.provider import AgentRPC


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="example/.local/config.json")
    parser.add_argument("--model", default="codex-default")
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()
    provider = AgentRPC(load_config(args.config))
    previous = litellm.custom_provider_map
    litellm.custom_provider_map = [
        *previous,
        {"provider": "agent_rpc", "custom_handler": provider},
    ]
    try:
        response = litellm.completion(
            model="agent_rpc/" + args.model,
            messages=[{"role": "user", "content": args.prompt}],
            max_retries=0,
        )
        # Do not interpret LiteLLM's potentially estimated usage as CLI counters.
        print(response.choices[0].message.content)
    finally:
        provider.close()
        litellm.custom_provider_map = previous


if __name__ == "__main__":
    main()
