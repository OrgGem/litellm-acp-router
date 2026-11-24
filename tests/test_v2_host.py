import asyncio
import copy
import json

import pytest

from litellm_agent_rpc.v2.broker import Binding, Broker
from litellm_agent_rpc.v2.contract import CONTRACT_HASH
from litellm_agent_rpc.v2.errors import ProtocolError
from litellm_agent_rpc.v2.identity import Authority
from litellm_agent_rpc.v2.usage import Ledger
from litellm_agent_rpc.v2.validation import request

TOOL = {
    "type": "function",
    "function": {
        "name": "echo",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}
PROFILE = {
    "profile_id": "fixture",
    "profile_revision": "1",
    "transport": "fixture",
    "models": {"test": "fake"},
}


def usage(**changes):
    return {
        "invocation_id": "inv",
        "step_id": "s1",
        "epoch": "e1",
        "event_id": "u1",
        "sequence": 1,
        "scope": "step",
        "semantics": "snapshot",
        "coverage": ["s1"],
        "source": "fixture",
        "unit": "tokens",
        "completeness": "complete",
        "final": True,
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_tokens": 2,
        "cache_write_tokens": None,
        "reasoning_tokens": 1,
        **changes,
    }


def manifest():
    feature = {
        "state": "verified",
        "evidence_level": "fixture",
        "live_verified": False,
        "evidence": ["fixture"],
        "reason": None,
    }
    return {
        "contract_version": "0.1",
        "contract_hash": CONTRACT_HASH,
        "backend": "codex",
        "cli_version": "fixture",
        "sdk_version": None,
        "model": "fake",
        "profile_id": "fixture",
        "transport": "fixture",
        "features": {
            k: dict(feature)
            for k in (
                "text",
                "client_tools",
                "parallel_tools",
                "reasoning_output",
                "reasoning_control",
                "usage",
                "output_token_limit",
                "profile_isolation",
            )
        },
        "tool_choices": ["auto", "none"],
        "reasoning_efforts": ["low"],
        "reasoning_kinds": ["summary"],
        "limitations": [],
    }


class Driver:
    def __init__(self, tool=True, error=False, terminal=True):
        self.opens = self.closed = self.cancelled = 0
        self.submitted = []
        self.tool, self.error, self.terminal = tool, error, terminal

    async def capabilities(self, profile, model):
        return manifest()

    async def open_session(self, context, config):
        self.opens += 1
        return {}

    async def begin_turn(self, session, turn_input):
        return {"seq": 0, "stage": 0, "input": turn_input}

    async def events(self, turn):
        events = []
        if self.tool and turn["stage"] == 0:
            events = [
                ("tool_call_start", {"native_ref": "n1", "index": 0, "name": "echo"}),
                (
                    "tool_arguments_delta",
                    {"native_ref": "n1", "arguments_delta": '{"text":"h'},
                ),
                (
                    "tool_arguments_delta",
                    {"native_ref": "n1", "arguments_delta": 'éllo"}'},
                ),
                (
                    "tool_call_ready",
                    {
                        "native_ref": "n1",
                        "index": 0,
                        "name": "echo",
                        "arguments": {"text": "héllo"},
                    },
                ),
                ("tool_batch_ready", {"native_refs": ["n1"]}),
            ]
        else:
            events = [
                (
                    "reasoning_delta",
                    {
                        "block_id": "r1",
                        "sequence": 0,
                        "kind": "summary",
                        "text": "public",
                        "opaque_replay_ref": "secret-opaque",
                    },
                ),
                ("text_delta", {"text": "answer"}),
                ("usage", usage()),
            ]
            if self.error:
                events.append(
                    ("backend_error", {"code": "native_private_error", "status": 502})
                )
            elif self.terminal:
                events.append(
                    (
                        "native_turn_ended",
                        {
                            "kind": "native_terminal",
                            "finish_reason": "stop",
                            "usage_completeness": "complete",
                        },
                    )
                )
        for kind, payload in events:
            turn["seq"] += 1
            yield {
                "type": kind,
                "invocation_id": "inv",
                "sequence": turn["seq"],
                "payload": payload,
            }

    async def submit_tool_results(self, turn, results):
        self.submitted.append(results)
        turn["stage"] += 1

    async def cancel(self, turn, reason, cleanup_deadline):
        self.cancelled += 1

    async def close_session(self, session, cleanup_deadline):
        self.closed += 1


def setup(tool=True, **kwargs):
    authority = Authority()
    principal = authority.register("alice", {"test", "alternate"})
    driver = Driver(tool=tool)
    binding = Binding(driver, PROFILE, "fake")
    broker = Broker(
        authority, {"test": binding, "alternate": binding}, _fixture=True, **kwargs
    )
    return authority, principal, driver, broker


def payload(tool=True):
    return {
        "model": "test",
        "messages": [{"role": "user", "content": "hello"}],
        **({"tools": [TOOL]} if tool else {}),
    }


def continuation(first, initial):
    return {
        "model": "test",
        "messages": initial["messages"]
        + [
            first.message,
            {
                "role": "tool",
                "tool_call_id": first.message["tool_calls"][0]["id"],
                "content": "client result",
            },
        ],
    }


async def test_prepared_ownership_ack_and_single_native_continuation():
    _, principal, driver, broker = setup()
    try:
        initial = payload()
        result = await broker.invoke(principal, initial, "same")
        assert result.finish_reason == "tool_calls" and result.usage is None
        assert broker.counts()["prepared"] == 1 and driver.cancelled == 0
        assert "n1" not in json.dumps(result.message)
        task = asyncio.create_task(
            broker.invoke(principal, continuation(result, initial), "same")
        )
        await asyncio.sleep(0)
        assert not task.done() and not driver.submitted
        await broker.acknowledge(result)
        final = await task
        assert final.message == {"role": "assistant", "content": "answer"}
        assert final.usage is None  # Native total must not be charged again to round2.
        assert driver.opens == 1 and len(driver.submitted) == 1 and driver.closed == 1
        assert driver.submitted[0]["results"][0]["native_ref"] == "n1"
        assert broker.active == 0 and not broker.records
        with pytest.raises(ProtocolError, match="session_expired"):
            await broker.invoke(principal, continuation(result, initial), "same")
    finally:
        await broker.close()


async def test_foreign_key_and_owned_conflicts_do_not_consume_pending():
    authority, principal, driver, broker = setup()
    try:
        initial = payload()
        first = await broker.invoke(principal, initial, "same")
        await broker.acknowledge(first)
        second = authority.register("bob", {"test"})
        with pytest.raises(ProtocolError) as foreign:
            await broker.invoke(second, continuation(first, initial), "same")
        assert foreign.value.status == 410
        bad = continuation(first, initial)
        bad["model"] = "alternate"
        with pytest.raises(ProtocolError) as changed:
            await broker.invoke(principal, bad, "same")
        assert changed.value.status == 409
        bad = continuation(first, initial)
        bad["messages"][-1]["tool_call_id"] = "wrong"
        with pytest.raises(ProtocolError) as wrong:
            await broker.invoke(principal, bad, "same")
        assert wrong.value.status == 409
        assert not driver.submitted and broker.counts()["awaiting_client"] == 1
        await broker.invoke(principal, continuation(first, initial), "same")
    finally:
        await broker.close()


async def test_paused_permit_drain_and_revocation():
    authority, principal, driver, broker = setup(max_active=1)
    try:
        first = await broker.invoke(principal, payload(), "one")
        await broker.acknowledge(first)
        with pytest.raises(ProtocolError) as capacity:
            await broker.invoke(principal, payload(), "two")
        assert capacity.value.status == 429 and driver.opens == 1
        broker.draining = True
        final = await broker.invoke(principal, continuation(first, payload()), "one")
        assert final.native_complete and broker.active == 0
        broker.draining = False
        first = await broker.invoke(principal, payload(), "one")
        await broker.acknowledge(first)
        authority.revoke("alice")
        with pytest.raises(ProtocolError) as revoked:
            await broker.invoke(principal, continuation(first, payload()), "one")
        assert revoked.value.status == 401
        await asyncio.sleep(0.15)
        assert not broker.records and driver.cancelled == 1
    finally:
        await broker.close()


async def test_expiry_cleanup_and_unknown_continuation():
    _, principal, driver, broker = setup(tool_wait_seconds=0.02)
    try:
        first = await broker.invoke(principal, payload(), "one")
        await asyncio.sleep(0.15)
        assert not broker.records and driver.closed == 1 and driver.cancelled == 1
        with pytest.raises(ProtocolError) as expired:
            await broker.acknowledge(first)
        assert expired.value.status == 410
    finally:
        await broker.close()


async def test_text_reasoning_usage_native_and_partial_error():
    _, principal, driver, broker = setup(tool=False)
    try:
        initial = {**payload(False), "reasoning_output": "summary"}
        final = await broker.invoke(principal, initial)
        assert final.message["reasoning_content"] == "public"
        assert "secret-opaque" not in json.dumps(final.message)
        assert final.usage["total_tokens"] == 13
        driver.error = True
        with pytest.raises(ProtocolError, match="native_backend_error"):
            await broker.invoke(principal, initial)
        assert any(o["input_tokens"] == 10 for o in broker.ledger.current.values())
        driver.error = False
        driver.terminal = False
        with pytest.raises(ProtocolError, match="missing_native_terminal"):
            await broker.invoke(principal, initial)
    finally:
        await broker.close()


async def test_production_rejects_fixture_manifest_before_spawn():
    _, principal, driver, broker = setup()
    broker._fixture = False
    try:
        with pytest.raises(ProtocolError, match="backend_capability_unverified"):
            await broker.invoke(principal, payload(), "one")
        assert driver.opens == 0
    finally:
        await broker.close()


@pytest.mark.parametrize(
    "change",
    [
        {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "parameters": {"$ref": "https://bad.invalid"},
                    },
                }
            ]
        },
        {"max_completion_tokens": True},
        {"parallel_tool_calls": 1},
        {"metadata": {"key_scope_id": "other"}},
        {"stream_options": {"include_usage": True}},
    ],
)
def test_request_rejects_unsupported_before_native(change):
    with pytest.raises(ProtocolError):
        request({**payload(), **change}, "one")


