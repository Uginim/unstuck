import time
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from daybreak.auth import make_order_token, make_seller_cookie
from daybreak.web import router
from daybreak.execute import Service
from daybreak.ports import AgentTurn
from test_integration import World, NOW
from datetime import timedelta


class ScriptedAgent:
    async def respond(self, history, session_name, options):
        return AgentTurn('refund', False, 'Refund request', 'Please choose an available option.', 'en', 'refund')


@pytest.fixture
async def web(tmp_path, monkeypatch):
    monkeypatch.setenv('DAYBREAK_SECRET', 'synthetic-test-signing-secret-32-characters')
    monkeypatch.setenv('DAYBREAK_SELLER_PASSWORD', 'synthetic-password')
    w=World(tmp_path/'web.db')
    w.service=Service(w.db,w.client,w.extractor,w.clock,agent=ScriptedAgent())
    app=FastAPI()
    app.state.service=w.service
    app.state.paypal_mode='dummy'
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        yield client,w
    await w.client.aclose()


def csrf(client): return client.cookies.get('daybreak_csrf')

def seller(client): client.cookies.set('daybreak_seller',make_seller_cookie())

def token(order='a',expires=None): return make_order_token(order,expires or int(time.time())+3600)


async def test_customer_confirmation_and_seller_approval(web):
    c,w=web
    link='/order/'+token()
    r=await c.get(link)
    assert 'Dummy PayPal, not Sandbox' in r.text
    r=await c.post(link+'/message',data={'message':'Please refund me','csrf':csrf(c)})
    assert '$39.00' in r.text and 'Choose what happens next' in r.text
    draft=w.sql('SELECT id,version FROM request_drafts')[0]
    r=await c.post(link+'/confirm',data={'draft_id':draft[0],'draft_version':draft[1],'csrf':csrf(c)})
    assert 'The seller will review your request' in r.text
    seller(c)
    request_id=w.sql('SELECT id FROM confirmed_requests')[0][0]
    r=await c.post('/seller/cases/'+request_id+'/approve',data={'csrf':csrf(c)})
    assert r.status_code==303
    r=await c.get(r.headers['location'])
    assert 'completed' in r.text and 'Dummy PayPal, not Sandbox' in r.text
    assert w.dummy.money_out_calls==1


async def test_login_cookie_and_csrf(web):
    c,w=web
    await c.get('/login')
    assert (await c.post('/login',data={'password':'synthetic-password'})).status_code==403
    assert (await c.post('/login',data={'password':'wrong','csrf':csrf(c)})).status_code==401
    r=await c.post('/login',data={'password':'synthetic-password','csrf':csrf(c)})
    assert r.status_code==303
    cookie=r.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert (await c.get('/seller')).status_code==200
    await c.post('/logout',data={'csrf':csrf(c)})
    assert (await c.get('/seller')).status_code==401
    w.no_refund()


@pytest.mark.parametrize('variant',['missing','expired','tampered','different_order','unauthenticated_seller','client_actor'])
async def test_case17_authorization_boundaries(web,variant):
    c,w=web
    await c.get('/login')
    if variant in ('missing','expired','tampered'):
        link={'missing':'invalid','expired':token(expires=int(time.time())-1),'tampered':token()+'bad'}[variant]
        r=await c.post('/order/'+link+'/message',data={'message':'Please refund me','csrf':csrf(c)})
        assert r.status_code==404
    elif variant=='different_order':
        w.order('b',3900)
        draft=await w.service.submit_message('a','Please refund me')
        r=await c.post('/order/'+token('b')+'/confirm',data={'draft_id':draft.draft_id,'draft_version':draft.version,'csrf':csrf(c)})
        assert r.status_code==404
    elif variant=='unauthenticated_seller':
        for url in ['/seller/cases/anything/approve','/seller/cases/anything/reconcile','/seller/delegations/stop']:
            assert (await c.post(url,data={'csrf':csrf(c)})).status_code==401
    else:
        draft=await w.service.submit_message('a','Please refund me')
        r=await c.post('/order/'+token()+'/confirm',data={'draft_id':draft.draft_id,'draft_version':draft.version,'csrf':csrf(c),'actor':'seller'})
        assert 'The seller will review your request' in r.text
    w.no_refund()


async def test_delegation_forms_and_unknown_recheck(web):
    c,w=web
    seller(c);await c.get('/seller')
    data={'per_refund':'50','total':'100','expires_at':(NOW+timedelta(hours=7)).isoformat(),'csrf':csrf(c)}
    assert (await c.post('/seller/delegations',data={**data,'per_refund':'0.001'})).status_code==422
    assert (await c.post('/seller/delegations',data=data)).status_code==303
    delegation=w.service.latest_delegation().id
    assert 'daybreak_delegation' not in c.cookies
    assert delegation in (await c.get('/seller')).text
    w.dummy.faults=['lost']
    case=await w.submit()
    r=await c.get('/seller/cases/'+case.request_id)
    assert 'Result unconfirmed' in r.text
    assert (await c.post('/seller/cases/'+case.request_id+'/reconcile',data={'csrf':csrf(c)})).status_code==303
    assert w.dummy.money_out_calls==1
    assert (await c.post('/seller/delegations/stop',data={'delegation_id':delegation,'csrf':csrf(c)})).status_code==303
    assert w.sql('SELECT stopped_at FROM delegations')[0][0]


async def test_stale_version_and_escaping(web):
    c,w=web
    link='/order/'+token();await c.get(link)
    draft=await w.service.submit_message('a','Please refund me')
    await w.service.edit_message(draft.draft_id,'a','Refund please')
    r=await c.post(link+'/confirm',data={'draft_id':draft.draft_id,'draft_version':draft.version,'csrf':csrf(c)})
    assert r.status_code==409 and 'Dummy PayPal, not Sandbox' in r.text
    w.sql('UPDATE orders SET session_name=?',('<script>alert(1)</script>',))
    r=await c.post(link+'/message',data={'message':'Please refund me','csrf':csrf(c)})
    assert '<script>alert(1)</script>' not in r.text
    assert '&lt;script&gt;' in r.text
    w.no_refund()


