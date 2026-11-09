"""Author's smoke test of the core path against the dummy. Independent cases live in cases.json."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from daybreak.dummy_paypal import DummyPayPal
from daybreak.execute import Service
from daybreak.paypal import PayPalClient
from daybreak.ports import Extraction, InvalidState, StaleVersion
from daybreak.seed import ORDERS, seed

NOW = datetime(2026, 11, 8, 1, tzinfo=timezone.utc)


class FixedClock:
    def now(self):
        return NOW


class Stub:
    async def extract(self, message, session_name):
        return Extraction("refund", False, "Refund request")


@pytest.fixture
def world(tmp_path):
    dummy = DummyPayPal()
    for _, capture_id, cents, _ in ORDERS:
        dummy.add_capture(capture_id, cents)
    client = PayPalClient("id", "secret", transport=dummy.transport())
    db = str(tmp_path / "t.db")
    service = Service(db, client, Stub(), FixedClock())
    seed(db, NOW)
    return service, dummy, db, client


async def submit(service, order):
    draft = await service.submit_message(order, "Please refund me")
    return await service.confirm(draft.draft_id, draft.version, order)


async def test_climax_39_49_complete_29_waits_then_seller_approves(world):
    service, dummy, *_ = world
    await service.start_delegation(5000, 10000, NOW + timedelta(hours=7))
    assert (await submit(service, "order-39")).job_state == "completed"
    assert (await submit(service, "order-49")).job_state == "completed"
    waiting = await submit(service, "order-29")
    assert (waiting.status, waiting.reason, waiting.job_state) == ("pending_approval", "over_total", None)
    assert dummy.money_out_calls == 2
    done = await service.approve(waiting.request_id)
    assert (done.job_state, done.actor) == ("completed", "seller")
    summary = service.morning_summary()
    assert (summary.auto_completed_cents, summary.manual_completed_cents) == (8800, 2900)
    assert summary.remaining_delegation_cents == 1200


async def test_second_request_links_without_new_refund(world):
    service, dummy, *_ = world
    await service.start_delegation(5000, 10000, NOW + timedelta(hours=7))
    await submit(service, "order-39")
    again = await submit(service, "order-39")
    assert (again.status, again.reason) == ("linked", "already_refunded")
    assert dummy.money_out_calls == 1


async def test_concurrent_requests_same_capture_refund_once(world):
    service, dummy, *_ = world
    await service.start_delegation(5000, 10000, NOW + timedelta(hours=7))
    await asyncio.gather(*(submit(service, "order-39") for _ in range(5)))
    assert dummy.money_out_calls == 1


async def test_lost_response_keeps_hold_and_retries_same_key(world):
    service, dummy, *_ = world
    await service.start_delegation(5000, 4000, NOW + timedelta(hours=7))
    dummy.faults = ["lost"]
    case = await submit(service, "order-39")
    assert case.job_state == "unknown"
    assert service.morning_summary().remaining_delegation_cents == 100
    await service.reconcile_job(case.job_id)
    assert service.get_case(case.request_id).job_state == "completed"
    assert dummy.money_out_calls == 1


async def test_restart_marks_interrupted_unknown_and_pauses_auto(world):
    service, dummy, db, client = world
    await service.start_delegation(5000, 10000, NOW + timedelta(hours=7))
    dummy.faults = ["timeout"]
    case = await submit(service, "order-39")
    restarted = Service(db, client, Stub(), FixedClock())
    assert restarted.recover_on_start() == 1
    paused = await submit(restarted, "order-49")
    assert (paused.status, paused.reason) == ("pending_approval", "delegation_stopped")
    await restarted.reconcile_job(case.job_id)
    assert (await submit(restarted, "order-29")).job_state == "completed"


async def test_definite_failure_allows_new_key_and_releases_hold(world):
    service, dummy, *_ = world
    await service.start_delegation(5000, 10000, NOW + timedelta(hours=7))
    dummy.faults = ["REFUND_NOT_ALLOWED"]
    failed = await submit(service, "order-39")
    assert failed.job_state == "failed"
    assert service.morning_summary().remaining_delegation_cents == 10000
    retried = await submit(service, "order-39")
    assert retried.job_state == "completed"
    assert dummy.money_out_calls == 1


async def test_edit_after_draft_requires_current_version(world):
    service, *_ = world
    draft = await service.submit_message("order-39", "Maybe refund?")
    await service.edit_message(draft.draft_id, "order-39", "Refund please")
    with pytest.raises(StaleVersion):
        await service.confirm(draft.draft_id, draft.version, "order-39")


async def test_approve_rejects_non_pending(world):
    service, *_ = world
    await service.start_delegation(5000, 10000, NOW + timedelta(hours=7))
    case = await submit(service, "order-39")
    with pytest.raises(InvalidState):
        await service.approve(case.request_id)
