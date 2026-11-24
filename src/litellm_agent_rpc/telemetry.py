"""Bounded gateway metadata, with usage captured at the adapter boundary.

The store never persists messages, responses, headers, credentials or exception
text. All counters are optional; an absent adapter counter is never a zero.
"""

import asyncio
import contextlib
import contextvars
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_trace = contextvars.ContextVar("agent_rpc_telemetry", default=None)
_store = None
_MAX_ROWS = 10000
_RETENTION_SECONDS = 30 * 86400
_LEASE_SECONDS = 45


@dataclass
class Trace:
    id: str
    start: float
    alias: str | None = None
    backend: str | None = None
    ttft_ms: float | None = None
    source: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


class Store:
    def __init__(self, path, environment):
        self.path = str(path)
        self.environment = environment
        self.degraded = False
        self.lock = threading.Lock()
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(self.connect()) as db, db:
                db.execute("""CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, environment TEXT NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL, lease_until REAL,
                    status TEXT NOT NULL, alias TEXT, backend TEXT,
                    latency_ms REAL, ttft_ms REAL, source TEXT,
                    input_tokens INTEGER, output_tokens INTEGER,
                    cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                    reasoning_tokens INTEGER
                )""")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS requests_started ON requests(started_at)"
                )
                self._prune(db, time.time())
        except (OSError, sqlite3.Error):
            self.degraded = True

    def connect(self):
        return sqlite3.connect(self.path, timeout=0.15)

    def write(self, sql, params):
        try:
            with contextlib.closing(self.connect()) as db, db:
                db.execute(sql, params)
        except (OSError, sqlite3.Error):
            with self.lock:
                self.degraded = True

    def start(self, trace):
        now = time.time()
        self.write(
            "INSERT INTO requests (id, environment, started_at, lease_until, status) "
            "VALUES (?, ?, ?, ?, 'active')",
            (trace.id, self.environment, now, now + _LEASE_SECONDS),
        )

    def refresh(self, trace):
        self.write(
            "UPDATE requests SET lease_until=? WHERE id=? AND status='active'",
            (time.time() + _LEASE_SECONDS, trace.id),
        )

    @staticmethod
    def _prune(db, now):
        db.execute(
            "DELETE FROM requests WHERE started_at < ?", (now - _RETENTION_SECONDS,)
        )
        db.execute(
            "DELETE FROM requests WHERE id IN (SELECT id FROM requests "
            "WHERE status != 'active' ORDER BY started_at DESC LIMIT -1 OFFSET ?)",
            (_MAX_ROWS,),
        )

    def prune(self):
        try:
            with contextlib.closing(self.connect()) as db, db:
                self._prune(db, time.time())
        except (OSError, sqlite3.Error):
            with self.lock:
                self.degraded = True

    def finish(self, trace, status):
        now = time.time()
        latency = max(0.0, (time.monotonic() - trace.start) * 1000)
        usage = (
            trace.source,
            trace.input_tokens,
            trace.output_tokens,
            trace.cache_read_tokens,
            trace.cache_write_tokens,
            trace.reasoning_tokens,
        )
        if status != "success":
            usage = (None,) * 6
        try:
            with contextlib.closing(self.connect()) as db, db:
                db.execute(
                    "UPDATE requests SET finished_at=?, lease_until=NULL, status=?, "
                    "alias=?, backend=?, latency_ms=?, ttft_ms=?, source=?, "
                    "input_tokens=?, output_tokens=?, cache_read_tokens=?, "
                    "cache_write_tokens=?, reasoning_tokens=? WHERE id=?",
                    (
                        now,
                        status,
                        trace.alias,
                        trace.backend,
                        latency,
                        trace.ttft_ms,
                        *usage,
                        trace.id,
                    ),
                )
                self._prune(db, now)
        except (OSError, sqlite3.Error):
            with self.lock:
                self.degraded = True


def configure(path, environment="local"):
    global _store
    # Docker labels include both project and service, each up to 63 chars.
    if not isinstance(environment, str) or not environment or len(environment) > 134:
        raise ValueError("Invalid telemetry environment")
    _store = Store(path, environment) if path else None


def selected(alias, backend):
    trace = _trace.get()
    if trace is not None:
        trace.alias, trace.backend = alias, backend


def first_text(text):
    trace = _trace.get()
    if trace is not None and trace.ttft_ms is None and isinstance(text, str) and text:
        trace.ttft_ms = max(0.0, (time.monotonic() - trace.start) * 1000)


def capture_usage(backend, usage):
    trace = _trace.get()
    if (
        trace is None
        or backend not in ("codex", "antigravity", "claude")
        or usage is None
    ):
        return
    if not isinstance(usage, dict):
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump(exclude_none=True)
        else:
            return
    inputs, outputs = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if type(inputs) is not int or inputs < 0 or type(outputs) is not int or outputs < 0:
        return
    trace.source = {
        "codex": "codex_rpc",
        "antigravity": "antigravity_acp",
        "claude": "claude_sdk",
    }[backend]
    trace.input_tokens, trace.output_tokens = inputs, outputs
    trace.cache_read_tokens = trace.cache_write_tokens = trace.reasoning_tokens = None
    prompt = usage.get("prompt_tokens_details") or {}
    completion = usage.get("completion_tokens_details") or {}
    if not isinstance(prompt, dict) or not isinstance(completion, dict):
        return
    for attr, value, maximum in (
        ("cache_read_tokens", prompt.get("cached_tokens"), inputs),
        ("cache_write_tokens", prompt.get("cache_write_tokens"), inputs),
        ("reasoning_tokens", completion.get("reasoning_tokens"), outputs),
    ):
        if type(value) is int and 0 <= value <= maximum:
            setattr(trace, attr, value)


