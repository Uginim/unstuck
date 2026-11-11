"""Offline Unstuck regressions: scripted model, real SQLite, HTTP MockTransport."""
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from daybreak.logbook import Logbook
from daybreak.paypal import PayPalClient
from unstuck.agent import Investigator
from unstuck.invoicing import Invoicing, InvoicingError
from unstuck.store import Store


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('UNSTUCK_AUTOCREATE', '0')

    async def no_network(*args, **kwargs):
        raise AssertionError('Real HTTP is forbidden in Unstuck tests')

    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', no_network)
    monkeypatch.setattr(Investigator, 'call', no_network)


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / 'unstuck.db')
    store.add_case('case', 'test', 'INV-TEST', 'Client', 'Design')
    scenario = {
        'key': 'test', 'client': 'Client', 'title': 'Design',
        'contacts': {'ap@client.example': 'Accounts payable'},
        'docs': [
            {'id': 'mail', 'kind': 'mail', 'title': 'Payment',
             'text': 'From: ap@client.example\n\nNo payment is blocked. Please check the invoice.'},
            {'id': 'contract', 'kind': 'contract', 'title': 'Terms',
             'text': 'The supplier record is on hold. Send the updated form.'},
        ],
    }
    inv = SimpleNamespace(get=AsyncMock(return_value=invoice()), remind=AsyncMock(), allow_partial=AsyncMock())
    agent = Investigator(store, inv, [scenario], Logbook(None))
    return store, inv, agent


def invoice():
    return {
        'id': 'INV-TEST', 'status': 'SENT',
        'detail': {'invoice_number': 'TEST-1', 'currency_code': 'USD'},
        'amount': {'currency_code': 'USD', 'value': '100.00'},
        'due_amount': {'currency_code': 'USD', 'value': '100.00'},
        'payments': {'paid_amount': {'currency_code': 'USD', 'value': '0.00'}},
        'items': [{'name': 'Design', 'description': 'Final artwork', 'quantity': '1',
                   'unit_amount': {'currency_code': 'USD', 'value': '100.00'}}],
        'primary_recipients': [
            {'billing_info': {'email_address': 'ap@client.example', 'business_name': 'Client'}},
            {'billing_info': {'email_address': 'books@client.example'}},
        ],
        'configuration': {'partial_payment': {'allow_partial_payment': False}},
    }


def answer(**changes):
    return {
        'status': 'CONFIRMED', 'cause': 'The supplier record is on hold.',
        'quote': 'The supplier record is on hold.', 'quote_doc': 'contract',
        'open_question': '', 'summary_for_owner': 'Ask AP to release the hold.',
        'proposed_actions': [], **changes,
    }


