import time
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from daybreak.auth import make_order_token, make_seller_cookie
from daybreak.web import router
from test_integration import World, NOW
from datetime import timedelta


@pytest.fixture
async def web(tmp_path, monkeypatch):
    monkeypatch.setenv('DAYBREAK_SECRET', 'synthetic-test-signing-secret-32-characters')
    monkeypatch.setenv('DAYBREAK_SELLER_PASSWORD', 'synthetic-password')
    w=World(tmp_path/'web.db')
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
    assert '$39.00' in r.text and 'Review before submitting' in r.text
    draft=w.sql('SELECT id,version FROM request_drafts')[0]
    r=await c.post(link+'/confirm',data={'draft_id':draft[0],'draft_version':draft[1],'csrf':csrf(c)})
    assert 'pending approval' in r.text
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
        assert 'pending approval' in r.text
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
