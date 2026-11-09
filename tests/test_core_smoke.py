"""Author's smoke test of the core path against the dummy. Independent cases live in cases.json."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from daybreak.dummy_paypal import DummyPayPal
from daybreak.execute import Service
from daybreak.paypal import PayPalClient
from daybreak.ports import AgentTurn, Extraction, InvalidState, StaleVersion
from daybreak.seed import ORDERS, seed

NOW = datetime(2026, 11, 8, 1, tzinfo=timezone.utc)


class FixedClock:
    def now(self):
        return NOW


class Stub:
    async def extract(self, message, session_name):
        return Extraction("refund", False, "Refund request")


class ScriptedAgent:
    def __init__(self, reply="OK", kind="refund", suggested="refund"):
        self.reply, self.kind, self.suggested = reply, kind, suggested

    async def respond(self, history, session_name, options):
        return AgentTurn(self.kind, False, "Summary", self.reply, "ko", self.suggested)


async def world_with_agent(world, agent, starts_in=None):
    service, dummy, db, client = world
    return Service(db, client, Stub(), FixedClock(), agent=agent), dummy, db


async def test_agent_cannot_promise_unoffered_amount(world):
    service, *_ = await world_with_agent(world, ScriptedAgent("전액 $500 환불해 드릴게요"))
    view = await service.converse("order-39", "돈 돌려줘요")
    assert view.reply_was_replaced and "$500" not in view.reply


async def test_agent_may_state_offered_amount_and_suggest(world):
    service, *_ = await world_with_agent(world, ScriptedAgent("$39.00 환불이 가능합니다"))
    view = await service.converse("order-39", "돈 돌려줘요")
    assert not view.reply_was_replaced and view.suggested_option == "refund"


async def test_forged_option_rejected_and_refund_button_runs_money_path(world):
    service, dummy, *_ = await world_with_agent(world, ScriptedAgent(suggested="move:nope"))
    await service.start_delegation(5000, 10000, NOW + timedelta(hours=7))
    view = await service.converse("order-39", "refund")
    assert view.suggested_option is None
    with pytest.raises(InvalidState):
        await service.choose(view.draft_id, view.version, "order-39", "refund:99999")
    case = await service.choose(view.draft_id, view.version, "order-39", "refund")
    assert case.job_state == "completed" and dummy.money_out_calls == 1


async def test_move_takes_seat_moves_no_money_then_seat_runs_out(world):
    service, dummy, db, = await world_with_agent(world, ScriptedAgent(kind="other"))
    view = await service.converse("order-39", "다른 날짜로 옮길 수 있나요")
    move = next(o for o in view.options if o.kind == "move")
    case = await service.choose(view.draft_id, view.version, "order-39", move.id)
    assert case.status == "moved" and dummy.refund_calls == 0
    assert any(e.kind == "moved" for e in service.timeline())


async def test_partial_refund_inside_partial_window(world):
    service, dummy, db, client = world
    agent_service = Service(db, client, Stub(), FixedClock(), agent=ScriptedAgent())
    import sqlite3
    with sqlite3.connect(db) as c:
        c.execute("UPDATE orders SET session_starts_at=? WHERE id='order-39'", ((NOW + timedelta(hours=5)).isoformat(),))
    await agent_service.start_delegation(5000, 10000, NOW + timedelta(hours=7))
    view = await agent_service.converse("order-39", "refund")
    refund = next(o for o in view.options if o.kind == "refund")
    assert refund.amount_cents == 1950
    case = await agent_service.choose(view.draft_id, view.version, "order-39", "refund")
    assert (case.job_state, case.amount_cents) == ("completed", 1950)
    assert dummy.captures["CAP-39"]["status"] == "PARTIALLY_REFUNDED"


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
    assert (paused.status, paused.reason) == ("pending_approval", "restore_reconciliation_required")
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
