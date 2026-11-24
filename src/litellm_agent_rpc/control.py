"""Per-instance management, enabled only for an explicitly managed gateway."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import time

PREFIX = "/_agent-rpc/control/"


def config_digest(config):
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ManagedControl:
    def __init__(
        self,
        app,
        config,
        token=None,
        instance_id=None,
        stop=None,
        usage=None,
        v2_host=None,
    ):
        self.app = app
        self.token = token
        self.instance_id = instance_id
        self.stop = stop
        self.usage = usage
        self.v2_host = v2_host
        self.config_digest = config_digest(config)
        self.started = time.monotonic()
        self.draining = False
        self.stopping = False
        self.active = 0
        self._host_drained = False
        self._transition = asyncio.Lock()

    def status(self):
        usage = None
        if self.usage is not None:
            try:
                usage = self.usage()
            except Exception:
                usage = {"available": False, "degraded": True}
        return {
            "state": "draining" if self.draining else "running",
            "active_requests": self.active,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "uptime_seconds": time.monotonic() - self.started,
            "config_digest": self.config_digest,
            "usage": usage,
        }

    async def status_snapshot(self):
        result = self.status()
        if self.v2_host is not None:
            try:
                details = await self.v2_host.status()
                if not isinstance(details, dict):
                    raise ValueError
                result["v2"] = {**details, "available": True}
            except Exception:
                result["v2"] = {"available": False, "error": "v2_status_unavailable"}
        return result

    @staticmethod
    async def _cancel_id(receive):
        body = bytearray()
        try:
            async with asyncio.timeout(2):
                while True:
                    part = await receive()
                    if part.get("type") != "http.request":
                        raise ValueError
                    body.extend(part.get("body", b""))
                    if len(body) > 4096:
                        raise ValueError
                    if not part.get("more_body", False):
                        break
            value = json.loads(body)
            if (
                not isinstance(value, dict)
                or set(value) != {"id"}
                or not isinstance(value["id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value["id"])
            ):
                raise ValueError
            return value["id"]
        except (TimeoutError, ValueError, TypeError, UnicodeError):
            return None

    @staticmethod
    async def reply(send, code, payload):
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith(PREFIX):
            if not self.token or not self.instance_id:
                return await self.reply(send, 404, {"error": "management_disabled"})
            values = [
                v for k, v in scope.get("headers", []) if k.lower() == b"authorization"
            ]
            expected = ("Bearer " + self.token).encode()
            if len(values) != 1 or not hmac.compare_digest(values[0], expected):
                return await self.reply(send, 401, {"error": "unauthorized"})
            action = path.removeprefix(PREFIX)
            method = scope.get("method")
            if action == "status" and method == "GET":
                return await self.reply(send, 200, await self.status_snapshot())
            if method != "POST" or action not in {"drain", "resume", "stop", "cancel"}:
                return await self.reply(send, 404, {"error": "unknown_action"})
            if self.stopping:
                return await self.reply(send, 409, {"error": "service_stopping"})
            if action == "cancel":
                if self.v2_host is None:
                    return await self.reply(send, 404, {"error": "v2_unavailable"})
                pending_id = await self._cancel_id(receive)
                if pending_id is None:
                    return await self.reply(send, 400, {"error": "invalid_pending_id"})
                try:
                    cancelled = await self.v2_host.cancel_pending(pending_id)
                except Exception:
                    return await self.reply(send, 503, {"error": "cancel_unavailable"})
                if not cancelled:
                    return await self.reply(send, 404, {"error": "pending_not_found"})
                return await self.reply(send, 200, await self.status_snapshot())
            async with self._transition:
                if self.stopping:
                    return await self.reply(send, 409, {"error": "service_stopping"})
                if action == "drain":
                    self.draining = True
                    self._host_drained = False
                    if self.v2_host is not None:
                        try:
                            await self.v2_host.drain()
                            self._host_drained = True
                        except Exception:
                            return await self.reply(
                                send, 503, {"error": "drain_unavailable"}
                            )
                elif action == "resume":
                    if self.v2_host is not None:
                        try:
                            await self.v2_host.resume()
                        except Exception:
                            self._host_drained = False
                            self.draining = True
                            return await self.reply(
                                send, 503, {"error": "resume_unavailable"}
                            )
                    self.draining = False
                    self._host_drained = False
                else:
                    if not self.draining or self.active:
                        return await self.reply(
                            send,
                            409,
                            {
                                "error": "service_not_drained",
                                **(await self.status_snapshot()),
                            },
                        )
                    # Fence all HTTP admission before awaiting the broker's
                    # atomic quiescence check and stop latch.
                    self.stopping = True
                    if self.v2_host is not None:
                        try:
                            accepted = await self.v2_host.try_stop(lambda: self.active)
                        except Exception:
                            # Unknown latch state: remain fenced, never claim stop.
                            return await self.reply(
                                send, 503, {"error": "stop_unavailable"}
                            )
                        if not accepted:
                            self.stopping = False
                            return await self.reply(
                                send,
                                409,
                                {
                                    "error": "service_not_drained",
                                    **(await self.status_snapshot()),
                                },
                            )
                    try:
                        await self.reply(
                            send,
                            200,
                            {"state": "stopping", "instance_id": self.instance_id},
                        )
                    finally:
                        if self.stop is not None:
                            asyncio.get_running_loop().call_soon(self.stop)
                    return
                return await self.reply(send, 200, await self.status_snapshot())

        # No await separates admission checking and incrementing. Everything is
        # owned by the ASGI loop; count uploads and complete streaming lifetimes.
        counted = scope.get("method") == "POST"
        if counted:
            if self.stopping or (
                self.draining
                and (
                    self.v2_host is None
                    or not self._host_drained
                    or path not in {"/chat/completions", "/v1/chat/completions"}
                )
            ):
                return await self.reply(
                    send,
                    503,
                    {
                        "error": {
                            "type": "service_draining",
                            "message": "Gateway is draining",
                        }
                    },
                )
            self.active += 1
        try:
            return await self.app(scope, receive, send)
        finally:
            if counted:
                self.active -= 1
