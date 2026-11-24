"""Owned SDK delivery boundary. Bare CustomLLM cannot acknowledge handoffs."""

import asyncio
import contextlib
import threading

from .serialization import chunks, response


class AsyncClient:
    """Internal explicit host client; principal is registered by trusted Python code.

    API-key authentication and driver readiness are not bypassed here. Streamed
    responses currently buffer one bounded native round before yielding chunks.
    """

    def __init__(self, broker, principal):
        self.broker, self.principal = broker, principal

    async def completion(self, payload, conversation_id=None):
        result = await self.broker.invoke(self.principal, payload, conversation_id)
        try:
            body = response(result)
            await self.broker.acknowledge(result)
            return body
        except BaseException:
            await self.broker.abort_delivery(result)
            raise

    async def stream(self, payload, conversation_id=None):
        result = await self.broker.invoke(self.principal, payload, conversation_id)
        acknowledged = False
        try:
            output = list(
                chunks(
                    result,
                    (payload.get("stream_options") or {}).get("include_usage", False),
                )
            )
            for index, chunk in enumerate(output):
                if index == len(output) - 1:
                    # Returning the final SDK chunk is the delivery boundary;
                    # consumers may close immediately without requesting EOF.
                    await self.broker.acknowledge(result)
                    acknowledged = True
                yield chunk
        finally:
            if not acknowledged:
                await self.broker.abort_delivery(result)


class Client:
    """Sync facade owning one loop for the entire broker lifetime.

    factory runs on the owner loop and returns (broker, registered_principal).
    Never pass an already active broker from another loop.
    """

    def __init__(self, factory):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

        async def create():
            broker, principal = factory()
            return AsyncClient(broker, principal)

        try:
            self.client = self._run(create())
        except BaseException:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            self.loop.close()
            raise
        self.closed = False

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def completion(self, payload, conversation_id=None):
        return self._run(self.client.completion(payload, conversation_id))

    def stream(self, payload, conversation_id=None):
        iterator = self.client.stream(payload, conversation_id)
        try:
            while True:
                try:
                    yield self._run(anext(iterator))
                except StopAsyncIteration:
                    return
        finally:
            self._run(iterator.aclose())

    def close(self):
        if not self.closed:
            # Keep the owner loop available for retry if native cleanup is
            # quarantined; stopping it would orphan the remaining owner tasks.
            self._run(self.client.broker.close())
            self.closed = True
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            with contextlib.suppress(RuntimeError):
                self.loop.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
