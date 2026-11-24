"""One native owner across HTTP handoffs; client results never execute locally."""

import asyncio
import contextlib
import copy
import secrets
import time
from dataclasses import dataclass, field

from .answer_contract import check_binding as check_answer_binding
from .answer_contract import validate as validate_answer
from .answer_contract import verify_live_binding
from .answer_only import check_answer_features, decision, prompt
from .capabilities import check_features
from .contract import canonical, digest, parse, validate
from .errors import ProtocolError
from .usage import Ledger
from .validation import check_arguments, request


@dataclass
class Binding:
    driver: object
    config: dict
    model: str | None = None


@dataclass
class Record:
    locator: str
    principal: object
    alias: str
    binding: Binding
    manifest: dict
    fingerprint: str
    payload: dict
    policy: dict
    context: dict
    session: object = None
    turn: object = None
    state: str = "RUNNING"
    invocation: str | None = None
    sequence: int = -1
    history: list = field(default_factory=list)
    batch: dict | None = None
    expires: float = float("inf")
    ack: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_seconds: float = 0
    handoffs: int = 0
    permit: bool = True
    request_id: str = ""
    owner: object = None
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    native_refs: set = field(default_factory=set)
    usage_completeness: str = "unknown"
    answer_usage_complete: bool = True


@dataclass
class Result:
    message: dict
    finish_reason: str
    usage: dict | None
    request_id: str
    model: str
    locator: str
    handoff_id: str | None = None
    native_complete: bool = False


