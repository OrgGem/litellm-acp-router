"""LiteLLM 1.109 CustomLLM seam with an observed HTTP delivery boundary."""

from litellm import ModelResponse, ModelResponseStream
from litellm.llms.custom_llm import CustomLLMError

from . import delivery
from .auth import current_request
from .errors import ProtocolError
from .serialization import chunks, response


def _context(host, alias):
    context = current_request.get()
    if (
        context is None
        or context.host is not host
        or context.principal is None
        or context.payload.get("model") != alias
        or delivery.current.get() is None
    ):
        raise CustomLLMError(400, "trusted_proxy_context_required")
    return context


async def _invoke(host, alias):
    context = _context(host, alias)
    try:
        result = await host.broker.invoke(
            context.principal, context.payload, context.conversation_id
        )
        try:
            delivery.attach(result)
        except BaseException:
            await host.broker.abort_delivery(result)
            raise
        return result
    except ProtocolError as error:
        raise CustomLLMError(error.status, error.code) from None


async def complete(host, alias):
    result = await _invoke(host, alias)
    return ModelResponse(**response(result))


async def stream(host, alias):
    result = await _invoke(host, alias)
    context = _context(host, alias)
    include_usage = (context.payload.get("stream_options") or {}).get(
        "include_usage", False
    )
    # LiteLLM consumes provider usage before the finishing chunk. The observer
    # replaces any public usage with the ledger's authoritative projection.
    pieces = list(chunks(result, include_usage))
    if include_usage and pieces[-1].get("choices") == []:
        pieces[-1], pieces[-2] = pieces[-2], pieces[-1]
    for piece in pieces:
        yield ModelResponseStream(**piece)
