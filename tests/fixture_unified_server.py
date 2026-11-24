"""Controlled backend boundary for real LiteLLM HTTP integration tests."""

import asyncio
import json
import sys
from pathlib import Path

from test_unified import FakeProvider

from litellm_agent_rpc.backends import Handle
from litellm_agent_rpc.server import run

directory = Path(sys.argv[1])


def record(name, event):
    with (directory / (name + ".events")).open("a", encoding="utf-8") as file:
        file.write(json.dumps(event) + "\n")


class Backend(FakeProvider):
    def __init__(self, name):
        super().__init__()
        self.name = name

    async def acompletion(self, model, messages, **kwargs):
        record(
            self.name,
            {
                "event": "call",
                "cid": (kwargs.get("optional_params") or {}).get("conversation_id"),
            },
        )
        if messages[-1]["content"] == "wait":
            try:
                await asyncio.sleep(120)
            finally:
                record(self.name, {"event": "cancelled"})
        return self.completion(model, messages, **kwargs)

    async def astreaming(self, model, messages, **kwargs):
        record(self.name, {"event": "stream"})
        try:
            yield {
                "text": self.name,
                "is_finished": False,
                "finish_reason": "",
                "index": 0,
                "usage": None,
            }
            if messages[-1]["content"] == "wait":
                await asyncio.sleep(120)
            yield {
                "text": "",
                "is_finished": True,
                "finish_reason": "stop",
                "index": 0,
                "usage": None,
            }
        finally:
            record(self.name, {"event": "stream_closed"})


def factory(name, config):
    record(name, {"event": "create"})
    provider = Backend(name)
    return Handle(provider, provider.close)


run(json.loads((directory / "config.json").read_text()), factory=factory)
