import asyncio
import json
import sqlite3

import pytest
from test_v2_host import continuation, payload, setup, usage

from litellm_agent_rpc.v2.errors import ProtocolError
from litellm_agent_rpc.v2.usage import Ledger


async def test_native_management_drain_pending_cancel_and_stop():
    _, principal, driver, broker = setup()
    try:
        first = await broker.invoke(principal, payload(), "private-cid")
        await broker.acknowledge(first)
        await broker.set_draining(True)
        snapshot = broker.status_snapshot()
        assert snapshot["awaiting_client"] == 1
        assert not await broker.try_stop(lambda: 0)
        public = json.dumps(snapshot)
        assert "private-cid" not in public and "n1" not in public
        assert snapshot["sessions"][0]["key_scope"] != principal.scope_id
        with pytest.raises(ProtocolError) as draining:
            await broker.invoke(principal, payload(), "other")
        assert draining.value.status == 503
        result = await broker.invoke(
            principal, continuation(first, payload()), "private-cid"
        )
        assert result.native_complete
        assert not await broker.try_stop(lambda: 1)
        assert await broker.try_stop(lambda: 0)
        with pytest.raises(ProtocolError, match="service_stopping"):
            await broker.set_draining(False)
        assert driver.opens == 1
    finally:
        await broker.close()


async def test_operator_cancel_uses_public_chain_identity():
    _, principal, driver, broker = setup()
    try:
        first = await broker.invoke(principal, payload(), "one")
        await broker.acknowledge(first)
        chain = broker.status_snapshot()["sessions"][0]["chain_id"]
        assert await broker.cancel_pending(chain)
        assert not await broker.cancel_pending(chain)
        assert driver.cancelled == 1 and not broker.records
        entry = broker.ledger.status_snapshot()["invocations"][0]
        assert entry["status"] == "cancelled" and entry["input_tokens"] is None
    finally:
        await broker.close()


def test_ledger_persists_metadata_and_nullable_counts_without_content(tmp_path):
    path = tmp_path / "native.sqlite"
    ledger = Ledger(path)
    ledger.begin(
        "inv", key_scope="a" * 64, model="alias", backend="codex", request_id="r1"
    )
    ledger.observe(usage())
    ledger.next_request("inv", "r2")
    ledger.finish("inv", "failed")
    snapshot = ledger.status_snapshot()
    assert snapshot["invocations"][0]["input_tokens"] == 10
    assert snapshot["invocations"][0]["usage_completeness"] == "partial"
    assert snapshot["invocations"][0]["request_count"] == 2
    ledger.close()
    reopened = Ledger(path)
    assert reopened.status_snapshot()["invocations"][0] == snapshot["invocations"][0]
    assert reopened.status_snapshot()["totals"]["cache_write_tokens"] is None
    reopened.close()
    content = path.read_bytes()
    assert b"private-cid" not in content and b"client result" not in content


def test_ledger_bounds_memory_and_sqlite_retention(tmp_path):
    path = tmp_path / "usage.sqlite"
    ledger = Ledger(path, max_invocations=3, max_observations=4)
    for index in range(20):
        invocation = f"i{index}"
        ledger.begin(invocation, request_id=f"r{index}")
        ledger.observe(usage(invocation_id=invocation))
        ledger.finish(invocation, "succeeded")
    assert len(ledger.status_snapshot()["invocations"]) <= 3
    assert len(ledger.events) <= 4
    ledger.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM native_summaries_v2").fetchone()[0] <= 3
        assert db.execute("SELECT COUNT(*) FROM native_usage_v2").fetchone()[0] <= 4


def test_accounting_overflow_never_claims_complete():
    ledger = Ledger(max_observations=1)
    ledger.observe(usage())
    assert not ledger.observe(usage(event_id="u2", sequence=2))
    assert ledger.project("inv").openai() is None
    assert ledger.project("inv").counters["input_tokens"] == 10
    assert ledger.project("inv").completeness == "partial"
    assert ledger.degraded