def test_usage_snapshot_correction_delta_dedup_coverage_and_unknown():
    ledger = Ledger()
    ledger.observe(usage())
    assert ledger.observe(usage()) is False
    ledger.observe(usage(event_id="u2", sequence=2, input_tokens=11))
    assert ledger.project("inv").counters["input_tokens"] == 11
    ledger.observe(
        usage(
            event_id="u3",
            sequence=3,
            scope="native_turn",
            step_id=None,
            input_tokens=11,
        )
    )
    assert ledger.project("inv").openai()["total_tokens"] == 14
    assert ledger.project("inv", request_id="unproven").openai() is None
    with pytest.raises(ProtocolError, match="contradictory_usage_event"):
        ledger.observe(usage(input_tokens=90))
    ledger.observe(
        usage(epoch="reset", event_id="reset", input_tokens=1, cache_read_tokens=0)
    )
    assert ledger.project("inv").completeness == "unknown"
    delta = Ledger()
    delta.observe(usage(semantics="delta"))
    delta.observe(usage(semantics="delta", event_id="d2", sequence=2))
    assert delta.project("inv").counters["input_tokens"] == 20
    missing = Ledger()
    missing.observe(usage(input_tokens=None, completeness="partial"))
    assert missing.project("inv").openai() is None
    assert missing.project("inv").counters["input_tokens"] is None


def test_usage_storage_contains_counters_not_conversation(tmp_path):
    ledger = Ledger(tmp_path / "usage.db")
    ledger.observe(usage())
    ledger.close()
    data = (tmp_path / "usage.db").read_bytes()
    assert b"input_tokens" in data
    assert b"client result" not in data


def test_identity_rotation_and_environment_are_distinct():
    authority = Authority(secret=b"s" * 32)
    old = authority.register("a", {"test"})
    locator = authority.locator(old, "same")
    new = authority.register("a", {"test"})
    assert locator != authority.locator(new, "same")
    with pytest.raises(ProtocolError):
        authority.check(copy.copy(new), "test")
