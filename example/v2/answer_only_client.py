"""Example client-owned tool loop. Requires a verified answer-only route."""

import json
import os
import urllib.request
from uuid import uuid4


def main():
    api_key = os.environ.get("ROUTER_API_KEY")
    if not api_key:
        raise SystemExit("Set ROUTER_API_KEY in the environment first.")
    endpoint = os.environ.get(
        "ROUTER_CHAT_URL", "http://127.0.0.1:4000/v1/chat/completions"
    )
    alias = os.environ.get("ROUTER_MODEL", "coding-answer-only")
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "X-Conversation-ID": "example-" + uuid4().hex,
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Return the supplied text.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "maxLength": 256}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    messages = [{"role": "user", "content": "Use echo to repeat hello, then reply."}]
    for _ in range(16):
        body = json.dumps(
            {"model": alias, "messages": messages, "tools": tools}
        ).encode()
        request = urllib.request.Request(endpoint, body, headers, method="POST")
        with urllib.request.urlopen(request, timeout=350) as response:
            choice = json.load(response)["choices"][0]
        message = choice["message"]
        if choice["finish_reason"] != "tool_calls":
            print(message.get("content") or "")
            return
        calls = message.get("tool_calls", [])
        if len(calls) != 1 or calls[0].get("type") != "function":
            raise RuntimeError("Unexpected tool request")
        function = calls[0].get("function", {})
        arguments = json.loads(function.get("arguments", ""))
        if (
            function.get("name") != "echo"
            or not isinstance(arguments, dict)
            or set(arguments) != {"text"}
            or not isinstance(arguments["text"], str)
            or len(arguments["text"]) > 256
        ):
            raise RuntimeError("Tool request did not pass client policy")
        messages.extend(
            [
                message,
                {
                    "role": "tool",
                    "tool_call_id": calls[0]["id"],
                    "content": arguments["text"],
                },
            ]
        )
    raise RuntimeError("Client tool loop reached its handoff limit")


if __name__ == "__main__":
    main()