def tool(tool_id, name, **args):
    return {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': args}


def script(agent, *turns):
    agent.call = AsyncMock(side_effect=[{'content': blocks} for blocks in turns])
    return agent.call


def question(id):
    return tool(id, 'ask_contact', to='ap@client.example', question='What blocks the supplier record?',
                hypothesis='Supplier hold', evidence_ids=['contract'])


def results(store):
    return [b for m in store.case('case')['messages'] if m['role'] == 'user' and isinstance(m['content'], list)
            for b in m['content'] if b.get('type') == 'tool_result']


@pytest.mark.parametrize('quote,doc,accepted', [
    ('The supplier record is on hold.', 'contract', True),
    ('The supplier record is\non hold.', 'contract', True),
    ('payment is blocked.', 'mail', False),
    ('The supplier record is on hold', 'contract', False),
    ('The supplier record is on hold. Send the updated form.', 'contract', False),
    ('The supplier record is on hold.', 'mail', False),
    ('A fabricated explanation.', 'missing', False),
])
def test_gate_requires_whole_sentence_from_cited_document(world, quote, doc, accepted):
    store, _, agent = world
    rejection = agent.gate(store.case('case'), answer(quote=quote, quote_doc=doc), 0)
    assert (rejection is None) == accepted


@pytest.mark.parametrize('used', [0, 1, 2])
def test_exhausted_only_after_question_budget(world, used):
    store, _, agent = world
    a = answer(status='EXHAUSTED', quote='', quote_doc='', open_question='Who owns the hold?')
    assert (agent.gate(store.case('case'), a, used) is None) == (used == 2)


@pytest.mark.parametrize('action', [
    {}, {'type': 'cancel', 'why': 'Requested'},
    {'type': 'send_reminder', 'why': 'Due'},
    {'type': 'send_reminder', 'why': 'Due', 'note': '  '},
    {'type': 'send_reminder', 'note': 'Please pay.'},
    {'type': 'draft_reply', 'why': 'Ask', 'to': 'ap@client.example'},
    {'type': 'draft_reply', 'why': 'Ask', 'to': ' ', 'body': 'Question'},
    {'type': 'draft_reply', 'why': 'Ask', 'to': 'ap@client.example', 'body': 42},
    {'type': 'allow_partial_payment', 'why': 'Agreed'},
    *[{'type': 'allow_partial_payment', 'why': 'Agreed', 'minimum_amount_usd': v}
      for v in [0, -1, True, '25', float('nan'), float('inf'), 0.001, 1.001]],
])
def test_gate_rejects_invalid_actions_without_crashing(world, action):
    store, _, agent = world
    assert agent.gate(store.case('case'), answer(proposed_actions=[action]), 2)


@pytest.mark.parametrize('action', [
    {'type': 'send_reminder', 'why': 'Due', 'note': 'Please pay the balance.'},
    {'type': 'allow_partial_payment', 'why': 'Agreed', 'minimum_amount_usd': 25.01},
    {'type': 'draft_reply', 'why': 'Ask', 'to': 'ap@client.example', 'body': 'Please confirm.'},
])
def test_gate_accepts_valid_actions(world, action):
    store, _, agent = world
    assert agent.gate(store.case('case'), answer(proposed_actions=[action]), 2) is None


@pytest.mark.parametrize('changes', [{'status': 'invented'}, {'quote': None}, {'cause': ''},
                                    {'proposed_actions': None}, {'proposed_actions': ['oops']}])
def test_gate_rejects_malformed_final(world, changes):
    store, _, agent = world
    assert agent.gate(store.case('case'), answer(**changes), 2)


async def test_pause_persists_then_restart_resume_includes_reply_and_pending_results(world):
    store, inv, agent = world
    first_call = script(agent, [tool('read', 'read_document', id='contract'), question('ask')])
    await agent.start('case')
    assert first_call.await_count == 1
    assert store.case('case')['state'] == 'needs_approval'
    q = store.questions('case')[0]
    assert q['state'] == 'needs_approval'
    assert len(store.case('case')['pending_results']) == 1
    assert not store.actions('case')
    store.set_question(q['id'], state='sent')
    store.save_case('case', state='waiting_reply')
    reply = 'The supplier form has expired.'
    store.set_question(q['id'], state='answered', reply=reply)
    restarted_store = Store(store.path)
    restarted = Investigator(restarted_store, inv, list(agent.scen.values()), Logbook(None))
    final = answer(quote=reply, quote_doc=f"reply-{q['id']}", cause=reply)
    call = script(restarted, [tool('finish', 'final_answer', **final)])
    await restarted.resume('case', 'ask', {'reply_id': f"reply-{q['id']}", 'text': reply})
    assert call.await_count == 1
    assert restarted_store.case('case')['state'] == 'done'
    assert restarted_store.case('case')['pending_results'] == []
    assert {r['tool_use_id'] for r in results(store)} == {'read', 'ask'}
    assert reply in restarted.docs(store.case('case'))[f"reply-{q['id']}"]['text']


async def test_question_budget_two_even_across_resume(world):
    store, _, agent = world
    script(agent, [question('q1')], [question('q2')], [question('q3')],
           [tool('end', 'final_answer', **answer(status='EXHAUSTED', quote='', quote_doc=''))])
    await agent.start('case')
    for expected in [1, 2]:
        assert len(store.questions('case')) == expected
        q = store.questions('case')[-1]
        store.set_question(q['id'], state='answered', reply='I do not know.')
        await agent.resume('case', q['tool_use_id'], {'text': 'I do not know.'})
    assert len(store.questions('case')) == 2
    assert store.case('case')['state'] == 'done'
    third = next(r for r in results(store) if r['tool_use_id'] == 'q3')
    assert 'budget' in json.loads(third['content'])['error']


async def test_duplicate_read_blocked_after_resume(world):
    store, _, agent = world
    script(agent, [tool('r1', 'read_document', id='contract'), question('q1')],
           [tool('r2', 'read_document', id='contract')], [tool('f', 'final_answer', **answer())])
    await agent.start('case')
    q = store.questions('case')[0]
    store.set_question(q['id'], state='skipped')
    await agent.resume('case', 'q1', {'reply': 'Owner skipped.'})
    assert len([e for e in store.events('case') if e['kind'] == 'read']) == 1
    second = json.loads(next(r['content'] for r in results(store) if r['tool_use_id'] == 'r2'))
    assert 'text' not in second and 'already read' in second['note']


async def test_third_invalid_final_cannot_bypass_gate(world):
    store, _, agent = world
    bad = answer(quote='An invented cause sentence.')
    script(agent, *[[tool(f'bad{i}', 'final_answer', **bad)] for i in range(3)],
           [tool('good', 'final_answer', **answer())])
    await agent.start('case')
    assert len([e for e in store.events('case') if e['kind'] == 'gate']) == 3
    assert store.case('case')['final']['quote'] == answer()['quote']


async def test_final_in_same_turn_cannot_skip_question_approval(world):
    store, _, agent = world
    script(agent, [question('q1'), tool('premature', 'final_answer', **answer())])
    await agent.start('case')
    assert store.case('case')['state'] == 'needs_approval'
    assert store.case('case')['final'] is None
    assert not store.actions('case')
    pending = store.case('case')['pending_results']
    assert json.loads(pending[0]['content'])['error']


@pytest.fixture
def paypal():
    clients = []

    def make(change=None, before=None, put_status=200):
        state = deepcopy(before or invoice())
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path == '/v1/oauth2/token':
                return httpx.Response(200, json={'access_token': 'fake-token', 'expires_in': 3600})
            assert request.url.path == '/v2/invoicing/invoices/INV-TEST'
            if request.method == 'GET':
                return httpx.Response(200, json=state)
            assert request.method == 'PUT'
            if put_status != 200:
                return httpx.Response(put_status, json={'name': 'UPDATE_FAILED'})
            state.update(json.loads(request.content))
            if change:
                change(state)
            return httpx.Response(200, json=state)

        client = PayPalClient('synthetic-id', 'synthetic-secret', transport=httpx.MockTransport(handle))
        clients.append(client)
        return Invoicing(client), requests

    return make, clients


async def test_allow_partial_preserves_complete_invoice_fields(paypal):
    make, clients = paypal
    inv, requests = make()
    try:
        after = await inv.allow_partial('INV-TEST', 2500)
        body = json.loads(next(r.content for r in requests if r.method == 'PUT'))
        for key in ['items', 'amount', 'primary_recipients', 'detail']:
            assert body[key] == invoice()[key]
        assert after['total_cents'] == 10000 and after['minimum_due_cents'] == 2500
        assert after['allow_partial_payment']
        assert [r.method for r in requests if r.url.path != '/v1/oauth2/token'] == ['GET', 'PUT', 'GET']
    finally:
        for c in clients:
            await c.aclose()


@pytest.mark.parametrize('field', ['total', 'item_name', 'item_description', 'item_currency',
                                   'recipient', 'second_recipient', 'business_name', 'minimum', 'minimum_currency', 'enabled'])
async def test_allow_partial_fails_loudly_on_changed_fields(paypal, field):
    def change(state):
        if field == 'total': state['amount']['value'] = '90.00'
        elif field == 'item_name': state['items'][0]['name'] = 'Changed'
        elif field == 'item_description': state['items'][0]['description'] = 'Other work'
        elif field == 'item_currency': state['items'][0]['unit_amount']['currency_code'] = 'EUR'
        elif field == 'recipient': state['primary_recipients'][0]['billing_info']['email_address'] = 'other@example.com'
        elif field == 'second_recipient': state['primary_recipients'].pop()
        elif field == 'business_name': state['primary_recipients'][0]['billing_info']['business_name'] = 'Other'
        elif field == 'minimum': state['configuration']['partial_payment']['minimum_amount_due']['value'] = '30.00'
        elif field == 'minimum_currency': state['configuration']['partial_payment']['minimum_amount_due']['currency_code'] = 'EUR'
        elif field == 'enabled': state['configuration']['partial_payment']['allow_partial_payment'] = False
    make, clients = paypal
    inv, _ = make(change)
    try:
        with pytest.raises(RuntimeError):
            await inv.allow_partial('INV-TEST', 2500)
    finally:
        for c in clients:
            await c.aclose()


async def test_allow_partial_propagates_paypal_failure(paypal):
    make, clients = paypal
    inv, requests = make(put_status=422)
    try:
        with pytest.raises(InvoicingError, match='422'):
            await inv.allow_partial('INV-TEST', 2500)
        assert requests[-1].method == 'PUT'
    finally:
        for c in clients:
            await c.aclose()


@pytest.mark.parametrize('minimum,due,status', [(0, '100.00', 'SENT'), (-1, '100.00', 'SENT'),
    (True, '100.00', 'SENT'), (25.5, '100.00', 'SENT'), (2500, '20.00', 'PARTIALLY_PAID'),
    (2500, '0.00', 'PAID'), (2500, '100.00', 'CANCELLED')])
async def test_allow_partial_rechecks_latest_invoice_before_write(paypal, minimum, due, status):
    make, clients = paypal
    before = invoice()
    before['status'] = status
    before['due_amount']['value'] = due
    inv, requests = make(before=before)
    try:
        with pytest.raises(ValueError):
            await inv.allow_partial('INV-TEST', minimum)
        assert not [r for r in requests if r.method == 'PUT']
    finally:
        for c in clients:
            await c.aclose()


@pytest.mark.parametrize('status', ['PAID', 'MARKED_AS_PAID', 'CANCELLED', 'REFUNDED'])
@pytest.mark.parametrize('kind', ['send_reminder', 'allow_partial_payment', 'draft_reply'])
async def test_execute_action_refuses_terminal_invoice(world, status, kind):
    store, inv, _ = world
    raw = invoice(); raw['status'] = status
    inv.get.return_value = raw
    app_module = importlib.import_module('unstuck.app')
    app = SimpleNamespace(state=SimpleNamespace(store=store, inv=inv))
    state, result = await app_module.execute_action(app, {'case_id': 'case', 'type': kind, 'params': {}})
    assert state == 'refused' and status in result['reason']
    inv.remind.assert_not_awaited(); inv.allow_partial.assert_not_awaited()


@pytest.mark.parametrize('due', ['0.00', None])
async def test_execute_reminder_refuses_nothing_due(world, due):
    store, inv, _ = world
    raw = invoice(); raw['due_amount'] = {'value': due} if due else {}
    inv.get.return_value = raw
    app = SimpleNamespace(state=SimpleNamespace(store=store, inv=inv))
    module = importlib.import_module('unstuck.app')
    state, _ = await module.execute_action(app, {'case_id': 'case', 'type': 'send_reminder', 'params': {'note': 'Pay'}})
    assert state == 'refused'
    inv.remind.assert_not_awaited()


@pytest.mark.parametrize('minimum', [0, -1, 100, 101])
async def test_execute_partial_refuses_minimum_not_below_due(world, minimum):
    store, inv, _ = world
    app = SimpleNamespace(state=SimpleNamespace(store=store, inv=inv))
    module = importlib.import_module('unstuck.app')
    state, _ = await module.execute_action(app, {'case_id': 'case', 'type': 'allow_partial_payment',
                                               'params': {'minimum_amount_usd': minimum}})
    assert state == 'refused'
    inv.allow_partial.assert_not_awaited()


@pytest.mark.parametrize('quote,accepted', [('No payment is blocked.', True), ('payment is blocked.', False)])
def test_gate_preserves_negation_across_wrapped_source_lines(world, quote, accepted):
    store, _, agent = world
    agent.scen['test']['docs'][0]['text'] = 'From: ap@client.example\n\nNo\npayment is blocked. Please check.'
    assert (agent.gate(store.case('case'), answer(quote=quote, quote_doc='mail'), 0) is None) == accepted


async def test_only_one_question_pauses_per_model_turn(world):
    store, _, agent = world
    script(agent, [question('first'), question('second')])
    await agent.start('case')
    assert len(store.questions('case')) == 1
    assert store.case('case')['state'] == 'needs_approval'
    result = store.case('case')['pending_results'][0]
    assert result['tool_use_id'] == 'second'
    assert 'one question at a time' in json.loads(result['content'])['error']


@pytest.mark.parametrize('kind,params', [
    ('send_reminder', {'note': 'Please pay the balance.'}),
    ('allow_partial_payment', {'minimum_amount_usd': 25}),
    ('draft_reply', {'to': 'ap@client.example', 'body': 'Please confirm.'}),
])
async def test_execute_action_valid_paths(world, kind, params):
    store, inv, _ = world
    app = SimpleNamespace(state=SimpleNamespace(store=store, inv=inv))
    module = importlib.import_module('unstuck.app')
    state, _ = await module.execute_action(app, {'case_id': 'case', 'type': kind, 'params': params})
    assert state == 'done'
    if kind == 'send_reminder':
        inv.remind.assert_awaited_once_with('INV-TEST', 'Reminder: invoice TEST-1', params['note'])
        assert inv.get.await_count == 2
        inv.allow_partial.assert_not_awaited()
    elif kind == 'allow_partial_payment':
        inv.allow_partial.assert_awaited_once_with('INV-TEST', 2500)
        inv.remind.assert_not_awaited()
    else:
        inv.remind.assert_not_awaited()
        inv.allow_partial.assert_not_awaited()


async def test_http_reply_reaches_reply_handler(world, monkeypatch):
    store, inv, _ = world
    module = importlib.import_module('unstuck.app')
    monkeypatch.setenv('PAYPAL_CLIENT_ID', 'synthetic-id')
    monkeypatch.setenv('PAYPAL_CLIENT_SECRET', 'synthetic-secret')
    monkeypatch.setattr(module, 'PayPalClient', lambda *a, **k: object())
    monkeypatch.setattr(module, 'Invoicing', lambda _: inv)
    monkeypatch.setattr(module, 'Store', lambda _: store)
    monkeypatch.setattr(module, 'Logbook', lambda _: Logbook(None))
    app = module.create_app()
    qid = store.add_question('case', 'ask', 'ap@client.example', 'Why the hold?', 'Hold', ['contract'])
    store.set_question(qid, state='sent')
    store.save_case('case', state='waiting_reply')
    spawned = []
    def collect(coro):
        spawned.append(coro)
        coro.close()  # Check routing without starting the agent in the background.
    monkeypatch.setattr(module, 'spawn', collect)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        response = await http.post(f'/questions/{qid}/reply', data={'reply': 'The supplier form has expired.'})
    assert response.status_code == 303
    assert store.question(qid)['state'] == 'answered'
    assert store.question(qid)['reply'] == 'The supplier form has expired.'
    assert len(spawned) == 1


@pytest.fixture
def unbilled(world):
    store, inv, agent = world
    agent.scen['test']['recipient'] = ('Alex Chen', 'ap@client.example')
    agent.scen['test']['docs'].append({'id': 'agreement', 'kind': 'mail', 'title': 'Approved extras',
        'text': 'Yes, I approve three episode cards for an additional USD 180.'})
    store.save_case('case', mode='unbilled')
    inv.search_recipient = AsyncMock(return_value=[invoice()])
    return store, inv, agent


def create_proposal(**changes):
    return {'type': 'create_invoice', 'why': 'Agreed and delivered extras are missing from invoices.',
            'currency': 'USD', 'recipient': 'ap@client.example', 'note': 'Approved additional artwork.',
            'items': [{'name': 'three episode cards', 'amount_usd': 180,
                       'quote': 'Yes, I approve three episode cards for an additional USD 180.',
                       'quote_doc': 'agreement'}], **changes}


def create_final(action=None):
    return answer(status='NEEDS_OWNER', quote=create_proposal()['items'][0]['quote'], quote_doc='agreement',
                  proposed_actions=[action or create_proposal()])


@pytest.mark.parametrize('mutation', ['wrong_amount', 'substring', 'missing_doc', 'other_recipient',
    'currency', 'empty_items', 'duplicate_items', 'invented_name', 'nan', 'negative', 'free', 'wrong_mode'])
def test_create_gate_rejects_unproved_amounts_and_identity(unbilled, mutation):
    store, _, agent = unbilled
    p = create_proposal()
    if mutation == 'wrong_amount': p['items'][0]['amount_usd'] = 18
    elif mutation == 'substring': p['items'][0]['quote'] = 'three episode cards for an additional USD 180.'
    elif mutation == 'missing_doc': p['items'][0]['quote_doc'] = 'absent'
    elif mutation == 'other_recipient': p['recipient'] = 'other@example.com'
    elif mutation == 'currency': p['currency'] = 'EUR'
    elif mutation == 'empty_items': p['items'] = []
    elif mutation == 'duplicate_items': p['items'] *= 2
    elif mutation == 'invented_name': p['items'][0]['name'] = 'Logo redesign'
    elif mutation == 'nan': p['items'][0]['amount_usd'] = float('nan')
    elif mutation == 'negative': p['items'][0]['amount_usd'] = -180
    elif mutation == 'free':
        p['items'][0]['quote'] = 'The three episode cards worth USD 180 are free.'
        agent.scen['test']['docs'][-1]['text'] = p['items'][0]['quote']
    elif mutation == 'wrong_mode': store.save_case('case', mode='investigate')
    assert agent.gate(store.case('case'), create_final(p), 0)


def test_create_gate_accepts_exact_agreement(unbilled):
    store, _, agent = unbilled
    assert agent.gate(store.case('case'), create_final(), 0) is None


@pytest.mark.parametrize('amount_text', ['USD 180.', '$180.', 'USD 180.00.', '$180.00.'])
def test_agreement_price_with_sentence_punctuation(unbilled, amount_text):
    store, _, agent = unbilled
    p = create_proposal()
    quote = 'Yes, I approve three episode cards for ' + amount_text
    agent.scen['test']['docs'][-1]['text'] = quote
    p['items'][0]['quote'] = quote
    final = create_final(p)
    final['quote'] = quote
    assert agent.gate(store.case('case'), final, 0) is None


@pytest.mark.parametrize('status', ['DRAFT', 'SENT', 'PAID', 'CANCELLED'])
async def test_create_proposal_blocks_already_invoiced_work(unbilled, status):
    store, inv, agent = unbilled
    existing = invoice()
    existing['status'] = status
    existing['items'] = [{'name': 'Episode card set', 'quantity': '3',
                          'unit_amount': {'currency_code': 'USD', 'value': '60.00'}}]
    inv.search_recipient.return_value = [existing]
    assert 'Already invoiced' in await agent.check_unbilled(store.case('case'), [create_proposal()])


async def test_loop_rechecks_duplicates_before_storing_proposal(unbilled):
    store, inv, agent = unbilled
    billed = invoice()
    billed['items'] = [{'name': 'Episode cards', 'quantity': '1',
                        'unit_amount': {'currency_code': 'USD', 'value': '180.00'}}]
    inv.search_recipient.return_value = [billed]
    script(agent, [tool('create', 'final_answer', **create_final())],
           [tool('none', 'final_answer', **answer(status='NOT_A_BLOCKER'))])
    await agent.start('case')
    assert store.case('case')['state'] == 'done'
    assert not store.actions('case')
    assert any('Already invoiced' in e['detail']['reason'] for e in store.events('case') if e['kind'] == 'gate')


async def test_search_adapter_paginates_gets_full_items_and_filters_exact_recipient():
    calls = []
    def handle(r):
        calls.append(r)
        if r.url.path == '/v1/oauth2/token':
            return httpx.Response(200, json={'access_token': 'fake', 'expires_in': 3600})
        if r.method == 'POST':
            assert json.loads(r.content) == {'recipient_email': 'ap@client.example'}
            page = int(r.url.params['page'])
            assert r.url.params['page_size'] == '100'
            return httpx.Response(200, json={'total_pages': 2, 'items': [{'id': f'INV-{page}'}]})
        raw = invoice(); raw['id'] = r.url.path.rsplit('/', 1)[-1]
        if raw['id'] == 'INV-2':
            raw['primary_recipients'] = [{'billing_info': {'email_address': 'ap@other.example'}}]
        return httpx.Response(200, json=raw)
    pp = PayPalClient('fake-id', 'fake-secret', transport=httpx.MockTransport(handle))
    try:
        found = await Invoicing(pp).search_recipient('ap@client.example')
        assert [i['id'] for i in found] == ['INV-1']
        assert found[0]['items'] == invoice()['items']
        assert [r.method for r in calls] == ['POST', 'POST', 'GET', 'POST', 'GET']
    finally:
        await pp.aclose()


@pytest.mark.parametrize('body', [{}, {'items': None}, {'total_pages': 2, 'items': []}])
async def test_search_adapter_fails_closed_on_incomplete_results(body):
    pp = PayPalClient('fake-id', 'fake-secret', transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'access_token': 'fake', 'expires_in': 3600}
                                if r.url.path.endswith('/token') else body)))
    try:
        with pytest.raises(RuntimeError):
            await Invoicing(pp).search_recipient('ap@client.example')
    finally:
        await pp.aclose()


