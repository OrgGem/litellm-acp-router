"""OpenAI client for either deployment. No model call unless --prompt is supplied."""

import argparse
import json
from pathlib import Path

from openai import OpenAI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("example/.local/config.json")
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:4000/v1")
    parser.add_argument("--model", default="codex-default")
    parser.add_argument("--prompt")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--conversation-id")
    args = parser.parse_args()
    config = json.loads(args.config.read_text("utf-8"))
    with OpenAI(
        base_url=args.base_url,
        api_key=config["keys"]["router"],
        max_retries=0,
        timeout=360,
    ) as client:
        if args.prompt is None:
            print("\n".join(model.id for model in client.models.list().data))
            return
        options = {}
        if args.conversation_id:
            options["extra_headers"] = {"X-Conversation-ID": args.conversation_id}
        response = client.chat.completions.create(
            model=args.model,
            messages=[{"role": "user", "content": args.prompt}],
            stream=args.stream,
            **options,
        )
        if args.stream:
            try:
                for chunk in response:
                    for choice in chunk.choices:
                        print(choice.delta.content or "", end="", flush=True)
                print()
            finally:
                response.close()
        else:
            print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