async def test_sensitive_pages_no_cache_or_referrer(web):
    c,w=web
    for url in ['/login','/seller','/order/'+token()]:
        r=await c.get(url)
        assert r.headers['cache-control']=='no-store'
        assert r.headers['referrer-policy']=='no-referrer'
        assert 'Dummy PayPal, not Sandbox' in r.text


async def test_unexpected_error_still_labels_dummy(web,monkeypatch):
    c,w=web
    seller(c)
    def broken(): raise RuntimeError('synthetic failure')
    monkeypatch.setattr(w.service,'list_cases',broken)
    r=await c.get('/seller')
    assert r.status_code==503
    assert 'Dummy PayPal, not Sandbox' in r.text
    assert 'synthetic failure' not in r.text


def test_main_includes_router(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from daybreak import main
    from daybreak.dummy_paypal import DummyPayPal
    from daybreak.paypal import PayPalClient
    from daybreak.extract import DummyExtractor
    dummy=DummyPayPal()
    client=PayPalClient('synthetic','synthetic',transport=dummy.transport())
    monkeypatch.setattr(main,'_paypal',lambda log:(client,'dummy'))
    monkeypatch.setattr('daybreak.extract.build_extractor',lambda log:DummyExtractor())
    monkeypatch.setenv('DAYBREAK_DB',str(tmp_path/'main.db'))
    monkeypatch.setenv('DAYBREAK_LOG',str(tmp_path/'requests.jsonl'))
    with TestClient(main.create_app()) as app:
        assert app.get('/health').json()['paypal_mode']=='dummy'
        assert 'Dummy PayPal, not Sandbox' in app.get('/login').text


async def test_restore_pause_and_explicit_resume(web):
    c,w=web
    await w.delegate()
    w.service.mark_restored()
    seller(c)
    r=await c.get('/seller')
    assert 'Automatic execution is paused' in r.text
    assert (await c.post('/seller/resume',data={'csrf':csrf(c)})).status_code==422
    assert w.service.auto_paused()
    assert (await c.post('/seller/resume',data={'csrf':csrf(c),'reviewed':'yes'})).status_code==303
    assert not w.service.auto_paused()
    w.dummy.faults=['lost']
    await w.submit()
    w.service.mark_restored()
    assert (await c.post('/seller/resume',data={'csrf':csrf(c),'reviewed':'yes'})).status_code==409
    assert w.service.auto_paused()


@pytest.mark.parametrize('outcome',['completed','pending','unknown'])
@pytest.mark.parametrize('signed_in',[False,True])
async def test_customer_result_never_exposes_seller_evidence(web,outcome,signed_in):
    c,w=web
    await w.delegate(per=2000 if outcome=='pending' else 5000)
    if outcome=='unknown': w.dummy.faults=['lost']
    if signed_in: seller(c)
    link='/order/'+token()
    await c.get(link)
    draft=await w.service.submit_message('a','Please refund me')
    response=await c.post(link+'/confirm',data={
        'draft_id':draft.draft_id,'draft_version':draft.version,'csrf':csrf(c),
    })
    assert response.status_code==200
    assert '$39.00' in response.text and 'Workshop order' in response.text
    for private in ('Seller allowed','Customer requested','Policy v3','Delegation:',
                    '$50.00','$100.00','$20.00','Reason:', 'over per refund',
                    'per_refund', 'total_limit', '2026-11-08T08:00:00'):
        assert private not in response.text
    assert BANNER_TEXT in response.text
    if outcome=='pending': assert 'The seller will review your request' in response.text
    if outcome=='unknown': assert 'Result unconfirmed' in response.text
    if outcome=='completed':
        assert 'Refund completed' in response.text
        assert w.sql('SELECT refund_id FROM refund_jobs')[0][0] in response.text


BANNER_TEXT='Dummy PayPal, not Sandbox'


async def test_navigation_checks_actual_seller_session(web):
    c,w=web
    assert 'Seller sign in' in (await c.get('/login')).text
    c.cookies.set('daybreak_seller','invalid-cookie')
    assert 'Seller sign in' in (await c.get('/login')).text
    seller(c)
    for path in ('/seller','/login','/order/'+token()):
        response=await c.get(path)
        assert 'Seller account' in response.text
        assert 'Seller sign in</a>' not in response.text
    await c.post('/logout',data={'csrf':csrf(c)})
    assert 'Seller sign in' in (await c.get('/login')).text


async def test_seller_reason_is_readable_and_retains_private_evidence(web):
    c,w=web
    await w.delegate()
    await w.submit()
    w.order('b',4900);w.order('c',2900)
    await w.submit('b')
    case=await w.submit('c')
    seller(c)
    for path in ('/seller','/seller/cases/'+case.request_id):
        response=await c.get(path)
        assert response.status_code==200
        assert 'Eligible, but beyond the automatic allowance available when reviewed.' in response.text
        assert '$12.00' in response.text
        assert 'over total' not in response.text and 'over_total' not in response.text
    detail=await c.get('/seller/cases/'+case.request_id)
    assert 'Seller allowed' in detail.text and 'Policy v3' in detail.text
    assert 'Current remaining automatic allowance: $12.00.' in detail.text


def test_all_contract_reasons_have_seller_copy():
    from typing import get_args
    from daybreak.domain import Reason
    from daybreak.web import SELLER_REASONS
    assert set(get_args(Reason))==set(SELLER_REASONS)
