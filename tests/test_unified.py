import asyncio
from types import SimpleNamespace

import pytest
from litellm import ModelResponse
from litellm.llms.custom_llm import CustomLLMError

from litellm_agent_rpc.backends import Handle
from litellm_agent_rpc.config import ALIASES, routes
from litellm_agent_rpc.provider import AgentRPC
from litellm_agent_rpc.server import proxy_config


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.cancelled = False

    def completion(self, model, messages, **kwargs):
        self.calls.append((model, messages, kwargs))
        return ModelResponse(
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": model},
                    "finish_reason": "stop",
                }
            ],
        )

    async def acompletion(self, *args, **kwargs):
        return self.completion(*args, **kwargs)

    def streaming(self, model, messages, **kwargs):
        self.calls.append((model, messages, kwargs))
        try:
            yield {
                "text": model,
                "is_finished": False,
                "finish_reason": "",
                "index": 0,
                "usage": None,
            }
            yield {
                "text": "",
                "is_finished": True,
                "finish_reason": "stop",
                "index": 0,
                "usage": None,
            }
        finally:
            self.cancelled = True

    async def astreaming(self, *args, **kwargs):
        try:
            for chunk in self.streaming(*args, **kwargs):
                yield chunk
                await asyncio.sleep(0)
        finally:
            self.cancelled = True

    def close(self):
        self.closed = True


@pytest.fixture
def unified():
    providers = {}

    def factory(name, config):
        providers[name] = FakeProvider()
        return Handle(providers[name], providers[name].close)

    provider = AgentRPC({"enabled": dict.fromkeys(ALIASES, True)}, factory)
    yield provider, providers
    provider.close()


@pytest.mark.parametrize("backend", ALIASES)
@pytest.mark.parametrize(
    "mode", ["completion", "acompletion", "streaming", "astreaming"]
)
async def test_all_four_surfaces_share_one_dispatcher(unified, backend, mode):
    provider, handles = unified
    args = {
        "model": ALIASES[backend],
        "messages": [{"role": "user", "content": "hello"}],
    }
    result = getattr(provider, mode)(**args)
    if mode == "acompletion":
        result = await result
    elif mode == "astreaming":
        result = [chunk async for chunk in result]
    elif mode == "streaming":
        result = list(result)
    assert set(handles) == {backend}
    assert handles[backend].calls[0][0] == ALIASES[backend]
    assert result


def test_disabled_backend_never_constructed(unified):
    provider, handles = unified
    provider.config["enabled"]["kiro"] = False
    with pytest.raises(CustomLLMError):
        provider.completion("kiro-default", [])
    assert not handles


async def test_stream_close_releases_adapter(unified):
    provider, handles = unified
    stream = provider.astreaming("claude-default", [{"role": "user", "content": "x"}])
    await anext(stream)
    await stream.aclose()
    assert handles["claude"].cancelled
    provider.close()
    assert handles["claude"].closed


def test_conversation_id_and_usage_policy(unified):
    provider, handles = unified
    logging = SimpleNamespace(stream_options={"include_usage": True})
    list(
        provider.streaming(
            "claude-default",
            [],
            headers={"X-Conversation-ID": "stable"},
            logging_obj=logging,
        )
    )
    assert (
        handles["claude"].calls[0][2]["optional_params"]["conversation_id"] == "stable"
    )
    assert logging.stream_options == {"include_usage": False}


def test_single_custom_provider_no_http_backend_urls():
    config = proxy_config({"enabled": dict.fromkeys(ALIASES, True)})
    assert len(config["litellm_settings"]["custom_provider_map"]) == 1
    assert {m["litellm_params"]["model"] for m in config["model_list"]} == {
        "agent_rpc/" + alias for alias in ALIASES.values()
    }
    assert all(
        "api_base" not in m["litellm_params"] and "api_key" not in m["litellm_params"]
        for m in config["model_list"]
    )


def test_only_explicitly_selected_backend_is_exposed():
    assert routes({"enabled": {"claude": True}}) == {"claude-default": "claude"}


@pytest.mark.parametrize("params", [[], "invalid", 42])
def test_malformed_parameters_rejected_before_backend_start(unified, params):
    provider, handles = unified
    with pytest.raises(CustomLLMError, match="Invalid parameters"):
        provider.completion("codex-default", [], optional_params=params)
    assert not handles


async def test_concurrent_requests_initialize_backend_once():
    calls = []
    made = FakeProvider()

    def factory(name, config):
        calls.append(name)
        return Handle(made, made.close)

    provider = AgentRPC({"enabled": {"codex": True}}, factory)
    try:
        await asyncio.gather(
            *(provider.acompletion("codex-default", []) for _ in range(8))
        )
        assert calls == ["codex"]
    finally:
        provider.close()


def test_factory_failure_does_not_poison_other_backend():
    made = FakeProvider()

    def factory(name, config):
        if name == "kiro":
            raise RuntimeError("sensitive details must not escape")
        return Handle(made, made.close)

    provider = AgentRPC({"enabled": dict.fromkeys(ALIASES, True)}, factory)
    try:
        with pytest.raises(CustomLLMError, match="backend_not_ready"):
            provider.completion("kiro-default", [])
        assert (
            provider.completion("codex-default", []).choices[0].message.content
            == "codex-default"
        )
    finally:
        provider.close()