class Broker:
    def __init__(
        self,
        authority,
        bindings,
        *,
        ledger=None,
        max_active=2,
        max_sessions=4,
        native_seconds=300,
        chain_seconds=600,
        tool_wait_seconds=120,
        cleanup_seconds=5,
        max_handoffs=16,
        _fixture=False,
    ):
        self.authority, self.bindings = authority, dict(bindings)
        self.ledger = ledger or Ledger()
        self.max_active, self.max_sessions = max_active, max_sessions
        self.native_seconds, self.chain_seconds = native_seconds, chain_seconds
        self.tool_wait_seconds, self.cleanup_seconds = (
            tool_wait_seconds,
            cleanup_seconds,
        )
        self.max_handoffs, self._fixture = max_handoffs, _fixture
        self.records = {}
        self.active = 0
        self.cleaning = 0
        self.draining = False
        self.closed = False
        self._admission = asyncio.Lock()
        self._reaper = None
        self._reaper_cleanups = {}

    def counts(self):
        return {
            "native_running": sum(r.state == "RUNNING" for r in self.records.values()),
            "prepared": sum(r.state == "PREPARED" for r in self.records.values()),
            "awaiting_client": sum(
                r.state == "DELIVERED" for r in self.records.values()
            ),
            "cleaning": self.cleaning,
            "generation_permits": self.active,
            "quarantined": sum(
                r.state in {"FENCED", "QUARANTINED"} for r in self.records.values()
            ),
        }

    def status_snapshot(self):
        now = time.monotonic()
        return {
            "state": "stopping"
            if self.closed
            else "draining"
            if self.draining
            else "running",
            **self.counts(),
            "sessions": [
                {
                    "chain_id": r.context["chain_id"],
                    "model": r.alias,
                    "backend": r.manifest["backend"],
                    "state": r.state,
                    "key_scope": self.authority.scope_tag(r.principal),
                    "expires_in_seconds": max(
                        0,
                        round(
                            min(r.expires, r.context["limits"]["native_deadline"])
                            - now,
                            3,
                        ),
                    ),
                }
                for r in self.records.values()
            ],
        }

    async def set_draining(self, value):
        if type(value) is not bool:
            raise ProtocolError("invalid_drain_state")
        async with self._admission:
            if self.closed:
                raise ProtocolError("service_stopping", 409)
            self.draining = value

    async def try_stop(self, active_http=lambda: 0):
        """Atomically close admission; sample HTTP ownership inside the lock."""
        async with self._admission:
            if (
                self.closed
                or not self.draining
                or self.records
                or self.cleaning
                or active_http()
            ):
                return False
            self.closed = True
            return True

    async def cancel_pending(self, chain_id):
        if not isinstance(chain_id, str) or len(chain_id) > 128:
            raise ProtocolError("invalid_chain_id")
        record = next(
            (r for r in self.records.values() if r.context["chain_id"] == chain_id),
            None,
        )
        if record is None:
            return False
        await self._finish(record, cancel=True, reason="operator_cancelled")
        if record.state != "CLOSED":
            raise ProtocolError("native_cleanup_incomplete", 503)
        return True

    async def invoke(self, principal, payload, cid=None):
        # Snapshot client-owned Python structures before any asynchronous boundary.
        payload = copy.deepcopy(payload)
        alias = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(alias, str):
            raise ProtocolError("invalid_model")
        self.authority.check(principal, alias)
        locator = self.authority.locator(principal, cid or secrets.token_hex(16))
        record = self.records.get(locator)
        try:
            policy = request(payload, cid)
        except ProtocolError as error:
            if record is not None and record.state in {"PREPARED", "DELIVERED"}:
                raise ProtocolError("pending_request_conflict", 409) from error
            raise
        if record is not None:
            return await self._resume(record, principal, payload, policy)
        if alias not in self.bindings:
            raise ProtocolError("unknown_model")
        binding = self.bindings[alias]
        answer_only = binding.config.get("execution_mode") == "answer_only"
        if payload["messages"][-1]["role"] == "tool" or (
            not answer_only
            and any(
                m.get("tool_calls") or m["role"] == "tool" for m in payload["messages"]
            )
        ):
            raise ProtocolError("session_expired", 410)
        manifest = await binding.driver.capabilities(binding.config, binding.model)
        if answer_only:
            validate_answer("CapabilityManifest", manifest)
            check_answer_binding(
                manifest, binding.config, binding.model, fixture=self._fixture
            )
        if (
            manifest.get("model") != binding.model
            or manifest.get("profile_id") != binding.config["profile_id"]
            or manifest.get("transport") != binding.config["transport"]
        ):
            raise ProtocolError("capability_binding_mismatch", 503)
        checker = check_answer_features if answer_only else check_features
        checker(manifest, payload, policy, fixture=self._fixture)
        if answer_only:
            await verify_live_binding(
                binding.driver,
                binding.config,
                binding.model,
                manifest,
                fixture=self._fixture,
            )
        now = time.monotonic()
        context = {
            "key_scope_id": principal.scope_id,
            "key_epoch": principal.epoch,
            "environment": self.authority.environment,
            "request_id": "req_" + secrets.token_hex(16),
            "chain_id": "chain_" + secrets.token_hex(16),
            "locator": locator,
            "profile_id": binding.config["profile_id"],
            "profile_revision": binding.config["profile_revision"],
            "limits": {
                "inference_seconds": self.native_seconds,
                "native_deadline": now
                + (self.chain_seconds if answer_only else self.native_seconds),
                "chain_deadline": now + self.chain_seconds,
                "tool_wait_seconds": self.tool_wait_seconds,
                "cleanup_seconds": self.cleanup_seconds,
                "max_handoffs": self.max_handoffs,
            },
        }
        fingerprint = self._fingerprint(binding, payload)
        context["session_scope"] = self.authority.session_scope(locator, fingerprint)
        validate("ExecutionContext", context)
        async with self._admission:
            self.authority.check(principal, alias)
            if self.closed or self.draining:
                raise ProtocolError("service_draining", 503)
            if locator in self.records:
                raise ProtocolError("conversation_busy", 409)
            owned = sum(
                r.principal.scope_id == principal.scope_id
                for r in self.records.values()
            )
            if (
                self.active >= self.max_active
                or len(self.records) >= self.max_sessions
                or owned >= principal.max_sessions
            ):
                raise ProtocolError("capacity_exhausted", 429)
            self.active += 1
            record = Record(
                locator,
                principal,
                alias,
                binding,
                manifest,
                fingerprint,
                payload,
                policy,
                context,
                history=copy.deepcopy(payload["messages"]),
                request_id=context["request_id"],
            )
            self.records[locator] = record
            self.ledger.begin(
                context["chain_id"],
                key_scope=self.authority.scope_tag(principal),
                model=alias,
                backend=manifest["backend"],
                request_id=context["request_id"],
            )
            record.owner = asyncio.current_task()
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._reap())
        try:
            if answer_only:
                return await self._answer_round(record)
            async with asyncio.timeout(
                max(0, context["limits"]["native_deadline"] - time.monotonic())
            ):
                native_config = copy.deepcopy(binding.config)
                native_config.pop("execution_mode", None)
                record.session = await binding.driver.open_session(
                    context, native_config
                )
                if record.state != "RUNNING":
                    await binding.driver.close_session(
                        record.session, time.monotonic() + self.cleanup_seconds
                    )
                    raise ProtocolError("native_turn_fenced", 410)
                turn_input = {
                    "messages": payload["messages"],
                    "history_digest": digest(payload["messages"]),
                    "toolset_digest": digest(payload.get("tools", [])),
                    "model_alias": alias,
                    "resolved_model": binding.model,
                    "tools": payload.get("tools", []),
                    "round_policy": policy,
                    "reasoning_output": payload.get("reasoning_output", "off"),
                    "reasoning_effort": payload.get("reasoning_effort"),
                }
                validate("TurnInput", turn_input)
                record.turn = await binding.driver.begin_turn(
                    record.session, copy.deepcopy(turn_input)
                )
                if record.state != "RUNNING":
                    await binding.driver.cancel(
                        record.turn,
                        "native_turn_fenced",
                        time.monotonic() + self.cleanup_seconds,
                    )
                    raise ProtocolError("native_turn_fenced", 410)
            result = await self._collect(record)
            self._check_active_identity(record)
            return result
        except BaseException:
            await self._finish(record, cancel=True, reason="request_failed")
            raise

    @staticmethod
    def _fingerprint(binding, payload):
        return digest(
            [
                binding.config,
                binding.model,
                payload["model"],
                payload.get("tools", []),
                payload.get("reasoning_output", "off"),
                payload.get("reasoning_effort"),
                payload.get("max_completion_tokens"),
            ]
        )

    async def _answer_round(self, record):
        """Complete and close one isolated invocation before publishing output."""
        started = time.monotonic()
        limits = record.context["limits"]
        remaining = min(
            limits["chain_deadline"] - started,
            limits["inference_seconds"] - record.active_seconds,
        )
        output = []
        terminal = None
        invocation_id = None
        sequence = -1
        native_epochs = {}
        try:
            async with asyncio.timeout(max(0, remaining)):
                manifest = await record.binding.driver.capabilities(
                    record.binding.config, record.binding.model
                )
                validate_answer("CapabilityManifest", manifest)
                check_answer_binding(
                    manifest,
                    record.binding.config,
                    record.binding.model,
                    fixture=self._fixture,
                )
                for field in (
                    "backend",
                    "contract_hash",
                    "profile_revision",
                    "platform",
                    "executable_sha256",
                    "cli_version",
                    "sdk_version",
                ):
                    if manifest[field] != record.manifest[field]:
                        raise ProtocolError("answer_only_profile_changed", 503)
                check_answer_features(
                    manifest, record.payload, record.policy, fixture=self._fixture
                )
                await verify_live_binding(
                    record.binding.driver,
                    record.binding.config,
                    record.binding.model,
                    manifest,
                    fixture=self._fixture,
                )
                record.manifest = manifest
                self.authority.check(record.principal, record.alias)
                invocation_context = copy.deepcopy(record.context)
                invocation_context["limits"]["native_deadline"] = started + max(
                    0, remaining
                )
                record.turn = await record.binding.driver.start(
                    invocation_context,
                    {
                        "prompt": prompt(record.payload, record.policy),
                        "model": record.binding.model,
                        "config": copy.deepcopy(record.binding.config),
                    },
                )
                if record.state != "RUNNING":
                    raise ProtocolError("native_turn_fenced", 410)
                iterator = record.binding.driver.events(record.turn)
                try:
                    size = 0
                    async for event in iterator:
                        validate_answer("AdapterEvent", event)
                        if terminal is not None or record.state != "RUNNING":
                            raise ProtocolError("invalid_answer_only_sequence", 502)
                        self._check_active_identity(record)
                        if invocation_id is None:
                            invocation_id = event["invocation_id"]
                        if (
                            event["invocation_id"] != invocation_id
                            or event["sequence"] <= sequence
                        ):
                            raise ProtocolError("invalid_answer_only_sequence", 502)
                        sequence = event["sequence"]
                        size += len(canonical(event))
                        if size > 4 * 1024 * 1024:
                            raise ProtocolError("native_output_too_large", 502)
                        kind, value = event["type"], event["payload"]
                        if kind == "text_delta":
                            output.append(value["text"])
                        elif kind == "usage":
                            if value["invocation_id"] != invocation_id:
                                raise ProtocolError("invalid_usage_invocation", 502)
                            # Native IDs can repeat between CLI processes. Namespace
                            # them per HTTP round and attach observations to the
                            # host-owned chain instead of creating orphan records.
                            namespaced = dict(value)
                            namespaced["invocation_id"] = record.context["chain_id"]
                            native_epoch = value["epoch"]
                            if native_epoch not in native_epochs:
                                native_epochs[native_epoch] = (
                                    digest([record.context["chain_id"], "answer_epoch"])
                                    if not native_epochs
                                    else digest(
                                        [record.request_id, "epoch", native_epoch]
                                    )
                                )
                            namespaced["epoch"] = native_epochs[native_epoch]
                            for field in ("event_id", "step_id"):
                                if value[field] is not None:
                                    namespaced[field] = digest(
                                        [record.request_id, field, value[field]]
                                    )
                            if value["coverage"] is not None:
                                namespaced["coverage"] = [
                                    digest([record.request_id, "step_id", step])
                                    for step in value["coverage"]
                                ]
                            for step in namespaced["coverage"] or []:
                                self.ledger.bind_step(
                                    record.context["chain_id"], step, record.request_id
                                )
                            self.ledger.observe(namespaced)
                        elif kind == "native_turn_ended":
                            terminal = value
                        elif kind == "backend_error":
                            raise ProtocolError("native_backend_error", value["status"])
                        else:
                            raise ProtocolError("unexpected_backend_activity", 502)
                finally:
                    await iterator.aclose()
                if terminal is None:
                    raise ProtocolError("missing_native_terminal", 502)
                if terminal["finish_reason"] != "stop":
                    if (
                        record.payload.get("tools")
                        and terminal["finish_reason"] == "length"
                    ):
                        raise ProtocolError("incomplete_answer_only_output", 502)
                    # Refusals and length stops remain native terminal reasons.
                    selected = {"kind": "final", "text": "".join(output)}
                else:
                    selected = decision("".join(output), record.payload, record.policy)
        except TimeoutError:
            raise ProtocolError("answer_only_timeout", 504) from None
        except ProtocolError:
            raise
        except Exception as error:
            status = getattr(error, "status", 502)
            if type(status) is not int or status < 400 or status > 599:
                status = 502
            raise ProtocolError("answer_only_backend_error", status) from None
        finally:
            record.active_seconds += time.monotonic() - started

        # A successful close is mandatory before text or a tool request escapes.
        async with record.cleanup_lock:
            self.cleaning += 1
            try:
                async with asyncio.timeout(self.cleanup_seconds):
                    await record.binding.driver.close(
                        record.turn, time.monotonic() + self.cleanup_seconds
                    )
                record.turn = None
            except (Exception, asyncio.CancelledError):
                self.ledger.degraded = True
                raise ProtocolError("native_cleanup_incomplete", 503) from None
            finally:
                self.cleaning -= 1
        self.authority.check(record.principal, record.alias)
        usage_feature = record.manifest["features"]["usage"]
        usage_verified = usage_feature["state"] == "verified" and (
            self._fixture
            or (
                usage_feature["live_verified"]
                and usage_feature["evidence_level"] == "live"
            )
        )
        round_usage = None
        if usage_verified and terminal["usage_completeness"] == "complete":
            round_usage = self.ledger.project(
                record.context["chain_id"], request_id=record.request_id
            ).openai()
        record.answer_usage_complete &= round_usage is not None
        record.usage_completeness = (
            "complete" if record.answer_usage_complete else "partial"
        )
        if selected["kind"] == "final":
            result = Result(
                {"role": "assistant", "content": selected["text"]},
                terminal["finish_reason"],
                round_usage,
                record.request_id,
                record.alias,
                record.locator,
                native_complete=True,
            )
            await self._finish(record, cancel=False)
            return result

        record.handoffs += 1
        if record.handoffs > self.max_handoffs:
            raise ProtocolError("handoff_limit", 429)
        public_id = "call_" + secrets.token_hex(12)
        call = {
            "id": public_id,
            "type": "function",
            "function": {
                "name": selected["name"],
                "arguments": canonical(selected["arguments"]).decode("utf-8"),
            },
        }
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        record.history.append(copy.deepcopy(message))
        record.expires = min(
            time.monotonic() + self.tool_wait_seconds, limits["chain_deadline"]
        )
        record.batch = {
            "handoff_id": "handoff_" + secrets.token_hex(16),
            "calls": [{"public_id": public_id}],
        }
        async with self._admission:
            self.authority.check(record.principal, record.alias)
            if record.state != "RUNNING":
                raise ProtocolError("native_turn_fenced", 410)
            record.state = "PREPARED"
            record.owner = None
            if record.permit:
                record.permit = False
                self.active -= 1
        return Result(
            message,
            "tool_calls",
            round_usage,
            record.request_id,
            record.alias,
            record.locator,
            record.batch["handoff_id"],
        )

    async def _resume_answer(self, record, principal, payload, policy):
        if record.principal is not principal:
            raise ProtocolError("session_expired", 410)
        if record.lock.locked() or record.state == "RUNNING":
            raise ProtocolError("conversation_busy", 409)
        async with record.lock:
            if record.state == "PREPARED":
                try:
                    await asyncio.wait_for(
                        record.ack.wait(),
                        min(2, max(0, record.expires - time.monotonic())),
                    )
                except TimeoutError:
                    raise ProtocolError("delivery_pending", 409) from None
            if record.state != "DELIVERED" or time.monotonic() >= record.expires:
                raise ProtocolError("session_expired", 410)
            effective = copy.deepcopy(payload)
            for key in (
                "tools",
                "reasoning_output",
                "reasoning_effort",
                "max_completion_tokens",
            ):
                if key not in effective and key in record.payload:
                    effective[key] = copy.deepcopy(record.payload[key])
            if self._fingerprint(record.binding, effective) != record.fingerprint:
                raise ProtocolError("pending_configuration_conflict", 409)
            try:
                check_answer_features(
                    record.manifest, effective, policy, fixture=self._fixture
                )
            except ProtocolError:
                raise ProtocolError("pending_policy_conflict", 409) from None
            history = effective["messages"]
            prefix = record.history
            results = history[len(prefix) :]
            if (
                history[: len(prefix)] != prefix
                or not results
                or any(message["role"] != "tool" for message in results)
            ):
                raise ProtocolError("pending_history_conflict", 409)
            expected = {call["public_id"] for call in record.batch["calls"]}
            ids = [message["tool_call_id"] for message in results]
            if len(ids) != len(set(ids)) or set(ids) != expected:
                raise ProtocolError("pending_results_conflict", 409)
            if len(canonical(results)) > 131072:
                raise ProtocolError("tool_results_too_large", 409)
            async with self._admission:
                self.authority.check(principal, record.alias)
                if self.closed:
                    raise ProtocolError("service_stopping", 503)
                if record.state != "DELIVERED" or time.monotonic() >= record.expires:
                    raise ProtocolError("session_expired", 410)
                if self.active >= self.max_active:
                    raise ProtocolError("capacity_exhausted", 429)
                self.active += 1
                record.permit = True
                record.state = "RUNNING"
                record.owner = asyncio.current_task()
                record.request_id = "req_" + secrets.token_hex(16)
                record.context["request_id"] = record.request_id
                self.ledger.next_request(record.context["chain_id"], record.request_id)
                record.history = copy.deepcopy(history)
                record.payload = effective
                record.policy = policy
                record.ack.clear()
                record.batch = None
            try:
                return await self._answer_round(record)
            except BaseException:
                await self._finish(record, cancel=True, reason="continuation_failed")
                raise

    async def _resume(self, record, principal, payload, policy):
        if record.binding.config.get("execution_mode") == "answer_only":
            return await self._resume_answer(record, principal, payload, policy)
        if record.principal is not principal:
            raise ProtocolError("session_expired", 410)
        if record.lock.locked() or record.state == "RUNNING":
            raise ProtocolError("conversation_busy", 409)
        async with record.lock:
            if record.state == "PREPARED":
                try:
                    await asyncio.wait_for(
                        record.ack.wait(),
                        timeout=min(2, max(0, record.expires - time.monotonic())),
                    )
                except TimeoutError:
                    raise ProtocolError("delivery_pending", 409) from None
            if record.state != "DELIVERED" or time.monotonic() >= record.expires:
                raise ProtocolError("session_expired", 410)
            self.authority.check(principal, payload["model"])
            effective = copy.deepcopy(payload)
            for key in (
                "tools",
                "reasoning_output",
                "reasoning_effort",
                "max_completion_tokens",
            ):
                if key not in effective and key in record.payload:
                    effective[key] = record.payload[key]
            if "parallel_tool_calls" not in payload:
                policy["parallel_tool_calls"] = record.policy["parallel_tool_calls"]
            if (
                "max_completion_tokens" not in policy
                and "max_completion_tokens" in record.policy
            ):
                policy["max_completion_tokens"] = record.policy["max_completion_tokens"]
            if self._fingerprint(record.binding, effective) != record.fingerprint:
                raise ProtocolError("pending_configuration_conflict", 409)
            try:
                check_features(
                    record.manifest, effective, policy, fixture=self._fixture
                )
            except ProtocolError:
                raise ProtocolError("pending_policy_conflict", 409) from None
            # Native policy changes require a capability not present in v0.1.
            # Conservatively permit only the same effective policy.
            if policy != record.policy:
                raise ProtocolError("pending_policy_change_unavailable", 409)
            history = payload["messages"]
            prefix = record.history
            result_messages = history[len(prefix) :]
            if (
                history[: len(prefix)] != prefix
                or not result_messages
                or any(m["role"] != "tool" for m in result_messages)
            ):
                raise ProtocolError("pending_history_conflict", 409)
            mapping = {c["public_id"]: c["native_ref"] for c in record.batch["calls"]}
            results = {m["tool_call_id"]: m["content"] for m in result_messages}
            if len(results) != len(result_messages) or set(results) != set(mapping):
                raise ProtocolError("pending_results_conflict", 409)
            if len(canonical(result_messages)) > 131072:
                raise ProtocolError("tool_results_too_large", 409)
            request_id = "req_" + secrets.token_hex(16)
            dto = {
                "handoff_id": record.batch["handoff_id"],
                "request_id": request_id,
                "locator": record.locator,
                "content_digest": digest(result_messages),
                "results": [
                    {"public_id": key, "native_ref": mapping[key], "content": value}
                    for key, value in results.items()
                ],
                "round_policy": policy,
            }
            validate("ToolResults", dto)
            # No await between final fresh identity check and the driver call.
            self.authority.check(principal, record.alias)
            async with self._admission:
                if self.closed:
                    raise ProtocolError("service_stopping", 503)
                self.authority.check(principal, record.alias)
                if (
                    record.state != "DELIVERED"
                    or self.records.get(record.locator) is not record
                    or time.monotonic() >= record.expires
                ):
                    raise ProtocolError("session_expired", 410)
                record.state = "RUNNING"
                record.owner = asyncio.current_task()
                record.request_id = request_id
                self.ledger.next_request(record.context["chain_id"], request_id)
                record.history = copy.deepcopy(history)
                record.ack.clear()
                record.batch = None
            try:
                async with asyncio.timeout(
                    max(
                        0,
                        record.context["limits"]["native_deadline"] - time.monotonic(),
                    )
                ):
                    await record.binding.driver.submit_tool_results(record.turn, dto)
                result = await self._collect(record)
                self._check_active_identity(record)
                return result
            except BaseException:
                await self._finish(record, cancel=True, reason="continuation_failed")
                raise

    def _check_active_identity(self, record):
        try:
            self.authority.check(record.principal, record.alias)
        except ProtocolError as error:
            # An outage retains the bounded native lease; explicit revocation
            # cannot publish a completed result or a new tool handoff.
            if error.status != 503:
                raise

    async def _collect(self, record):
        started = time.monotonic()
        limits = record.context["limits"]
        timeout = min(
            limits["native_deadline"] - started,
            limits["chain_deadline"] - started,
            limits["inference_seconds"] - record.active_seconds,
        )
        text, reasoning, calls, fragments, indices = [], [], {}, {}, {}
        byte_count = 0
        iterator = record.binding.driver.events(record.turn)
        try:
            async with asyncio.timeout(max(0, timeout)):
                async for event in iterator:
                    validate("AdapterEvent", event)
                    if record.state != "RUNNING":
                        raise ProtocolError("native_turn_fenced", 410)
                    self._check_active_identity(record)
                    if record.invocation is None:
                        record.invocation = event["invocation_id"]
                    if (
                        record.invocation != event["invocation_id"]
                        or event["sequence"] <= record.sequence
                    ):
                        raise ProtocolError("invalid_native_sequence", 502)
                    record.sequence = event["sequence"]
                    kind, value = event["type"], event["payload"]
                    byte_count += len(canonical(event))
                    if byte_count > 4 * 1024 * 1024:
                        raise ProtocolError("native_output_too_large", 502)
                    if kind == "text_delta":
                        text.append(value["text"])
                    elif kind == "reasoning_delta":
                        expected = record.payload.get("reasoning_output", "off")
                        if expected != "off" and value["kind"] == expected:
                            reasoning.append(value["text"])
                    elif kind == "usage":
                        if value["invocation_id"] != record.invocation:
                            raise ProtocolError("invalid_usage_invocation", 502)
                        self.ledger.observe(
                            {**value, "invocation_id": record.context["chain_id"]}
                        )
                        # Arrival time does not prove HTTP-round ownership. Native
                        # usage can arrive late from a previous tool round.
                    elif kind in {"tool_call_start", "tool_call_ready"}:
                        native = value["native_ref"]
                        if native in calls and calls[native].get("ready"):
                            raise ProtocolError("duplicate_native_call", 502)
                        if (
                            value["index"] in indices
                            and indices[value["index"]] != native
                        ):
                            raise ProtocolError("duplicate_native_index", 502)
                        if native in calls and (
                            calls[native]["index"] != value["index"]
                            or calls[native]["name"] != value["name"]
                        ):
                            raise ProtocolError("native_call_changed", 502)
                        indices[value["index"]] = native
                        if native not in calls:
                            if native in record.native_refs:
                                raise ProtocolError("reused_native_ref", 502)
                            record.native_refs.add(native)
                            calls[native] = {
                                **value,
                                "public_id": "call_" + secrets.token_hex(12),
                                "ready": False,
                            }
                        if kind == "tool_call_ready":
                            tools = {
                                t["function"]["name"]: t
                                for t in record.payload.get("tools", [])
                            }
                            if (
                                value["name"] not in tools
                                or record.policy["tool_choice"] == "none"
                            ):
                                raise ProtocolError("undeclared_native_tool", 502)
                            check_arguments(tools[value["name"]], value["arguments"])
                            choice = record.policy["tool_choice"]
                            if (
                                isinstance(choice, dict)
                                and choice["function"]["name"] != value["name"]
                            ):
                                raise ProtocolError("native_tool_choice_violation", 502)
                            if (
                                native in fragments
                                and parse("".join(fragments[native]))
                                != value["arguments"]
                            ):
                                raise ProtocolError("native_argument_mismatch", 502)
                            calls[native].update(value, ready=True)
                    elif kind == "tool_arguments_delta":
                        native = value["native_ref"]
                        if native not in calls or calls[native]["ready"]:
                            raise ProtocolError("invalid_native_argument_delta", 502)
                        fragments.setdefault(native, []).append(
                            value["arguments_delta"]
                        )
                        if len("".join(fragments[native]).encode("utf-8")) > 65536:
                            raise ProtocolError("native_arguments_too_large", 502)
                    elif kind == "tool_batch_ready":
                        refs = value["native_refs"]
                        if (
                            set(refs) != set(calls)
                            or any(not c["ready"] for c in calls.values())
                            or [calls[n]["index"] for n in refs]
                            != list(range(len(refs)))
                        ):
                            raise ProtocolError("incomplete_native_batch", 502)
                        if len(refs) > (
                            8 if record.policy["parallel_tool_calls"] else 1
                        ):
                            raise ProtocolError(
                                "parallel_native_batch_unavailable", 502
                            )
                        record.handoffs += 1
                        if record.handoffs > limits["max_handoffs"]:
                            raise ProtocolError("handoff_limit", 429)
                        public = [
                            {
                                "id": calls[n]["public_id"],
                                "type": "function",
                                "function": {
                                    "name": calls[n]["name"],
                                    "arguments": canonical(
                                        calls[n]["arguments"]
                                    ).decode(),
                                },
                            }
                            for n in refs
                        ]
                        message = self._message(text, reasoning, public)
                        record.history.append(message)
                        # Reasoning is presentation; it is not part of required request history.
                        record.history[-1] = copy.deepcopy(
                            {
                                k: v
                                for k, v in message.items()
                                if k != "reasoning_content"
                            }
                        )
                        now = time.monotonic()
                        record.expires = min(
                            now + limits["tool_wait_seconds"],
                            limits["native_deadline"],
                            limits["chain_deadline"],
                        )
                        record.batch = {
                            "handoff_id": "handoff_" + secrets.token_hex(16),
                            "calls": [
                                {
                                    k: calls[n][k]
                                    for k in (
                                        "native_ref",
                                        "public_id",
                                        "name",
                                        "arguments",
                                    )
                                }
                                for n in refs
                            ],
                            "toolset_digest": digest(record.payload.get("tools", [])),
                            "transcript_digest": digest(record.history),
                            "expires_at": record.expires,
                        }
                        validate("ToolBatch", record.batch)
                        record.state = "PREPARED"
                        record.owner = None
                        return Result(
                            message,
                            "tool_calls",
                            None,
                            record.request_id,
                            record.alias,
                            record.locator,
                            record.batch["handoff_id"],
                        )
                    elif kind == "native_turn_ended":
                        record.usage_completeness = value["usage_completeness"]
                        if calls:
                            raise ProtocolError(
                                "native_terminal_with_pending_calls", 502
                            )
                        message = self._message(text, reasoning)
                        choice = record.policy["tool_choice"]
                        if choice == "required" or isinstance(choice, dict):
                            raise ProtocolError("native_tool_choice_violation", 502)
                        usage = (
                            self.ledger.project(record.context["chain_id"]).openai()
                            if not record.handoffs
                            and value["usage_completeness"] == "complete"
                            and record.manifest["features"]["usage"]["state"]
                            == "verified"
                            and (
                                self._fixture
                                or record.manifest["features"]["usage"]["live_verified"]
                            )
                            else None
                        )
                        result = Result(
                            message,
                            value["finish_reason"],
                            usage,
                            record.request_id,
                            record.alias,
                            record.locator,
                            native_complete=True,
                        )
                        await self._finish(record, cancel=False)
                        return result
                    elif kind == "backend_error":
                        raise ProtocolError("native_backend_error", value["status"])
                raise ProtocolError("missing_native_terminal", 502)
        finally:
            record.active_seconds += time.monotonic() - started
            async with asyncio.timeout(self.cleanup_seconds):
                await iterator.aclose()

    @staticmethod
    def _message(text, reasoning, calls=None):
        message = {"role": "assistant", "content": "".join(text) or None}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = calls
        return message

    async def acknowledge(self, result):
        record = self.records.get(result.locator)
        if result.handoff_id is None:
            return
        if (
            record is None
            or record.state != "PREPARED"
            or record.batch["handoff_id"] != result.handoff_id
            or time.monotonic() >= record.expires
        ):
            raise ProtocolError("session_expired", 410)
        self.authority.check(record.principal, record.alias)
        record.state = "DELIVERED"
        record.ack.set()

    async def abort(self, locator, reason="cancelled"):
        record = self.records.get(locator)
        if record is not None:
            await self._finish(record, cancel=True, reason=reason)

    async def abort_delivery(self, result):
        record = self.records.get(result.locator)
        if (
            record is not None
            and record.batch is not None
            and record.batch["handoff_id"] == result.handoff_id
            and record.state == "PREPARED"
        ):
            await self._finish(record, cancel=True, reason="delivery_failed")

    async def _finish(self, record, *, cancel, reason="complete"):
        if record.state == "CLOSED":
            return
        owner = record.owner
        if (
            owner is not None
            and owner is not asyncio.current_task()
            and not owner.done()
        ):
            record.state = "FENCED"
            if not owner.cancelling():
                owner.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(owner), self.cleanup_seconds)
            except (Exception, asyncio.CancelledError):
                pass
            if not owner.done():
                self.ledger.degraded = True
                return
        async with record.cleanup_lock:
            if record.state == "CLOSED":
                return
            record.state = "CLEANING"
            record.ack.set()
            self.cleaning += 1
            deadline = time.monotonic() + self.cleanup_seconds
            answer_only = record.binding.config.get("execution_mode") == "answer_only"
            closed = record.turn is None if answer_only else record.session is None
            try:
                if cancel and record.turn is not None:
                    try:
                        async with asyncio.timeout(self.cleanup_seconds / 2):
                            if answer_only:
                                await record.binding.driver.cancel(
                                    record.turn, deadline
                                )
                            else:
                                await record.binding.driver.cancel(
                                    record.turn, reason, deadline
                                )
                    except (Exception, asyncio.CancelledError):
                        self.ledger.degraded = True
                if answer_only and record.turn is not None:
                    try:
                        async with asyncio.timeout(
                            max(0.001, deadline - time.monotonic())
                        ):
                            await record.binding.driver.close(record.turn, deadline)
                        record.turn = None
                        closed = True
                    except (Exception, asyncio.CancelledError):
                        self.ledger.degraded = True
                elif record.session is not None:
                    try:
                        async with asyncio.timeout(
                            max(0.001, deadline - time.monotonic())
                        ):
                            await record.binding.driver.close_session(
                                record.session, deadline
                            )
                        closed = True
                    except (Exception, asyncio.CancelledError):
                        self.ledger.degraded = True
            finally:
                self.cleaning -= 1
                if closed:
                    record.state = "CLOSED"
                    self.ledger.finish(
                        record.context["chain_id"],
                        "succeeded"
                        if not cancel
                        else "failed"
                        if reason in {"request_failed", "continuation_failed"}
                        else "cancelled",
                        completeness=record.usage_completeness,
                    )
                    if self.records.get(record.locator) is record:
                        self.records.pop(record.locator)
                    if record.permit:
                        record.permit = False
                        self.active -= 1
                else:
                    record.state = "QUARANTINED"

    async def _reap(self):
        try:
            while not self.closed:
                await asyncio.sleep(0.1)
                for record in list(self.records.values()):
                    if record.locator in self._reaper_cleanups:
                        continue
                    now = time.monotonic()
                    revoked = False
                    try:
                        self.authority.check(record.principal, record.alias)
                    except ProtocolError as error:
                        revoked = error.status in (401, 403)
                    if (
                        revoked
                        or now >= record.context["limits"]["native_deadline"]
                        or (
                            record.state in {"PREPARED", "DELIVERED"}
                            and now >= record.expires
                        )
                    ):
                        # Fence every affected generation before any slow native
                        # cleanup; one backend cannot delay another key's revoke.
                        record.state = "FENCED"
                        if (
                            record.owner is not None
                            and not record.owner.done()
                            and not record.owner.cancelling()
                        ):
                            record.owner.cancel()
                        task = asyncio.create_task(
                            self._finish(record, cancel=True, reason="lease_expired")
                        )
                        self._reaper_cleanups[record.locator] = task
                        task.add_done_callback(
                            lambda done, key=record.locator: self._reaped(key, done)
                        )
        except asyncio.CancelledError:
            pass

    def _reaped(self, locator, task):
        self._reaper_cleanups.pop(locator, None)
        if not task.cancelled() and task.exception() is not None:
            self.ledger.degraded = True

    async def close(self):
        self.closed = self.draining = True
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        if self._reaper_cleanups:
            await asyncio.gather(
                *list(self._reaper_cleanups.values()), return_exceptions=True
            )
        for record in list(self.records.values()):
            await self._finish(record, cancel=True, reason="shutdown")
        if self.records:
            raise ProtocolError("native_cleanup_incomplete", 503)
