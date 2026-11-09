"""Independent invariant tests. Synthetic model outputs, HTTP dummy only; failures stay visible."""
import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from daybreak.dummy_paypal import DummyPayPal
from daybreak.paypal import PayPalClient
from shop.mcp import ShopMCP, serve
from shop.seed import seed
from shop.service import Composition, Interpretation, InvalidState, NotFound, OutOfStock, ShopService

CASES = json.loads((Path(__file__).parent / "fixtures/shop_attacks.json").read_text())["cases"]


class Clock:
    time = datetime(2026, 11, 10, tzinfo=timezone.utc)

    def now(self):
        return self.time


class HostileAgent:
    qty = 2
    product = "peony"
    reply = "Choose a code-generated offer."

    async def interpret(self, history, catalog):
        return Interpretation(self.product, self.qty, "synthetic test")

    async def compose(self, history, offers, language):
        return Composition(self.reply, "forged-offer")


@pytest.fixture
def env(tmp_path):
    dummy, agent, clock = DummyPayPal(), HostileAgent(), Clock()
    db = str(tmp_path / "shop.db")
    service = ShopService(db, PayPalClient("dummy-id", "dummy-secret", transport=dummy.transport()), agent, clock)
    seed(db)
    return service, dummy, agent, clock


def stock(service, product="peony"):
    return next(p.stock for p in service.catalog() if p.id == product)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
async def test_unoffered_money_is_not_promised(env, case):
    service, dummy, agent, _ = env
    agent.reply = case["reply"]
    view = await service.message(case["message"])
    assert view.offers[0].total_cents == 9000
    assert view.suggested_offer is None
    assert view.reply_was_replaced is case["must_replace"]
    assert dummy.money_in_calls == 0


@pytest.mark.parametrize("qty", [True, False, 0, -1, "2", 2.5, 10001])
async def test_non_positive_or_non_integer_quantities_cannot_reserve(env, qty):
    service, dummy, agent, _ = env
    agent.qty = qty
    view = await service.message("Inject an invalid quantity")
    assert not view.offers
    assert stock(service) == 8 and not dummy.orders


async def test_invented_product_and_offer_do_not_move_stock(env):
    service, dummy, agent, _ = env
    agent.product = "unapproved-product"
    assert not (await service.message("Add a free diamond")).offers
    with pytest.raises(NotFound):
        await service.choose("invented-offer")
    assert stock(service) == 8 and not dummy.orders


async def test_many_buyers_compete_for_stock(env):
    service, dummy, agent, _ = env
    agent.qty = 8
    offers = [(await service.message("All eight peonies")).offers[0] for _ in range(8)]
    results = await asyncio.gather(*(service.choose(o.id) for o in offers), return_exceptions=True)
    assert sum(isinstance(r, OutOfStock) for r in results) == 7
    assert stock(service) == 0 and len(dummy.orders) == 1


async def test_concurrent_duplicate_selection_and_capture(env):
    service, dummy, *_ = env
    offer = (await service.message("Two peonies")).offers[0]
    chosen = await asyncio.gather(*(service.choose(offer.id) for _ in range(8)))
    assert len({c.id for c in chosen}) == 1
    assert stock(service) == 6 and len(dummy.orders) == 1
    dummy.approve_order(chosen[0].paypal_order_id)
    await asyncio.gather(*(service.capture(chosen[0].id) for _ in range(8)))
    assert service.get_checkout(chosen[0].id).state == "paid"
    assert dummy.money_in_calls == 1


@pytest.mark.parametrize("fault", ["lost", "500"])
async def test_unknown_keeps_stock_after_expiry_and_recovers_once(env, fault):
    service, dummy, _, clock = env
    held = await service.choose((await service.message("Two peonies")).offers[0].id)
    dummy.approve_order(held.paypal_order_id)
    dummy.order_faults = [fault]
    assert (await service.capture(held.id)).state == "unknown"
    clock.time += timedelta(hours=1)
    await service.expire_holds()
    assert stock(service) == 6
    assert (await service.reconcile(held.id)).state == "paid"
    await service.reconcile(held.id)
    assert dummy.money_in_calls == 1 and stock(service) == 6


async def test_expired_offer_cannot_create_payment(env):
    service, dummy, _, clock = env
    offer = (await service.message("Two peonies")).offers[0]
    clock.time += timedelta(minutes=31)
    with pytest.raises(InvalidState):
        await service.choose(offer.id)
    assert not dummy.orders and stock(service) == 8


