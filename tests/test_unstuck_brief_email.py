"""Offline brief and human-operated email regressions."""
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest

from unstuck.brief_email import compose, money_brief
from unstuck.store import Store
from unstuck.scenarios import SCENARIOS


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv('UNSTUCK_AUTOCREATE', '0')
    monkeypatch.setenv('PAYPAL_CLIENT_ID', 'synthetic')
    monkeypatch.setenv('PAYPAL_CLIENT_SECRET', 'synthetic')
    monkeypatch.setenv('UNSTUCK_DB', str(tmp_path/'db'))
    monkeypatch.setenv('UNSTUCK_LOG', str(tmp_path/'log'))
    m = importlib.import_module('unstuck.app')
    monkeypatch.setattr(m, 'PayPalClient', lambda *a, **kw: object())
    inv = SimpleNamespace(get=AsyncMock(return_value={
        'id': 'INV', 'status': 'SENT', 'amount': {'currency_code': 'USD', 'value': '10.00'},
        'due_amount': {'currency_code': 'USD', 'value': '10.00'}}))
    monkeypatch.setattr(m, 'Invoicing', lambda _: inv)
    app = m.create_app()
    store = app.state.store
    store.add_case('case', 'harbor', 'INV', 'Client', 'Work')
    app.state.agent.resume = AsyncMock()
    def spawn(coro):
        coro.close()
    monkeypatch.setattr(m, 'spawn', spawn)
    return app, store, inv


@pytest.mark.parametrize('to', ['a@x.test\r\nBcc:b@x.test', 'a@x.test,b@x.test', 'a@x.test?cc=b@x.test', 'bad', 'a@x.test;b@x.test'])
def test_invalid_recipients(to):
    assert compose(to, 'Subject', 'Text') is None


def test_encoding():
    subject, body = '합의 & scope?', 'Hello + tax & fees\nQuote: "£10" #1'
    links = compose('ap+bill@client.example', subject, body)
    gmail = parse_qs(urlsplit(links['gmail']).query)
    assert gmail == {'view': ['cm'], 'fs': ['1'], 'to': ['ap+bill@client.example'], 'su': [subject], 'body': [body]}
    assert parse_qs(urlsplit(links['mailto']).query) == {'subject': [subject], 'body': [body]}
    assert compose('a@b.test', 'Subject\r\nBcc:', 'body') is None


def test_brief_all_modes_currency_dedup_and_missing(tmp_path):
    store = Store(str(tmp_path/'db'))
    for cid, mode in [('a', 'investigate'), ('b', 'scope'), ('c', 'unbilled')]:
        store.add_case(cid, 'harbor', cid, cid.upper(), 'work', mode=mode)
    q = store.add_question('a', 'q', 'ap@x.test', 'Why the hold?', 'Hold', [])
    store.set_question(q, state='sent')
    store.add_question('b', 'q2', 'ap@x.test', 'Is it included?', 'Scope', [])
    store.add_action('c', 'create_invoice', {'why': 'Agreed work remains unbilled.'})
    store.save_case('b', final={'status': 'NEEDS_OWNER', 'summary_for_owner': 'Agree the scope first.'})
    store.save_case('c', final={'status': 'EXHAUSTED', 'cause': 'Terms remain unclear.'})
    rows = [dict(id='a', invoice_id='a', client='A', live={'currency':'USD','due_cents':1000,'status':'SENT'}),
            dict(id='b', invoice_id='b', client='B', live={'currency':'EUR','due_cents':700,'status':'SENT'}),
            dict(id='copy', invoice_id='a', client='A', live={'currency':'USD','due_cents':1000,'status':'SENT'}),
            dict(id='x', invoice_id='x', client='X', live=None)]
    b = money_brief(store, rows)
    assert b['totals'] == {'USD':1000, 'EUR':700}
    assert b['unavailable'] == 1
    assert [x['id'] for x in b['groups']['Needs your approval']] == ['b', 'c']
    assert [x['id'] for x in b['groups']['Waiting on client']] == ['a']
    assert len(b['groups']["Don't bill yet / unclear"]) == 2
    assert b['read_at'].endswith('+00:00')