def test_observation_and_summary_commit_atomically(tmp_path):
    path = tmp_path / "atomic.sqlite"
    ledger = Ledger(path)
    ledger.db.execute(
        "CREATE TRIGGER reject_summary BEFORE INSERT ON native_summaries_v2 BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    ledger.observe(usage())
    assert ledger.degraded
    assert ledger.db.execute("SELECT COUNT(*) FROM native_usage_v2").fetchone()[0] == 0
    ledger.db.execute("DROP TRIGGER reject_summary")
    ledger.observe(usage(event_id="u2", sequence=2))
    ledger.finish("inv", "succeeded")
    ledger.close()
    reopened = Ledger(path)
    assert reopened.status_snapshot()["invocations"][0]["input_tokens"] == 10
    assert (
        reopened.db.execute("SELECT COUNT(*) FROM native_usage_v2").fetchone()[0] == 1
    )
    reopened.close()


def test_per_invocation_observation_and_coverage_bounds():
    ledger = Ledger(max_invocation_observations=2, max_coverage=2)
    ledger.observe(usage())
    ledger.observe(usage(event_id="u2", sequence=2))
    assert not ledger.observe(usage(event_id="u3", sequence=3))
    assert len(ledger.events) == 2
    assert ledger.project("inv").completeness == "partial"
    assert not ledger.observe(usage(invocation_id="oversize", coverage=["a", "b", "c"]))
    assert ledger.project("oversize").completeness == "unknown"


async def test_native_partial_terminal_downgrades_operator_usage():
    _, principal, driver, broker = setup(tool=False)
    original = driver.events

    async def events(turn):
        async for event in original(turn):
            if event["type"] == "native_turn_ended":
                event["payload"]["usage_completeness"] = "partial"
            yield event

    driver.events = events
    try:
        result = await broker.invoke(principal, payload(False))
        assert result.usage is None
        row = broker.ledger.status_snapshot()["invocations"][0]
        assert row["usage_completeness"] == "partial" and row["input_tokens"] == 10
    finally:
        await broker.close()


@pytest.mark.parametrize("tool", [False, True])
async def test_revocation_immediately_before_native_boundary_blocks_publish(tool):
    authority, principal, driver, broker = setup(tool=tool)
    original = driver.events

    async def events(turn):
        async for event in original(turn):
            if event["type"] in {"native_turn_ended", "tool_batch_ready"}:
                authority.revoke(principal.scope_id)
            yield event

    driver.events = events
    try:
        with pytest.raises(ProtocolError, match="key_revoked"):
            await broker.invoke(principal, payload(tool), "cid")
        assert not broker.records and driver.cancelled == 1
    finally:
        await broker.close()


async def test_operator_cancel_reports_incomplete_native_cleanup():
    _, principal, driver, broker = setup()

    async def broken_close(session, deadline):
        raise RuntimeError("synthetic close failure")

    original = driver.close_session
    try:
        await broker.invoke(principal, payload(), "one")
        driver.close_session = broken_close
        chain = broker.status_snapshot()["sessions"][0]["chain_id"]
        with pytest.raises(ProtocolError, match="native_cleanup_incomplete") as error:
            await broker.cancel_pending(chain)
        assert error.value.status == 503 and broker.counts()["quarantined"] == 1
    finally:
        driver.close_session = original
        await broker.close()


async def test_reaper_cancels_owner_once_and_allows_iterator_cleanup():
    authority, principal, driver, broker = setup(tool=False)
    entered = asyncio.Event()
    iterator_cleaned = False

    async def waiting(turn):
        nonlocal iterator_cleaned
        try:
            entered.set()
            await asyncio.Event().wait()
            yield
        finally:
            await asyncio.sleep(0.03)
            iterator_cleaned = True

    driver.events = waiting
    task = asyncio.create_task(broker.invoke(principal, payload(False), "waiting"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        authority.revoke(principal.scope_id)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert iterator_cleaned
    finally:
        await broker.close()


async def test_later_revocation_is_fenced_while_another_cleanup_waits():
    authority, principal, driver, broker = setup()
    other = authority.register("bob", {"test"})
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_close(session, deadline):
        entered.set()
        await release.wait()

    driver.close_session = slow_close
    try:
        await broker.invoke(principal, payload(), "a")
        second = await broker.invoke(other, payload(), "b")
        authority.revoke(principal.scope_id)
        await asyncio.wait_for(entered.wait(), 1)
        authority.revoke(other.scope_id)
        async with asyncio.timeout(1):
            while broker.records[second.locator].state == "PREPARED":
                await asyncio.sleep(0.01)
        assert broker.records[second.locator].state in {"FENCED", "CLEANING"}
    finally:
        release.set()
        await broker.close()


async def test_stop_latch_rechecks_http_count_after_waiting_for_lock():
    _, _, _, broker = setup()
    await broker.set_draining(True)
    await broker._admission.acquire()
    active = 0
    stopping = asyncio.create_task(broker.try_stop(lambda: active))
    await asyncio.sleep(0)
    active = 1
    broker._admission.release()
    assert not await stopping
    await broker.close()
