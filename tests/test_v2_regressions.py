import asyncio
import copy

import pytest
from test_v2_host import continuation, payload, setup, usage

from litellm_agent_rpc.v2.errors import ProtocolError
from litellm_agent_rpc.v2.sdk import AsyncClient
from litellm_agent_rpc.v2.usage import Ledger


async def test_native_call_ref_cannot_be_reused_in_later_round():
    _, principal, driver, broker = setup()
    original = driver.submit_tool_results

    async def reuse(turn, results):
        await original(turn, results)
        turn["stage"] = 0

    driver.submit_tool_results = reuse
    try:
        initial = payload()
        first = await broker.invoke(principal, initial, "one")
        await broker.acknowledge(first)
        with pytest.raises(ProtocolError, match="reused_native_ref"):
            await broker.invoke(principal, continuation(first, initial), "one")
        assert not broker.records and driver.cancelled == 1
    finally:
        await broker.close()


async def test_cancel_failure_still_closes_session():
    _, principal, driver, broker = setup()

    async def broken(*args):
        raise RuntimeError("private native failure")

    driver.cancel = broken
    first = await broker.invoke(principal, payload(), "one")
    await broker.abort_delivery(first)
    assert driver.closed == 1 and broker.active == 0
    await broker.close()


async def test_late_open_cannot_abort_replacement_or_release_permit_early():
    _, principal, driver, broker = setup(cleanup_seconds=0.03)
    gate = asyncio.Event()
    entered = asyncio.Event()
    original = driver.open_session

    async def blocked(context, config):
        entered.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await gate.wait()  # Simulate a native opener that delays cancellation.
        return await original(context, config)

    driver.open_session = blocked
    task = asyncio.create_task(broker.invoke(principal, payload(), "one"))
    try:
        await entered.wait()
        old = next(iter(broker.records.values()))
        await broker.abort(old.locator)
        assert broker.active == 1 and broker.records[old.locator] is old
        with pytest.raises(ProtocolError):
            await broker.invoke(principal, payload(), "one")
        gate.set()
        with pytest.raises(ProtocolError):
            await task
        assert not broker.records and broker.active == 0 and driver.closed >= 1
        driver.open_session = original
        new = await broker.invoke(principal, payload(), "one")
        await broker._finish(old, cancel=True)
        assert broker.records[new.locator].batch["handoff_id"] == new.handoff_id
    finally:
        gate.set()
        await broker.close()


async def test_close_failure_quarantines_native_capacity():
    _, principal, driver, broker = setup()
    original = driver.close_session

    async def broken(*args):
        raise RuntimeError("private")

    first = await broker.invoke(principal, payload(), "one")
    driver.close_session = broken
    await broker.abort_delivery(first)
    assert broker.active == 1
    assert next(iter(broker.records.values())).state == "QUARANTINED"
    driver.close_session = original
    await broker.close()
    assert broker.active == 0


def test_contradictory_covering_usage_never_publishes_complete():
    ledger = Ledger()
    ledger.observe(usage())
    ledger.observe(
        usage(
            scope="native_turn",
            step_id=None,
            coverage=["s1", "s2"],
            input_tokens=4,
            event_id="aggregate",
        )
    )
    assert ledger.project("inv").openai() is None
    partition = Ledger()
    partition.observe(usage(input_tokens=7))
    partition.observe(
        usage(input_tokens=7, step_id="s2", coverage=["s2"], event_id="s2")
    )
    partition.observe(
        usage(
            scope="native_turn",
            step_id=None,
            coverage=["s1", "s2"],
            input_tokens=10,
            event_id="total",
        )
    )
    assert partition.project("inv").openai() is None


def test_invalid_usage_does_not_poison_dedup_and_equal_sequence_conflicts():
    ledger = Ledger()
    ledger.observe(usage())
    with pytest.raises(ProtocolError, match="contradictory_usage_sequence"):
        ledger.observe(usage(event_id="conflict", input_tokens=6))
    for _ in range(2):
        with pytest.raises(ProtocolError, match="ambiguous_usage_semantics"):
            ledger.observe(usage(event_id="wrong-kind", semantics="delta", sequence=2))
    assert len(ledger.events) == 1


@pytest.mark.parametrize("field", ["model", "reasoning_output"])
async def test_wrong_field_type_is_controlled_400(field):
    _, principal, driver, broker = setup()
    try:
        with pytest.raises(ProtocolError) as caught:
            await broker.invoke(principal, {**payload(), field: []}, "one")
        assert caught.value.status == 400 and driver.opens == 0
    finally:
        await broker.close()


async def test_mutating_sdk_reply_cannot_modify_owned_history():
    _, principal, driver, broker = setup()
    try:
        client = AsyncClient(broker, principal)
        initial = payload()
        body = await client.completion(initial, "one")
        original = copy.deepcopy(body["choices"][0]["message"])
        message = body["choices"][0]["message"]
        message["tool_calls"][0]["function"]["arguments"] = '{"text":"tampered"}'
        result = {
            "role": "tool",
            "tool_call_id": original["tool_calls"][0]["id"],
            "content": "client",
        }
        with pytest.raises(ProtocolError) as changed:
            await client.completion(
                {"model": "test", "messages": initial["messages"] + [message, result]},
                "one",
            )
        assert changed.value.status == 409 and not driver.submitted
        original["reasoning_content"] = "Public presentation returned by a client"
        await client.completion(
            {"model": "test", "messages": initial["messages"] + [original, result]},
            "one",
        )
        assert len(driver.submitted) == 1
    finally:
        await broker.close()