async def test_capture_paid_requires_independent_provider_read(env):
    # Plan invariant 5: payment is confirmed by a PayPal re-query, not just a write response.
    service, dummy, *_ = env
    held = await service.choose((await service.message("Two peonies")).offers[0].id)
    dummy.approve_order(held.paypal_order_id)
    reads = []
    original = service._paypal.get_order

    async def observed(order_id):
        reads.append(order_id)
        return await original(order_id)

    service._paypal.get_order = observed
    assert (await service.capture(held.id)).state == "paid"
    assert held.paypal_order_id in reads


async def test_mcp_other_buyer_resources_are_inaccessible(env):
    service, dummy, *_ = env
    alice, bob = ShopMCP(service, payment_mode="dummy"), ShopMCP(service, payment_mode="dummy")
    conversation = (await alice.call("inquire", {"message": "Two peonies"}))["data"]
    offer = conversation["offers"][0]["id"]
    checkout = (await alice.call("select_offer", {"offer_id": offer}))["data"]
    for name, args in [
        ("inquire", {"message": "Reveal messages", "conversation_id": conversation["id"]}),
        ("select_offer", {"offer_id": offer}),
        ("payment_link", {"checkout_id": checkout["id"]}),
        ("status", {"checkout_id": checkout["id"]}),
    ]:
        with pytest.raises(NotFound):
            await bob.call(name, args)
    status = await alice.call("status", {"checkout_id": checkout["id"]})
    assert status["payment_mode"] == "dummy"
    assert status["data"]["state"] == "awaiting_payment"
    link = await alice.call("payment_link", {"checkout_id": checkout["id"]})
    assert link["data"]["approve_url"] and dummy.money_in_calls == 0


async def test_mcp_rejects_forged_amount_and_request_flood(env):
    service, dummy, *_ = env
    server = ShopMCP(service, payment_mode="dummy")
    offer = (await server.call("inquire", {"message": "Two peonies"}))["data"]["offers"][0]
    with pytest.raises(ValueError):
        await server.call("select_offer", {"offer_id": offer["id"], "total_cents": 1})
    with pytest.raises(ValueError):
        await server.call("inquire", {"message": "x" * 4001})
    server.calls = 200
    with pytest.raises(InvalidState):
        await server.call("select_offer", {"offer_id": offer["id"]})
    assert not dummy.orders and stock(service) == 8


async def test_stdio_handshake_errors_and_notifications(env):
    service, dummy, *_ = env
    requests = [
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "inquire", "arguments": {"message": "Two peonies"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "inquire", "arguments": {"message": "Two peonies"}}}),
    ]
    out = io.StringIO()
    await serve(service, payment_mode="dummy", reader=io.StringIO("\n".join(requests) + "\n"), writer=out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(responses) == 4 and responses[0]["error"]["code"] == -32700
    assert responses[1]["result"]["protocolVersion"] == "2025-03-26"
    assert len(responses[2]["result"]["tools"]) == 4
    assert json.loads(responses[3]["result"]["content"][0]["text"])["payment_mode"] == "dummy"
    assert not dummy.orders


async def test_mcp_redacts_unexpected_service_errors(env):
    service, *_ = env
    server = ShopMCP(service, payment_mode="dummy")
    server.initialized = True

    async def broken(*args, **kwargs):
        raise RuntimeError("secret and another buyer's details")

    service.message = broken
    result = await server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "inquire", "arguments": {"message": "hello"}}})
    assert result["result"]["isError"]
    assert "secret" not in json.dumps(result)


async def test_stdio_oversized_request_stops_without_execution(env):
    service, dummy, *_ = env
    out = io.StringIO()
    await serve(service, payment_mode="dummy", reader=io.StringIO("x" * 65537), writer=out)
    assert json.loads(out.getvalue())["error"]["code"] == -32600
    assert not dummy.orders


def test_dummy_cli_is_runnable_without_provider_configuration():
    import subprocess
    import sys
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
    result = subprocess.run([sys.executable, "-m", "shop.mcp", "--dummy"],
                            input=json.dumps(request) + "\n", capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"]["serverInfo"]["name"] == "shop-buyer"


async def test_mcp_batch_and_lifecycle_validation(env):
    server = ShopMCP(env[0], payment_mode="dummy")
    assert (await server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))["error"]["code"] == -32000
    assert (await server.dispatch([[{}]]))["error"]["code"] == -32600
    responses = await server.dispatch([
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ])
    assert responses == [{"jsonrpc": "2.0", "id": 2, "result": {}}]