async def test_create_adapter_reuses_request_ids_on_replay():
    keys, ids = [], set()
    def handle(r):
        if r.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'fake', 'expires_in': 3600})
        if r.method == 'POST':
            keys.append((r.url.path, r.headers['PayPal-Request-Id']))
            if r.url.path.endswith('/invoices'):
                ids.add(r.headers['PayPal-Request-Id'])
                return httpx.Response(201, json={'id': 'INV-TEST'})
            return httpx.Response(202)
        return httpx.Response(200, json=invoice())
    pp = PayPalClient('fake-id', 'fake-secret', transport=httpx.MockTransport(handle))
    try:
        inv = Invoicing(pp)
        for _ in range(2):
            await inv.create_and_send(number='SAME', recipient_email='ap@client.example', recipient_name='Alex Chen',
                items=[('Episode cards', 18000)], note='Agreed', request_id='stable-approval-id')
        assert len(ids) == 1
        assert keys[:2] == keys[2:]
        assert keys[0][1] != keys[1][1]
    finally:
        await pp.aclose()


@pytest.fixture
def unbilled_app(unbilled, monkeypatch):
    store, inv, agent = unbilled
    module = importlib.import_module('unstuck.app')
    monkeypatch.setenv('PAYPAL_CLIENT_ID', 'fake-id')
    monkeypatch.setenv('PAYPAL_CLIENT_SECRET', 'fake-secret')
    monkeypatch.setenv('UNSTUCK_SWEEP_HOURS', '0')
    monkeypatch.setattr(module, 'PayPalClient', lambda *a, **k: object())
    monkeypatch.setattr(module, 'Invoicing', lambda _: inv)
    monkeypatch.setattr(module, 'Store', lambda _: store)
    monkeypatch.setattr(module, 'Logbook', lambda _: Logbook(None))
    app = module.create_app()
    app.state.agent = agent
    return app, module