def _empty():
    return {
        "available": False,
        "degraded": False,
        "environment": None,
        "requests": 0,
        "success": 0,
        "error": 0,
        "cancelled": 0,
        "active": 0,
        "avg_latency_ms": None,
        "avg_ttft_ms": None,
        "usage": {
            "source": "unavailable",
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
            "detail_reported_requests": {},
            "reported_requests": 0,
            "by_source": {},
        },
    }


def summary(path):
    result = _empty()
    if not path or not Path(path).is_file():
        return result
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.15)) as db:
            rows = db.execute(
                "SELECT environment, status, lease_until, latency_ms, ttft_ms, "
                "source, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, reasoning_tokens FROM requests WHERE started_at >= ?",
                (time.time() - _RETENTION_SECONDS,),
            ).fetchall()
        result["available"] = True
        environments = {row[0] for row in rows}
        result["environment"] = (
            next(iter(environments)) if len(environments) == 1 else None
        )
        result["requests"] = len(rows)
        latency, ttft = [], []
        sources = {}
        detail_values = {
            name: []
            for name in ("cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
        }
        for _, status, lease, elapsed, first, source, inputs, outputs, *details in rows:
            if status == "active":
                status = (
                    "active" if lease is not None and lease >= time.time() else "error"
                )
            if status in ("success", "error", "cancelled", "active"):
                result[status] += 1
            if elapsed is not None:
                latency.append(elapsed)
            if first is not None:
                ttft.append(first)
            if source and type(inputs) is int and type(outputs) is int:
                item = sources.setdefault(
                    source, {"input_tokens": 0, "output_tokens": 0, "requests": 0}
                )
                item["input_tokens"] += inputs
                item["output_tokens"] += outputs
                item["requests"] += 1
                for name, value in zip(detail_values, details):
                    if type(value) is int:
                        detail_values[name].append(value)
        result["avg_latency_ms"] = sum(latency) / len(latency) if latency else None
        result["avg_ttft_ms"] = sum(ttft) / len(ttft) if ttft else None
        if sources:
            result["usage"] = {
                "source": "backend",
                "input_tokens": sum(v["input_tokens"] for v in sources.values()),
                "output_tokens": sum(v["output_tokens"] for v in sources.values()),
                "reported_requests": sum(v["requests"] for v in sources.values()),
                "by_source": sources,
                **{
                    name: sum(values) if values else None
                    for name, values in detail_values.items()
                },
                "detail_reported_requests": {
                    name: len(values) for name, values in detail_values.items()
                },
            }
    except (OSError, sqlite3.Error, ValueError):
        result["degraded"] = True
    return result


def current_summary():
    if _store is None:
        return _empty()
    result = summary(_store.path)
    result["environment"] = _store.environment
    result["degraded"] |= _store.degraded
    return result


class TelemetryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        store = _store
        if store is not None and scope["type"] == "lifespan":

            async def janitor():
                while True:
                    await asyncio.sleep(3600)
                    await asyncio.to_thread(store.prune)

            task = asyncio.create_task(janitor())
            try:
                return await self.app(scope, receive, send)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if store is None or scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        trace = Trace(uuid.uuid4().hex, time.monotonic())
        token = _trace.set(trace)
        status_code, disconnected, completed, stream_error = 500, False, False, False
        pending = bytearray()

        async def observed_receive():
            nonlocal disconnected
            event = await receive()
            disconnected |= event["type"] == "http.disconnect"
            return event

        async def observed_send(message):
            nonlocal status_code, completed, stream_error, disconnected
            if message["type"] == "http.response.start":
                status_code = message["status"]
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                # Only detect an SSE error; do not retain response content.
                pending.extend(chunk)
                while b"\n\n" in pending:
                    frame, _, rest = pending.partition(b"\n\n")
                    pending[:] = rest
                    if frame.startswith(b"data:"):
                        with contextlib.suppress(ValueError, UnicodeError):
                            value = json.loads(frame[5:])
                            stream_error |= isinstance(value, dict) and "error" in value
                if len(pending) > 65536:
                    pending.clear()
                completed = not message.get("more_body", False)
            try:
                await send(message)
            except OSError:
                disconnected = True
                raise

        async def heartbeat():
            while True:
                await asyncio.sleep(10)
                await asyncio.to_thread(store.refresh, trace)

        # The thread continues after its awaiter is cancelled. Keep its task
        # alive and serialize completion after insertion, including when the
        # request is cancelled before the app receives it.
        startup = asyncio.create_task(asyncio.to_thread(store.start, trace))
        pulse = None
        try:
            try:
                await asyncio.shield(startup)
            except Exception:
                with store.lock:
                    store.degraded = True
            pulse = asyncio.create_task(heartbeat())
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            disconnected = True
            raise
        except BaseException:
            stream_error = True
            raise
        finally:
            if pulse is not None:
                pulse.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pulse
            status = (
                "cancelled"
                if disconnected
                else "error"
                if status_code >= 400 or stream_error or not completed
                else "success"
            )

            async def finish_after_start():
                try:
                    await startup
                except Exception:
                    with store.lock:
                        store.degraded = True
                try:
                    await asyncio.to_thread(store.finish, trace, status)
                except Exception:
                    with store.lock:
                        store.degraded = True

            finish_task = asyncio.create_task(finish_after_start())
            try:
                await asyncio.shield(finish_task)
            finally:
                _trace.reset(token)
