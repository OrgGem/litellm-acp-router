"""The single CustomLLM registration, dispatching into local RPC adapters."""

import asyncio
import contextlib
import importlib
import os
import threading

from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError

from . import telemetry
from .backends import create_backend
from .config import load_config, routes, v2_route_specs
from .policy import responses_context


def _capture_response(name, response):
    # Only an explicit adapter value is evidence; a library default is not.
    if "usage" in getattr(response, "model_fields_set", set()):
        telemetry.capture_usage(name, getattr(response, "usage", None))
    return response


def _capture_chunk(name, chunk):
    if isinstance(chunk, dict):
        telemetry.first_text(chunk.get("text"))
        telemetry.capture_usage(name, chunk.get("usage"))
        if chunk.get("finish_reason") == "" and not chunk.get("is_finished"):
            chunk = {**chunk, "finish_reason": None}
    return chunk


@contextlib.contextmanager
def _bind_responses_context(name, alias, ctx=None):
    if ctx is None:
        ctx = responses_context.get()
    if ctx is None or name not in ("codex", "antigravity", "claude"):
        yield
        return
    try:
        module = importlib.import_module(f"litellm_{name}_rpc.responses_context")
    except ImportError:
        yield
        return
    token = module.responses_request_context.set(
        module.ResponsesRequestContext(
            alias=alias,
            instructions=ctx.instructions,
            phases=ctx.phases,
            roles=ctx.roles,
        )
    )
    try:
        yield
    finally:
        try:
            module.responses_request_context.reset(token)
        except ValueError:
            module.responses_request_context.set(None)


class AgentRPC(CustomLLM):
    def __init__(self, config=None, factory=create_backend):
        super().__init__()
        self.config, self.factory = config, factory
        self.handles = {}
        self.lock = threading.RLock()
        self.closed = False
        self.v2_host = None

    def configure(self, config, factory=None, v2_host=None):
        with self.lock:
            if self.handles:
                raise RuntimeError("Cannot reconfigure an active provider")
            self.config = config
            self.v2_host = v2_host
            self.closed = False
            if factory is not None:
                self.factory = factory

    def _v2_route(self, model):
        if not isinstance(model, str):
            return False
        with self.lock:
            if self.config is None:
                self.config = load_config(
                    os.environ.get("AGENT_RPC_CONFIG", ".local/config.json")
                )
            return model.removeprefix("agent_rpc/") in v2_route_specs(self.config)

    def _select(self, model, kwargs):
        if not isinstance(model, str):
            raise CustomLLMError(400, "Invalid model")
        alias = model.removeprefix("agent_rpc/")
        with self.lock:
            if self.closed:
                raise CustomLLMError(503, "Provider closed")
            if self.config is None:
                self.config = load_config(
                    os.environ.get("AGENT_RPC_CONFIG", ".local/config.json")
                )
            name = routes(self.config).get(alias)
        if name is None:
            raise CustomLLMError(400, "Unknown or disabled model")
        telemetry.selected(alias, name)
        raw_params = kwargs.get("optional_params")
        if raw_params is not None and not isinstance(raw_params, dict):
            raise CustomLLMError(400, "Invalid parameters")
        params = dict(raw_params or {})
        if set(params) - {"stream", "stream_options", "max_retries", "conversation_id"}:
            raise CustomLLMError(400, "Unsupported parameter")
        if (
            type(params.get("max_retries", 0)) is not int
            or params.get("max_retries", 0) != 0
        ):
            raise CustomLLMError(400, "Retries are disabled")
        headers = kwargs.get("headers") or {}
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) for k in headers
        ):
            raise CustomLLMError(400, "Invalid headers")
        cid = next(
            (v for k, v in headers.items() if k.lower() == "x-conversation-id"), None
        )
        if cid is not None:
            params["conversation_id"] = cid
        if name not in ("claude", "kiro"):
            if params.get("conversation_id") is not None:
                raise CustomLLMError(
                    400, "Session reuse is unavailable for this backend"
                )
            params.pop("conversation_id", None)
        options = params.get("stream_options")
        if options is not None and (
            not isinstance(options, dict)
            or set(options) - {"include_usage"}
            or (
                "include_usage" in options
                and type(options["include_usage"]) is not bool
            )
        ):
            raise CustomLLMError(400, "Invalid stream_options")
        forwarded = {**kwargs, "optional_params": params}
        # One policy across all backends: never expose LiteLLM estimates as usage.
        logging = kwargs.get("logging_obj")
        if logging is not None:
            logging.stream_options = {"include_usage": False}
        return name, alias, forwarded

    def _get(self, name):
        with self.lock:
            if self.closed:
                raise CustomLLMError(503, "Provider closed")
            if name not in self.handles:
                try:
                    self.handles[name] = self.factory(name, self.config)
                except Exception as error:
                    code = getattr(error, "code", None) or "backend_not_ready"
                    raise CustomLLMError(503, str(code)) from None
            return self.handles[name].provider

    def completion(self, model, messages, **kwargs):
        if self._v2_route(model):
            raise CustomLLMError(400, "trusted_async_proxy_required")
        name, alias, forwarded = self._select(model, kwargs)
        with _bind_responses_context(name, alias):
            return _capture_response(
                name, self._get(name).completion(alias, messages, **forwarded)
            )

    async def acompletion(self, model, messages, **kwargs):
        if self._v2_route(model):
            from .v2.provider_bridge import complete

            return await complete(self.v2_host, model.removeprefix("agent_rpc/"))
        name, alias, forwarded = self._select(model, kwargs)
        provider = await asyncio.to_thread(self._get, name)
        with _bind_responses_context(name, alias):
            return _capture_response(
                name, await provider.acompletion(alias, messages, **forwarded)
            )

    def streaming(self, model, messages, **kwargs):
        if self._v2_route(model):
            raise CustomLLMError(400, "trusted_async_proxy_required")
        name, alias, forwarded = self._select(model, kwargs)
        ctx = responses_context.get()

        def consume():
            provider = self._get(name)
            with _bind_responses_context(name, alias, ctx):
                raw_stream = provider.streaming(alias, messages, **forwarded)
            with contextlib.closing(raw_stream) as stream:
                while True:
                    try:
                        with _bind_responses_context(name, alias, ctx):
                            chunk = next(stream)
                    except StopIteration:
                        return
                    yield _capture_chunk(name, chunk)

        return consume()

    def astreaming(self, model, messages, **kwargs):
        if self._v2_route(model):
            from .v2.provider_bridge import stream

            return stream(self.v2_host, model.removeprefix("agent_rpc/"))
        name, alias, forwarded = self._select(model, kwargs)
        ctx = responses_context.get()

        async def consume():
            provider = await asyncio.to_thread(self._get, name)
            with _bind_responses_context(name, alias, ctx):
                raw_stream = provider.astreaming(alias, messages, **forwarded)
            async with contextlib.aclosing(raw_stream) as stream:
                while True:
                    try:
                        with _bind_responses_context(name, alias, ctx):
                            chunk = await anext(stream)
                    except StopAsyncIteration:
                        return
                    yield _capture_chunk(name, chunk)

        return consume()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            handles, self.handles = list(self.handles.values()), {}
        errors = []
        for handle in handles:
            try:
                handle.close()
            except Exception as error:
                errors.append(type(error).__name__)
        if errors:
            raise RuntimeError("Backend cleanup failed")


plugin = AgentRPC()
