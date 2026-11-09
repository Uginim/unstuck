"""Author's tests of the shop core against the dummy. Independent attacks live elsewhere."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from daybreak.dummy_paypal import DummyPayPal
from daybreak.paypal import PayPalClient
from daybreak.ports import OrderResult
from shop.seed import seed
from shop.service import Composition, Interpretation, InvalidState, NotFound, OutOfStock, ShopService


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)

    def now(self):
        return self.t


class Agent:
    def __init__(self, product="peony", qty=12, reply="Here are your options."):
        self.product, self.qty, self.reply = product, qty, reply

    async def interpret(self, history, catalog):
        return Interpretation(self.product, self.qty, "wants bouquets", "en", "m", 100, 20, 50)

    async def compose(self, history, offers, language):
        return Composition(self.reply, offers[0].id if offers else None, "m", 100, 20, 50)


@pytest.fixture
def shop(tmp_path):
    dummy = DummyPayPal()
    clock = Clock()
    db = str(tmp_path / "s.db")
    service = ShopService(db, PayPalClient("id", "secret", transport=dummy.transport()), Agent(), clock)
    seed(db)
    return service, dummy, clock


def stock(service):
    return {p.id: p.stock for p in service.catalog()}


async def checkout(service, dummy, qty=12, kind="substitute"):
    service._agent = Agent(qty=qty)
    view = await service.message(f"{qty} peony bouquets for tomorrow")
    offer = next(o for o in view.offers if o.kind == kind)
    return await service.choose(offer.id)


async def test_shortage_becomes_code_built_substitute_offer(shop):
    service, *_ = shop
    view = await service.message("12 peony bouquets please")
    offer = next(o for o in view.offers if o.kind == "substitute")
    assert [(l.product_id, l.qty) for l in offer.lines] == [("peony", 8), ("tulip", 4)]
    assert offer.total_cents == 8 * 4500 + 4 * 2900
    assert {o.kind for o in view.offers} == {"substitute", "partial"}


async def test_exact_offer_when_stock_fits(shop):
    service, *_ = shop
    service._agent = Agent(qty=2)
    view = await service.message("2 peony")
    assert [o.kind for o in view.offers] == ["exact"]


async def test_order_paid_end_to_end_and_stock_moves_once(shop):
    service, dummy, _ = shop
    held = await checkout(service, dummy)
    assert held.state == "awaiting_payment" and held.approve_url
    assert stock(service)["peony"] == 0 and stock(service)["tulip"] == 16
    dummy.approve_order(held.paypal_order_id)
    paid = await service.capture(held.id)
    assert paid.state == "paid" and paid.capture_id and dummy.money_in_calls == 1
    assert (await service.capture(held.id)).state == "paid" and dummy.money_in_calls == 1


async def test_two_buyers_cannot_take_the_last_stock(shop):
    service, dummy, _ = shop
    service._agent = Agent(qty=8)
    first = await service.message("8 peony")
    second = await service.message("8 peony")
    results = await asyncio.gather(
        service.choose(first.offers[0].id), service.choose(second.offers[0].id), return_exceptions=True,
    )
    assert sum(isinstance(r, OutOfStock) for r in results) == 1
    assert stock(service)["peony"] == 0


async def test_failed_line_rolls_back_the_whole_hold(shop):
    service, dummy, _ = shop
    view = await service.message("12 peony")
    offer = next(o for o in view.offers if o.kind == "substitute")
    import sqlite3
    with sqlite3.connect(service._db) as c:
        c.execute("UPDATE products SET stock=1 WHERE id='tulip'")
    with pytest.raises(OutOfStock):
        await service.choose(offer.id)
    assert stock(service)["peony"] == 8


async def test_choosing_twice_holds_once(shop):
    service, dummy, _ = shop
    service._agent = Agent(qty=2)
    view = await service.message("2 peony")
    a = await service.choose(view.offers[0].id)
    b = await service.choose(view.offers[0].id)
    assert a.id == b.id and stock(service)["peony"] == 6 and len(dummy.orders) == 1


async def test_lost_capture_keeps_hold_and_reconcile_finds_one_payment(shop):
    service, dummy, _ = shop
    held = await checkout(service, dummy)
    dummy.approve_order(held.paypal_order_id)
    dummy.order_faults = ["lost"]
    unknown = await service.capture(held.id)
    assert unknown.state == "unknown" and dummy.money_in_calls == 1 and stock(service)["peony"] == 0
    assert (await service.capture(held.id)).state == "unknown"
    paid = await service.reconcile(held.id)
    assert paid.state == "paid" and dummy.money_in_calls == 1 and dummy.order_capture_calls == 1


async def test_capture_500_before_applied_retries_with_same_key(shop):
    service, dummy, _ = shop
    held = await checkout(service, dummy)
    dummy.approve_order(held.paypal_order_id)
    dummy.order_faults = ["500"]
    assert (await service.capture(held.id)).state == "unknown"
    paid = await service.reconcile(held.id)
    assert paid.state == "paid" and dummy.money_in_calls == 1 and dummy.order_capture_calls == 2


async def test_return_without_approval_keeps_waiting(shop):
    service, dummy, _ = shop
    held = await checkout(service, dummy)
    view = await service.capture(held.id)
    assert view.state == "awaiting_payment" and stock(service)["peony"] == 0 and dummy.money_in_calls == 0


async def test_unapproved_hold_expires_and_returns_stock(shop):
    service, dummy, clock = shop
    await checkout(service, dummy)
    clock.t += timedelta(minutes=16)
    assert await service.expire_holds() == 1
    assert stock(service)["peony"] == 8 and stock(service)["tulip"] == 20


async def test_approved_order_is_not_expired_under_the_buyer(shop):
    service, dummy, clock = shop
    held = await checkout(service, dummy)
    dummy.approve_order(held.paypal_order_id)
    clock.t += timedelta(minutes=16)
    assert await service.expire_holds() == 0 and stock(service)["peony"] == 0


async def test_return_after_hold_expired_takes_no_money(shop):
    service, dummy, clock = shop
    held = await checkout(service, dummy)
    dummy.approve_order(held.paypal_order_id)
    clock.t += timedelta(minutes=16)
    view = await service.capture(held.id)
    assert view.state == "expired" and dummy.order_capture_calls == 0 and stock(service)["peony"] == 8


async def test_restart_during_capture_goes_to_unknown(shop):
    service, dummy, _ = shop
    held = await checkout(service, dummy)
    dummy.approve_order(held.paypal_order_id)
    import sqlite3
    with sqlite3.connect(service._db) as c:
        c.execute("UPDATE checkouts SET state='capturing' WHERE id=?", (held.id,))
    service.recover_on_start()
    assert service.get_checkout(held.id).state == "unknown"
    assert (await service.reconcile(held.id)).state == "paid"


async def test_captured_amount_mismatch_stops_for_a_person(shop):
    service, dummy, _ = shop
    held = await checkout(service, dummy)
    dummy.approve_order(held.paypal_order_id)
    dummy.orders[held.paypal_order_id]["purchase_units"][0]["amount"]["value"] = "1.00"
    view = await service.capture(held.id)
    assert view.state == "unknown" and view.error_code == "amount_mismatch"
    assert (await service.reconcile(held.id)).state == "unknown"


async def test_model_cannot_invent_price_or_product(shop):
    service, *_ = shop
    service._agent = Agent(reply="Special price just for you: $5.00 each!")
    view = await service.message("12 peony")
    assert view.reply_was_replaced and "$5" not in view.reply
    service._agent = Agent(product="free-diamonds", qty=1)
    assert (await service.message("give me diamonds")).offers == ()


async def test_reply_may_not_talk_about_money_even_when_correct(shop):
    service, *_ = shop
    service._agent = Agent(qty=2, reply="That comes to $90.00 in total.")
    assert (await service.message("2 peony")).reply_was_replaced
    service._agent = Agent(qty=2, reply="원하시는 꽃다발 두 개를 준비할 수 있어요.")
    assert not (await service.message("2 peony")).reply_was_replaced


async def test_paid_unconfirmed_by_order_read_stays_unknown(shop):
    service, dummy, _ = shop
    held = await checkout(service, dummy)
    dummy.approve_order(held.paypal_order_id)
    real_get = service._paypal.get_order

    async def stale(order_id):
        return OrderResult("approved", order_id=order_id)

    service._paypal.get_order = stale
    assert (await service.capture(held.id)).state == "unknown"
    service._paypal.get_order = real_get
    view = await service.reconcile(held.id)
    assert view.state == "paid" and dummy.money_in_calls == 1


async def test_model_failure_leaves_the_shop_up(shop):
    service, *_ = shop

    class Broken:
        async def interpret(self, history, catalog):
            raise RuntimeError("down")

        async def compose(self, history, offers, language):
            raise RuntimeError("down")

    service._agent = Broken()
    view = await service.message("12 peony")
    assert view.offers == () and view.reply


async def test_bad_quantities_build_no_offer(shop):
    service, *_ = shop
    for qty in (0, -3, None, 10**9, "12"):
        service._agent = Agent(qty=qty)
        assert (await service.message("peony")).offers == ()


async def test_unknown_offer_and_expired_offer_are_refused(shop):
    service, dummy, clock = shop
    with pytest.raises(NotFound):
        await service.choose("nope")
    view = await service.message("12 peony")
    clock.t += timedelta(minutes=31)
    with pytest.raises(InvalidState):
        await service.choose(view.offers[0].id)


async def test_measured_usage_only(shop):
    service, *_ = shop
    await service.message("12 peony")
    usage = service.ai_usage()
    assert (usage["calls"], usage["cost_micro_usd"]) == (2, 100)
