"""Pinned LiteLLM interface probe; every backend is synthetic and in-process."""

import json

import litellm
import pytest
from litellm import Choices, CustomLLM, Message, ModelResponse, ModelResponseStream
from litellm.types.utils import Delta, StreamingChoices, Usage


class Probe(CustomLLM):
    def __init__(self):
        self.calls = []

    def completion(self, model, messages, **kwargs):
        self.calls.append(kwargs.get("optional_params"))
        return ModelResponse(
            model=model,
            choices=[
                Choices(
                    index=0,
                    finish_reason="tool_calls",
                    message=Message(
                        role="assistant",
                        content=None,
                        reasoning_content="public summary",
                        tool_calls=[
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "echo",
                                    "arguments": '{"text":"héllo"}',
                                },
                            }
                        ],
                    ),
                )
            ],
            usage=Usage(prompt_tokens=10, completion_tokens=3, total_tokens=13),
        )

    async def acompletion(self, model, messages, **kwargs):
        return self.completion(model, messages, **kwargs)

    def streaming(self, model, messages, **kwargs):
        self.calls.append(kwargs.get("optional_params"))
        for delta in [
            {"role": "assistant", "reasoning_content": "public summary"},
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"text":'},
                    }
                ]
            },
            {"tool_calls": [{"index": 0, "function": {"arguments": '"héllo"}'}}]},
        ]:
            yield ModelResponseStream(
                model=model, choices=[StreamingChoices(index=0, delta=Delta(**delta))]
            )
        # Native counters precede the finish event; never rely on synthesized usage.
        yield ModelResponseStream(
            model=model,
            choices=[],
            usage=Usage(prompt_tokens=10, completion_tokens=3, total_tokens=13),
        )
        yield ModelResponseStream(
            model=model,
            choices=[
                StreamingChoices(index=0, delta=Delta(), finish_reason="tool_calls")
            ],
        )

    async def astreaming(self, model, messages, **kwargs):
        for item in self.streaming(model, messages, **kwargs):
            yield item


@pytest.fixture
def probe(monkeypatch):
    handler = Probe()
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setattr(
        litellm,
        "custom_provider_map",
        [{"provider": "g0_probe", "custom_handler": handler}],
    )
    return handler


def options():
    return dict(
        model="g0_probe/test",
        messages=[{"role": "user", "content": "synthetic"}],
        max_retries=0,
        caching=False,
        cache={"no-cache": True, "no-store": True},
    )


def check_complete(result):
    assert result.choices[0].finish_reason == "tool_calls"
    assert result.choices[0].message.reasoning_content == "public summary"
    assert result.usage.prompt_tokens == 10
    assert json.loads(result.choices[0].message.tool_calls[0].function.arguments) == {
        "text": "héllo"
    }


def check_stream(chunks):
    reasoning, arguments, finishes, usages = [], [], [], []
    for chunk in chunks:
        if getattr(chunk, "usage", None):
            usages.append(chunk.usage)
        for choice in chunk.choices:
            if choice.finish_reason:
                finishes.append(choice.finish_reason)
            if getattr(choice.delta, "reasoning_content", None):
                reasoning.append(choice.delta.reasoning_content)
            for call in choice.delta.tool_calls or []:
                arguments.append(call.function.arguments or "")
    assert "".join(reasoning) == "public summary"
    assert json.loads("".join(arguments)) == {"text": "héllo"}
    assert finishes == ["tool_calls"]
    assert usages[-1].prompt_tokens == 10
    assert usages[-1].completion_tokens == 3


def test_sync_custom_provider_seam(probe):
    check_complete(litellm.completion(**options()))
    check_stream(
        list(
            litellm.completion(
                **options(), stream=True, stream_options={"include_usage": True}
            )
        )
    )


async def test_async_custom_provider_seam(probe):
    check_complete(await litellm.acompletion(**options()))
    stream = await litellm.acompletion(
        **options(), stream=True, stream_options={"include_usage": True}
    )
    check_stream([chunk async for chunk in stream])


@pytest.mark.parametrize("stream", [False, True])
async def test_cache_flags_prevent_reads_and_writes_sync_and_async(
    probe, monkeypatch, stream
):
    from litellm.caching.caching import Cache

    class Spy(Cache):
        def __init__(self):
            super().__init__(type="local")
            self.reads = self.writes = 0

        def get_cache(self, *args, **kwargs):
            self.reads += 1
            return super().get_cache(*args, **kwargs)

        async def async_get_cache(self, *args, **kwargs):
            self.reads += 1
            return await super().async_get_cache(*args, **kwargs)

        def add_cache(self, *args, **kwargs):
            self.writes += 1
            return super().add_cache(*args, **kwargs)

        async def async_add_cache(self, *args, **kwargs):
            self.writes += 1
            return await super().async_add_cache(*args, **kwargs)

        async def async_add_cache_pipeline(self, *args, **kwargs):
            self.writes += 1
            return await super().async_add_cache_pipeline(*args, **kwargs)

    cache = Spy()
    monkeypatch.setattr(litellm, "cache", cache)
    for _ in range(2):
        sync = litellm.completion(**options(), stream=stream)
        if stream:
            list(sync)
        asynchronous = await litellm.acompletion(**options(), stream=stream)
        if stream:
            [chunk async for chunk in asynchronous]
    assert len(probe.calls) == 4
    assert cache.reads == cache.writes == 0


def test_reasoning_effort_requires_trusted_supported_parameter_declaration(probe):
    with pytest.raises(litellm.UnsupportedParamsError):
        litellm.completion(**options(), reasoning_effort="low")
    assert not probe.calls
    result = litellm.completion(
        **options(), reasoning_effort="low", allowed_openai_params=["reasoning_effort"]
    )
    check_complete(result)
    assert probe.calls[-1]["reasoning_effort"] == "low"
