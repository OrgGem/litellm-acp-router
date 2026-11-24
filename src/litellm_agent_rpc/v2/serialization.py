"""One OpenAI representation for native results, without inferred usage."""

import copy
import time


def response(result):
    return {
        "id": "chatcmpl-" + result.request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.model,
        "choices": [
            {
                "index": 0,
                "message": copy.deepcopy(result.message),
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": result.usage,
    }


def chunks(result, include_usage=False):
    """Bounded, buffered-round stream. Native incremental delivery is a later gate.

    Call identity appears once; argument fragments concatenate without duplicates.
    Usage follows the public finish and precedes [DONE]. This serializer bypasses
    LiteLLM inference/estimation; its provider seam needs usage BEFORE its finish.
    """
    base = {
        "id": "chatcmpl-" + result.request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": result.model,
    }

    def chunk(delta, finish=None):
        return {
            **base,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    yield chunk({"role": "assistant"})
    for field in ("reasoning_content", "content"):
        if result.message.get(field):
            yield chunk({field: result.message[field]})
    for index, call in enumerate(result.message.get("tool_calls", [])):
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["function"]["name"], "arguments": ""},
                    }
                ]
            }
        )
        args = call["function"]["arguments"]
        for offset in range(0, len(args), 256):
            yield chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "function": {"arguments": args[offset : offset + 256]},
                        }
                    ]
                }
            )
    yield chunk({}, result.finish_reason)
    if include_usage:
        yield {**base, "choices": [], "usage": result.usage}