async def test_create_approval_double_click_creates_and_sends_once(unbilled_app):
    import asyncio
    app, _ = unbilled_app
    store, inv = app.state.store, app.state.inv
    final = create_final()
    store.save_case('case', final=final, state='done')
    store.add_action('case', 'create_invoice', final['proposed_actions'][0])
    aid = store.actions('case')[0]['id']
    created = invoice()
    created['id'] = 'INV-NEW'
    created['amount']['value'] = created['due_amount']['value'] = '180.00'
    created['detail']['metadata'] = {'recipient_view_url': 'https://www.sandbox.paypal.com/invoice/p/#TEST'}
    created['items'] = [{'name': 'three episode cards', 'quantity': '1',
                         'unit_amount': {'currency_code': 'USD', 'value': '180.00'}}]
    async def create(**kwargs):
        await asyncio.sleep(.01)
        return created
    inv.create_and_send = AsyncMock(side_effect=create)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        responses = await asyncio.gather(*(client.post(f'/actions/{aid}/approve') for _ in range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert store.action(aid)['state'] == 'done'
    inv.create_and_send.assert_awaited_once()
    assert inv.create_and_send.call_args.kwargs['request_id']
    from unstuck.unbilled import agreement_refs
    assert inv.create_and_send.call_args.kwargs['references'] == agreement_refs(final['proposed_actions'][0])
    assert store.case('inv-INV-NEW')
    assert store.actions('case')[0]['result']['invoice']['payer_view_url'].startswith('https://')


async def test_create_execution_rechecks_new_duplicate(unbilled_app):
    app, module = unbilled_app
    store, inv = app.state.store, app.state.inv
    store.save_case('case', final=create_final())
    raw = invoice()
    raw['items'] = [{'name': 'Episode cards', 'quantity': '1',
                     'unit_amount': {'currency_code': 'USD', 'value': '180.00'}}]
    inv.search_recipient.return_value = [raw]
    inv.create_and_send = AsyncMock()
    state, result = await module.execute_action(app, {'id': 1, 'case_id': 'case', 'type': 'create_invoice',
                                                     'params': create_proposal()})
    assert state == 'refused' and 'Already invoiced' in result['reason']
    inv.create_and_send.assert_not_awaited()


async def test_sweep_sequential_per_client_no_money_actions(unbilled_app, monkeypatch):
    app, module = unbilled_app
    store, agent = app.state.store, app.state.agent
    store.save_case('case', mode='investigate')
    store.add_case('second', 'test', 'INV-SECOND', 'Client', 'Second invoice')
    order = []
    async def start(case_id):
        order.append(case_id)
        store.save_case(case_id, state='needs_approval')
    agent.start = AsyncMock(side_effect=start)
    await module.sweep(app, lambda: True)
    child = store.unbilled_case(store.case('case'), 'ap@client.example')
    assert order == ['case', child['id'], 'second']
    assert store.case('case')['state'] == 'needs_approval'
    assert store.case(child['id'])['state'] == 'needs_approval'
    assert not store.actions('case')
    app.state.inv.remind.assert_not_awaited()
    app.state.inv.allow_partial.assert_not_awaited()
    assert not app.state.sweeps['']['running']


async def test_sweep_stops_at_budget_and_does_not_start_remaining(unbilled_app, monkeypatch):
    app, module = unbilled_app
    store = app.state.store
    store.save_case('case', mode='investigate')
    monkeypatch.setattr(module, 'DAILY_USD', .01)
    async def start(case_id):
        store.add_spend(case_id, .01)
        store.save_case(case_id, state='done')
    app.state.agent.start = AsyncMock(side_effect=start)
    await module.sweep(app, lambda: True)
    app.state.agent.start.assert_awaited_once_with('case')
    child = store.unbilled_case(store.case('case'), 'ap@client.example')
    assert child['state'] == 'new'
    assert 'budget' in app.state.sweeps['']['status']


async def test_sweep_skips_paid_and_respects_hourly_limit(unbilled_app):
    app, module = unbilled_app
    app.state.store.save_case('case', mode='investigate')
    app.state.agent.start = AsyncMock()
    await module.sweep(app, lambda: False)
    app.state.agent.start.assert_not_awaited()
    assert 'Hourly' in app.state.sweeps['']['status']
    app.state.inv.get.return_value['status'] = 'PAID'
    await module.sweep(app, lambda: True)
    app.state.agent.start.assert_not_awaited()


async def test_new_endpoints_share_t2_model_start_limit(unbilled_app, monkeypatch):
    app, module = unbilled_app
    monkeypatch.setattr(module, 'spawn', lambda coro: coro.close())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for _ in range(20):
            assert (await client.post('/case/missing/unbilled')).status_code == 404
        assert (await client.post('/sweep')).status_code == 429


def test_unbilled_child_persists_without_overwriting_investigation(world):
    store, _, _ = world
    store.save_case('case', state='waiting_reply', messages=[{'role': 'user', 'content': 'Original'}])
    child = store.unbilled_case(store.case('case'), 'ap@client.example', create=True)
    restarted = Store(store.path)
    assert restarted.case('case')['state'] == 'waiting_reply'
    assert restarted.case('case')['messages'][0]['content'] == 'Original'
    assert restarted.unbilled_case(store.case('case'), 'ap@client.example')['id'] == child['id']
    assert [c['id'] for c in restarted.cases()] == ['case']


async def test_sweep_stops_when_next_call_will_not_fit_budget(unbilled_app):
    app, module = unbilled_app
    store = app.state.store
    store.save_case('case', mode='investigate')
    async def start(case_id):
        store.event(case_id, 'error', 'Budget reached', {'budget_exceeded': True})
        store.save_case(case_id, state='error')
    app.state.agent.start = AsyncMock(side_effect=start)
    await module.sweep(app, lambda: True)
    app.state.agent.start.assert_awaited_once_with('case')
    assert 'budget' in app.state.sweeps['']['status']


async def test_periodic_sweep_uses_same_sequential_worker(unbilled_app, monkeypatch):
    import asyncio
    app, module = unbilled_app
    sleeps = []
    async def tick(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError
    worker = AsyncMock()
    monkeypatch.setattr(module.asyncio, 'sleep', tick)
    monkeypatch.setattr(module, 'sweep', worker)
    allow = lambda: True
    with pytest.raises(asyncio.CancelledError):
        await module.periodic_sweep(app, 2, allow)
    assert sleeps == [7200, 7200]
    worker.assert_awaited_once_with(app, allow)


async def test_home_exposes_triage_and_unbilled_approval_count(unbilled_app):
    app, module = unbilled_app
    store = app.state.store
    store.save_case('case', mode='investigate', state='done', final=answer())
    child = store.unbilled_case(store.case('case'), 'ap@client.example', create=True)
    store.save_case(child['id'], state='done', final=create_final())
    store.add_action(child['id'], 'create_invoice', create_proposal())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/')
    assert response.status_code == 200
    assert 'Check all invoices' in response.text and 'Check for unbilled work' in response.text
    assert '1 approval(s) need you' in response.text
    assert 'The supplier record is on hold.' in response.text
    assert f'/case/{child["id"]}' in response.text

# Scope checks reuse the investigation transcript and approval routes, without money actions.
@pytest.fixture
def scope_world(world):
    from unstuck.scenarios import SCENARIOS
    store, inv, agent = world
    scenario = deepcopy(next(s for s in SCENARIOS if s['key'] == 'cedar'))
    agent.scen['cedar'] = scenario
    store.save_case('case', scenario='cedar', mode='scope')
    return store, inv, agent


def scope_finding(classification='paid_change'):
    included = classification == 'included'
    return dict(classification=classification,
                name='cover title color' if included else 'three episode cards',
                contract_doc='cl-c1',
                contract_quote=('Cover title color changes are included in two revision rounds.' if included else
                                'Three episode cards are outside the cover artwork scope and require a separate written agreement before work starts.'),
                request_doc='cl-m1',
                request_quote=('Please revise the cover title color in our second revision round.' if included else
                               'Please create three episode cards using the cover title color.'),
                reason='Compare the signed scope with the new request.',
                price_doc='' if included else 'cl-r1',
                price_quote='' if included else 'Three episode cards cost USD 180.',
                timing_doc='', timing_quote='', **({} if included else {'amount_usd': 180}))


def scope_final(*findings):
    return answer(status='NEEDS_OWNER', quote='Cover title color changes are included in two revision rounds.',
                  quote_doc='cl-c1', scope_findings=list(findings or [scope_finding()]))


@pytest.mark.parametrize('classification', ['included', 'paid_change', 'unclear'])
async def test_scope_three_classes_use_same_loop(scope_world, classification):
    store, inv, agent = scope_world
    f = scope_finding(classification)
    if classification == 'unclear':
        f.update(contract_doc='', contract_quote='', price_doc='', price_quote='')
        f.pop('amount_usd')
    final = scope_final(f)
    script(agent, [tool('f', 'final_answer', **final)])
    await agent.start('case')
    assert store.case('case')['final']['scope_findings'][0]['classification'] == classification
    actions = store.actions('case')
    assert len(actions) == (classification == 'paid_change')
    if actions:
        assert actions[0]['type'] == 'draft_reply'
        assert 'USD 180.00' in actions[0]['params']['body']
        assert 'Not agreed' in actions[0]['params']['body']
    inv.get.assert_not_awaited()
    inv.remind.assert_not_awaited()
    inv.allow_partial.assert_not_awaited()


@pytest.mark.parametrize('mutation', ['invented_price', 'missing_price', 'request_fragment', 'contract_fragment',
    'wrong_kind', 'price_from_mail', 'negative_price', 'negative_inclusion', 'invented_timing',
    'money_action', 'invented_name', 'duplicate_request'])
def test_scope_gate_rejects_ungrounded_or_negative_claims(scope_world, mutation):
    store, _, agent = scope_world
    f = scope_finding()
    final = scope_final(f)
    docs = agent.scen['cedar']['docs']
    if mutation == 'invented_price': f['amount_usd'] = 250
    if mutation == 'missing_price': f['price_quote'] = ''
    if mutation == 'request_fragment': f['request_quote'] = 'create three episode cards using the cover title color.'
    if mutation == 'contract_fragment': f['contract_quote'] = 'episode cards are outside the cover artwork scope.'
    if mutation == 'wrong_kind': f['contract_doc'] = 'cl-r1'
    if mutation == 'price_from_mail': docs[2]['kind'] = 'mail'
    if mutation == 'negative_price':
        docs[2]['text'] = f['price_quote'] = 'Three episode cards do not cost USD 180.'
    if mutation == 'negative_inclusion':
        f = scope_finding('included')
        docs[1]['text'] = f['contract_quote'] = 'Cover title color changes are not included in two revision rounds.'
        final = scope_final(f)
        final.update(quote=f['contract_quote'])
    if mutation == 'invented_timing': f.update(timing_doc='cl-r1', timing_quote='Delivery is tomorrow.')
    if mutation == 'money_action': final['proposed_actions'] = [{'type': 'send_reminder', 'why': 'Pay', 'note': 'Pay'}]
    if mutation == 'invented_name': f['name'] = 'ten video trailers'
    if mutation == 'duplicate_request': final['scope_findings'].append(deepcopy(f))
    assert agent.gate(store.case('case'), final, 0)


def test_scope_unpriced_change_needs_owner_without_draft(scope_world):
    from unstuck.scope import change_drafts
    store, _, agent = scope_world
    f = scope_finding()
    f.update(price_quote='', price_doc='')
    f.pop('amount_usd')
    final = scope_final(f)
    assert agent.gate(store.case('case'), final, 0) is None
    assert change_drafts(final, agent.docs(store.case('case')), 'riley@cedarlantern.example.com') == []
    final['status'] = 'CONFIRMED'
    assert agent.gate(store.case('case'), final, 0)


async def test_scope_http_duplicate_guard_reply_resume_and_approval(unbilled_app):
    import asyncio
    from unstuck.scenarios import SCENARIOS
    app, module = unbilled_app
    store, inv, agent = app.state.store, app.state.inv, app.state.agent
    cedar = deepcopy(next(s for s in SCENARIOS if s['key'] == 'cedar'))
    agent.scen['cedar'] = cedar
    store.save_case('case', mode='investigate', scenario='cedar')
    final = scope_final(scope_finding('included'), scope_finding())
    script(agent, [tool('q', 'ask_contact', to='riley@cedarlantern.example.com',
                       question='Does the three episode cards request require written change approval?',
                       hypothesis='New scope', evidence_ids=['cl-c1', 'cl-m1'])],
           [tool('f', 'final_answer', **final)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        responses = await asyncio.gather(*(client.post('/case/case/scope') for _ in range(2)))
        assert all(r.status_code == 303 for r in responses)
        assert responses[0].headers['location'] == responses[1].headers['location']
        await asyncio.gather(*list(module.TASKS))
        child = store.scope_case(store.case('case'))
        assert child['state'] == 'needs_approval'
        assert len(store.questions(child['id'])) == 1
        qid = store.questions(child['id'])[0]['id']
        assert (await client.post(f'/questions/{qid}/approve')).status_code == 200
        assert (await client.post(f'/questions/{qid}/mark-sent')).status_code == 200
        assert (await client.get('/inbox')).status_code == 200
        assert (await client.post(f'/questions/{qid}/reply', data={'reply': cedar['contact_replies']['riley@cedarlantern.example.com']})).status_code == 303
        await asyncio.gather(*list(module.TASKS))
        assert store.case(child['id'])['state'] == 'done'
        inv.get.reset_mock()
        action = store.actions(child['id'])[0]
        approved = await client.post(f"/actions/{action['id']}/approve")
        assert approved.json()['state'] == 'ready_to_send'
        inv.get.assert_not_awaited()
        assert (await client.post(f"/actions/{action['id']}/approve")).status_code == 409
        assert (await client.post(f"/case/{child['id']}/investigate")).status_code == 409
        assert 'Check new requests' in (await client.get('/')).text
        assert 'New requests check' in (await client.get(f"/case/{child['id']}")).text
    assert store.case('case')['state'] == 'new'
    assert Store(store.path).scope_case(store.case('case'))['id'] == child['id']
    assert agent.call.await_count == 2


async def test_scope_execution_blocks_forged_money_action(unbilled_app):
    app, module = unbilled_app
    store, inv = app.state.store, app.state.inv
    store.save_case('case', mode='scope', final=scope_final())
    for kind in ('send_reminder', 'allow_partial_payment', 'create_invoice'):
        state, result = await module.execute_action(app, {'case_id': 'case', 'type': kind, 'params': {}})
        assert state == 'refused'
    inv.get.assert_not_awaited()


def test_scope_exact_sentence_preserves_wrapped_negation(scope_world):
    store, _, agent = scope_world
    agent.scen['cedar']['docs'][1]['text'] = 'Cover title color changes are not\nincluded in two revision rounds.'
    f = scope_finding('included')
    final = scope_final(f)
    assert agent.gate(store.case('case'), final, 0)


async def test_scope_approval_revalidates_stored_price(unbilled_app):
    from unstuck.scenarios import SCENARIOS
    from unstuck.scope import change_drafts
    app, _ = unbilled_app
    store, inv, agent = app.state.store, app.state.inv, app.state.agent
    agent.scen['test'] = {**deepcopy(next(s for s in SCENARIOS if s['key'] == 'cedar')), 'key': 'test'}
    final = scope_final()
    store.save_case('case', mode='scope', state='done', final=final)
    proposal = change_drafts(final, agent.docs(store.case('case')), 'riley@cedarlantern.example.com')[0]
    proposal['body'] = proposal['body'].replace('180.00', '999.00')
    store.add_action('case', 'draft_reply', proposal)
    aid = store.actions('case')[0]['id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post(f'/actions/{aid}/approve')).status_code == 409
    inv.get.assert_not_awaited()


async def test_scope_endpoint_uses_model_start_limit(unbilled_app):
    app, _ = unbilled_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for _ in range(20):
            assert (await client.post('/case/missing/scope')).status_code == 404
        assert (await client.post('/case/missing/scope')).status_code == 429


@pytest.mark.parametrize('history,expected', [(None, None), ([], 0), ([{'event_type': 'REMINDER_SENT'}], 1)])
def test_missing_paypal_reminder_history_is_not_zero(history, expected):
    from unstuck.invoicing import summarize
    raw = invoice()
    if history is not None:
        raw['detail']['metadata'] = {'history': history}
    assert summarize(raw)['reminders_sent'] == expected

# Draft honesty: rejection uses the same tool-result retry as all other final-answer checks.
@pytest.mark.parametrize('claim', [
    'Please find attached the W-9.', 'I am attaching the W-9.', 'The W-9 is enclosed.',
    'I have sent the W-9.', "I've submitted the form.", 'I’ve uploaded the document.',
    'I have updated the PO.', "I've completed the work.", 'I have signed the contract.',
    "I've paid the invoice.", 'The PO has been updated.', 'The form has been submitted.',
    'The email has been sent.',
])
@pytest.mark.parametrize('kind', ['draft_reply', 'send_reminder'])
def test_draft_completed_claim_requires_evidence(world, claim, kind):
    store, _, agent = world
    action = dict(type=kind, why='Follow up', to='ap@client.example', body=claim, note=claim)
    assert 'Rejected:' in agent.gate(store.case('case'), answer(proposed_actions=[action]), 0)


@pytest.mark.parametrize('text', [
    'I can send the updated W-9 through your portal — where should it go?',
    'Please update the PO to include the approved templates.',
])
def test_honest_next_step_draft_passes(world, text):
    store, _, agent = world
    a = dict(type='draft_reply', why='Next step', to='ap@client.example', body=text)
    assert agent.gate(store.case('case'), answer(proposed_actions=[a]), 0) is None


def test_client_quote_exempt_but_not_own_claim(world):
    store, _, agent = world
    claim = 'I have sent the W-9.'
    agent.scen['test']['docs'][0]['text'] = 'From: ap@client.example\n\n' + claim
    a = dict(type='draft_reply', why='Clarify', to='ap@client.example', body=f'You wrote: “{claim}” Please confirm receipt.')
    assert agent.gate(store.case('case'), answer(proposed_actions=[a]), 0) is None
    a['body'] = claim
    assert agent.gate(store.case('case'), answer(proposed_actions=[a]), 0)
    a['body'] = 'You wrote: “I have submitted the form.”'
    assert agent.gate(store.case('case'), answer(proposed_actions=[a]), 0)


@pytest.mark.parametrize('source', ['work', 'reply', 'result', 'pending'])
def test_recorded_completion_and_pending_are_distinct(world, source):
    store, _, agent = world
    claim = 'The PO has been updated.'
    if source in ('work', 'reply'):
        agent.scen['test']['docs'].append(dict(id='done', kind=source, text=claim))
    else:
        store.add_action('case', 'allow_partial_payment', {'why': 'test'})
        aid = store.actions('case')[-1]['id']
        store.set_action(aid, 'done' if source == 'result' else 'proposed', {'note': claim})
    a = dict(type='draft_reply', why='Follow up', to='ap@client.example', body=claim)
    assert (agent.gate(store.case('case'), answer(proposed_actions=[a]), 0) is None) == (source != 'pending')


async def test_draft_honesty_retries_without_persisting_bad_proposal(world):
    store, _, agent = world
    bad = dict(type='draft_reply', why='Follow up', to='ap@client.example', body='Please find attached the W-9.')
    good = {**bad, 'body': 'Where should I submit the W-9?'}
    script(agent, [tool('bad', 'final_answer', **answer(proposed_actions=[bad]))],
           [tool('good', 'final_answer', **answer(proposed_actions=[good]))])
    await agent.start('case')
    assert [a['params']['body'] for a in store.actions('case')] == [good['body']]
    assert any('attachments' in r['content'] for r in results(store))


def test_scope_draft_runs_honesty_guard(scope_world, monkeypatch):
    import unstuck.agent as module
    store, _, agent = scope_world
    monkeypatch.setattr(module, 'change_drafts', lambda *args: [dict(
        type='draft_reply', subject='Agreement', body='I have completed the extra work.')])
    assert 'completed-action' in agent.gate(store.case('case'), scope_final(), 0)


def test_scope_client_source_label_is_not_a_freelancer_claim(scope_world):
    store, _, agent = scope_world
    f = scope_finding()
    f['request_quote'] = 'I have sent the request for three episode cards.'
    agent.scen['cedar']['docs'][0]['text'] += '\n\n' + f['request_quote']
    assert agent.gate(store.case('case'), scope_final(f), 0) is None


def test_recorded_freelancer_completion_but_never_new_attachment(world):
    from unstuck.scenarios import FREELANCER
    store, _, agent = world
    doc = dict(id='sent', kind='mail', text=f"From: {FREELANCER['email']}\n\nI have submitted the form.")
    agent.scen['test']['docs'].append(doc)
    a = dict(type='draft_reply', why='Follow up', to='ap@client.example', body='I have submitted the form.')
    assert agent.gate(store.case('case'), answer(proposed_actions=[a]), 0) is None
    doc['text'] += '\n\nPlease find attached the W-9.'
    a['body'] = 'Please find attached the W-9.'
    assert 'attachments' in agent.gate(store.case('case'), answer(proposed_actions=[a]), 0)


def test_client_attachment_quote_does_not_hide_unbacked_claim(world):
    store, _, agent = world
    claim = 'Please find attached the approval.'
    agent.scen['test']['docs'][0]['text'] = 'From: ap@client.example\n\n' + claim
    a = dict(type='draft_reply', why='Clarify', to='ap@client.example', body=f'You wrote: "{claim}" Please confirm receipt.')
    assert agent.gate(store.case('case'), answer(proposed_actions=[a]), 0) is None
    a['body'] += ' I have submitted the form.'
    assert agent.gate(store.case('case'), answer(proposed_actions=[a]), 0)


def test_draft_subject_is_checked(world):
    store, _, agent = world
    a = dict(type='draft_reply', why='Next step', to='ap@client.example',
             subject='The form has been submitted.', body='Please confirm the submission channel.')
    assert agent.gate(store.case('case'), answer(proposed_actions=[a]), 0)


def test_agreement_reference_stable_and_precedes_lexical():
    from unstuck.unbilled import agreement_refs, duplicate_item
    action = {'items': [{'quote_doc': 'agreement-1', 'quote': 'Cards cost USD 180.'}]}
    refs = agreement_refs(action)
    action['items'][0]['quote'] = ' Cards  cost\nUSD 180. '
    assert agreement_refs(action) == refs
    existing = invoice()
    existing['items'][0]['description'] = refs[0]
    assert 'agreement reference' in duplicate_item([('Unrelated', 999)], [existing], refs)
    action['items'][0]['quote_doc'] = 'other'
    assert agreement_refs(action) != refs
    assert duplicate_item([('Unrelated', 999)], [existing], agreement_refs(action)) is None
    assert duplicate_item([('Design', 10000)], [existing])


@pytest.mark.parametrize('preserved', [True, False])
async def test_agreement_reference_transport_roundtrip(preserved):
    inv = Invoicing(None)
    raw = invoice()
    if preserved:
        raw['items'][0]['description'] = 'unstuck-agreement:abc'
    inv._ok = AsyncMock(side_effect=[{'id': 'INV-TEST'}, {}, raw])
    kwargs = dict(number='TEST', recipient_email='ap@client.example', recipient_name='Client',
                  items=[('Design', 10000)], note='Test', references=['unstuck-agreement:abc'])
    if preserved:
        await inv.create_and_send(**kwargs)
    else:
        with pytest.raises(RuntimeError, match='references not preserved'):
            await inv.create_and_send(**kwargs)
    assert inv._ok.call_args_list[0].args[3]['items'][0]['description'] == 'unstuck-agreement:abc'


async def test_owner_document_add_read_quote_and_isolation(unbilled_app):
    app, _ = unbilled_app
    store, agent = app.state.store, app.state.agent
    store.add_case('other', 'test', 'INV-OTHER', 'Other', 'Other')
    body = 'Payment is blocked because the supplier tax form is missing.'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        r = await c.post('/case/case/documents', data={'title': '<script>alert(1)</script>', 'text': body+'\n\n<img src=x onerror=alert(1)>'})
        assert r.status_code == 303
        page = await c.get('/case/case')
        assert '&lt;script&gt;' in page.text and '<script>alert(1)' not in page.text
        assert '&lt;img' in page.text and 'Added by you' in page.text
        assert (await c.post('/case/missing/documents', data={'title':'t','text':body})).status_code == 404
        assert (await c.post('/case/case/documents', data={'title':'t','text':'a'*20001})).status_code == 400
        assert (await c.post('/case/case/documents', data={'title':'','text':body})).status_code == 400
    doc = store.documents('case')[0]
    assert store.documents('other') == []
    assert doc['id'] not in agent.docs(store.case('other'))
    assert any(x['id']==doc['id'] for x in agent.search(store.case('case'), 'supplier tax form'))
    final = answer(quote=body, quote_doc=doc['id'])
    script(agent, [tool('r', 'read_document', id=doc['id'])], [tool('f', 'final_answer', **final)])
    await agent.start('case')
    assert store.case('case')['final']['quote_doc'] == doc['id']
    assert any('Added by you' in r['content'] and body in r['content'] for r in results(store))
    assert not any(d['kind']=='owner' for d in agent.docs(store.case('other')).values())


async def test_reference_duplicate_blocks_proposal_and_execution(unbilled_app):
    from unstuck.unbilled import agreement_refs
    app, module = unbilled_app
    store, inv, agent = app.state.store, app.state.inv, app.state.agent
    action = create_proposal()
    raw = invoice()  # deliberately different name and amount
    raw['items'][0]['description'] = agreement_refs(action)[0]
    inv.search_recipient.return_value = [raw]
    assert 'agreement reference' in await agent.check_unbilled(store.case('case'), [action])
    store.save_case('case', final=create_final())
    inv.create_and_send = AsyncMock()
    state, result = await module.execute_action(app, {'id':1,'case_id':'case','type':'create_invoice','params':action})
    assert state == 'refused' and 'agreement reference' in result['reason']
    inv.create_and_send.assert_not_awaited()


def test_distinct_agreements_do_not_fall_back_to_same_name():
    from unstuck.unbilled import duplicate_item
    raw = invoice()
    raw['items'][0]['description'] = 'unstuck-agreement:'+'a'*24
    assert duplicate_item([('Design',10000)], [raw], ['unstuck-agreement:'+'b'*24]) is None
    del raw['items'][0]['description']
    assert duplicate_item([('Design',10000)], [raw], ['unstuck-agreement:'+'b'*24])


async def test_document_posts_share_existing_rate_limit(unbilled_app):
    app, _ = unbilled_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        for _ in range(120):
            assert (await c.post('/case/case/documents',data={'title':'','text':''})).status_code == 400
        assert (await c.post('/case/case/documents',data={'title':'t','text':'Evidence.'})).status_code == 429
    assert app.state.store.documents('case') == []


def test_owner_documents_preserve_workspace_and_survive_reopen(world):
    from unstuck.store import Store
    store, _, _ = world
    with store.db() as c:
        columns = {r[1] for r in c.execute('PRAGMA table_info(cases)')}
        if 'workspace' not in columns:
            c.execute("ALTER TABLE cases ADD COLUMN workspace TEXT DEFAULT ''")
        c.execute("UPDATE cases SET workspace='visitor-a' WHERE id='case'")
    doc = store.add_document('case','Owner evidence','A deciding sentence.')
    assert Store(store.path).documents('case')[0]['id'] == doc
    with store.db() as c:
        assert c.execute('SELECT workspace FROM documents WHERE id=?',(doc,)).fetchone()[0] == 'visitor-a'
        c.execute("UPDATE cases SET workspace='visitor-b' WHERE id='case'")
    assert store.documents('case') == []


async def test_public_workspace_document_route_is_private(unbilled_app):
    import time
    from unstuck.workspaces import sign, COOKIE
    app, _ = unbilled_app
    settings = app.state.workspace_settings
    settings.mode, settings.secret = 'public', 'offline-test-secret-' * 3
    store = app.state.store
    first, other = 'a'*12, 'b'*12
    store.add_case('other', 'test', 'INV-OTHER', 'Other', 'Other')
    with store.db() as db:
        for ws, cid in [(first, 'case'), (other, 'other')]:
            db.execute("INSERT INTO workspaces (id,created,expires,state) VALUES (?,?,?,'ready')",(ws,time.time(),time.time()+3600))
            db.execute('UPDATE cases SET workspace=? WHERE id=?',(ws,cid))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        c.cookies.set(COOKIE, sign(settings.secret, first))
        assert (await c.post('/case/case/documents',data={'title':'Private','text':'Only visitor A can read this.'})).status_code == 303
        assert store.documents('case')[0]['title'] == 'Private'
        assert (await c.post('/case/other/documents',data={'title':'Forged','text':'Not mine.'})).status_code == 404
        c.cookies.set(COOKIE, sign(settings.secret, other))
        assert (await c.get('/case/case')).status_code == 404
        assert (await c.post('/case/case/documents',data={'title':'Forged','text':'Not mine.'})).status_code == 404
    assert store.documents('other') == []
