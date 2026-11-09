"""HTTP contracts against an isolated dummy: no external PayPal or model calls."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from daybreak.dummy_paypal import DummyPayPal
from daybreak.paypal import PayPalClient
from shop.seed import seed
from shop.service import Composition, Interpretation, ShopService
from shop.web import router


class Clock:
    value = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)

    def now(self):
        return self.value


class ScriptedAgent:
    reply = 'Here are the available options.'

    async def interpret(self, history, catalog):
        return Interpretation('peony', 12, 'bouquets', model='scripted-test', input_tokens=100, output_tokens=10, cost_micro_usd=20)

    async def compose(self, history, offers, language):
        return Composition(self.reply, offers[0].id if offers else None)


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv('DAYBREAK_SECRET', 'synthetic-test-signing-value-32-characters')
    monkeypatch.setenv('DAYBREAK_SELLER_PASSWORD', 'test-only-password')
    db = str(tmp_path / 'shop.db')
    dummy, clock, agent = DummyPayPal(), Clock(), ScriptedAgent()
    svc = ShopService(db, PayPalClient('test', 'test', transport=dummy.transport()), agent, clock)
    seed(db)
    app = FastAPI()
    app.state.service, app.state.paypal_mode = svc, 'dummy'
    app.state.shop_dummy, app.state.agent_mode = dummy, 'dummy'
    app.include_router(router)
    with TestClient(app) as client:
        yield client, app, svc, dummy, clock, agent


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def offer(response):
    return re.search(r'name="offer" value="([^"]+)"', response.text)[1]


def start(client):
    page = client.get('/')
    return client.post('/message', data={'csrf': csrf(page), 'message': '12 peony bouquets for tomorrow'})


def select(client):
    page = start(client)
    return client.post('/choose', data={'csrf': csrf(page), 'offer': offer(page)})


def login(client):
    page = client.get('/seller/login')
    return client.post('/seller/login', data={'csrf': csrf(page), 'password': 'test-only-password'})


def test_buyer_offer_reservation_dummy_return_payment_seller(ui):
    client, app, svc, dummy, *_ = ui
    page = start(client)
    assert page.status_code == 200
    assert '8 × Peony bouquet' in page.text and '4 × Tulip bouquet' in page.text
    assert 'USD 476.00' in page.text and 'AI suggestion' in page.text
    assert 'Delivery dates are requests' in page.text
    selected = client.post('/choose', data={'csrf': csrf(page), 'offer': offer(page)})
    checkout = svc.list_checkouts()[0]
    assert selected.status_code == 200 and checkout.state == 'awaiting_payment'
    assert 'dummy.invalid' not in selected.text
    returned = client.post('/checkout/dummy-approve', data={'csrf': csrf(selected)})
    assert returned.status_code == 200 and dummy.money_in_calls == 0
    assert 'Returning alone is not proof of payment' in returned.text
    paid = client.post('/checkout/complete', data={'csrf': csrf(returned)})
    assert 'Simulated payment confirmed' in paid.text and dummy.money_in_calls == 1
    repeat = client.post('/checkout/complete', data={'csrf': csrf(returned)})
    assert repeat.status_code == 200 and dummy.money_in_calls == 1
    desk = login(client)
    assert desk.status_code == 200 and checkout.id in desk.text
    assert '0 available' in desk.text and '16 available' in desk.text
    assert 'Payment confirmed' in desk.text and 'AI cost' in desk.text
    assert 'Cost amounts are hidden' in desk.text and 'Usage-based total cost' not in desk.text
    assert 'Dummy PayPal, not Sandbox' in desk.text


def test_return_token_cancel_and_get_do_not_capture(ui):
    client, _, svc, dummy, *_ = ui
    page = select(client)
    checkout = svc.list_checkouts()[0]
    assert client.get('/paid?token=other').status_code == 404
    assert client.get('/paid').status_code == 404
    assert client.get('/paid', params={'token': checkout.paypal_order_id}).status_code == 200
    cancel = client.get('/cancel')
    assert 'not a confirmed cancellation' in cancel.text
    assert dummy.order_capture_calls == 0 and svc.get_checkout(checkout.id).state == 'awaiting_payment'
    result = client.post('/checkout/complete', data={'csrf': csrf(page)})
    assert 'Awaiting buyer approval' in result.text and dummy.money_in_calls == 0


def test_lost_response_retains_checkout_and_reconciles_once(ui):
    client, _, svc, dummy, *_ = ui
    page = select(client)
    checkout = svc.list_checkouts()[0]
    dummy.approve_order(checkout.paypal_order_id)
    dummy.order_faults = ['lost']
    result = client.post('/checkout/complete', data={'csrf': csrf(page)})
    assert 'Payment not yet confirmed' in result.text and dummy.money_in_calls == 1
    assert 'Your stock remains reserved' in result.text
    result = client.post('/checkout/refresh', data={'csrf': csrf(result)})
    assert 'Simulated payment confirmed' in result.text
    assert dummy.money_in_calls == 1 and dummy.order_capture_calls == 1


def test_another_browser_cannot_choose_or_read_checkout(ui):
    client, app, svc, dummy, *_ = ui
    offers = start(client)
    client.post('/choose', data={'csrf': csrf(offers), 'offer': offer(offers)})
    with TestClient(app) as other:
        other_page = other.get('/')
        assert other.get('/checkout').status_code == 404
        assert other.post('/choose', data={'csrf': csrf(other_page), 'offer': offer(offers)}).status_code == 404
        assert other.post('/checkout/complete', data={'csrf': csrf(other_page)}).status_code == 404
    assert dummy.money_in_calls == 0


def test_csrf_form_limits_and_script_escaping(ui):
    client, _, svc, dummy, _, agent = ui
    page = client.get('/')
    assert client.post('/message', data={'message': 'hello'}).status_code == 403
    assert client.post('/message', data={'csrf': '한글', 'message': 'hello'}).status_code == 403
    assert client.post('/message', data={'csrf': csrf(page), 'message': 'x' * 2001}).status_code == 422
    assert client.post('/message', content='x' * 40000, headers={'content-type': 'application/x-www-form-urlencoded'}).status_code == 413
    agent.reply = '<script>alert(1)</script>'
    result = client.post('/message', data={'csrf': csrf(page), 'message': '<script>alert(2)</script>'})
    assert '<script>alert' not in result.text and '&lt;script&gt;' in result.text
    assert "script-src 'self'" in result.headers['content-security-policy']
    assert result.headers['cache-control'] == 'no-store'
    assert 'HttpOnly' in result.headers['set-cookie'] and 'SameSite=lax' in result.headers['set-cookie']
    assert dummy.money_in_calls == 0


def test_seller_auth_required_and_buyer_never_sees_cost(ui):
    client, _, svc, dummy, *_ = ui
    assert client.get('/seller').status_code == 401
    assert client.post('/seller/expire').status_code == 401
    assert client.post('/seller/reconcile/anything').status_code == 401
    assert 'AI cost' not in start(client).text
    desk = login(client)
    assert 'Seller desk' in desk.text
    assert client.post('/seller/expire', data={'csrf': 'wrong'}).status_code == 403
    out = client.post('/seller/logout', data={'csrf': csrf(desk)})
    assert out.status_code == 200 and client.get('/seller').status_code == 401


def test_expired_hold_and_seller_timeline(ui):
    client, _, svc, dummy, clock, _ = ui
    select(client)
    clock.value += timedelta(minutes=16)
    desk = login(client)
    released = client.post('/seller/expire', data={'csrf': csrf(desk)})
    assert 'Reservation expired' in released.text and '8 available' in released.text
    assert '<ol id="timeline">' in released.text
    assert '<strong>Preparing checkout</strong>' in released.text
    assert ' hidden' not in released.text.split('<ol id="timeline">')[1].split('</ol>')[0]


def test_dummy_approval_is_disabled_in_sandbox_and_bad_urls_hidden(ui):
    client, app, svc, dummy, *_ = ui
    page = select(client)
    app.state.paypal_mode = 'sandbox'
    assert client.post('/checkout/dummy-approve', data={'csrf': csrf(page)}).status_code == 404
    page = client.get('/checkout')
    assert 'dummy.invalid' not in page.text and 'Approval link unavailable' in page.text
    assert 'PayPal Sandbox' in page.text


def test_no_usage_and_measured_cost_visibility(ui):
    client, app, svc, dummy, *_ = ui
    assert 'No measured model calls' in login(client).text
    start(client)
    app.state.paypal_mode, app.state.agent_mode = 'sandbox', 'live'
    desk = client.get('/seller')
    assert 'Usage-based total cost' in desk.text and 'USD 0.000020' in desk.text
    assert 'Average per measured conversation' in desk.text
    assert 'AI cost' not in client.get('/').text


def test_reply_replacement_and_stale_offer(ui):
    client, _, svc, dummy, clock, agent = ui
    agent.reply = 'Special price $1.00!'
    page = start(client)
    assert 'AI reply was replaced' in page.text
    assert 'Special price' not in page.text
    clock.value += timedelta(minutes=31)
    result = client.post('/choose', data={'csrf': csrf(page), 'offer': offer(page)})
    assert result.status_code == 409 and dummy.money_in_calls == 0


def test_existing_hold_cannot_be_hidden_by_second_checkout(ui):
    client, _, svc, dummy, *_ = ui
    page = start(client)
    ids = re.findall(r'name="offer" value="([^\"]+)"', page.text)
    client.post('/choose', data={'csrf': csrf(page), 'offer': ids[0]})
    first_id = svc.list_checkouts()[0].id
    result = client.post('/choose', data={'csrf': csrf(page), 'offer': ids[1]})
    assert result.status_code == 409
    assert first_id in client.get('/checkout').text
    assert len(svc.list_checkouts()) == 1 and len(dummy.orders) == 1


def test_static_assets_and_invalid_cookie(ui):
    client, *_ = ui
    client.cookies.set('shop_buyer', 'invalid')
    assert client.get('/').status_code == 200
    assert client.get('/static/shop/shop.css').status_code == 200
    assert client.get('/static/shop/timeline.js').status_code == 200


def test_only_sandbox_approval_links_are_rendered(ui, monkeypatch):
    from dataclasses import replace
    client, app, svc, *_ = ui
    select(client)
    original = svc.list_checkouts()[0]
    app.state.paypal_mode = 'sandbox'
    for url in ('javascript:alert(1)', 'https://www.sandbox.paypal.com.evil.example/approve', 'https://user@www.sandbox.paypal.com/approve'):
        monkeypatch.setattr(svc, 'get_checkout', lambda _, link=url: replace(original, approve_url=link))
        assert 'Continue to PayPal Sandbox' not in client.get('/checkout').text
    monkeypatch.setattr(svc, 'get_checkout', lambda _: replace(original, approve_url='https://www.sandbox.paypal.com/checkoutnow?token=TEST'))
    page = client.get('/checkout')
    assert 'Continue to PayPal Sandbox' in page.text
    assert 'https://www.sandbox.paypal.com/checkoutnow?token=TEST' in page.text