async def test_question_approve_compose_mark_and_case_paste(web):
    app, store, inv = web
    qid = store.add_question('case','ask','ap+one@client.example','Why & when?\nThanks!','Hold',[])
    store.save_case('case', state='needs_approval')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        assert (await c.post(f'/questions/{qid}/mark-sent')).status_code == 409
        r = await c.post(f'/questions/{qid}/approve?to=attacker@x.test', data={'to':'attacker@x.test','question':'changed'})
        assert r.status_code == 200
        assert store.question(qid)['state'] == 'ready_to_send'
        assert store.case('case')['state'] == 'needs_approval'
        data = (await c.get('/api/case/case')).json()
        q = data['questions'][0]
        assert parse_qs(urlsplit(q['compose']['gmail']).query)['to'] == ['ap+one@client.example']
        assert parse_qs(urlsplit(q['compose']['gmail']).query)['body'] == ['Why & when?\nThanks!']
        assert (await c.post(f'/questions/{qid}/reply', data={'reply':'too soon'})).status_code == 409
        assert (await c.post(f'/questions/{qid}/mark-sent')).status_code == 200
        assert (await c.post(f'/questions/{qid}/mark-sent')).status_code == 409
        assert store.case('case')['state'] == 'waiting_reply'
        assert (await c.post(f'/questions/{qid}/reply',data={'reply':'  '})).status_code == 400
        r = await c.post(f'/questions/{qid}/reply',data={'reply':'Tax form expired.','source':'case'})
        assert r.status_code == 303 and r.headers['location'] == '/case/case'
        assert store.question(qid)['reply'] == 'Tax form expired.'
        app.state.agent.resume.assert_called_once_with('case','ask',{'reply_id':f'reply-{qid}','from':'ap+one@client.example','text':'Tax form expired.'})
        assert (await c.post(f'/questions/{qid}/reply',data={'reply':'duplicate'})).status_code == 409
    labels = [e['label'] for e in store.events('case')]
    assert any('Ready to send' in x for x in labels)
    assert any('You marked as sent' in x for x in labels)
    assert not any('Question sent' in x for x in labels)


async def test_draft_agreement_uses_stored_payload_no_paypal(web):
    app, store, inv = web
    store.add_action('case','draft_reply',{'to':'ap@client.example','subject':'Scope & price','body':'Please agree.','why':'Agreement required.'})
    aid = store.actions('case')[0]['id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        assert (await c.post(f'/actions/{aid}/approve',data={'to':'attacker@x.test'})).status_code == 200
        inv.get.assert_not_awaited()
        a = (await c.get('/api/case/case')).json()['actions'][0]
        assert a['state'] == 'ready_to_send'
        assert parse_qs(urlsplit(a['compose']['gmail']).query)['su'] == ['Scope & price']
        assert parse_qs(urlsplit(a['compose']['gmail']).query)['to'] == ['ap@client.example']
        assert (await c.post(f'/actions/{aid}/mark-sent')).status_code == 200
        assert (await c.post(f'/actions/{aid}/mark-sent')).status_code == 409
        assert store.action(aid)['state'] == 'sent'


async def test_home_and_demo_wording(web):
    app, store, _ = web
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        text = (await c.get('/')).text
        for s in ['Weekly money brief', 'Needs your approval', 'Waiting on client', 'Owed', 'USD 10.00', 'Read at']:
            assert s in text
        text = (await c.get('/inbox')).text
        assert 'Simulated client inbox (demo)' in text
        assert 'does not send email' in text
        js = (await c.get('/static/app.js')).text
        assert "Paste the client's reply" in js and 'I sent it' in js
        assert 'Open in Gmail' in js and 'Open default mail app' in js
        assert 'Nothing is sent until you approve' not in js


def test_harbor_contract_matches_invoice():
    harbor = next(s for s in SCENARIOS if s['key'] == 'harbor')
    assert harbor['terms_days'] == 15
    assert 'within 15 days of the invoice date' in next(d['text'] for d in harbor['docs'] if d['kind']=='contract')


async def test_paste_uses_existing_length_validation_and_rate_limit(web):
    app, store, _ = web
    qid = store.add_question('case','ask','ap@client.example','Why?', 'Hold',[])
    store.set_question(qid,state='sent')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        r=await c.post(f'/questions/{qid}/reply',data={'source':'case','reply':'x'*2500})
        assert r.status_code == 303
        assert len(store.question(qid)['reply']) == 2000
        for _ in range(19):
            assert (await c.post('/questions/999/reply',data={'source':'case','reply':'x'})).status_code == 409
        assert (await c.post('/questions/999/reply',data={'source':'case','reply':'x'})).status_code == 429


async def test_invalid_stored_draft_cannot_be_approved(web):
    app, store, inv = web
    store.add_action('case','draft_reply',{'to':'a@b.test\r\nBcc:evil@x.test','body':'No','why':'Invalid'})
    aid=store.actions('case')[0]['id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.post(f'/actions/{aid}/approve')).status_code == 400
    assert store.action(aid)['state']=='proposed'
    inv.get.assert_not_awaited()
