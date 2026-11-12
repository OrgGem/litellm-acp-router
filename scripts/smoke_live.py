"""Explicit model acceptance test through the shared router (consumes CLI quota)."""

import argparse
import json

import httpx
from workspace import ALIASES, read_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--model", choices=list(ALIASES.values()), required=True)
    args = parser.parse_args()
    config = read_config()
    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": "Reply with OK only."}],
    }
    with httpx.Client(
        base_url=f"http://127.0.0.1:{config['ports']['router']}",
        headers={"Authorization": "Bearer " + config["keys"]["router"]},
        timeout=360,
        trust_env=False,
    ) as client:
        result = client.post("/v1/chat/completions", json=body)
        result.raise_for_status()
        if not result.json()["choices"][0]["message"].get("content"):
            raise ValueError("Missing assistant text")
        terminal, text, done = False, False, False
        with client.stream(
            "POST", "/v1/chat/completions", json={**body, "stream": True}
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    continue
                chunk = json.loads(data)
                if "error" in chunk:
                    raise ValueError("Backend stream returned an error")
                for choice in chunk.get("choices", []):
                    terminal |= choice.get("finish_reason") is not None
                    text |= bool(choice.get("delta", {}).get("content"))
        if not (text and terminal and done):
            raise ValueError("Incomplete stream")
    print(f"{args.model}: completion and SSE passed (2 real model calls).")


if __name__ == "__main__":
    main()
