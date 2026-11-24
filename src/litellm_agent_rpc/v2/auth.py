"""Trusted proxy request context and host-owned identity adapters."""

import asyncio
import contextlib
import contextvars
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS
from litellm.proxy._types import UserAPIKeyAuth

from .errors import ProtocolError
from .identity import Authority


class ProxyAuthority(Authority):
    """Enforce registry freshness at every authority check, including resume."""

    def __init__(self):
        super().__init__()
        self.fresh_until = float("inf")
        self.expirations = {}

    def check(self, principal, alias):
        if time.monotonic() >= self.fresh_until:
            self.available = False
        expiry = self.expirations.get(principal.scope_id)
        if expiry is not None and time.time() >= expiry:
            raise ProtocolError("key_revoked", 401)
        return super().check(principal, alias)


class PrismaKeySource:
    """Fresh, bounded writer lookups of keys already authenticated by LiteLLM.

    ACLs involving other DB entities and mutable budget/rate policies are not
    interpreted here; those keys are deliberately excluded until their full
    effective policy can be refreshed atomically.
    """

    _empty = (
        "user_id",
        "team_id",
        "agent_id",
        "project_id",
        "organization_id",
        "object_permission_id",
        "budget_id",
        "access_group_ids",
        "policies",
        "aliases",
        "config",
        "router_settings",
        "permissions",
        "metadata",
        "allowed_cache_controls",
        "model_max_budget",
        "budget_fallbacks",
        "budget_limits",
        "max_parallel_requests",
        "tpm_limit",
        "rpm_limit",
        "tpd_limit",
        "max_budget",
        "budget_duration",
        "budget_reset_at",
    )
    _json_fields = frozenset(
        {
            "aliases",
            "config",
            "router_settings",
            "permissions",
            "metadata",
            "model_max_budget",
            "budget_fallbacks",
            "budget_limits",
        }
    )

    def __init__(self, aliases):
        self.aliases = frozenset(aliases)
        self.tracked = {}
        self.queried = set()
        self.secret = secrets.token_bytes(32)

    def track(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ProtocolError("verified_key_required", 401)
        if digest in self.tracked:
            self.tracked[digest] = time.monotonic()
            return False
        if len(self.tracked) >= 1024:
            raise ProtocolError("auth_capacity_exhausted", 503)
        self.tracked[digest] = time.monotonic()
        return True

    def idle(self, digest):
        return time.monotonic() - self.tracked[digest] >= 120

    def forget(self, digest):
        self.tracked.pop(digest, None)

    @staticmethod
    def _timestamp(value):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise ValueError("invalid key date")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        timestamp = value.timestamp()
        if not math.isfinite(timestamp):
            raise ValueError("invalid key date")
        return timestamp

    async def __call__(self):
        # The proxy's cached/read-replica paths are insufficient for revocation.
        from litellm.proxy import proxy_server

        client = proxy_server.prisma_client
        if client is None:
            raise ValueError("prisma unavailable")
        self.queried = set(self.tracked)
        if not self.queried:
            # An empty IN filter can be satisfied without reaching PostgreSQL;
            # probe the writer so outage never masquerades as a fresh registry.
            await client.writer_db.query_raw("SELECT 1")
            return {}
        rows = await client.writer_db.litellm_verificationtoken.find_many(
            where={"token": {"in": sorted(self.queried)}}
        )
        if not isinstance(rows, list):
            raise ValueError("invalid registry rows")
        output = {}
        missing = object()
        for row in rows:
            get = (
                row.get
                if isinstance(row, dict)
                else lambda name, default=None: getattr(row, name, default)
            )
            digest = get("token", missing)
            if digest not in self.queried or digest in output:
                raise ValueError("unexpected registry row")
            if get("blocked", missing) not in (None, False):
                continue
            if get("soft_budget_cooldown", missing) is not False:
                continue

            def is_empty(name):
                value = get(name, missing)
                if name in self._json_fields and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (TypeError, ValueError):
                        return False
                return value in (None, {}, [], ())

            if any(not is_empty(name) for name in self._empty):
                continue
            key_type = get("key_type", missing)
            allowed_routes = get("allowed_routes", missing)
            if (
                key_type not in (None, "default", "llm_api")
                or not isinstance(allowed_routes, list)
                or any(not isinstance(route, str) for route in allowed_routes)
                or (
                    allowed_routes
                    and not set(allowed_routes)
                    & {"llm_api_routes", "/chat/completions", "/v1/chat/completions"}
                )
            ):
                continue
            models = get("models", missing)
            if (
                not isinstance(models, list)
                or not models
                or any(
                    not isinstance(model, str) or model not in self.aliases
                    for model in models
                )
            ):
                continue
            expires = self._timestamp(get("expires", missing))
            if expires is not None and expires <= time.time():
                continue
            created = self._timestamp(get("created_at", missing))
            modified = self._timestamp(get("settings_updated_at", missing))
            rotation = get("rotation_count", missing)
            if rotation is not None and (type(rotation) is not int or rotation < 0):
                raise ValueError("invalid key rotation")
            fields = [
                sorted(set(models)),
                sorted(set(allowed_routes)),
                key_type,
                expires,
                created,
                modified,
                rotation,
            ]
            revision = hashlib.sha256(
                json.dumps(fields, separators=(",", ":")).encode()
            ).hexdigest()
            scope = hmac.digest(self.secret, digest.encode(), "sha256").hex()
            output[digest] = {
                "scope_id": scope,
                "revision": revision,
                "grants": sorted(set(models)),
                "max_sessions": 4,
                "expires_at": expires,
            }
        return output


@dataclass
class RequestContext:
    host: object
    payload: dict
    conversation_id: str | None
    principal: object = None


current_request = contextvars.ContextVar("agent_rpc_v2_request", default=None)


class ProxyAuth:
    """Map only LiteLLM-authenticated credentials to a host principal.

    A registry is a trusted Python dependency, never request/config metadata.
    Its source returns a snapshot indexed by LiteLLM's hashed virtual key. Every
    snapshot entry must contain scope_id, revision, grants and max_sessions.
    """

    def __init__(
        self, authority, aliases, *, registry_source=None, has_active_scope=None
    ):
        self.authority = authority
        self.owner = authority.register("local-owner", aliases)
        self.source = registry_source
        self.has_active_scope = has_active_scope or (lambda scope: False)
        self.records = {}
        self.refreshed_at = 0.0
        self.refresh_lock = asyncio.Lock()
        self.init_lock = asyncio.Lock()
        self.watcher = None
        self.closed = False
        if self.source is not None:
            self.authority.fresh_until = 0.0
            self.authority.available = False

    async def _refresh(self):
        async with self.refresh_lock:
            try:
                started_at = time.monotonic()
                entries = await asyncio.wait_for(self.source(), timeout=3)
                if not isinstance(entries, dict):
                    raise ValueError("invalid registry")
                normalized = {}
                scopes = set()
                for digest, info in entries.items():
                    if (
                        not isinstance(digest, str)
                        or not re.fullmatch(r"[a-f0-9]{64}", digest)
                        or not isinstance(info, dict)
                        or set(info)
                        - {
                            "scope_id",
                            "revision",
                            "grants",
                            "max_sessions",
                            "expires_at",
                        }
                        or not {"scope_id", "revision", "grants", "max_sessions"}
                        <= set(info)
                        or not isinstance(info["scope_id"], str)
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", info["scope_id"])
                        or info["scope_id"] == "local-owner"
                        or info["scope_id"] in scopes
                        or not isinstance(info["revision"], str)
                        or not info["revision"]
                        or not isinstance(info["grants"], (list, tuple, set, frozenset))
                        or any(not isinstance(g, str) for g in info["grants"])
                        or type(info["max_sessions"]) is not int
                        or not 1 <= info["max_sessions"] <= 1024
                        or (
                            info.get("expires_at") is not None
                            and (
                                type(info["expires_at"]) not in (int, float)
                                or not math.isfinite(info["expires_at"])
                            )
                        )
                    ):
                        raise ValueError("invalid registry entry")
                    scopes.add(info["scope_id"])
                    normalized[digest] = (
                        info["scope_id"],
                        info["revision"],
                        frozenset(info["grants"]),
                        info["max_sessions"],
                        info.get("expires_at"),
                    )
                if hasattr(self.source, "idle") and hasattr(self.source, "forget"):
                    for digest in list(normalized):
                        old = self.records.get(digest)
                        if (
                            old is not None
                            and self.source.idle(digest)
                            and not self.has_active_scope(old[1].scope_id)
                        ):
                            self.source.forget(digest)
                            normalized.pop(digest)
                    for digest in self.source.queried - set(normalized):
                        self.source.forget(digest)
                for digest, (old, principal) in list(self.records.items()):
                    if normalized.get(digest) != old:
                        self.authority.revoke(principal.scope_id)
                        self.authority.expirations.pop(principal.scope_id, None)
                        del self.records[digest]
                for digest, item in normalized.items():
                    if digest not in self.records:
                        scope_id, _, grants, limit, expiry = item
                        self.records[digest] = (
                            item,
                            self.authority.register(
                                scope_id, grants, max_sessions=limit
                            ),
                        )
                        self.authority.expirations[scope_id] = expiry
                # A source snapshot can predate its response, so bound freshness
                # from the beginning of the read rather than the end.
                self.refreshed_at = started_at
                self.authority.fresh_until = self.refreshed_at + 15
                self.authority.available = True
            except asyncio.CancelledError:
                raise
            except Exception:
                self.authority.available = False
                raise ProtocolError("auth_unavailable", 503) from None

    async def _watch(self):
        try:
            while not self.closed:
                await asyncio.sleep(5)
                with contextlib.suppress(ProtocolError):
                    await self._refresh()
        except asyncio.CancelledError:
            pass

    async def authenticate(self, auth):
        if self.closed:
            raise ProtocolError("auth_unavailable", 503)
        if (
            not isinstance(auth, UserAPIKeyAuth)
            or not auth.via_virtual_key
            or not isinstance(auth.api_key, str)
        ):
            raise ProtocolError("verified_key_required", 401)
        if self.source is not None:
            async with self.init_lock:
                if self.closed:
                    raise ProtocolError("auth_unavailable", 503)
                added = False
                if auth.api_key != LITELLM_PROXY_MASTER_KEY_ALIAS and hasattr(
                    self.source, "track"
                ):
                    try:
                        added = self.source.track(auth.api_key)
                    except ProtocolError as error:
                        if error.code != "auth_capacity_exhausted":
                            raise
                        await self._refresh()
                        added = self.source.track(auth.api_key)
                if self.watcher is None:
                    await self._refresh()
                    self.watcher = asyncio.create_task(self._watch())
                elif added:
                    await self._refresh()
            if time.monotonic() - self.refreshed_at >= 15:
                self.authority.available = False
                raise ProtocolError("auth_unavailable", 503)
            if not self.authority.available:
                raise ProtocolError("auth_unavailable", 503)
        if auth.api_key == LITELLM_PROXY_MASTER_KEY_ALIAS:
            return self.owner
        if self.source is None:
            raise ProtocolError("owner_key_required", 403)
        entry = self.records.get(auth.api_key)
        if entry is None:
            raise ProtocolError("key_revoked", 401)
        return entry[1]

    async def close(self):
        async with self.init_lock:
            self.closed = True
            if self.watcher is not None:
                self.watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.watcher
